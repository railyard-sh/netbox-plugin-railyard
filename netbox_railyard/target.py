"""The NetBox DiffSync *target* adapter.

Each ``NetBox*`` model subclasses the canonical model in ``railyard.models`` (identical diff surface)
and implements ``create``/``update``/``delete`` against the NetBox ORM (``dcim.models``).

Ownership & scope: every object the sync creates (or adopts) is tagged with the project's ``RY:<name>``
tag, and ``NetBoxAdapter.load()`` reads back **only** objects carrying that tag. So a re-sync diffs to
no-change, operator-added objects are never touched, and a full-mirror delete only ever removes
Railyard-owned objects. Creates use get-or-create, so the first run over pre-existing data simply
adopts and tags it rather than duplicating.

NetBox-only module — needs a live NetBox; exercised by the local harness in ``dev/``.
"""

from __future__ import annotations

from dcim.models import (
    Cable as NBCable,
    Device as NBDevice,
    DeviceRole as NBDeviceRole,
    DeviceType as NBDeviceType,
    FrontPort as NBFrontPort,
    Interface as NBInterface,
    Manufacturer as NBManufacturer,
    PortMapping as NBPortMapping,
    PowerOutlet as NBPowerOutlet,
    PowerPort as NBPowerPort,
    Rack as NBRack,
    RearPort as NBRearPort,
    Site as NBSite,
)
from diffsync import Adapter

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


def _set_railyard_id(obj, value: str) -> None:
    if value:
        obj.custom_field_data[CUSTOM_FIELD] = value


def _tag(obj, tag) -> None:
    """Mark an object as Railyard-owned. ``tag`` may be None (adapter used without a tag)."""
    if tag is not None:
        obj.tags.add(tag)


def _ct_string(term) -> str:
    """dcim.* content-type id for a cable termination object (e.g. an Interface -> 'dcim.interface')."""
    return f"dcim.{term._meta.model_name}"


# ---- target models ---------------------------------------------------------


