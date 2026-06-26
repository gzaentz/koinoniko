from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from leadpoet_social_brain.platforms.x import X_POST_TYPES
from leadpoet_social_brain.platforms.x_api import (
    DEFAULT_IMPORT_POST_TYPE,
    NormalizedXApiPost,
    UNCATEGORIZED_TOPIC,
)
from leadpoet_social_brain.providers import DraftProvider


TOPIC_OPTIONS = (
    "sales LLMs",
    "Bittensor",
    "Leadpoet",
    "AI",
    "founder/company",
    UNCATEGORIZED_TOPIC,
)


class XPostLLMCategorizer:
    def __init__(self, provider: DraftProvider):
        self.provider = provider

    def categorize(self, tweet: dict[str, Any], post: NormalizedXApiPost) -> NormalizedXApiPost:
        raw = self.provider.generate(build_x_category_prompt(tweet, post), variants=1)[0]
        category = parse_x_category_response(
            raw,
            default_topic=post.topic or UNCATEGORIZED_TOPIC,
            default_post_type=post.post_type or DEFAULT_IMPORT_POST_TYPE,
        )
        return replace(post, topic=category["topic"], post_type=category["post_type"])


def build_x_category_prompt(tweet: dict[str, Any], post: NormalizedXApiPost) -> str:
    referenced = tweet.get("referenced_tweets") or []
    return f"""Classify this X post for Leadpoet Social Brain.

Return only valid JSON with this exact shape:
{{"topic":"<topic>","post_type":"<post_type>","confidence":0.0,"reason":"<short reason>"}}

Allowed topics:
- sales LLMs: sales AI, lead generation, GTM, pipeline, buyer context, Leadpoet's sales product.
- Bittensor: Bittensor, TAO, subnets, validators, miners, incentives, subnet/company updates.
- Leadpoet: company/product updates not clearly about the sales category.
- AI: general AI that is not specifically sales or Bittensor.
- founder/company: founder journey, hiring, fundraising, operating lessons, personal company building.
- uncategorized: unclear or unrelated.

Allowed post_type values:
{", ".join(X_POST_TYPES)}

Rules:
- Classify only topic and post_type. Do not infer hooks, tone, patterns, or imitate status.
- If referenced_tweets contains type=quoted, use post_type quote_tweet unless another post_type is clearly better.
- Prefer bittensor_update for Bittensor posts.
- Prefer product_update for shipping, launch, feature, or product progress posts.
- Prefer technical_explanation for implementation, architecture, data, evals, or system explanation posts.
- Prefer founder_insight for opinionated founder lessons and market takes.
- Use uncategorized when unsure.

Post:
id: {post.x_post_id}
date: {post.date or "unknown"}
referenced_tweets: {json.dumps(referenced, separators=(",", ":"))}
text:
{post.text}
"""


def parse_x_category_response(
    raw: str,
    *,
    default_topic: str,
    default_post_type: str,
) -> dict[str, str]:
    parsed = json.loads(_json_object_text(raw))
    topic = normalize_topic(str(parsed.get("topic") or ""), default_topic)
    post_type = normalize_post_type(str(parsed.get("post_type") or ""), default_post_type)
    return {"topic": topic, "post_type": post_type}


def normalize_topic(value: str, default: str) -> str:
    cleaned = value.strip()
    aliases = {
        "sales": "sales LLMs",
        "sales llm": "sales LLMs",
        "sales llms": "sales LLMs",
        "lead generation": "sales LLMs",
        "gtm": "sales LLMs",
        "bittensor": "Bittensor",
        "tao": "Bittensor",
        "leadpoet": "Leadpoet",
        "ai": "AI",
        "founder": "founder/company",
        "company": "founder/company",
        "founder/company": "founder/company",
        "other": UNCATEGORIZED_TOPIC,
        "uncategorized": UNCATEGORIZED_TOPIC,
    }
    if cleaned in TOPIC_OPTIONS:
        return cleaned
    return aliases.get(cleaned.lower(), default or UNCATEGORIZED_TOPIC)


def normalize_post_type(value: str, default: str) -> str:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "bittensor": "bittensor_update",
        "investor": "investor_facing",
        "investor_post": "investor_facing",
        "technical": "technical_explanation",
        "funny": "light_funny",
        "humor": "light_funny",
        "quote": "quote_tweet",
    }
    normalized = aliases.get(cleaned, cleaned)
    if normalized in X_POST_TYPES:
        return normalized
    return default if default in X_POST_TYPES else DEFAULT_IMPORT_POST_TYPE


def _json_object_text(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if text.startswith("{") and text.endswith("}"):
        return text
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"Category provider did not return JSON: {raw}")
    return match.group(0)
