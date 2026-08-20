"""Registry for the three independent site extraction adapters.

Only adapter classes live in the registry.  Search URLs remain configuration
values and are passed verbatim to the selected class by the runner.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from watcher.sites.base import SiteAdapter
from watcher.sites.leboncoin import LeboncoinAdapter
from watcher.sites.seloger import SelogerAdapter
from watcher.sites.seventee import SeventeeAdapter


AdapterFactory = Callable[[str], SiteAdapter]

SITE_ADAPTERS: Mapping[str, AdapterFactory] = {
    "leboncoin": LeboncoinAdapter,
    "seloger": SelogerAdapter,
    "seventee": SeventeeAdapter,
}

# Explicit aliases keep integrations readable without duplicating registry
# state or, more importantly, any configured URL.
ADAPTER_REGISTRY = SITE_ADAPTERS


def create_adapter(
    site: str,
    search_url: str,
    *,
    registry: Mapping[str, AdapterFactory] = SITE_ADAPTERS,
) -> SiteAdapter:
    """Instantiate the registered adapter with the configured URL unchanged."""

    site_name = str(site).strip().lower()
    try:
        factory = registry[site_name]
    except KeyError as exc:
        raise ValueError(f"unsupported site: {site_name or site!r}") from exc
    adapter = factory(search_url)
    if adapter.site_name != site_name:
        raise ValueError(
            f"adapter registry mismatch: {site_name!r} produced "
            f"{adapter.site_name!r}"
        )
    return adapter


__all__ = [
    "ADAPTER_REGISTRY",
    "SITE_ADAPTERS",
    "AdapterFactory",
    "LeboncoinAdapter",
    "SelogerAdapter",
    "SeventeeAdapter",
    "create_adapter",
]
