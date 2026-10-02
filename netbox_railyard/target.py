"""The NetBox DiffSync *target* adapter.

Each ``NetBox*`` model subclasses the canonical model in ``railyard.models`` (identical diff surface)
and implements ``create``/``update``/``delete`` against the NetBox ORM (``dcim.models``).

Ownership — what the sync will and won't touch:

- **Owned objects** carry the project's ownership tag (``policy.ownership_tag``). ``load()`` reads back
  only those, each remembering its NetBox primary key, and ``update``/``delete`` act only on that exact
  object, re-fetched by pk *and* tag. An object without the tag is never updated or deleted.
- **Shared objects that already exist** (manufacturers, device types, roles, sites, racks) are *used*
  as they are — a device may be placed in an existing site or rack — but never tagged, changed or
  deleted.
- **Conflicts** — a device of the same name already in the site, a component the sync didn't create, a
  port that is already cabled — are reported and skipped, together with whatever depends on them. The
  sync never adopts them.
- **Components on a device the sync owns** that already exist by name (typically created from the
  device type's component templates when the sync created the device) are adopted and tagged; they
  are part of a device the sync created.
- **Deletes** (opt-in) run in reverse dependency order and are refused — the object is kept and
  reported — when NetBox would cascade to, or is protected by, anything the sync doesn't own. Deleting
  an owned device removes its components with it, as NetBox always does.

Every query is ``.restrict()``-ed to what the job's user may view/add/change/delete, so NetBox
object-level permission constraints apply to the sync exactly as they would in the UI.

NetBox-only module — needs a live NetBox; exercised by ``tests/integration``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dcim.models import (
    Cable as NBCable,
    ConsolePort,
    ConsoleServerPort,
    Device as NBDevice,
    DeviceBay,
    DeviceRole as NBDeviceRole,
    DeviceType as NBDeviceType,
    FrontPort as NBFrontPort,
    Interface as NBInterface,
    InventoryItem,
    Manufacturer as NBManufacturer,
    ModuleBay,
    PortMapping as NBPortMapping,
    PowerOutlet as NBPowerOutlet,
    PowerPort as NBPowerPort,
    Rack as NBRack,
    RearPort as NBRearPort,
    Site as NBSite,
)
from diffsync import Adapter
from diffsync.exceptions import ObjectAlreadyExists, ObjectNotCreated
from django.core.exceptions import FieldDoesNotExist, PermissionDenied
from django.db import router
from django.db.models import QuerySet
from django.db.models.deletion import Collector, ProtectedError, RestrictedError

from . import devicetypes
from .railyard import models
from .railyard.devicetype_library import DeviceTypeSpec
from .railyard.mappings import slugify

CUSTOM_FIELD = "railyard_id"

# cable-termination content type -> NetBox component model
_CT_MODEL = {
    "dcim.interface": NBInterface,
    "dcim.frontport": NBFrontPort,
    "dcim.rearport": NBRearPort,
    "dcim.powerport": NBPowerPort,
    "dcim.poweroutlet": NBPowerOutlet,
}

# diffsync model name -> NetBox component model, for the (device, name) components the sync models.
_COMPONENT_TYPES = {
    "interface": NBInterface,
    "rear_port": NBRearPort,
    "front_port": NBFrontPort,
    "power_outlet": NBPowerOutlet,
    "power_port": NBPowerPort,
}

# Every NetBox device component model: deleting an owned device takes all of these with it.
_DEVICE_COMPONENTS = (
    NBInterface,
    NBFrontPort,
    NBRearPort,
    NBPowerPort,
    NBPowerOutlet,
    ConsolePort,
    ConsoleServerPort,
    DeviceBay,
    ModuleBay,
    InventoryItem,
)

# Rows NetBox maintains for an object rather than data anyone entered; they go when it goes.
_BOOKKEEPING = {
    "extras.taggeditem",
    "extras.cachedvalue",
    "dcim.cabletermination",
    "dcim.cablepath",
    "dcim.portmapping",
}


@dataclass
class OwnershipReport:
    """What the sync decided about objects it doesn't own, for the job log and result."""

    referenced: list[str] = field(default_factory=list)  # existing shared objects used as-is
    conflicts: list[str] = field(default_factory=list)  # skipped: would need an object the sync doesn't own
    dependents_skipped: int = 0  # components/cables skipped because what they hang off was skipped
    adopted: list[str] = field(default_factory=list)  # components on owned devices brought under the tag
    kept: list[str] = field(default_factory=list)  # deletes refused
    warnings: list[str] = field(default_factory=list)


