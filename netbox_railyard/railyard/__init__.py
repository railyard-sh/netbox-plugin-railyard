"""Railyard-side sync core — pure Python, no NetBox/Django imports.

This subpackage is deliberately independent of NetBox so it can be lifted out into a shared
``railyard-diffsync`` distribution that the future Nautobot plugin reuses. Everything here is
unit-testable without a NetBox install.
"""

from .client import RailyardClient
from .errors import RailyardAPIError, RailyardAuthError, RailyardNotFoundError
from .source import RailyardAdapter

__all__ = [
    "RailyardClient",
    "RailyardAdapter",
    "RailyardAPIError",
    "RailyardAuthError",
    "RailyardNotFoundError",
]
