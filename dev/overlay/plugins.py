# Mounted at /etc/netbox/config/plugins.py by netbox-docker → enables the plugin.
# Railyard connection comes from the environment (see docker-compose.override.yml); edit the defaults
# here if you'd rather hard-code them.
import os

PLUGINS = ["netbox_railyard"]

PLUGINS_CONFIG = {
    "netbox_railyard": {
        "railyard_url": os.environ.get("RAILYARD_URL", "https://railyard.sh"),
        "railyard_token": os.environ.get("RAILYARD_TOKEN", ""),
        "railyard_org": os.environ.get("RAILYARD_ORG") or None,
        "devicetype_library_ref": os.environ.get("RAILYARD_DEVICETYPE_REF", "master"),
        "devicetype_cache_dir": "/opt/netbox/devicetype-cache",
    }
}
