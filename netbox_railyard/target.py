"""The NetBox DiffSync *target* adapter over NetBox's ORM.

Each ``NetBox*`` model subclasses railyard-sync's canonical model (``railyard_sync.export.models``, the same
diff surface the sync document is read into) and implements ``create``/``update``/``delete`` against the NetBox
ORM (``dcim.models``). ``railyard_sync.export.netbox_rest`` applies the same rules over the REST API (the
``railyard-sync export netbox`` command); what does not depend on the API is shared through
``railyard_sync.export.ownership``. Keep the two in step.

Ownership — what the sync will and won't touch:

- **Owned objects** carry the project's ownership tag (``ownership_tag``). ``load()`` reads back only those,
  each remembering its NetBox primary key (``nb_id``), and ``update``/``delete`` act only on that exact
  object, re-fetched by pk *and* tag. An object without the tag is never updated or deleted.
- **Shared objects that already exist** (manufacturers, device types, roles, sites, locations, racks) are
  *used* as they are — a device may be placed in an existing site or rack — but never tagged, changed or
  deleted.
- **Conflicts** — a device of the same name already in the site, a device type whose slug another type uses,
  a port that is already cabled — are reported and skipped, together with whatever depends on them. The
  sync never adopts them.
- **Components on a device the sync owns** that already exist by name (typically created from the device
  type's component templates when the sync created the device) are adopted and tagged.
- **Renames**: an owned device whose Railyard id (the ``railyard_id`` custom field) has another name in the
  document is renamed in place, keeping its components, cables and history.
- **Deletes** (opt-in) run in reverse dependency order and are refused — the object is kept and reported —
  when NetBox would cascade to, or is protected by, anything the sync doesn't own. Deleting an owned device
  removes its components with it, as NetBox always does.

Every query is ``.restrict()``-ed to what the job's user may view/add/change/delete, so NetBox object-level
permission constraints apply to the sync exactly as they would in the UI.

NetBox-only module — needs a live NetBox; exercised by ``tests/integration``.
"""

from __future__ import annotations

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
    Location as NBLocation,
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
from railyard_sync.export import models
from railyard_sync.export.devicetype_library import DeviceTypeLibrary, DeviceTypeSpec
from railyard_sync.export.mappings import slugify
from railyard_sync.export.ownership import COMPONENT_TYPES, OwnershipMixin, OwnershipReport

from . import devicetypes

CUSTOM_FIELD = "railyard_id"
POWER_TYPES = {"dcim.powerport", "dcim.poweroutlet"}

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
assert tuple(_COMPONENT_TYPES) == COMPONENT_TYPES

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


def _find_in_site(nb_model, site_name: str, name: str | None):
    """A location or rack by name within the named site (``None`` when either is missing)."""
    site = _find_site(site_name) if name else None
    return nb_model.objects.filter(site=site, name=name).first() if site is not None else None


def _is_tagged(obj, tag) -> bool:
    return tag is not None and obj.tags.filter(pk=tag.pk).exists()


def _taggable(nb_model) -> bool:
    try:
        nb_model._meta.get_field("tags")
    except FieldDoesNotExist:
        return False
    return True


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
    obj = _owned(adapter, nb_model, action).filter(pk=model.nb_id).first() if model.nb_id else None
    if obj is None:
        raise PermissionDenied(
            f"{model.get_type()} {model.get_unique_id()}: no longer a Railyard-owned object that "
            f"{adapter.user} may {action}"
        )
    return obj


def _claim(adapter, obj, action: str = "add", user_tags=None) -> int:
    """Tag a just-created (or adopted) object as owned (plus its design tags), then check the user was allowed to."""
    obj.tags.add(adapter.tag, *adapter.tags_for(user_tags))
    _require(adapter, obj, action)
    return obj.pk


def _visible(adapter, nb_model) -> QuerySet:
    return nb_model.objects.restrict(adapter.user, "view")


def _visible_site(adapter, name: str):
    found = _find_site(name)
    return _visible(adapter, NBSite).filter(pk=found.pk).first() if found is not None else None


