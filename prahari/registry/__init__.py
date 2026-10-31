"""India registry layer.

Composite model:

    Digital footprint = early + noisy.
    Registry          = late  + authoritative.

An alert needs both: the footprint to be early enough to act on, the registry to
make it actionable.

We ship exactly one live provider (IBBI, which is public and free) and a
cached-snapshot provider for MCA21 and GST, which have no public API, are
captcha-gated, and whose terms forbid scraping. Cached data is always stamped
with `as_of` and `source: registry_cached_snapshot`.

We do not fabricate registry data. A dated cache is honest; an invented live
lookup is not.
"""
from .base import RegistryProvider, RegistryResult
from .ibbi import IBBIProvider
from .cached import CachedRegistryProvider

__all__ = [
    "RegistryProvider", "RegistryResult", "IBBIProvider", "CachedRegistryProvider",
    "all_providers",
]


def all_providers(cache_path: str | None = None) -> list[RegistryProvider]:
    return [IBBIProvider(), CachedRegistryProvider(cache_path)]
