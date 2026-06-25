from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PlatformRules:
    name: str
    default_max_chars: int
    supported_post_types: tuple[str, ...]


class PlatformAdapter(Protocol):
    rules: PlatformRules

    def validate_text(self, text: str, *, max_chars: int | None = None) -> None:
        ...

    def normalize_url(self, url: str | None) -> str | None:
        ...

