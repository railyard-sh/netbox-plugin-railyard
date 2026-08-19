"""Ensure a Manufacturer + DeviceType exist in NetBox from a resolved device-type spec.

This is the "netbox-community device types as a dependency" step: the source adapter has already
resolved each Railyard catalogue entry against the devicetype-library (``railyard.devicetype_library``),
so here we just materialise the Manufacturer + DeviceType in NetBox before a device that needs it is
created. Device *components* are created explicitly by the cabling models (matching Railyard's own
export), so — like that export — the device type itself is created lean; ``import_components`` is a hook
for a future "import the full library template" mode.

NetBox-only module: imports ``dcim.models`` and therefore only loads inside a NetBox runtime.
"""

from __future__ import annotations

from dcim.models import DeviceType, Manufacturer

from .railyard.devicetype_library import DeviceTypeSpec
from .railyard.mappings import slugify

# devicetype-library component key -> (NetBox template model attribute on DeviceType, field map)
_TEMPLATE_MODELS = {
    "interfaces": "interfacetemplates",
    "front-ports": "frontporttemplates",
    "rear-ports": "rearporttemplates",
    "power-ports": "powerporttemplates",
    "power-outlets": "poweroutlettemplates",
    "console-ports": "consoleporttemplates",
    "console-server-ports": "consoleserverporttemplates",
}


def ensure_manufacturer(name: str, slug: str | None = None) -> Manufacturer:
    slug = slug or slugify(name)
    obj = Manufacturer.objects.filter(name=name).first()
    if obj is None:
        obj, _ = Manufacturer.objects.get_or_create(slug=slug, defaults={"name": name})
    return obj


def ensure_device_type(spec: DeviceTypeSpec, *, import_components: bool = False) -> DeviceType:
    """Get-or-create the DeviceType for a spec. Idempotent — safe to call for every device."""
    manufacturer = ensure_manufacturer(spec.manufacturer)
    obj = DeviceType.objects.filter(manufacturer=manufacturer, model=spec.model).first()
    if obj is None:
        obj = DeviceType.objects.create(
            manufacturer=manufacturer,
            model=spec.model,
            slug=spec.slug or slugify(f"{spec.manufacturer}-{spec.model}"),
            u_height=spec.u_height or 1,
            is_full_depth=spec.is_full_depth,
            part_number=spec.part_number or "",
        )
        if import_components and spec.components:
            _import_component_templates(obj, spec.components)
    return obj


def _import_component_templates(device_type: DeviceType, components: dict) -> None:
    """Create component *templates* on a device type from library YAML (future full-import mode).

    Only whole-record fields NetBox accepts are passed through; unknown keys are dropped. Kept minimal
    and defensive because template model fields vary by NetBox minor version.
    """
    for lib_key, related_name in _TEMPLATE_MODELS.items():
        entries = components.get(lib_key) or []
        if not entries:
            continue
        manager = getattr(device_type, related_name, None)
        if manager is None:
            continue
        model = manager.model
        valid_fields = {f.name for f in model._meta.get_fields()}
        for entry in entries:
            data = {k.replace("-", "_"): v for k, v in entry.items() if k.replace("-", "_") in valid_fields}
            if not data.get("name"):
                continue
            manager.get_or_create(name=data["name"], defaults=data)