def _visible_in_site(adapter, nb_model, site, name: str | None):
    """An optional location/rack reference: ``None`` for a blank name; ``ObjectNotCreated`` when it is missing."""
    if not name:
        return None
    obj = _visible(adapter, nb_model).filter(site=site, name=name).first()
    if obj is None:
        raise ObjectNotCreated(f"{nb_model._meta.verbose_name} {site}/{name} not found")
    return obj


def _owned_device(adapter, name: str):
    return _owned(adapter, NBDevice, "view").filter(name=name).first()


def _created(cls, adapter, ids, attrs, pk):
    model = super(cls, cls).create(adapter, ids=ids, attrs=attrs)
    model.nb_id = pk
    return model


def _save(adapter, obj, *, clean: bool = False) -> None:
    """Save a changed owned object, then check the user may change it as it now is (object-level constraints).

    ``clean`` runs NetBox's validation first (devices: a U position another device occupies, a rack in
    another location)."""
    if clean:
        obj.full_clean()
    obj.save()
    _require(adapter, obj, "change")


def _delete(model, nb_model):
    """Delete an owned object unless that would reach beyond what the sync owns (then keep it)."""
    adapter = model.adapter
    obj = _owned(adapter, nb_model, "delete").filter(pk=model.nb_id).first() if model.nb_id else None
    blockers = [f"{adapter.user} may not delete it"] if obj is None else adapter.deletion_blockers(obj)
    if blockers:
        adapter.report.kept.append(f"{model.get_type()} {model.get_unique_id()}: {'; '.join(blockers)}")
        return False
    obj.delete()
    return True


class _Deletes:
    """``delete()`` for every target model: delete if safe, else keep and report."""

    def delete(self):
        _delete(self, _NB_MODEL[self.get_type()])
        super().delete()
        return self


# ---- target models ---------------------------------------------------------


class NetBoxManufacturer(_Deletes, models.Manufacturer):
    nb_id: int | None = None

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
        _save(self.adapter, obj)
        return super().update(attrs)


class NetBoxDeviceType(_Deletes, models.DeviceType):
    nb_id: int | None = None

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
            components=adapter.library_components(ids, attrs),
        )
        obj = devicetypes.create_device_type(spec, manufacturer, import_components=adapter.import_components)
        return _created(cls, adapter, ids, attrs, _claim(adapter, obj))

    def update(self, attrs):
        obj = _get_owned(self, NBDeviceType, "change")
        for name in ("slug", "u_height", "is_full_depth", "part_number"):
            if name in attrs:
                setattr(obj, name, attrs[name])
        _save(self.adapter, obj)
        return super().update(attrs)


class NetBoxDeviceRole(_Deletes, models.DeviceRole):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        if _find_role(ids["name"]) is not None:
            raise ObjectNotCreated(f"device role {ids['name']} already exists and is not managed by this sync")
        obj = NBDeviceRole(
            name=ids["name"],
            slug=attrs.get("slug") or slugify(ids["name"]),
            color=attrs.get("color") or "9e9e9e",
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
        _save(self.adapter, obj)
        return super().update(attrs)


class NetBoxSite(_Deletes, models.Site):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        if _find_site(ids["name"]) is not None:
            raise ObjectNotCreated(f"site {ids['name']} already exists and is not managed by this sync")
        obj = NBSite(
            name=ids["name"],
            slug=attrs.get("slug") or slugify(ids["name"]),
            status=attrs.get("status") or "active",
            facility=attrs.get("facility", ""),
        )
        obj.full_clean()
        obj.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, obj))

    def update(self, attrs):
        obj = _get_owned(self, NBSite, "change")
        for name in ("slug", "status", "facility"):
            if name in attrs:
                setattr(obj, name, attrs[name])
        _save(self.adapter, obj)
        return super().update(attrs)


