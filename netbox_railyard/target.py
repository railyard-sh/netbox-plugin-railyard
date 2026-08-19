"""The NetBox DiffSync *target* adapter.

Each ``NetBox*`` model subclasses the canonical model in ``railyard.models`` (so the diff surface is
identical to the source) and implements ``create``/``update``/``delete`` against the NetBox ORM
(``dcim.models``). ``NetBoxAdapter.load()`` reads the *existing* NetBox objects within the project's
sites so DiffSync can tell create from update; components and cables are create-idempotent rather than
loaded, so a re-sync never duplicates them.

NetBox-only module. Everything here needs a live NetBox — it is written correct-by-construction and
must be exercised by integration tests inside a real NetBox instance.
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


# ---- target models ---------------------------------------------------------


class NetBoxManufacturer(models.Manufacturer):
    @classmethod
    def create(cls, adapter, ids, attrs):
        devicetypes.ensure_manufacturer(ids["name"], attrs.get("slug"))
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
        devicetypes.ensure_device_type(spec, import_components=adapter.import_components)
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
        obj = NBDeviceRole.objects.filter(name=ids["name"]).first()
        if obj is None:
            NBDeviceRole.objects.create(
                name=ids["name"], slug=attrs.get("slug") or slugify(ids["name"]), color=attrs.get("color", "9e9e9e")
            )
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
        obj = NBSite.objects.filter(name=ids["name"]).first()
        if obj is None:
            NBSite.objects.create(
                name=ids["name"], slug=attrs.get("slug") or slugify(ids["name"]), status=attrs.get("status", "active")
            )
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
        if site is not None and not NBRack.objects.filter(site=site, name=ids["name"]).exists():
            NBRack.objects.create(
                site=site,
                name=ids["name"],
                status=attrs.get("status", "active"),
                width=attrs.get("width", 19),
                u_height=attrs.get("u_height", 42),
                desc_units=attrs.get("desc_units", False),
            )
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
            dt = NBDeviceType.objects.filter(
                manufacturer__name=attrs["manufacturer"], model=attrs["device_type"]
            ).first()
            role = NBDeviceRole.objects.filter(name=attrs["role"]).first()
            site = NBSite.objects.filter(name=attrs["site"]).first()
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
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        obj = NBDevice.objects.filter(name=self.name).first()
        if obj:
            if "role" in attrs:
                obj.role = NBDeviceRole.objects.filter(name=attrs["role"]).first()
            if "rack" in attrs:
                site = obj.site
                obj.rack = NBRack.objects.filter(site=site, name=attrs["rack"]).first() if attrs["rack"] else None
            for field in ("position", "face", "status"):
                if field in attrs:
                    setattr(obj, field, attrs[field] if attrs[field] is not None else None)
            if "railyard_id" in attrs:
                _set_railyard_id(obj, attrs["railyard_id"])
            obj.save()
        return super().update(attrs)

    def delete(self):
        NBDevice.objects.filter(name=self.name).delete()
        super().delete()
        return self


class _ComponentModel:
    """Shared get-or-create/update/delete for the simple (device, name, type) components."""

    _nb_model = None  # set by subclasses

    @classmethod
    def _create_nb(cls, ids, attrs):
        device = NBDevice.objects.filter(name=ids["device"]).first()
        if device is None:
            return
        if not cls._nb_model.objects.filter(device=device, name=ids["name"]).exists():
            cls._nb_model.objects.create(device=device, name=ids["name"], type=attrs.get("type", ""))

    def _update_nb(self, attrs):
        obj = self._nb_model.objects.filter(device__name=self.device, name=self.name).first()
        if obj and "type" in attrs:
            obj.type = attrs["type"]
            obj.save()

    def _delete_nb(self):
        self._nb_model.objects.filter(device__name=self.device, name=self.name).delete()


class NetBoxInterface(_ComponentModel, models.Interface):
    _nb_model = NBInterface

    @classmethod
    def create(cls, adapter, ids, attrs):
        cls._create_nb(ids, attrs)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        self._update_nb(attrs)
        return super().update(attrs)

    def delete(self):
        self._delete_nb()
        super().delete()
        return self


class NetBoxRearPort(models.RearPort):
    @classmethod
    def create(cls, adapter, ids, attrs):
        device = NBDevice.objects.filter(name=ids["device"]).first()
        if device is not None and not NBRearPort.objects.filter(device=device, name=ids["name"]).exists():
            NBRearPort.objects.create(
                device=device, name=ids["name"], type=attrs.get("type", "8p8c"), positions=attrs.get("positions", 1)
            )
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
        if device is None or NBFrontPort.objects.filter(device=device, name=ids["name"]).exists():
            return super().create(adapter, ids=ids, attrs=attrs)
        rear = NBRearPort.objects.filter(device=device, name=attrs.get("rear_port")).first()
        if rear is not None:
            NBFrontPort.objects.create(
                device=device,
                name=ids["name"],
                type=attrs.get("type", "8p8c"),
                rear_port=rear,
                rear_port_position=attrs.get("rear_port_position", 1),
            )
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


class NetBoxPowerOutlet(_ComponentModel, models.PowerOutlet):
    _nb_model = NBPowerOutlet

    @classmethod
    def create(cls, adapter, ids, attrs):
        cls._create_nb(ids, attrs)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        self._update_nb(attrs)
        return super().update(attrs)

    def delete(self):
        self._delete_nb()
        super().delete()
        return self


class NetBoxPowerPort(_ComponentModel, models.PowerPort):
    _nb_model = NBPowerPort

    @classmethod
    def create(cls, adapter, ids, attrs):
        cls._create_nb(ids, attrs)
        return super().create(adapter, ids=ids, attrs=attrs)

    def update(self, attrs):
        self._update_nb(attrs)
        return super().update(attrs)

    def delete(self):
        self._delete_nb()
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
        # a cable end on a component the plan expected but NetBox lacks: give it something to land on
        obj = NBInterface.objects.create(device=device, name=name, type="other")
    return obj


class NetBoxCable(models.Cable):
    @classmethod
    def create(cls, adapter, ids, attrs):
        a = _resolve_termination(ids["a_device"], ids["a_type"], ids["a_name"])
        b = _resolve_termination(ids["b_device"], ids["b_type"], ids["b_name"])
        if a is None or b is None or a.cable_id or b.cable_id:
            # missing termination, or one end is already cabled — leave NetBox as-is
            return super().create(adapter, ids=ids, attrs=attrs)
        cable = NBCable(status="connected", label=attrs.get("label", ""))
        if attrs.get("is_power"):
            cable.type = "power"
        cable.a_terminations = [a]
        cable.b_terminations = [b]
        cable.save()
        return super().create(adapter, ids=ids, attrs=attrs)

    def delete(self):
        a = _resolve_termination(self.a_device, self.a_type, self.a_name)
        if a is not None and a.cable_id:
            a.cable.delete()
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

    def __init__(self, *, site_names: set[str], import_components: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.site_names = set(site_names)
        self.import_components = import_components

    def load(self) -> None:
        # Global prerequisites (bounded, and needed so a device create can resolve them).
        for mfr in NBManufacturer.objects.all():
            self.add(self.manufacturer(name=mfr.name, slug=mfr.slug))
        for dt in NBDeviceType.objects.select_related("manufacturer").all():
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
        for role in NBDeviceRole.objects.all():
            self.add(self.device_role(name=role.name, slug=role.slug, color=role.color))

        # Project-scoped objects: only the sites this sync touches.
        for site in NBSite.objects.filter(name__in=self.site_names):
            self.add(self.site(name=site.name, slug=site.slug, status=site.status))
        for rk in NBRack.objects.filter(site__name__in=self.site_names).select_related("site"):
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
        for dev in NBDevice.objects.filter(site__name__in=self.site_names).select_related(
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
        # Components + cables are create-idempotent (see module docstring) — not loaded in this version.
