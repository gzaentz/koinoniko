from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str | None,
        base_url: str = OPENROUTER_API_URL,
        temperature: float = 0.8,
        max_tokens: int = 4000,
        reasoning_max_tokens: int | None = None,
        site_url: str | None = None,
        app_name: str | None = None,
        request_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
    ):
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required.")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_max_tokens = reasoning_max_tokens
        self.site_url = site_url
        self.app_name = app_name
        self._request_json = request_json

    def generate(self, prompt: str, *, variants: int, model: str | None = None) -> list[str]:
        requested_variants = max(1, variants)
        selected_model = (model or self.model or "").strip()
        if not selected_model:
            raise ValueError("OpenRouter model is required. Set it in Account or OPENROUTER_MODEL.")
        payload = {
            "model": selected_model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": format_prompt_for_variants(prompt, requested_variants),
                }
            ],
        }
        reasoning_budget = self._reasoning_budget(self.max_tokens)
        if reasoning_budget:
            payload["reasoning"] = {
                "max_tokens": reasoning_budget,
                "exclude": True,
            }
        response = self.post_json(payload)
        try:
            content = extract_content(response)
        except RuntimeError:
            if not response_exhausted_without_content(response):
                raise
            retry_max_tokens = max(8000, self.max_tokens * 2)
            retry_payload = {
                **payload,
                "max_tokens": retry_max_tokens,
                "reasoning": {
                    "max_tokens": self._reasoning_budget(retry_max_tokens) or 1024,
                    "exclude": True,
                },
            }
            content = extract_content(self.post_json(retry_payload))
        parsed = split_variants(content)
        return parsed[:requested_variants] if parsed else [content.strip()]

    def _reasoning_budget(self, output_budget: int) -> int | None:
        if not self.reasoning_max_tokens or output_budget <= 1536:
            return None
        return min(max(1024, self.reasoning_max_tokens), output_budget - 512)

    def post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "leadpoet-social-brain/0.2",
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_name:
            headers["X-Title"] = self.app_name
        if self._request_json:
            return self._request_json(self.base_url, payload, headers)

        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenRouter request failed ({exc.code}): {body}") from exc


def create_provider() -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        model=(
            os.getenv("SOCIAL_BRAIN_MODEL")
            or os.getenv("SOCIAL_BRAIN_DEFAULT_MODEL")
            or os.getenv("OPENROUTER_MODEL")
            or None
        ),
        base_url=os.getenv("OPENROUTER_API_URL", OPENROUTER_API_URL),
        temperature=float(os.getenv("OPENROUTER_TEMPERATURE", "0.8")),
        max_tokens=int(os.getenv("OPENROUTER_MAX_TOKENS", "4000")),
        reasoning_max_tokens=max(0, int(os.getenv("OPENROUTER_REASONING_MAX_TOKENS", "1024"))) or None,
        site_url=os.getenv("OPENROUTER_SITE_URL") or None,
        app_name=os.getenv("OPENROUTER_APP_NAME", "Leadpoet Social Brain"),
    )


def format_prompt_for_variants(prompt: str, variants: int) -> str:
    if variants <= 1:
        return prompt
    return (
        f"{prompt.strip()}\n\n"
        f"Return exactly {variants} variants. Separate variants with a line containing only ---."
    )


def extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter response did not include choices: {response}")
    message = choices[0].get("message") or {}
    content = str(message.get("content") or "").strip()
    if not content:
        raise RuntimeError(
            "OpenRouter response did not include message content: "
            + json.dumps(openrouter_response_summary(response), sort_keys=True)
        )
    return content


def response_exhausted_without_content(response: dict[str, Any]) -> bool:
    choices = response.get("choices") or []
    if not choices:
        return False
    choice = choices[0] or {}
    message = choice.get("message") or {}
    return (
        not str(message.get("content") or "").strip()
        and (
            str(choice.get("finish_reason") or "").lower() in {"length", "max_tokens"}
            or bool(message.get("reasoning_details"))
            or bool(message.get("reasoning"))
        )
    )


def openrouter_response_summary(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    return {
        "id": response.get("id"),
        "model": response.get("model"),
        "provider": response.get("provider"),
        "finish_reason": choice.get("finish_reason"),
        "has_reasoning": bool(message.get("reasoning") or message.get("reasoning_details")),
    }


def split_variants(content: str) -> list[str]:
    cleaned = strip_code_fence(content.strip())
    if "\n---" in cleaned or "---\n" in cleaned:
        return [part.strip() for part in re.split(r"(?m)^\s*---\s*$", cleaned) if part.strip()]
    numbered = re.split(r"(?m)^\s*\d+[.)]\s+", cleaned)
    parsed = [part.strip() for part in numbered if part.strip()]
    return parsed if len(parsed) > 1 else [cleaned] if cleaned else []


def strip_code_fence(value: str) -> str:
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        return "\n".join(lines[1:-1]).strip()
    return value
