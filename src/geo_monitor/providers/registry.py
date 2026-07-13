"""Registry for concrete provider SDK backends."""

from __future__ import annotations

from importlib.util import find_spec

from .base import ProviderBackend
from .deepseek import DeepSeekProvider
from .doubao import DoubaoArkProvider
from .openai_compatible import OpenAICompatibleProvider
from .qwen import QwenDashScopeProvider

_PROVIDERS: dict[str, ProviderBackend] = {
    "openai_compatible": OpenAICompatibleProvider(),
    "doubao": DoubaoArkProvider(),
    "qwen": QwenDashScopeProvider(),
    "deepseek": DeepSeekProvider(),
}


def get_provider(name: str) -> ProviderBackend:
    key = str(name or "").strip()
    try:
        return _PROVIDERS[key]
    except KeyError as exc:
        raise ValueError(f"未知 provider：{key}") from exc


def provider_dependency_available(name: str) -> bool:
    module_name = get_provider(name).sdk_name
    try:
        return find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


__all__ = ["get_provider", "provider_dependency_available"]
