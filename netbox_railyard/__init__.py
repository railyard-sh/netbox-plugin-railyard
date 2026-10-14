"""NetBox plugin entry point.

The ``PluginConfig`` import is guarded so this package (and its NetBox-independent ``policy`` module)
imports cleanly outside a NetBox install, which is how the NetBox-free tests run. Inside NetBox, ``config``
is defined and picked up by the plugin loader as usual. The Railyard side (client, sync document, importer,
ownership tag) is the ``railyard-sync`` package.
"""

from __future__ import annotations

try:
    from netbox.plugins import PluginConfig
except Exception:  # pragma: no cover - only taken outside a NetBox runtime
    PluginConfig = None

__version__ = "0.2.0"

if PluginConfig is not None:

    class RailyardConfig(PluginConfig):
        name = "netbox_railyard"
        verbose_name = "Railyard Sync"
        description = "Sync a Railyard design into NetBox, and import NetBox sites into Railyard."
        version = __version__
        author = "Railyard"
        base_url = "railyard"
        # The releases the integration suite runs against in CI (4.5, 4.6 and 4.7): 4.5 introduced the
        # PortMapping model the target writes front-to-rear port couplings with. Widen once tested.
        min_version = "4.5.0"
        max_version = "4.7.99"
        default_settings = {
            "railyard_url": "https://railyard.sh",
            "railyard_token": "",
            "railyard_token_file": None,  # path to a file holding the token (e.g. a mounted secret)
            "railyard_org": None,  # default org (id, slug or name); always allowed
            "railyard_allowed_orgs": [],  # further orgs users may pick; nothing else is reachable
            "railyard_allowed_projects": [],  # optional project id/slug allowlist; empty = any project
            "devicetype_library_ref": None,  # devicetype-library commit; None = the pinned DEFAULT_REF
            "devicetype_cache_dir": None,  # optional on-disk cache for fetched device-type YAML
            "import_components": False,  # give new device types the library's component templates
            "netbox_url": None,  # this NetBox's URL, recorded as an import's source; None = the URL it was run from
        }

    config = RailyardConfig
