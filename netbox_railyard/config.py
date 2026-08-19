"""Read the plugin's settings out of NetBox's ``PLUGINS_CONFIG``."""

from __future__ import annotations

from netbox.plugins import get_plugin_config

PLUGIN = "netbox_railyard"


def setting(key: str, default=None):
    return get_plugin_config(PLUGIN, key, default)


def railyard_settings() -> dict:
    return {
        "url": setting("railyard_url", "https://railyard.sh"),
        "token": setting("railyard_token", ""),
        "org": setting("railyard_org"),
        "devicetype_library_ref": setting("devicetype_library_ref", "master"),
        "devicetype_cache_dir": setting("devicetype_cache_dir"),
        "import_components": setting("import_components", False),
    }