class NetBoxLocation(_Deletes, models.Location):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        site = _visible_site(adapter, ids["site"])
        if site is None:
            raise ObjectNotCreated(f"location {ids['name']}: site {ids['site']} not found")
        if NBLocation.objects.filter(site=site, name=ids["name"]).exists():
            raise ObjectNotCreated(f"location {ids['name']} already exists and is not managed by this sync")
        obj = NBLocation(
            site=site,
            parent=_visible_in_site(adapter, NBLocation, site, attrs.get("parent")),
            name=ids["name"],
            slug=attrs.get("slug") or slugify(ids["name"]),
            status=attrs.get("status") or "active",
            facility=attrs.get("facility", ""),
        )
        obj.full_clean()
        obj.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, obj))

    def update(self, attrs):
        obj = _get_owned(self, NBLocation, "change")
        for name in ("slug", "status", "facility"):
            if name in attrs:
                setattr(obj, name, attrs[name])
        if "parent" in attrs:
            obj.parent = _visible_in_site(self.adapter, NBLocation, obj.site, attrs["parent"])
        _save(self.adapter, obj)
        return super().update(attrs)


class NetBoxRack(_Deletes, models.Rack):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        site = _visible_site(adapter, ids["site"])
        if site is None:
            raise ObjectNotCreated(f"rack {ids['name']}: site {ids['site']} not found")
        if NBRack.objects.filter(site=site, name=ids["name"]).exists():
            raise ObjectNotCreated(f"rack {ids['name']} already exists and is not managed by this sync")
        obj = NBRack(
            site=site,
            location=_visible_in_site(adapter, NBLocation, site, attrs.get("location")),
            name=ids["name"],
            status=attrs.get("status") or "active",
            width=attrs.get("width", 19),
            u_height=attrs.get("u_height", 42),
            desc_units=attrs.get("desc_units", False),
            comments=attrs.get("comments", ""),
        )
        obj.full_clean()
        obj.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, obj, user_tags=attrs.get("tags")))

    def update(self, attrs):
        obj = _get_owned(self, NBRack, "change")
        for name in ("status", "width", "u_height", "desc_units", "comments"):
            if name in attrs:
                setattr(obj, name, attrs[name])
        if "location" in attrs:
            obj.location = _visible_in_site(self.adapter, NBLocation, obj.site, attrs["location"])
        _save(self.adapter, obj)
        if "tags" in attrs:
            self.adapter.set_tags(obj, attrs["tags"])
        return super().update(attrs)


class NetBoxDevice(_Deletes, models.Device):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        site = _visible_site(adapter, attrs["site"])
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
            location=_visible_in_site(adapter, NBLocation, site, attrs.get("location")),
            rack=rack,
            position=attrs.get("position"),
            face=attrs.get("face") or "",
            status=attrs.get("status") or "active",
            serial=attrs.get("serial", ""),
            comments=attrs.get("comments", ""),
        )
        if attrs.get("railyard_id"):
            device.custom_field_data[CUSTOM_FIELD] = attrs["railyard_id"]
        device.full_clean()  # e.g. refuses a position another device already occupies
        device.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, device, user_tags=attrs.get("tags")))

    def update(self, attrs):
        obj = _get_owned(self, NBDevice, "change")
        if "role" in attrs:
            obj.role = _find_role(attrs["role"])
        if "device_type" in attrs or "manufacturer" in attrs:
            device_type = _find_device_type(
                attrs.get("manufacturer", self.manufacturer), attrs.get("device_type", self.device_type)
            )
            if device_type is None:
                raise PermissionDenied(f"device {self.name}: device type {attrs.get('device_type')} not found")
            obj.device_type = device_type
        if "site" in attrs:
            obj.site = _visible_site(self.adapter, attrs["site"])
        if "rack" in attrs or "site" in attrs:
            rack = attrs.get("rack", self.rack)
            obj.rack = NBRack.objects.filter(site=obj.site, name=rack).first() if rack else None
        if "location" in attrs or "site" in attrs:
            obj.location = _visible_in_site(self.adapter, NBLocation, obj.site, attrs.get("location", self.location))
        for name in ("position", "face", "status", "serial", "comments"):
            if name in attrs:
                setattr(obj, name, attrs[name])
        if "position" in attrs and attrs["position"] is None:
            obj.face = ""  # a device without a U position has no face
        if "railyard_id" in attrs:
            obj.custom_field_data[CUSTOM_FIELD] = attrs["railyard_id"] or None
        _save(self.adapter, obj, clean=True)
        if "tags" in attrs:
            self.adapter.set_tags(obj, attrs["tags"])
        return super().update(attrs)


