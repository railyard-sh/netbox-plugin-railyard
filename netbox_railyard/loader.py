"""Read NetBox sites from NetBox's own database into a railyard-sync ``Snapshot`` (the import's source).

``ORMReader`` subclasses railyard-sync's ``NetBoxReader``, which holds everything that turns NetBox's data
into a snapshot: which objects one load asks for, the units, enumerations and front/rear port mappings, and
the warnings. The ``railyard-sync import netbox`` command feeds that reader from the REST API; this module
feeds it from the ORM, answering the same requests (``fetch`` and ``fetch_all``) with the fields of the REST
representation the reader reads, so a site imported here and one imported by the command line are the same
snapshot (``tests/integration/test_import.py`` checks it against the real REST API).

Differences from the REST API, none of which the snapshot uses: nested objects carry only what the reader
reads (an id, a name), and an object-type custom field carries the related object's id only.

``NetBoxReader``'s private methods (``_site``, ``_rack``, ``_component``…) are its mappers: this subclass names
its own serialisers ``_api_*`` so it never overrides one.

Every query is ``.restrict()``-ed to what the job's user may view, as the REST API would be: what the user may
not see is not imported.

NetBox-only module: needs a live NetBox; exercised by ``tests/integration``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

from dcim.models import (
    Cable,
    ConsolePort,
    ConsolePortTemplate,
    Device,
    DeviceType,
    FrontPort,
    FrontPortTemplate,
    Interface,
    InterfaceTemplate,
    Location,
    PowerFeed,
    PowerOutlet,
    PowerOutletTemplate,
    PowerPanel,
    PowerPort,
    PowerPortTemplate,
    Rack,
    RackType,
    RearPort,
    RearPortTemplate,
    Region,
    Site,
)
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from extras.models import CustomField
from railyard_sync.dcim.errors import DCIMNotFoundError
from railyard_sync.dcim.netbox import NetBoxReader

# ---- REST-shaped values --------------------------------------------------------------------------


def _choice(value: Any) -> Any:
    """A choice field as the REST API shows it: its value, or ``None`` when blank."""
    return None if value in ("", None) else value


def _ref(pk: Any) -> dict | None:
    return None if pk is None else {"id": pk}


def _named(obj: Any) -> dict | None:
    return None if obj is None else {"id": obj.pk, "name": obj.name}


def _tags(obj) -> list[dict]:
    return [{"name": tag.name, "slug": tag.slug} for tag in obj.tags.all()]


def _number(value: Any) -> Any:
    """Decimals as the REST API renders them (JSON numbers); the reader converts them to float."""
    return None if value is None else float(value)


class _CustomFields:
    """``custom_fields`` as the REST API shows them: every custom field assigned to the model, ``None`` when unset."""

    def __init__(self) -> None:
        self._fields: dict[type, list[tuple[str, bool]]] = {}

    def __call__(self, obj) -> dict:
        model = type(obj)
        if model not in self._fields:
            self._fields[model] = [(cf.name, cf.type == "object") for cf in CustomField.objects.get_for_model(model)]
        data = obj.custom_field_data or {}
        out = {}
        for name, is_object in self._fields[model]:
            value = data.get(name)
            out[name] = _ref(value) if is_object and value is not None else value
        return out


# ---- the reader ----------------------------------------------------------------------------------


class ORMReader(NetBoxReader):
    """Answers railyard-sync's ``NetBoxReader`` from NetBox's ORM, as ``user`` would see it over the REST API.

    ``url`` is this NetBox's base URL: the snapshot records it as its source, and a refresh of an estate is
    refused when the estate was imported from another NetBox.
    """

    def __init__(self, user, url: str = "") -> None:
        self.user = user
        self.url = (url or "").rstrip("/")
        self.version = ""
        self._custom_fields = _CustomFields()
        self._endpoints: dict[str, tuple[Callable[[], Any], Callable[[Any], dict], dict[str, str]]] = {
            # endpoint: (queryset, serialiser, REST filter -> ORM lookup)
            "sites": (self._sites, self._api_site, {"slug": "slug__in", "name": "name__in"}),
            "locations": (self._locations, self._api_location, {"site_id": "site_id__in"}),
            "rack-types": (self._rack_types, self._api_rack_type, {}),
            "racks": (self._racks, self._api_rack, {"site_id": "site_id__in"}),
            "device-types": (self._device_types, self._api_device_type, {}),
            "devices": (self._devices, self._api_device, {"site_id": "site_id__in"}),
            "interface-templates": (
                lambda: InterfaceTemplate.objects,
                self._api_interface_template,
                {"device_type_id": "device_type_id__in"},
            ),
            "rear-port-templates": (
                lambda: RearPortTemplate.objects,
                self._api_rear_port_template,
                {"device_type_id": "device_type_id__in"},
            ),
            "front-port-templates": (
                lambda: FrontPortTemplate.objects.prefetch_related("mappings"),
                self._api_front_port_template,
                {"device_type_id": "device_type_id__in"},
            ),
            "power-port-templates": (
                lambda: PowerPortTemplate.objects,
                self._api_power_port_template,
                {"device_type_id": "device_type_id__in"},
            ),
            "power-outlet-templates": (
                lambda: PowerOutletTemplate.objects,
                self._api_power_outlet_template,
                {"device_type_id": "device_type_id__in"},
            ),
            "console-port-templates": (
                lambda: ConsolePortTemplate.objects,
                self._api_template_base,
                {"device_type_id": "device_type_id__in"},
            ),
            "interfaces": (lambda: self._components(Interface), self._api_interface, {"site_id": "_site_id__in"}),
            "rear-ports": (lambda: self._components(RearPort), self._api_rear_port, {"site_id": "_site_id__in"}),
            "front-ports": (
                lambda: self._components(FrontPort).prefetch_related("mappings"),
                self._api_front_port,
                {"site_id": "_site_id__in"},
            ),
            "power-ports": (lambda: self._components(PowerPort), self._api_power_port, {"site_id": "_site_id__in"}),
            "power-outlets": (
                lambda: self._components(PowerOutlet),
                self._api_power_outlet,
                {"site_id": "_site_id__in"},
            ),
            "console-ports": (
                lambda: self._components(ConsolePort),
                self._api_component_base,
                {"site_id": "_site_id__in"},
            ),
            "power-panels": (lambda: PowerPanel.objects, self._api_power_panel, {"site_id": "site_id__in"}),
            "power-feeds": (lambda: PowerFeed.objects, self._api_power_feed, {"site_id": "power_panel__site_id__in"}),
            "cables": (
                lambda: Cable.objects.prefetch_related("tags", "terminations__termination_type"),
                self._api_cable,
                {"site_id": "terminations___site_id__in"},
            ),
        }

    def __repr__(self) -> str:
        return f"ORMReader(url={self.url!r}, user={self.user!s})"

    # -- the two requests the reader makes ---------------------------------------------------------

    def fetch(self, path: str, params: list[tuple[str, Any]] | None = None) -> Any:
        if path == "/api/status/":
            return {"netbox-version": settings.RELEASE.version}
        if match := re.fullmatch(r"/api/dcim/regions/(\d+)/", path):
            region = Region.objects.restrict(self.user, "view").filter(pk=int(match.group(1))).first()
            if region is None:
                raise DCIMNotFoundError(
                    f"Not found in NetBox: region {match.group(1)} (or {self.user} may not view it)."
                )
            return {"id": region.pk, "name": region.name, "slug": region.slug, "parent": _ref(region.parent_id)}
        raise ValueError(f"the import does not read {path}")

    def fetch_all(self, path: str, params: list[tuple[str, Any]] | None = None) -> Iterator[dict]:
        match = re.fullmatch(r"/api/dcim/([a-z-]+)/", path)
        if match is None or match.group(1) not in self._endpoints:
            raise ValueError(f"the import does not read {path}")
        endpoint = match.group(1)
        queryset, serialise, lookups = self._endpoints[endpoint]
        filters: dict[str, list] = {}
        for key, value in params or []:
            if key == "exclude":  # exclude=config_context: the ORM never computes it
                continue
            lookup = "pk__in" if key == "id" else lookups.get(key)
            if lookup is None:
                raise ValueError(f"the import does not filter {endpoint} by {key}")
            filters.setdefault(lookup, []).append(value)
        qs = queryset().all().restrict(self.user, "view").filter(**filters)
        if endpoint == "cables" and filters:
            qs = qs.distinct()
        for obj in qs:
            yield serialise(obj)

    # -- querysets ---------------------------------------------------------------------------------

    @staticmethod
    def _sites():
        return Site.objects.prefetch_related("tags")

    @staticmethod
    def _locations():
        return Location.objects.prefetch_related("tags")

    @staticmethod
    def _rack_types():
        return RackType.objects.select_related("manufacturer")

    @staticmethod
    def _racks():
        return Rack.objects.select_related("role").prefetch_related("tags")

    @staticmethod
    def _device_types():
        return DeviceType.objects.select_related("manufacturer")

    @staticmethod
    def _devices():
        return Device.objects.select_related("role", "platform", "tenant", "parent_bay").prefetch_related("tags")

    @staticmethod
    def _components(model):
        return model.objects.select_related("module__module_bay")

    # -- serialisers (the REST fields NetBoxReader reads) ------------------------------------------

    def _api_site(self, s) -> dict:
        return {
            "id": s.pk,
            "name": s.name,
            "slug": s.slug,
            "status": _choice(s.status),
            "facility": s.facility,
            "region": _ref(s.region_id),
            "description": s.description,
            "comments": s.comments,
            "tags": _tags(s),
        }

    def _api_location(self, loc) -> dict:
        return {
            "id": loc.pk,
            "name": loc.name,
            "slug": loc.slug,
            "site": _ref(loc.site_id),
            "parent": _ref(loc.parent_id),
            "status": _choice(loc.status),
            "facility": getattr(loc, "facility", ""),
            "description": loc.description,
            "tags": _tags(loc),
        }

    def _api_rack_type(self, rt) -> dict:
        return {
            "id": rt.pk,
            "manufacturer": _named(rt.manufacturer),
            "model": rt.model,
            "slug": rt.slug,
            "u_height": rt.u_height,
            "width": _choice(rt.width),
            "outer_width": rt.outer_width,
            "outer_depth": rt.outer_depth,
            "outer_unit": _choice(rt.outer_unit),
            "form_factor": _choice(rt.form_factor),
        }

    def _api_rack(self, r) -> dict:
        return {
            "id": r.pk,
            "name": r.name,
            "site": _ref(r.site_id),
            "location": _ref(r.location_id),
            "status": _choice(r.status),
            "role": _named(r.role),
            "u_height": r.u_height,
            "starting_unit": r.starting_unit,
            "desc_units": r.desc_units,
            "width": _choice(r.width),
            "outer_width": r.outer_width,
            "outer_depth": r.outer_depth,
            "outer_unit": _choice(r.outer_unit),
            "rack_type": _ref(r.rack_type_id),
            "form_factor": _choice(r.form_factor),
            "max_weight": r.max_weight,
            "weight_unit": _choice(r.weight_unit),
            "serial": r.serial,
            "asset_tag": r.asset_tag,
            "facility_id": r.facility_id,
            "comments": r.comments,
            "tags": _tags(r),
            "custom_fields": self._custom_fields(r),
        }

    def _api_device_type(self, dt) -> dict:
        return {
            "id": dt.pk,
            "manufacturer": _named(dt.manufacturer),
            "model": dt.model,
            "slug": dt.slug,
            "u_height": _number(dt.u_height),
            "is_full_depth": dt.is_full_depth,
            "part_number": dt.part_number,
            "weight": _number(dt.weight),
            "weight_unit": _choice(dt.weight_unit),
            "airflow": _choice(dt.airflow),
            "subdevice_role": _choice(dt.subdevice_role),
        }

    @staticmethod
    def _api_template_base(t) -> dict:
        return {"id": t.pk, "device_type": _ref(t.device_type_id), "name": t.name, "type": _choice(t.type)}

    def _api_interface_template(self, t) -> dict:
        return {**self._api_template_base(t), "mgmt_only": t.mgmt_only}

    def _api_rear_port_template(self, t) -> dict:
        return {**self._api_template_base(t), "positions": t.positions}

    def _api_front_port_template(self, t) -> dict:
        return {**self._api_template_base(t), "positions": t.positions, "rear_ports": _mappings(t)}

    def _api_power_port_template(self, t) -> dict:
        return {**self._api_template_base(t), "maximum_draw": t.maximum_draw, "allocated_draw": t.allocated_draw}

    def _api_power_outlet_template(self, t) -> dict:
        return {**self._api_template_base(t), "power_port": _ref(t.power_port_id), "feed_leg": _choice(t.feed_leg)}

    def _api_device(self, d) -> dict:
        try:
            parent = d.parent_bay.device_id
        except ObjectDoesNotExist:
            parent = None
        return {
            "id": d.pk,
            "name": d.name,
            "device_type": _ref(d.device_type_id),
            "site": _ref(d.site_id),
            "role": _named(d.role),
            "location": _ref(d.location_id),
            "rack": _ref(d.rack_id),
            "position": _number(d.position),
            "face": _choice(d.face),
            "status": _choice(d.status),
            "serial": d.serial,
            "asset_tag": d.asset_tag,
            "airflow": _choice(d.airflow),
            "parent_device": _ref(parent),
            "platform": _named(d.platform),
            "tenant": _named(d.tenant),
            "comments": d.comments,
            "tags": _tags(d),
            "custom_fields": self._custom_fields(d),
        }

    @staticmethod
    def _api_component_base(c) -> dict:
        module = None
        if c.module_id is not None:
            module = {"id": c.module_id, "display": str(c.module), "module_bay": _named(c.module.module_bay)}
        return {
            "id": c.pk,
            "device": _ref(c.device_id),
            "name": c.name,
            "type": _choice(c.type),
            "label": c.label,
            "description": c.description,
            "module": module,
        }

    def _api_interface(self, c) -> dict:
        return {**self._api_component_base(c), "mgmt_only": c.mgmt_only, "enabled": c.enabled}

    def _api_rear_port(self, c) -> dict:
        return {**self._api_component_base(c), "positions": c.positions}

    def _api_front_port(self, c) -> dict:
        return {**self._api_component_base(c), "positions": c.positions, "rear_ports": _mappings(c)}

    def _api_power_port(self, c) -> dict:
        return {**self._api_component_base(c), "maximum_draw": c.maximum_draw, "allocated_draw": c.allocated_draw}

    def _api_power_outlet(self, c) -> dict:
        return {**self._api_component_base(c), "power_port": _ref(c.power_port_id), "feed_leg": _choice(c.feed_leg)}

    @staticmethod
    def _api_power_panel(p) -> dict:
        return {"id": p.pk, "name": p.name, "site": _ref(p.site_id), "location": _ref(p.location_id)}

    @staticmethod
    def _api_power_feed(f) -> dict:
        return {
            "id": f.pk,
            "name": f.name,
            "power_panel": _ref(f.power_panel_id),
            "rack": _ref(f.rack_id),
            "status": _choice(f.status),
            "type": _choice(f.type),
            "supply": _choice(f.supply),
            "phase": _choice(f.phase),
            "voltage": f.voltage,
            "amperage": f.amperage,
            "max_utilization": f.max_utilization,
        }

    @staticmethod
    def _api_cable(c) -> dict:
        ends: dict[str, list[dict]] = {"A": [], "B": []}
        for term in c.terminations.all():  # NetBox's own order for a_terminations / b_terminations
            ct = term.termination_type
            ends.setdefault(term.cable_end, []).append(
                {
                    "object_type": f"{ct.app_label}.{ct.model}",
                    "object_id": term.termination_id,
                    "object": {"id": term.termination_id, "device": _ref(term._device_id)},
                }
            )
        return {
            "id": c.pk,
            "a_terminations": ends["A"],
            "b_terminations": ends["B"],
            "type": _choice(c.type),
            "status": _choice(c.status),
            "label": c.label,
            "color": c.color,
            "length": _number(c.length),
            "length_unit": _choice(c.length_unit),
            "description": c.description,
            "tags": _tags(c),
        }


def _mappings(port) -> list[dict]:
    """A front port's (or template's) rear port mappings in the 4.5+ REST shape."""
    return [
        {"position": m.front_port_position, "rear_port": m.rear_port_id, "rear_port_position": m.rear_port_position}
        for m in port.mappings.all()
    ]


def load_sites(user, sites: list[str] | None, *, url: str = ""):
    """Read ``sites`` (ids, slugs or names; ``None`` for every site the user may view) into a ``Snapshot``."""
    return ORMReader(user, url).load(sites)
