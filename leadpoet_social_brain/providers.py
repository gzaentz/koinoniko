from __future__ import annotations

import importlib
import inspect
import os
from dataclasses import dataclass
from typing import Protocol


MODEL_ROLES = ("default", "strategist", "natural_writer", "evaluator")
MODEL_SETTING_KEYS = {
    "default": "model.default",
    "strategist": "model.strategist",
    "natural_writer": "model.natural_writer",
    "evaluator": "model.evaluator",
}
MODEL_ENV_KEYS = {
    "default": "SOCIAL_BRAIN_DEFAULT_MODEL",
    "strategist": "SOCIAL_BRAIN_STRATEGIST_MODEL",
    "natural_writer": "SOCIAL_BRAIN_NATURAL_WRITER_MODEL",
    "evaluator": "SOCIAL_BRAIN_EVALUATOR_MODEL",
}
LEGACY_MODEL_SETTING = "llm_model"
LEGACY_MODEL_ENV = "SOCIAL_BRAIN_MODEL"
OPENROUTER_MODEL_ENV = "OPENROUTER_MODEL"


class DraftProvider(Protocol):
    def generate(self, prompt: str, *, variants: int, model: str | None = None) -> list[str]:
        ...


@dataclass(frozen=True)
class ModelRouting:
    default: str | None = None
    strategist: str | None = None
    natural_writer: str | None = None
    evaluator: str | None = None

    def for_role(self, role: str) -> str | None:
        if role == "default":
            return self.default
        value = getattr(self, role)
        return value or self.default

    def as_dict(self) -> dict[str, str | None]:
        return {
            "default": self.default,
            "strategist": self.strategist,
            "natural_writer": self.natural_writer,
            "evaluator": self.evaluator,
        }


def load_provider_from_env(*, model_override: str | None = None) -> DraftProvider | None:
    provider_path = os.getenv("SOCIAL_BRAIN_PROVIDER")
    if not provider_path:
        return None
    module_name, separator, factory_name = provider_path.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("SOCIAL_BRAIN_PROVIDER must look like 'module.path:create_provider'.")
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name)
    sentinel = object()
    previous_model = os.environ.get(LEGACY_MODEL_ENV, sentinel)
    if model_override:
        os.environ[LEGACY_MODEL_ENV] = model_override
    try:
        return factory()
    finally:
        if model_override:
            if previous_model is sentinel:
                os.environ.pop(LEGACY_MODEL_ENV, None)
            else:
                os.environ[LEGACY_MODEL_ENV] = str(previous_model)


def generate_with_provider(
    provider: DraftProvider,
    prompt: str,
    *,
    variants: int,
    model: str | None = None,
) -> list[str]:
    if model and provider_accepts_model(provider):
        return provider.generate(prompt, variants=variants, model=model)
    return provider.generate(prompt, variants=variants)


def provider_accepts_model(provider: DraftProvider) -> bool:
    try:
        signature = inspect.signature(provider.generate)
    except (TypeError, ValueError):
        return False
    return "model" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def resolve_model_routing(store=None, provider: DraftProvider | None = None) -> ModelRouting:
    default = resolve_model_value(store, "default", provider=provider)
    strategist = resolve_model_value(store, "strategist") or default
    natural_writer = resolve_model_value(store, "natural_writer") or default
    evaluator = resolve_model_value(store, "evaluator") or default
    return ModelRouting(
        default=default,
        strategist=strategist,
        natural_writer=natural_writer,
        evaluator=evaluator,
    )


def resolve_model_value(store, role: str, *, provider: DraftProvider | None = None) -> str | None:
    saved = saved_model_value(store, role)
    if saved:
        return saved
    env_value = env_model_value(role)
    if env_value:
        return env_value
    if role == "default":
        return clean_model_value(getattr(provider, "model", None)) if provider else None
    return None


def saved_model_value(store, role: str) -> str | None:
    if not store:
        return None
    value = clean_model_value(store.get_app_setting(MODEL_SETTING_KEYS[role]))
    if value:
        return value
    if role == "default":
        return clean_model_value(store.get_app_setting(LEGACY_MODEL_SETTING))
    return None


def env_model_value(role: str) -> str | None:
    value = clean_model_value(os.getenv(MODEL_ENV_KEYS[role]))
    if value:
        return value
    if role == "default":
        return (
            clean_model_value(os.getenv(LEGACY_MODEL_ENV))
            or clean_model_value(os.getenv(OPENROUTER_MODEL_ENV))
        )
    return None


def clean_model_value(value: object) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None