# ---- lookups ---------------------------------------------------------------


def _by_name_or_slug(nb_model, name: str, slug: str | None = None):
    obj = nb_model.objects.filter(name=name).first()
    if obj is None:
        obj = nb_model.objects.filter(slug=slug or slugify(name)).first()
    return obj


def _find_manufacturer(name: str):
    return _by_name_or_slug(NBManufacturer, name)


def _find_site(name: str):
    return _by_name_or_slug(NBSite, name)


def _find_role(name: str):
    return _by_name_or_slug(NBDeviceRole, name)


def _find_device_type(manufacturer: str, model: str):
    mfr = _find_manufacturer(manufacturer)
    return NBDeviceType.objects.filter(manufacturer=mfr, model=model).first() if mfr else None


def _is_tagged(obj, tag) -> bool:
    return tag is not None and obj.tags.filter(pk=tag.pk).exists()


def _taggable(nb_model) -> bool:
    try:
        nb_model._meta.get_field("tags")
    except FieldDoesNotExist:
        return False
    return True


def _set_railyard_id(obj, value: str) -> None:
    if value:
        obj.custom_field_data[CUSTOM_FIELD] = value


def _ct_string(term) -> str:
    """dcim.* content-type id for a cable termination object (e.g. an Interface -> 'dcim.interface')."""
    return f"dcim.{term._meta.model_name}"


def _verbose(nb_model, n: int) -> str:
    meta = nb_model._meta
    return f"{n} {meta.verbose_name if n == 1 else meta.verbose_name_plural}"


# ---- permission + ownership helpers ----------------------------------------


def _require(adapter, obj, action: str) -> None:
    """Enforce NetBox's object-level permission constraints on ``obj`` for ``action``."""
    if not type(obj).objects.restrict(adapter.user, action).filter(pk=obj.pk).exists():
        raise PermissionDenied(f"{adapter.user} may not {action} {type(obj)._meta.verbose_name} {obj}")


def _owned(adapter, nb_model, action: str) -> QuerySet:
    """The objects of ``nb_model`` the sync owns that the job's user may ``action``."""
    if adapter.tag is None:
        return nb_model.objects.none()
    return nb_model.objects.restrict(adapter.user, action).filter(tags=adapter.tag)


def _get_owned(model, nb_model, action: str):
    """Re-fetch the NetBox object behind a loaded model by its pk *and* the ownership tag."""
    adapter = model.adapter
    obj = _owned(adapter, nb_model, action).filter(pk=model.nb_pk).first() if model.nb_pk else None
    if obj is None:
        raise PermissionDenied(
            f"{model.get_type()} {model.get_unique_id()}: no longer a Railyard-owned object that "
            f"{adapter.user} may {action}"
        )
    return obj


def _claim(adapter, obj, action: str = "add") -> int:
    """Tag a just-created (or adopted) object as owned, then check the user was allowed to."""
    obj.tags.add(adapter.tag)
    _require(adapter, obj, action)
    return obj.pk


def _visible(adapter, nb_model) -> QuerySet:
    return nb_model.objects.restrict(adapter.user, "view")


def _owned_device(adapter, name: str):
    return _owned(adapter, NBDevice, "view").filter(name=name).first()


def _created(cls, adapter, ids, attrs, pk):
    model = super(cls, cls).create(adapter, ids=ids, attrs=attrs)
    model.nb_pk = pk
    return model


def _delete(model, nb_model):
    """Delete an owned object unless that would reach beyond what the sync owns (then keep it)."""
    adapter = model.adapter
    obj = _owned(adapter, nb_model, "delete").filter(pk=model.nb_pk).first() if model.nb_pk else None
    blockers = [f"{adapter.user} may not delete it"] if obj is None else adapter.deletion_blockers(obj)
    if blockers:
        adapter.report.kept.append(f"{model.get_type()} {model.get_unique_id()}: {'; '.join(blockers)}")
        return False
    obj.delete()
    return True


# ---- target models ---------------------------------------------------------


