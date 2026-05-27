"""Per-site adapters.

Each adapter is responsible for: pulling the right cookies out of the
jar, hitting one or more authenticated endpoints, deciding whether the
cookie is alive, and extracting whatever account info the site
exposes (email, plan, renewal date, credits…).
"""

from __future__ import annotations

from .base import SiteAdapter
from .blackbox import BlackboxAdapter
from .facebook import FacebookAdapter
from .freepik import FreepikAdapter
from .manus import ManusAdapter
from .netflix import NetflixAdapter
from .perplexity import PerplexityAdapter
from .prime import PrimeVideoAdapter
from .roblox import RobloxAdapter
from .shopify import ShopifyAdapter
from .spotify import SpotifyAdapter


def all_adapters() -> list[type[SiteAdapter]]:
    return [
        BlackboxAdapter,
        ManusAdapter,
        PerplexityAdapter,
        NetflixAdapter,
        PrimeVideoAdapter,
        RobloxAdapter,
        SpotifyAdapter,
        FacebookAdapter,
        ShopifyAdapter,
        FreepikAdapter,
    ]


__all__ = [
    "SiteAdapter",
    "all_adapters",
    "BlackboxAdapter",
    "FacebookAdapter",
    "FreepikAdapter",
    "ManusAdapter",
    "NetflixAdapter",
    "PerplexityAdapter",
    "PrimeVideoAdapter",
    "RobloxAdapter",
    "ShopifyAdapter",
    "SpotifyAdapter",
]