# Helpers for the (device, name, type) components. Module functions rather than a mixin with an `_nb_model`
# class attribute, because DiffSyncModel is Pydantic and would treat that attribute as a private attribute
# (so it wouldn't resolve on the subclass).
def _ensure_component(adapter, nb_model, ids, **defaults):
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


def _update_component(model, nb_model, attrs, fields=("type",)):
    obj = _get_owned(model, nb_model, "change")
    for name in fields:
        if name in attrs:
            setattr(obj, name, attrs[name])
    _save(model.adapter, obj)
    return obj


class NetBoxInterface(_Deletes, models.Interface):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        _, pk = _ensure_component(adapter, NBInterface, ids, type=attrs.get("type") or "other")
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        _update_component(self, NBInterface, attrs)
        return super().update(attrs)


class NetBoxRearPort(_Deletes, models.RearPort):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        _, pk = _ensure_component(
            adapter, NBRearPort, ids, type=attrs.get("type") or "8p8c", positions=attrs.get("positions", 1)
        )
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        _update_component(self, NBRearPort, attrs, ("type", "positions"))
        return super().update(attrs)


def _map_front_port(adapter, fp, rear_name: str, rear_position: int, *, replace: bool) -> None:
    """Couple a front port to its rear port through a ``PortMapping`` row (NetBox 4.5+ has no rear_port FK).

    A new or adopted front port is mapped only when it has no mapping yet; ``replace`` re-maps an owned one."""
    rear = _owned(adapter, NBRearPort, "view").filter(device=fp.device, name=rear_name).first() if rear_name else None
    if rear is None:
        return
    mappings = NBPortMapping.objects.filter(front_port=fp)
    if mappings.exists():
        if not replace:
            return
        mappings.delete()
    if replace:
        # Ports swapped in Railyard: the rear position is still held by another owned front port, which this
        # run re-maps too. (One the sync doesn't own keeps it, and the sync fails and rolls back.)
        NBPortMapping.objects.filter(
            rear_port=rear, rear_port_position=rear_position, front_port__tags=adapter.tag
        ).delete()
    NBPortMapping.objects.create(
        device=fp.device, front_port=fp, front_port_position=1, rear_port=rear, rear_port_position=rear_position
    )


class NetBoxFrontPort(_Deletes, models.FrontPort):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        fp, pk = _ensure_component(adapter, NBFrontPort, ids, type=attrs.get("type") or "8p8c", positions=1)
        _map_front_port(adapter, fp, attrs.get("rear_port", ""), attrs.get("rear_port_position", 1), replace=False)
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        fp = _update_component(self, NBFrontPort, attrs)
        if "rear_port" in attrs or "rear_port_position" in attrs:
            rear = attrs.get("rear_port", self.rear_port)
            _map_front_port(
                self.adapter, fp, rear, attrs.get("rear_port_position", self.rear_port_position), replace=True
            )
        return super().update(attrs)


class NetBoxPowerOutlet(_Deletes, models.PowerOutlet):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        _, pk = _ensure_component(adapter, NBPowerOutlet, ids, type=attrs.get("type") or "iec-60320-c13")
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        _update_component(self, NBPowerOutlet, attrs)
        return super().update(attrs)


class NetBoxPowerPort(_Deletes, models.PowerPort):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        _, pk = _ensure_component(adapter, NBPowerPort, ids, type=attrs.get("type") or "iec-60320-c14")
        return _created(cls, adapter, ids, attrs, pk)

    def update(self, attrs):
        _update_component(self, NBPowerPort, attrs)
        return super().update(attrs)


def _owned_termination(adapter, device_name: str, ct: str, name: str):
    """A cable end on a device the sync owns (``None`` if the device or component isn't there)."""
    model = _CT_MODEL.get(ct)
    device = _owned_device(adapter, device_name)
    if model is None or device is None:
        return None
    return model.objects.filter(device=device, name=name).first()


