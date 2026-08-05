from .base import ImageProvider, ProviderError
from . import prefs
from .registry import resolve_model

_providers: dict[str, ImageProvider] = {}


def get_enabled_provider(name: str) -> ImageProvider:
    """Like get_provider, but refuses providers turned off in Settings — the guard
    that stops a disabled (e.g. paid) provider from ever spending money. Raises
    ProviderError (routers map it to a 4xx) with a user-facing message."""
    provider = get_provider(name)
    if not prefs.is_enabled(name):
        raise ProviderError(
            f"The '{name}' image provider is disabled in Settings → Providers. "
            f"Enable it there to generate with it."
        )
    return provider


def get_provider(name: str) -> ImageProvider:
    if not _providers:
        from .antigravity_provider import AntigravityProvider
        from .higgsfield_provider import HiggsfieldProvider

        _providers["antigravity"] = AntigravityProvider()
        _providers["higgsfield"] = HiggsfieldProvider()
    if name not in _providers:
        raise ProviderError(f"Unknown provider '{name}'")
    return _providers[name]