class NetBoxManufacturer(models.Manufacturer):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        if _find_manufacturer(ids["name"]) is not None:
            raise ObjectNotCreated(f"manufacturer {ids['name']} already exists and is not managed by this sync")
        obj = NBManufacturer(name=ids["name"], slug=attrs.get("slug") or slugify(ids["name"]))
        obj.full_clean()
        obj.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, obj))

    def update(self, attrs):
        obj = _get_owned(self, NBManufacturer, "change")
        if attrs.get("slug"):
            obj.slug = attrs["slug"]
        obj.save()
        _require(self.adapter, obj, "change")
        return super().update(attrs)

    def delete(self):
        _delete(self, NBManufacturer)
        super().delete()
        return self


class NetBoxDeviceType(models.DeviceType):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        manufacturer = _find_manufacturer(ids["manufacturer"])
        if manufacturer is None:
            raise ObjectNotCreated(f"device type {ids['model']}: manufacturer {ids['manufacturer']} not found")
        if NBDeviceType.objects.filter(manufacturer=manufacturer, model=ids["model"]).exists():
            raise ObjectNotCreated(f"device type {ids['model']} already exists and is not managed by this sync")
        spec = DeviceTypeSpec(
            manufacturer=ids["manufacturer"],
            model=ids["model"],
            slug=attrs.get("slug", ""),
            u_height=attrs.get("u_height", 1),
            is_full_depth=attrs.get("is_full_depth", True),
            part_number=attrs.get("part_number", ""),
        )
        obj = devicetypes.create_device_type(spec, manufacturer, import_components=adapter.import_components)
        return _created(cls, adapter, ids, attrs, _claim(adapter, obj))

    def update(self, attrs):
        obj = _get_owned(self, NBDeviceType, "change")
        for name in ("slug", "u_height", "is_full_depth", "part_number"):
            if name in attrs:
                setattr(obj, name, attrs[name])
        obj.save()
        _require(self.adapter, obj, "change")
        return super().update(attrs)

    def delete(self):
        _delete(self, NBDeviceType)
        super().delete()
        return self


class NetBoxDeviceRole(models.DeviceRole):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        if _find_role(ids["name"]) is not None:
            raise ObjectNotCreated(f"device role {ids['name']} already exists and is not managed by this sync")
        obj = NBDeviceRole(
            name=ids["name"], slug=attrs.get("slug") or slugify(ids["name"]), color=attrs.get("color", "9e9e9e")
        )
        obj.full_clean()
        obj.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, obj))

    def update(self, attrs):
        obj = _get_owned(self, NBDeviceRole, "change")
        if attrs.get("slug"):
            obj.slug = attrs["slug"]
        if attrs.get("color"):
            obj.color = attrs["color"]
        obj.save()
        _require(self.adapter, obj, "change")
        return super().update(attrs)

    def delete(self):
        _delete(self, NBDeviceRole)
        super().delete()
        return self


class NetBoxSite(models.Site):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        if _find_site(ids["name"]) is not None:
            raise ObjectNotCreated(f"site {ids['name']} already exists and is not managed by this sync")
        obj = NBSite(
            name=ids["name"], slug=attrs.get("slug") or slugify(ids["name"]), status=attrs.get("status", "active")
        )
        obj.full_clean()
        obj.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, obj))

    def update(self, attrs):
        obj = _get_owned(self, NBSite, "change")
        if attrs.get("slug"):
            obj.slug = attrs["slug"]
        if attrs.get("status"):
            obj.status = attrs["status"]
        obj.save()
        _require(self.adapter, obj, "change")
        return super().update(attrs)

    def delete(self):
        _delete(self, NBSite)
        super().delete()
        return self


class NetBoxRack(models.Rack):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        site = _visible(adapter, NBSite).filter(pk=getattr(_find_site(ids["site"]), "pk", None)).first()
        if site is None:
            raise ObjectNotCreated(f"rack {ids['name']}: site {ids['site']} not found")
        if NBRack.objects.filter(site=site, name=ids["name"]).exists():
            raise ObjectNotCreated(f"rack {ids['name']} already exists and is not managed by this sync")
        obj = NBRack(
            site=site,
            name=ids["name"],
            status=attrs.get("status", "active"),
            width=attrs.get("width", 19),
            u_height=attrs.get("u_height", 42),
            desc_units=attrs.get("desc_units", False),
        )
        obj.full_clean()
        obj.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, obj))

    def update(self, attrs):
        obj = _get_owned(self, NBRack, "change")
        for name in ("status", "width", "u_height", "desc_units"):
            if name in attrs:
                setattr(obj, name, attrs[name])
        obj.save()
        _require(self.adapter, obj, "change")
        return super().update(attrs)

    def delete(self):
        _delete(self, NBRack)
        super().delete()
        return self