class NetBoxCable(_Deletes, models.Cable):
    nb_id: int | None = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        a = _owned_termination(adapter, ids["a_device"], ids["a_type"], ids["a_name"])
        b = _owned_termination(adapter, ids["b_device"], ids["b_type"], ids["b_name"])
        if a is None or b is None:
            raise ObjectNotCreated(f"cable {attrs.get('label') or ids}: a termination is missing")
        for end in (a, b):
            if end.cable_id:
                raise ObjectNotCreated(f"cable {attrs.get('label') or ids}: {end.device}:{end} is already cabled")
        cable = NBCable(
            status=attrs.get("status") or "connected",
            label=attrs.get("label", ""),
            type=attrs.get("type") or ("power" if attrs.get("is_power") else ""),
            color=attrs.get("color", ""),
        )
        cable.a_terminations = [a]
        cable.b_terminations = [b]
        cable.save()
        return _created(cls, adapter, ids, attrs, _claim(adapter, cable))

    def update(self, attrs):
        cable = _get_owned(self, NBCable, "change")
        for name in ("label", "status", "color", "type"):
            if name in attrs:
                setattr(cable, name, attrs[name])
        cable.save()
        _require(self.adapter, cable, "change")
        return super().update(attrs)


# ---- adapter ---------------------------------------------------------------


