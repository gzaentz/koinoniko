from __future__ import annotations

import importlib
import os
from typing import Protocol


class DraftProvider(Protocol):
    def generate(self, prompt: str, *, variants: int) -> list[str]:
        ...


def load_provider_from_env() -> DraftProvider | None:
    provider_path = os.getenv("SOCIAL_BRAIN_PROVIDER")
    if not provider_path:
        return None
    module_name, separator, factory_name = provider_path.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("SOCIAL_BRAIN_PROVIDER must look like 'module.path:create_provider'.")
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name)
    return factory()