class NetBoxDevice(models.Device):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        site = _visible(adapter, NBSite).filter(pk=getattr(_find_site(attrs["site"]), "pk", None)).first()
        if site is None:
            raise ObjectNotCreated(f"device {ids['name']}: site {attrs['site']} not found")
        if NBDevice.objects.filter(site=site, name__iexact=ids["name"]).exists():
            raise ObjectNotCreated(f"device {ids['name']} already exists and is not managed by this sync")
        device_type = _find_device_type(attrs["manufacturer"], attrs["device_type"])
        role = _find_role(attrs["role"])
        rack = NBRack.objects.filter(site=site, name=attrs["rack"]).first() if attrs.get("rack") else None
        if device_type is None or role is None or (attrs.get("rack") and rack is None):
            raise ObjectNotCreated(f"device {ids['name']}: its device type, role or rack was not found")
        device = NBDevice(
            name=ids["name"],
            device_type=device_type,
            role=role,
            site=site,
            rack=rack,
            position=attrs.get("position"),
            face=attrs.get("face") or "",
            status=attrs.get("status", "active"),
        )
        _set_railyard_id(device, attrs.get("railyard_id", ""))
        device.full_clean()  # e.g. refuses a position another device already occupies
        device.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, device))

    def update(self, attrs):
        obj = _get_owned(self, NBDevice, "change")
        if "role" in attrs:
            obj.role = _find_role(attrs["role"])
        if "rack" in attrs:
            obj.rack = NBRack.objects.filter(site=obj.site, name=attrs["rack"]).first() if attrs["rack"] else None
        for name in ("position", "face", "status"):
            if name in attrs:
                setattr(obj, name, attrs[name])
        if "railyard_id" in attrs:
            _set_railyard_id(obj, attrs["railyard_id"])
        obj.full_clean()
        obj.save()
        _require(self.adapter, obj, "change")
        return super().update(attrs)

    def delete(self):
        _delete(self, NBDevice)
        super().delete()
        return self


# Helpers for the simple (device, name, type) components. Module functions rather than a mixin with an
# `_nb_model` class attribute, because DiffSyncModel is Pydantic and would treat that attribute as a
# private attribute (so it wouldn't resolve on the subclass).
def _ensure_component(adapter, nb_model, ids, attrs, **defaults):
    """Create a component on a device the sync owns, or adopt a same-named one already on it."""
    device = _owned_device(adapter, ids["device"])
    if device is None:
        raise ObjectNotCreated(
            f"{nb_model._meta.verbose_name} {ids['name']}: device {ids['device']} is not managed by this sync"
        )
    obj = nb_model.objects.filter(device=device, name=ids["name"]).first()
    if obj is None:
        obj = nb_model(device=device, name=ids["name"], **defaults)
        obj.full_clean()
        obj.save()
        return obj, _claim(adapter, obj, "add")
    # Adopting: bring the component's type in line, but leave its positions/mappings as NetBox has them.
    if "type" in defaults:
        obj.type = defaults["type"]
    obj.save()
    label = f"{nb_model._meta.verbose_name} {ids['device']}:{ids['name']}"
    if label not in adapter.report.adopted:
        adapter.report.adopted.append(label)
    return obj, _claim(adapter, obj, "change")


def _update_component(model, nb_model, attrs):
    obj = _get_owned(model, nb_model, "change")
    if "type" in attrs:
        obj.type = attrs["type"]
        obj.save()
        _require(model.adapter, obj, "change")


class NetBoxInterface(models.Interface):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        _, pk = _ensure_component(adapter, NBInterface, ids, attrs, type=attrs.get("type", "other"))
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        _update_component(self, NBInterface, attrs)
        return super().update(attrs)

    def delete(self):
        _delete(self, NBInterface)
        super().delete()
        return self


class NetBoxRearPort(models.RearPort):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        _, pk = _ensure_component(
            adapter, NBRearPort, ids, attrs, type=attrs.get("type", "8p8c"), positions=attrs.get("positions", 1)
        )
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        obj = _get_owned(self, NBRearPort, "change")
        if "type" in attrs:
            obj.type = attrs["type"]
        if "positions" in attrs:
            obj.positions = attrs["positions"]
        obj.save()
        _require(self.adapter, obj, "change")
        return super().update(attrs)

    def delete(self):
        _delete(self, NBRearPort)
        super().delete()
        return self