class NetBoxManufacturer(models.Manufacturer):
    @classmethod
    def create(cls, adapter, ids, attrs):
        obj = devicetypes.ensure_manufacturer(ids["name"], attrs.get("slug"))
        _tag(obj, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        obj = NBManufacturer.objects.filter(name=self.name).first()
        if obj and attrs.get("slug"):
            obj.slug = attrs["slug"]
            obj.save()
        return super().update(attrs)

    def delete(self):
        NBManufacturer.objects.filter(name=self.name).delete()
        super().delete()
        return self


class NetBoxDeviceType(models.DeviceType):
    @classmethod
    def create(cls, adapter, ids, attrs):
        spec = DeviceTypeSpec(
            manufacturer=ids["manufacturer"],
            model=ids["model"],
            slug=attrs.get("slug", ""),
            u_height=attrs.get("u_height", 1),
            is_full_depth=attrs.get("is_full_depth", True),
            part_number=attrs.get("part_number", ""),
        )
        obj = devicetypes.ensure_device_type(spec, import_components=adapter.import_components)
        _tag(obj, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        obj = NBDeviceType.objects.filter(manufacturer__name=self.manufacturer, model=self.model).first()
        if obj:
            for field in ("slug", "u_height", "is_full_depth", "part_number"):
                if field in attrs:
                    setattr(obj, field, attrs[field])
            obj.save()
        return super().update(attrs)

    def delete(self):
        NBDeviceType.objects.filter(manufacturer__name=self.manufacturer, model=self.model).delete()
        super().delete()
        return self


class NetBoxDeviceRole(models.DeviceRole):
    @classmethod
    def create(cls, adapter, ids, attrs):
        obj, _ = NBDeviceRole.objects.get_or_create(
            name=ids["name"],
            defaults={"slug": attrs.get("slug") or slugify(ids["name"]), "color": attrs.get("color", "9e9e9e")},
        )
        _tag(obj, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        obj = NBDeviceRole.objects.filter(name=self.name).first()
        if obj:
            if attrs.get("slug"):
                obj.slug = attrs["slug"]
            if attrs.get("color"):
                obj.color = attrs["color"]
            obj.save()
        return super().update(attrs)

    def delete(self):
        NBDeviceRole.objects.filter(name=self.name).delete()
        super().delete()
        return self


class NetBoxSite(models.Site):
    @classmethod
    def create(cls, adapter, ids, attrs):
        obj, _ = NBSite.objects.get_or_create(
            name=ids["name"],
            defaults={"slug": attrs.get("slug") or slugify(ids["name"]), "status": attrs.get("status", "active")},
        )
        _tag(obj, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        obj = NBSite.objects.filter(name=self.name).first()
        if obj:
            if attrs.get("slug"):
                obj.slug = attrs["slug"]
            if attrs.get("status"):
                obj.status = attrs["status"]
            obj.save()
        return super().update(attrs)

    def delete(self):
        NBSite.objects.filter(name=self.name).delete()
        super().delete()
        return self


class NetBoxRack(models.Rack):
    @classmethod
    def create(cls, adapter, ids, attrs):
        site = NBSite.objects.filter(name=ids["site"]).first()
        if site is None:
            return super().create(adapter, ids=ids, attrs=attrs)
        obj, _ = NBRack.objects.get_or_create(
            site=site,
            name=ids["name"],
            defaults={
                "status": attrs.get("status", "active"),
                "width": attrs.get("width", 19),
                "u_height": attrs.get("u_height", 42),
                "desc_units": attrs.get("desc_units", False),
            },
        )
        _tag(obj, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        obj = NBRack.objects.filter(site__name=self.site, name=self.name).first()
        if obj:
            for field in ("status", "width", "u_height", "desc_units"):
                if field in attrs:
                    setattr(obj, field, attrs[field])
            obj.save()
        return super().update(attrs)

    def delete(self):
        NBRack.objects.filter(site__name=self.site, name=self.name).delete()
        super().delete()
        return self


class NetBoxDevice(models.Device):
    @classmethod
    def create(cls, adapter, ids, attrs):
        device = NBDevice.objects.filter(name=ids["name"]).first()
        if device is None:
            site = NBSite.objects.filter(name=attrs["site"]).first()
            if site is None:
                return super().create(adapter, ids=ids, attrs=attrs)
            dt = NBDeviceType.objects.filter(
                manufacturer__name=attrs["manufacturer"], model=attrs["device_type"]
            ).first()
            role = NBDeviceRole.objects.filter(name=attrs["role"]).first()
            rack = NBRack.objects.filter(site=site, name=attrs["rack"]).first() if attrs.get("rack") else None
            device = NBDevice(
                name=ids["name"],
                device_type=dt,
                role=role,
                site=site,
                rack=rack,
                position=attrs.get("position"),
                face=attrs.get("face") or "",
                status=attrs.get("status", "active"),
            )
        _set_railyard_id(device, attrs.get("railyard_id", ""))
        device.save()
        _tag(device, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        obj = NBDevice.objects.filter(name=self.name).first()
        if obj:
            if "role" in attrs:
                obj.role = NBDeviceRole.objects.filter(name=attrs["role"]).first()
            if "rack" in attrs:
                obj.rack = NBRack.objects.filter(site=obj.site, name=attrs["rack"]).first() if attrs["rack"] else None
            for field in ("position", "face", "status"):
                if field in attrs:
                    setattr(obj, field, attrs[field])
            if "railyard_id" in attrs:
                _set_railyard_id(obj, attrs["railyard_id"])
            obj.save()
        return super().update(attrs)

    def delete(self):
        NBDevice.objects.filter(name=self.name).delete()
        super().delete()
        return self


# Helpers for the simple (device, name, type) components. Module functions rather than a mixin with an
# `_nb_model` class attribute, because DiffSyncModel is Pydantic and would treat that attribute as a
# private attribute (so it wouldn't resolve on the subclass).
def _ensure_component(nb_model, ids, attrs, tag):
    device = NBDevice.objects.filter(name=ids["device"]).first()
    if device is None:
        return
    obj, _ = nb_model.objects.get_or_create(
        device=device, name=ids["name"], defaults={"type": attrs.get("type", "")}
    )
    _tag(obj, tag)


def _update_component(nb_model, device_name, name, attrs):
    obj = nb_model.objects.filter(device__name=device_name, name=name).first()
    if obj and "type" in attrs:
        obj.type = attrs["type"]
        obj.save()


def _delete_component(nb_model, device_name, name):
    nb_model.objects.filter(device__name=device_name, name=name).delete()


class NetBoxInterface(models.Interface):
    @classmethod
    def create(cls, adapter, ids, attrs):
        _ensure_component(NBInterface, ids, attrs, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        _update_component(NBInterface, self.device, self.name, attrs)
        return super().update(attrs)

    def delete(self):
        _delete_component(NBInterface, self.device, self.name)
        super().delete()
        return self


class NetBoxRearPort(models.RearPort):
    @classmethod
    def create(cls, adapter, ids, attrs):
        device = NBDevice.objects.filter(name=ids["device"]).first()
        if device is None:
            return super().create(adapter, ids=ids, attrs=attrs)
        obj, _ = NBRearPort.objects.get_or_create(
            device=device,
            name=ids["name"],
            defaults={"type": attrs.get("type", "8p8c"), "positions": attrs.get("positions", 1)},
        )
        _tag(obj, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        obj = NBRearPort.objects.filter(device__name=self.device, name=self.name).first()
        if obj:
            if "type" in attrs:
                obj.type = attrs["type"]
            if "positions" in attrs:
                obj.positions = attrs["positions"]
            obj.save()
        return super().update(attrs)

    def delete(self):
        NBRearPort.objects.filter(device__name=self.device, name=self.name).delete()
        super().delete()
        return self


class NetBoxFrontPort(models.FrontPort):
    @classmethod
    def create(cls, adapter, ids, attrs):
        device = NBDevice.objects.filter(name=ids["device"]).first()
        if device is None:
            return super().create(adapter, ids=ids, attrs=attrs)
        # NetBox 4.6 couples a front port to a rear port through a PortMapping row, not a rear_port FK.
        fp, _ = NBFrontPort.objects.get_or_create(
            device=device, name=ids["name"], defaults={"type": attrs.get("type", "8p8c"), "positions": 1}
        )
        rear = NBRearPort.objects.filter(device=device, name=attrs.get("rear_port")).first()
        if rear is not None and not fp.mappings.exists():
            NBPortMapping.objects.create(
                device=device,
                front_port=fp,
                front_port_position=1,
                rear_port=rear,
                rear_port_position=attrs.get("rear_port_position", 1),
            )
        _tag(fp, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        obj = NBFrontPort.objects.filter(device__name=self.device, name=self.name).first()
        if obj and "type" in attrs:
            obj.type = attrs["type"]
            obj.save()
        return super().update(attrs)

    def delete(self):
        NBFrontPort.objects.filter(device__name=self.device, name=self.name).delete()
        super().delete()
        return self


class NetBoxPowerOutlet(models.PowerOutlet):
    @classmethod
    def create(cls, adapter, ids, attrs):
        _ensure_component(NBPowerOutlet, ids, attrs, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        _update_component(NBPowerOutlet, self.device, self.name, attrs)
        return super().update(attrs)

    def delete(self):
        _delete_component(NBPowerOutlet, self.device, self.name)
        super().delete()
        return self


class NetBoxPowerPort(models.PowerPort):
    @classmethod
    def create(cls, adapter, ids, attrs):
        _ensure_component(NBPowerPort, ids, attrs, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        _update_component(NBPowerPort, self.device, self.name, attrs)
        return super().update(attrs)

    def delete(self):
        _delete_component(NBPowerPort, self.device, self.name)
        super().delete()
        return self


def _resolve_termination(device_name: str, ct: str, name: str):
    model = _CT_MODEL.get(ct)
    if model is None:
        return None
    device = NBDevice.objects.filter(name=device_name).first()
    if device is None:
        return None
    obj = model.objects.filter(device=device, name=name).first()
    if obj is None and model is NBInterface:
        obj = NBInterface.objects.create(device=device, name=name, type="other")
    return obj


class NetBoxCable(models.Cable):
    @classmethod
    def create(cls, adapter, ids, attrs):
        a = _resolve_termination(ids["a_device"], ids["a_type"], ids["a_name"])
        b = _resolve_termination(ids["b_device"], ids["b_type"], ids["b_name"])
        if a is None or b is None:
            return super().create(adapter, ids=ids, attrs=attrs)  # missing termination — leave as-is
        # Adopt an existing cable (either end already cabled), else create a new one.
        existing = a.cable if a.cable_id else (b.cable if b.cable_id else None)
        if existing is not None:
            _tag(existing, adapter.tag)
        else:
            cable = NBCable(status="connected", label=attrs.get("label", ""))
            if attrs.get("is_power"):
                cable.type = "power"
            cable.a_terminations = [a]
            cable.b_terminations = [b]
            cable.save()
            _tag(cable, adapter.tag)
        return super().create(adapter, ids=ids, attrs=attrs)

    def _existing_cable(self):
        for dev, ct, name in (
            (self.a_device, self.a_type, self.a_name),
            (self.b_device, self.b_type, self.b_name),
        ):
            term = _resolve_termination(dev, ct, name)
            if term is not None and term.cable_id:
                return term.cable
        return None

    def update(self, attrs):
        cable = self._existing_cable()
        if cable is not None:
            if "label" in attrs:
                cable.label = attrs["label"]
            if attrs.get("is_power"):
                cable.type = "power"
            cable.save()
        return super().update(attrs)

    def delete(self):
        cable = self._existing_cable()
        if cable is not None:
            cable.delete()
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

    def __init__(self, *, tag, import_components: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.tag = tag
        self.import_components = import_components

    def load(self) -> None:
        """Load only the objects this project owns (carry its ``RY:`` tag), so a re-sync diffs to
        no-change and unmatched-destination deletes can never touch operator-added objects."""
        tag = self.tag

        for mfr in NBManufacturer.objects.filter(tags=tag):
            self.add(self.manufacturer(name=mfr.name, slug=mfr.slug))
        for dt in NBDeviceType.objects.filter(tags=tag).select_related("manufacturer"):
            self.add(
                self.device_type(
                    manufacturer=dt.manufacturer.name,
                    model=dt.model,
                    slug=dt.slug,
                    u_height=int(dt.u_height or 0),
                    is_full_depth=dt.is_full_depth,
                    part_number=dt.part_number or "",
                )
            )
        for role in NBDeviceRole.objects.filter(tags=tag):
            self.add(self.device_role(name=role.name, slug=role.slug, color=role.color))
        for site in NBSite.objects.filter(tags=tag):
            self.add(self.site(name=site.name, slug=site.slug, status=site.status))
        for rk in NBRack.objects.filter(tags=tag).select_related("site"):
            self.add(
                self.rack(
                    site=rk.site.name,
                    name=rk.name,
                    status=rk.status,
                    width=int(rk.width),
                    u_height=int(rk.u_height),
                    desc_units=rk.desc_units,
                )
            )
        for dev in NBDevice.objects.filter(tags=tag).select_related(
            "device_type__manufacturer", "role", "site", "rack"
        ):
            self.add(
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
                )
            )

        # Components + cables (scoped to this project's tag).
        for rp in NBRearPort.objects.filter(tags=tag).select_related("device"):
            self.add(self.rear_port(device=rp.device.name, name=rp.name, type=rp.type, positions=rp.positions))
        for fp in NBFrontPort.objects.filter(tags=tag).select_related("device").prefetch_related(
            "mappings__rear_port"
        ):
            mapping = fp.mappings.first()
            self.add(
                self.front_port(
                    device=fp.device.name,
                    name=fp.name,
                    type=fp.type,
                    rear_port=mapping.rear_port.name if mapping else "",
                    rear_port_position=mapping.rear_port_position if mapping else 1,
                )
            )
        for iface in NBInterface.objects.filter(tags=tag).select_related("device"):
            self.add(self.interface(device=iface.device.name, name=iface.name, type=iface.type))
        for po in NBPowerOutlet.objects.filter(tags=tag).select_related("device"):
            self.add(self.power_outlet(device=po.device.name, name=po.name, type=po.type))
        for pp in NBPowerPort.objects.filter(tags=tag).select_related("device"):
            self.add(self.power_port(device=pp.device.name, name=pp.name, type=pp.type))
        for cable in NBCable.objects.filter(tags=tag):
            a_terms, b_terms = cable.a_terminations, cable.b_terminations
            if not a_terms or not b_terms:
                continue
            a, b = a_terms[0], b_terms[0]
            self.add(
                self.cable(
                    a_device=a.device.name,
                    a_type=_ct_string(a),
                    a_name=a.name,
                    b_device=b.device.name,
                    b_type=_ct_string(b),
                    b_name=b.name,
                    is_power=(cable.type == "power"),
                    label=cable.label or "",
                )
            )