class NetBoxAdapter(OwnershipMixin, Adapter):
    manufacturer = NetBoxManufacturer
    device_type = NetBoxDeviceType
    device_role = NetBoxDeviceRole
    site = NetBoxSite
    location = NetBoxLocation
    rack = NetBoxRack
    device = NetBoxDevice
    interface = NetBoxInterface
    rear_port = NetBoxRearPort
    front_port = NetBoxFrontPort
    power_outlet = NetBoxPowerOutlet
    power_port = NetBoxPowerPort
    cable = NetBoxCable

    top_level = models.TOP_LEVEL

    def __init__(
        self,
        *,
        tag,
        user,
        user_tags: dict | None = None,
        import_components: bool = False,
        devicetype_library: DeviceTypeLibrary | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.tag = tag  # None before the first real sync: nothing is owned yet
        self.user = user
        self.user_tags = dict(user_tags or {})  # design tag slug -> NetBox Tag
        self.import_components = import_components
        self.dtl = devicetype_library
        self.source: Adapter | None = None
        self.report = OwnershipReport()
        self.renames: list[tuple[str, str, int]] = []  # (old name, new name, NetBox pk)

    # -- tags and device types ---------------------------------------------------

    def tags_for(self, user_slugs) -> list:
        """The design tags an owned object carries, of those that exist in NetBox."""
        return [self.user_tags[s] for s in user_slugs or [] if s in self.user_tags]

    def set_tags(self, obj, user_slugs) -> None:
        """An owned object's tags: the ownership tag plus exactly its design tags."""
        obj.tags.set([self.tag, *self.tags_for(user_slugs)])

    def _user_tags(self, obj) -> list[str]:
        return sorted(t.slug for t in obj.tags.all() if self.tag is None or t.pk != self.tag.pk)

    def library_components(self, ids: dict, attrs: dict) -> dict:
        """A new device type's component templates from the devicetype-library, when ``import_components``."""
        if not self.import_components or self.dtl is None:
            return {}
        src = self.source.get_or_none("device_type", ids) if self.source is not None else None
        spec = self.dtl.resolve(
            {
                "manufacturer": ids["manufacturer"],
                "model": ids["model"],
                "uHeight": attrs.get("u_height", 1),
                "fullDepth": attrs.get("is_full_depth", True),
                "key": getattr(src, "library_slug", "") or attrs.get("slug", ""),
            }
        )
        return spec.components

    # -- load -----------------------------------------------------------------

    def _add(self, model) -> None:
        try:
            self.add(model)
        except ObjectAlreadyExists:
            self.report.warnings.append(
                f"two Railyard-owned objects share the identity {model.get_type()} {model.get_unique_id()}; "
                f"NetBox #{model.nb_id} is ignored (neither updated nor deleted)"
            )

    def load(self) -> None:
        """Load only the objects this project owns (carry its tag) that the user may view, each with its
        NetBox pk, so a re-sync diffs to no-change and writes go to exactly these objects."""

        def owned(nb_model):
            return _owned(self, nb_model, "view")

        for mfr in owned(NBManufacturer):
            self._add(self.manufacturer(name=mfr.name, slug=mfr.slug, nb_id=mfr.pk))
        for dt in owned(NBDeviceType).select_related("manufacturer"):
            self._add(
                self.device_type(
                    manufacturer=dt.manufacturer.name,
                    model=dt.model,
                    slug=dt.slug,
                    u_height=int(dt.u_height or 0),
                    is_full_depth=dt.is_full_depth,
                    part_number=dt.part_number or "",
                    nb_id=dt.pk,
                )
            )
        for role in owned(NBDeviceRole):
            self._add(self.device_role(name=role.name, slug=role.slug, color=role.color, nb_id=role.pk))
        for site in owned(NBSite):
            self._add(
                self.site(name=site.name, slug=site.slug, status=site.status, facility=site.facility, nb_id=site.pk)
            )
        for loc in owned(NBLocation).select_related("site", "parent"):
            self._add(
                self.location(
                    site=loc.site.name,
                    name=loc.name,
                    slug=loc.slug,
                    parent=loc.parent.name if loc.parent else "",
                    status=loc.status,
                    facility=loc.facility or "",
                    nb_id=loc.pk,
                )
            )
        for rk in owned(NBRack).select_related("site", "location").prefetch_related("tags"):
            self._add(
                self.rack(
                    site=rk.site.name,
                    name=rk.name,
                    status=rk.status,
                    width=int(rk.width),
                    u_height=int(rk.u_height),
                    desc_units=rk.desc_units,
                    location=rk.location.name if rk.location else "",
                    comments=rk.comments or "",
                    tags=self._user_tags(rk),
                    nb_id=rk.pk,
                )
            )
        devices = owned(NBDevice).select_related("device_type__manufacturer", "role", "site", "rack", "location")
        for dev in devices.prefetch_related("tags"):
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
                    railyard_id=str((dev.custom_field_data or {}).get(CUSTOM_FIELD) or ""),
                    serial=dev.serial or "",
                    location=dev.location.name if dev.location else "",
                    comments=dev.comments or "",
                    tags=self._user_tags(dev),
                    nb_id=dev.pk,
                )
            )

        for rp in owned(NBRearPort).select_related("device"):
            self._add(
                self.rear_port(device=rp.device.name, name=rp.name, type=rp.type, positions=rp.positions, nb_id=rp.pk)
            )
        for fp in owned(NBFrontPort).select_related("device").prefetch_related("mappings__rear_port"):
            mappings = sorted(fp.mappings.all(), key=lambda m: m.front_port_position)
            mapping = mappings[0] if mappings else None
            self._add(
                self.front_port(
                    device=fp.device.name,
                    name=fp.name,
                    type=fp.type,
                    rear_port=mapping.rear_port.name if mapping else "",
                    rear_port_position=mapping.rear_port_position if mapping else 1,
                    nb_id=fp.pk,
                )
            )
        for iface in owned(NBInterface).select_related("device"):
            self._add(self.interface(device=iface.device.name, name=iface.name, type=iface.type, nb_id=iface.pk))
        for po in owned(NBPowerOutlet).select_related("device"):
            self._add(self.power_outlet(device=po.device.name, name=po.name, type=po.type, nb_id=po.pk))
        for pp in owned(NBPowerPort).select_related("device"):
            self._add(self.power_port(device=pp.device.name, name=pp.name, type=pp.type, nb_id=pp.pk))
        for cable in owned(NBCable):
            a_terms, b_terms = cable.a_terminations, cable.b_terminations
            if len(a_terms) != 1 or len(b_terms) != 1:
                continue  # not a shape this sync creates
            a, b = a_terms[0], b_terms[0]
            if getattr(a, "device", None) is None or getattr(b, "device", None) is None:
                continue
            a_type, b_type = _ct_string(a), _ct_string(b)
            self._add(
                self.cable(
                    a_device=a.device.name,
                    a_type=a_type,
                    a_name=a.name,
                    b_device=b.device.name,
                    b_type=b_type,
                    b_name=b.name,
                    is_power=cable.type == "power" or bool({a_type, b_type} & POWER_TYPES),
                    label=cable.label or "",
                    type=cable.type or "",
                    status=cable.status,
                    color=(cable.color or "").lower(),
                    nb_id=cable.pk,
                )
            )

    # -- ownership pre-pass ---------------------------------------------------

    def _owns(self, model) -> bool:
        return self.get_or_none(model.get_type(), model.get_unique_id()) is not None

    def device_named(self, site: str, name: str) -> int | None:
        nb_site = _find_site(site)
        found = NBDevice.objects.filter(site=nb_site, name__iexact=name).first() if nb_site else None
        return found.pk if found else None

    def reconcile(self, source, *, allow_deletes: bool = False) -> OwnershipReport:
        """Decide, before anything is written, what to do about Railyard objects that the sync doesn't
        own yet but that already exist in NetBox; drop from ``source`` everything it must not create.

        Read-only, so a dry run reports exactly what a real run will skip, rename or use as-is.
        """
        self.source = source
        report = self.report
        self.detect_renames(source)
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
        for loc in new("location"):
            if _find_in_site(NBLocation, loc.site, loc.name) is not None:
                reference(loc, f"location {loc.site}/{loc.name}")
        for rack in new("rack"):
            if _find_in_site(NBRack, rack.site, rack.name) is not None:
                reference(rack, f"rack {rack.site}/{rack.name}")
        # NetBox puts a racked device in its rack's location; in a rack used as-is that is the rack's real
        # location, which need not be the one Railyard drew.
        used_as_is = {(r.site, r.name) for r in drop if r.get_type() == "rack"}
        for dev in source.get_all("device"):
            if dev.rack and (dev.site, dev.rack) in used_as_is:
                nb_rack = _find_in_site(NBRack, dev.site, dev.rack)
                dev.location = nb_rack.location.name if nb_rack.location else ""

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
                if owner is None or owner.nb_id is None:
                    continue  # its device is created by this run, so the component can't exist yet
                existing = nb_model.objects.filter(device_id=owner.nb_id, name=comp.name).first()
                if existing is not None and _is_tagged(existing, self.tag):
                    skipped_ends.add((comp.device, ct, comp.name))
                    conflict(comp, f"{type_name} {comp.device}:{comp.name} is managed by this sync but not visible")
                elif existing is not None:
                    report.adopted.append(f"{nb_model._meta.verbose_name} {comp.device}:{comp.name}")

        doomed_cables = (
            {c.nb_id for c in self.get_all("cable") if source.get_or_none("cable", c.get_unique_id()) is None}
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
                if owner is None or owner.nb_id is None or nb_model is None:
                    continue
                term = nb_model.objects.filter(device_id=owner.nb_id, name=name).first()
                if term is not None and term.cable_id and term.cable_id not in doomed_cables:
                    conflict(cab, f"cable {cab.label or cab.get_unique_id()}: {dev_name}:{name} is already cabled")
                    break

        for model in drop:
            source.remove(model)
        return report

    def apply_renames(self) -> None:
        """Rename the NetBox devices ``reconcile`` found renamed in Railyard (a real run only)."""
        for old, new, pk in self.renames:
            obj = _owned(self, NBDevice, "change").filter(pk=pk).first()
            if obj is None:
                raise PermissionDenied(f"device {old}: no longer a Railyard-owned device that {self.user} may change")
            obj.name = new
            _save(self, obj, clean=True)

    # -- deletes --------------------------------------------------------------

    def preview_delete(self, model, scheduled: set[tuple[str, int]]) -> list[str]:
        """Why deleting ``model`` would be refused (empty if it wouldn't), ignoring blockers that this run
        deletes first. Read-only; used by the dry run."""
        nb_model = _NB_MODEL[model.get_type()]
        obj = _owned(self, nb_model, "view").filter(pk=model.nb_id).first()
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
    "location": NBLocation,
    "rack": NBRack,
    "device": NBDevice,
    "cable": NBCable,
    **_COMPONENT_TYPES,
}
