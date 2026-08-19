"""NetBox plugin entry point.

The ``PluginConfig`` import is guarded so this package (and its NetBox-independent
``netbox_railyard.railyard`` core) imports cleanly outside a NetBox install — which is how the unit
tests and the future extraction of the core stay possible. Inside NetBox, ``config`` is defined and
picked up by the plugin loader as usual.
"""

from __future__ import annotations

try:
    from netbox.plugins import PluginConfig
except Exception:  # pragma: no cover - only taken outside a NetBox runtime
    PluginConfig = None

__version__ = "0.1.0"

if PluginConfig is not None:

    class RailyardConfig(PluginConfig):
        name = "netbox_railyard"
        verbose_name = "Railyard Sync"
        description = "Sync a Railyard data-centre design into NetBox (racks, devices, cables)."
        version = __version__
        author = "Railyard"
        base_url = "railyard"
        # NetBox 4.6 is the first target (Python 3.12 / Django 6). Widen once tested against newer.
        min_version = "4.6.0"
        max_version = "4.6.99"
        default_settings = {
            "railyard_url": "https://railyard.sh",
            "railyard_token": "",
            "railyard_org": None,  # optional default org (id, slug or name)
            "devicetype_library_ref": "master",  # pinned netbox-community/devicetype-library ref
            "devicetype_cache_dir": None,  # optional on-disk cache for fetched device-type YAML
        }

    config = RailyardConfig