class NetBoxFrontPort(models.FrontPort):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        fp, pk = _ensure_component(adapter, NBFrontPort, ids, attrs, type=attrs.get("type", "8p8c"), positions=1)
        # NetBox 4.6 couples a front port to a rear port through a PortMapping row, not a rear_port FK.
        rear = _owned(adapter, NBRearPort, "view").filter(device=fp.device, name=attrs.get("rear_port")).first()
        if rear is not None and not fp.mappings.exists():
            NBPortMapping.objects.create(
                device=fp.device,
                front_port=fp,
                front_port_position=1,
                rear_port=rear,
                rear_port_position=attrs.get("rear_port_position", 1),
            )
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        _update_component(self, NBFrontPort, attrs)
        return super().update(attrs)

    def delete(self):
        _delete(self, NBFrontPort)
        super().delete()
        return self


class NetBoxPowerOutlet(models.PowerOutlet):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        _, pk = _ensure_component(adapter, NBPowerOutlet, ids, attrs, type=attrs.get("type", ""))
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        _update_component(self, NBPowerOutlet, attrs)
        return super().update(attrs)

    def delete(self):
        _delete(self, NBPowerOutlet)
        super().delete()
        return self


class NetBoxPowerPort(models.PowerPort):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        _, pk = _ensure_component(adapter, NBPowerPort, ids, attrs, type=attrs.get("type", ""))
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        _update_component(self, NBPowerPort, attrs)
        return super().update(attrs)

    def delete(self):
        _delete(self, NBPowerPort)
        super().delete()
        return self


def _owned_termination(adapter, device_name: str, ct: str, name: str):
    """A cable end on a device the sync owns (``None`` if the device or component isn't there)."""
    model = _CT_MODEL.get(ct)
    device = _owned_device(adapter, device_name)
    if model is None or device is None:
        return None
    return model.objects.filter(device=device, name=name).first()


class NetBoxCable(models.Cable):
    nb_pk: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        a = _owned_termination(adapter, ids["a_device"], ids["a_type"], ids["a_name"])
        b = _owned_termination(adapter, ids["b_device"], ids["b_type"], ids["b_name"])
        if a is None or b is None:
            raise ObjectNotCreated(f"cable {attrs.get('label') or ids}: a termination is missing")
        for end in (a, b):
            if end.cable_id:
                raise ObjectNotCreated(f"cable {attrs.get('label') or ids}: {end.device}:{end} is already cabled")
        cable = NBCable(status="connected", label=attrs.get("label", ""))
        if attrs.get("is_power"):
            cable.type = "power"
        cable.a_terminations = [a]
        cable.b_terminations = [b]
        cable.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, cable))

    def update(self, attrs):
        cable = _get_owned(self, NBCable, "change")
        if "label" in attrs:
            cable.label = attrs["label"]
        if attrs.get("is_power"):
            cable.type = "power"
        cable.save()
        _require(self.adapter, cable, "change")
        return super().update(attrs)

    def delete(self):
        _delete(self, NBCable)
        super().delete()
        return self


# ---- adapter ---------------------------------------------------------------


