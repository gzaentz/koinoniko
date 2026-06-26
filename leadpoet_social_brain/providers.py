from __future__ import annotations

import importlib
import os
from typing import Protocol


class DraftProvider(Protocol):
    def generate(self, prompt: str, *, variants: int) -> list[str]:
        ...


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
    previous_model = os.environ.get("SOCIAL_BRAIN_MODEL", sentinel)
    if model_override:
        os.environ["SOCIAL_BRAIN_MODEL"] = model_override
    try:
        return factory()
    finally:
        if model_override:
            if previous_model is sentinel:
                os.environ.pop("SOCIAL_BRAIN_MODEL", None)
            else:
                os.environ["SOCIAL_BRAIN_MODEL"] = str(previous_model)
