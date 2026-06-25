from __future__ import annotations

from urllib.parse import urlparse

from leadpoet_social_brain.platforms.base import PlatformRules


X_POST_TYPES = (
    "product_update",
    "founder_insight",
    "bittensor",
    "bittensor_update",
    "investor_facing",
    "quote_tweet",
    "launch",
    "technical_explanation",
    "light_funny",
)


class XAdapter:
    rules = PlatformRules(
        name="x",
        default_max_chars=280,
        supported_post_types=X_POST_TYPES,
    )

    def validate_text(self, text: str, *, max_chars: int | None = None) -> None:
        if not text or not text.strip():
            raise ValueError("Post text is required.")
        limit = max_chars or self.rules.default_max_chars
        if len(text) > limit:
            raise ValueError(f"X post text is {len(text)} chars, over the {limit} char limit.")

    def validate_post_type(self, post_type: str) -> None:
        if post_type not in self.rules.supported_post_types:
            supported = ", ".join(self.rules.supported_post_types)
            raise ValueError(f"Unsupported X post type '{post_type}'. Supported: {supported}.")

    def normalize_url(self, url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("X URL must start with http or https.")
        if parsed.netloc.lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            raise ValueError("X URL must be on x.com or twitter.com.")
        return url


def get_x_adapter() -> XAdapter:
    return XAdapter()