class NetBoxAdapter(Adapter):
    manufacturer = NetBoxManufacturer
    device_type = NetBoxDeviceType
    device_role = NetBoxDeviceRole
    site = NetBoxSite
    rack = NetBoxRack
    device = NetBoxDevice
    interface = NetBoxInterface
    rear_port = NetBoxRearPort
    front_port = NetBoxFrontPort
    power_outlet = NetBoxPowerOutlet
    power_port = NetBoxPowerPort
    cable = NetBoxCable

    top_level = models.TOP_LEVEL

    def __init__(self, *, tag, user, import_components: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.tag = tag  # None before the first real sync: nothing is owned yet
        self.user = user
        self.import_components = import_components
        self.report = OwnershipReport()

    # -- load -----------------------------------------------------------------

    def _add(self, model) -> None:
        try:
            self.add(model)
        except ObjectAlreadyExists:
            self.report.warnings.append(
                f"two Railyard-owned objects share the identity {model.get_type()} {model.get_unique_id()}; "
                f"NetBox #{model.nb_pk} is ignored (neither updated nor deleted)"
            )

    def load(self) -> None:
        """Load only the objects this project owns (carry its tag) that the user may view, each with its
        NetBox pk, so a re-sync diffs to no-change and writes go to exactly these objects."""

        def owned(nb_model):
            return _owned(self, nb_model, "view")

        for mfr in owned(NBManufacturer):
            self._add(self.manufacturer(name=mfr.name, slug=mfr.slug, nb_pk=mfr.pk))
        for dt in owned(NBDeviceType).select_related("manufacturer"):
            self._add(
                self.device_type(
                    manufacturer=dt.manufacturer.name,
                    model=dt.model,
                    slug=dt.slug,
                    u_height=int(dt.u_height or 0),
                    is_full_depth=dt.is_full_depth,
                    part_number=dt.part_number or "",
                    nb_pk=dt.pk,
                )
            )
        for role in owned(NBDeviceRole):
            self._add(self.device_role(name=role.name, slug=role.slug, color=role.color, nb_pk=role.pk))
        for site in owned(NBSite):
            self._add(self.site(name=site.name, slug=site.slug, status=site.status, nb_pk=site.pk))
        for rk in owned(NBRack).select_related("site"):
            self._add(
                self.rack(
                    site=rk.site.name,
                    name=rk.name,
                    status=rk.status,
                    width=int(rk.width),
                    u_height=int(rk.u_height),
                    desc_units=rk.desc_units,
                    nb_pk=rk.pk,
                )
            )
        for dev in owned(NBDevice).select_related("device_type__manufacturer", "role", "site", "rack"):
            self._add(
                self.device(
                    name=dev.name,
                    device_type=dev.device_type.model if dev.device_type else "",
                    manufacturer=dev.device_type.manufacturer.name if dev.device_type else "",
                    role=dev.role.name if dev.role else "",
                    site=dev.site.name if dev.site else "",
                    rack=dev.rack.name if dev.rack else None,
                    position=int(dev.position) if dev.position is not None else None,
                    face=dev.face or "",
                    status=dev.status,
                    railyard_id=(dev.custom_field_data or {}).get(CUSTOM_FIELD, "") or "",
                    nb_pk=dev.pk,
                )
            )

        for rp in owned(NBRearPort).select_related("device"):
            self._add(
                self.rear_port(device=rp.device.name, name=rp.name, type=rp.type, positions=rp.positions, nb_pk=rp.pk)
            )
        for fp in owned(NBFrontPort).select_related("device").prefetch_related("mappings__rear_port"):
            mapping = fp.mappings.first()
            self._add(
                self.front_port(
                    device=fp.device.name,
                    name=fp.name,
                    type=fp.type,
                    rear_port=mapping.rear_port.name if mapping else "",
                    rear_port_position=mapping.rear_port_position if mapping else 1,
                    nb_pk=fp.pk,
                )
            )
        for iface in owned(NBInterface).select_related("device"):
            self._add(self.interface(device=iface.device.name, name=iface.name, type=iface.type, nb_pk=iface.pk))
        for po in owned(NBPowerOutlet).select_related("device"):
            self._add(self.power_outlet(device=po.device.name, name=po.name, type=po.type, nb_pk=po.pk))
        for pp in owned(NBPowerPort).select_related("device"):
            self._add(self.power_port(device=pp.device.name, name=pp.name, type=pp.type, nb_pk=pp.pk))
        for cable in owned(NBCable):
            a_terms, b_terms = cable.a_terminations, cable.b_terminations
            if len(a_terms) != 1 or len(b_terms) != 1:
                continue  # not a shape this sync creates
            a, b = a_terms[0], b_terms[0]
            if getattr(a, "device", None) is None or getattr(b, "device", None) is None:
                continue
            self._add(
                self.cable(
                    a_device=a.device.name,
                    a_type=_ct_string(a),
                    a_name=a.name,
                    b_device=b.device.name,
                    b_type=_ct_string(b),
                    b_name=b.name,
                    is_power=(cable.type == "power"),
                    label=cable.label or "",
                    nb_pk=cable.pk,
                )
            )

    # -- ownership pre-pass ---------------------------------------------------

    def _owns(self, model) -> bool:
        return self.get_or_none(model.get_type(), model.get_unique_id()) is not None

    def reconcile(self, source, *, allow_deletes: bool = False) -> OwnershipReport:
        """Decide, before anything is written, what to do about Railyard objects that the sync doesn't
        own yet but that already exist in NetBox; drop from ``source`` everything it must not create.

        Read-only, so a dry run reports exactly what a real run will skip or use as-is.
        """
        report = self.report
        drop: list = []
        skipped_types: set[tuple[str, str]] = set()
        skipped_devices: set[str] = set()
        skipped_ends: set[tuple[str, str, str]] = set()

        def reference(model, what: str) -> None:
            report.referenced.append(f"{what} (exists; used as-is, not modified)")
            drop.append(model)

        def conflict(model, why: str) -> None:
            report.conflicts.append(why)
            drop.append(model)

        def new(type_name: str):
            return [m for m in source.get_all(type_name) if not self._owns(m)]

        for m in new("manufacturer"):
            if _find_manufacturer(m.name) is not None:
                reference(m, f"manufacturer {m.name}")
        for dt in new("device_type"):
            mfr = _find_manufacturer(dt.manufacturer)
            if mfr is None:
                continue
            if NBDeviceType.objects.filter(manufacturer=mfr, model=dt.model).exists():
                reference(dt, f"device type {dt.manufacturer} {dt.model}")
            elif dt.slug and NBDeviceType.objects.filter(manufacturer=mfr, slug=dt.slug).exists():
                skipped_types.add((dt.manufacturer, dt.model))
                conflict(dt, f"device type {dt.manufacturer} {dt.model}: slug {dt.slug!r} is used by another type")
        for role in new("device_role"):
            if _find_role(role.name) is not None:
                reference(role, f"device role {role.name}")
        for site in new("site"):
            if _find_site(site.name) is not None:
                reference(site, f"site {site.name}")
        for rack in new("rack"):
            nb_site = _find_site(rack.site)
            if nb_site is not None and NBRack.objects.filter(site=nb_site, name=rack.name).exists():
                reference(rack, f"rack {rack.site}/{rack.name}")

        for dev in new("device"):
            if (dev.manufacturer, dev.device_type) in skipped_types:
                skipped_devices.add(dev.name)
                conflict(dev, f"device {dev.name}: skipped because its device type was skipped")
                continue
            nb_site = _find_site(dev.site)
            existing = NBDevice.objects.filter(site=nb_site, name__iexact=dev.name).first() if nb_site else None
            if existing is not None:
                skipped_devices.add(dev.name)
                why = (
                    "is managed by this sync but you may not view it"
                    if _is_tagged(existing, self.tag)
                    else "already exists there and is not managed by this sync"
                )
                conflict(dev, f"device {dev.name} in site {dev.site} {why}")

        for type_name, nb_model in _COMPONENT_TYPES.items():
            ct = f"dcim.{nb_model._meta.model_name}"
            for comp in new(type_name):
                if comp.device in skipped_devices:
                    report.dependents_skipped += 1
                    skipped_ends.add((comp.device, ct, comp.name))
                    drop.append(comp)
                    continue
                owner = self.get_or_none("device", comp.device)
                if owner is None or owner.nb_pk is None:
                    continue  # its device is created by this run, so the component can't exist yet
                existing = nb_model.objects.filter(device_id=owner.nb_pk, name=comp.name).first()
                if existing is not None and _is_tagged(existing, self.tag):
                    skipped_ends.add((comp.device, ct, comp.name))
                    conflict(comp, f"{type_name} {comp.device}:{comp.name} is managed by this sync but not visible")
                elif existing is not None:
                    report.adopted.append(f"{nb_model._meta.verbose_name} {comp.device}:{comp.name}")

        doomed_cables = (
            {c.nb_pk for c in self.get_all("cable") if source.get_or_none("cable", c.get_unique_id()) is None}
            if allow_deletes
            else set()
        )
        for cab in new("cable"):
            ends = [(cab.a_device, cab.a_type, cab.a_name), (cab.b_device, cab.b_type, cab.b_name)]
            if any(end[0] in skipped_devices or end in skipped_ends for end in ends):
                report.dependents_skipped += 1
                drop.append(cab)
                continue
            for dev_name, ct, name in ends:
                owner = self.get_or_none("device", dev_name)
                nb_model = _CT_MODEL.get(ct)
                if owner is None or owner.nb_pk is None or nb_model is None:
                    continue
                term = nb_model.objects.filter(device_id=owner.nb_pk, name=name).first()
                if term is not None and term.cable_id and term.cable_id not in doomed_cables:
                    conflict(cab, f"cable {cab.label or cab.get_unique_id()}: {dev_name}:{name} is already cabled")
                    break

        for model in drop:
            source.remove(model)
        return report

    # -- deletes --------------------------------------------------------------

    def delete_candidates(self, source) -> list:
        """Owned objects no longer in Railyard, in a safe deletion order (dependents first)."""
        out = []
        for type_name in reversed(self.top_level):
            out += [m for m in self.get_all(type_name) if source.get_or_none(type_name, m.get_unique_id()) is None]
        return out

    def preview_delete(self, model, scheduled: set[tuple[str, int]]) -> list[str]:
        """Why deleting ``model`` would be refused (empty if it wouldn't), ignoring blockers that this run
        deletes first. Read-only; used by the dry run."""
        nb_model = _NB_MODEL[model.get_type()]
        obj = _owned(self, nb_model, "view").filter(pk=model.nb_pk).first()
        if obj is None:
            return ["not visible"]
        if not nb_model.objects.restrict(self.user, "delete").filter(pk=obj.pk).exists():
            return [f"{self.user} may not delete it"]
        return self.deletion_blockers(obj, scheduled)

    def deletion_blockers(self, obj, scheduled: set[tuple[str, int]] = frozenset()) -> list[str]:
        """Reasons deleting ``obj`` would reach beyond what this sync owns: NetBox protecting it, or a
        cascade/SET_NULL touching untagged objects. ``scheduled`` lists (label, pk) this run deletes
        first, which therefore don't block."""
        tag_pk = self.tag.pk
        is_device = isinstance(obj, NBDevice)
        reasons: list[str] = []

        def foreign(nb_model, pks) -> int:
            label = nb_model._meta.label_lower
            if label in _BOOKKEEPING or (is_device and nb_model in _DEVICE_COMPONENTS):
                return 0
            pks = [pk for pk in pks if (label, pk) not in scheduled and not (nb_model is type(obj) and pk == obj.pk)]
            if not pks:
                return 0
            if _taggable(nb_model):
                return nb_model.objects.filter(pk__in=pks).exclude(tags__pk=tag_pk).count()
            return len(pks)

        def pks_of(objs) -> tuple:
            if isinstance(objs, QuerySet):
                return objs.model, list(objs.values_list("pk", flat=True))
            objs = list(objs)
            return (type(objs[0]), [o.pk for o in objs]) if objs else (None, [])

        collector = Collector(using=router.db_for_write(type(obj)))
        try:
            collector.collect([obj])
        except (ProtectedError, RestrictedError) as exc:
            blocking = exc.protected_objects if isinstance(exc, ProtectedError) else exc.restricted_objects
            counts: dict = {}
            for other in blocking:
                if (other._meta.label_lower, other.pk) not in scheduled:
                    counts[type(other)] = counts.get(type(other), 0) + 1
            if counts:
                listed = ", ".join(_verbose(m, n) for m, n in counts.items())
                return [f"still in use by {listed} this sync doesn't delete"]
            return []  # only blocked by things this run deletes first

        for nb_model, instances in collector.data.items():
            if n := foreign(nb_model, [i.pk for i in instances]):
                reasons.append(f"would also delete {_verbose(nb_model, n)} not managed by this sync")
        for qs in collector.fast_deletes:
            nb_model, pks = pks_of(qs)
            if nb_model is not None and (n := foreign(nb_model, pks)):
                reasons.append(f"would also delete {_verbose(nb_model, n)} not managed by this sync")
        for batches in collector.field_updates.values():
            for batch in batches:
                nb_model, pks = pks_of(batch)
                if nb_model is not None and (n := foreign(nb_model, pks)):
                    reasons.append(f"would modify {_verbose(nb_model, n)} not managed by this sync")

        foreign_cables = NBCable.objects.exclude(tags__pk=tag_pk)
        if is_device:
            n = foreign_cables.filter(terminations___device=obj).distinct().count()
        elif getattr(obj, "cable_id", None):
            n = foreign_cables.filter(pk=obj.cable_id).count()
        else:
            n = 0
        if n:
            reasons.append(f"connected by {_verbose(NBCable, n)} not managed by this sync")
        return reasons


_NB_MODEL = {
    "manufacturer": NBManufacturer,
    "device_type": NBDeviceType,
    "device_role": NBDeviceRole,
    "site": NBSite,
    "rack": NBRack,
    "device": NBDevice,
    "cable": NBCable,
    **_COMPONENT_TYPES,
}
