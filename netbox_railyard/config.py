"""Read the plugin's settings out of NetBox's ``PLUGINS_CONFIG``."""

from __future__ import annotations

from netbox.plugins import get_plugin_config

from .policy import SyncTargets, read_token

PLUGIN = "netbox_railyard"


def setting(key: str, default=None):
    return get_plugin_config(PLUGIN, key, default)


def railyard_settings() -> dict:
    return {
        "url": setting("railyard_url", "https://railyard.sh"),
        "token": setting("railyard_token", ""),
        "token_file": setting("railyard_token_file"),
        "org": setting("railyard_org"),
        "devicetype_library_ref": setting("devicetype_library_ref"),
        "devicetype_cache_dir": setting("devicetype_cache_dir"),
        "import_components": setting("import_components", False),
    }


def railyard_token() -> str:
    cfg = railyard_settings()
    return read_token(cfg["token"], cfg["token_file"])


def sync_targets() -> SyncTargets:
    """The orgs/projects this NetBox may sync from (``railyard_allowed_orgs``/``_projects``)."""
    return SyncTargets.from_settings(
        default_org=setting("railyard_org"),
        allowed_orgs=setting("railyard_allowed_orgs"),
        allowed_projects=setting("railyard_allowed_projects"),
    )
