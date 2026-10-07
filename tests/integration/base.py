"""Shared helpers for the NetBox integration suite.

These tests need a real NetBox (they import ``dcim``/``extras`` and use its database), so they run under
NetBox's own test runner rather than pytest — see ``dev/run-integration-tests.sh``. Railyard itself is never
contacted: the sync's client is a stub that serves a NetBox sync document (``tests/fixtures/sync/``, written by
Railyard's own exporter), the import's a fake Railyard API, and the device-type library is offline.
"""

from __future__ import annotations

import copy
import json
import logging
import pathlib
import uuid
from unittest import mock

import structlog
from core.exceptions import JobFailed
from core.models import Job
from django.conf import settings
from django.test import override_settings
from users.models import User
from utilities.testing import TestCase

from netbox_railyard.jobs import RailyardSyncJob, netbox_release
from netbox_railyard.policy import SYNC_MODELS

# DiffSync logs every step through structlog at debug level; keep the test output readable.
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "sync"
RAILYARD_URL = "https://railyard.example"

VIEW = [f"dcim.view_{m}" for m in SYNC_MODELS] + ["extras.view_tag"]
APPLY = VIEW + [f"dcim.{a}_{m}" for a in ("add", "change") for m in SYNC_MODELS]
DELETE = [f"dcim.delete_{m}" for m in SYNC_MODELS]
BOOTSTRAP = ["extras.add_tag", "extras.change_tag", "extras.add_customfield", "extras.change_customfield"]

# ---- sync documents --------------------------------------------------------------------------------------


def document(name: str = "integration") -> dict:
    """A NetBox sync document, as Railyard serves it for this NetBox's release.

    ``integration``: project ``prj_it`` "Integration": site DC1 with location A holding rack R1, which holds a
    switch SW-1 (two SFP+ interfaces, Eth1 cabled to SRV-1's iface1, label L1), a server SRV-1 (serial SN-0001)
    and a 0U PDU-1 whose OUT1 feeds SRV-1's PSU1. ``patch`` adds a patch panel PP-1 (two front ports mapped to
    two rear ports) cabled to SW-1's Eth2."""
    doc = json.loads((FIXTURES / f"netbox-sync-{name}.json").read_text())
    doc["netboxVersion"] = netbox_release()
    return doc


def rows(doc: dict, kind: str) -> list[dict]:
    return doc["objects"][kind]


def row(doc: dict, kind: str, **match) -> dict:
    """The one row of ``kind`` whose columns equal ``match``."""
    (found,) = [r for r in rows(doc, kind) if all(r.get(k) == v for k, v in match.items())]
    return found


def creates(doc: dict) -> int:
    """How many objects a first sync of ``doc`` into an empty NetBox creates (every row but design tags)."""
    return sum(len(v) for k, v in doc["objects"].items() if k != "tags")


def drop_devices(doc: dict, *names: str) -> dict:
    """``doc`` without these devices, their components and their cables (as Railyard would export it)."""
    gone = set(names)
    objs = doc["objects"]
    objs["devices"] = [r for r in objs["devices"] if r["name"] not in gone]
    for kind in ("interfaces", "rear-ports", "front-ports", "power-outlets", "power-ports"):
        objs[kind] = [r for r in objs[kind] if r["device"] not in gone]
    objs["cables"] = [r for r in objs["cables"] if r["side_a_device"] not in gone and r["side_b_device"] not in gone]
    # Railyard exports only the device types, roles and manufacturers its devices use.
    types = {(r["manufacturer"], r["device_type"]) for r in objs["devices"]}
    objs["device-types"] = [r for r in objs["device-types"] if (r["manufacturer"], r["model"]) in types]
    objs["device-roles"] = [r for r in objs["device-roles"] if r["name"] in {d["role"] for d in objs["devices"]}]
    makers = {r["manufacturer"] for r in objs["device-types"]}
    objs["manufacturers"] = [r for r in objs["manufacturers"] if r["name"] in makers]
    return doc


def drop_all_devices(doc: dict) -> dict:
    return drop_devices(doc, *[r["name"] for r in rows(doc, "devices")])


def drop_racks(doc: dict) -> dict:
    """``doc`` with no racks (and so nothing in them); sites, locations, types and roles stay."""
    drop_all_devices(doc)
    doc["objects"]["racks"] = []
    return doc


def rename_device(doc: dict, old: str, new: str) -> dict:
    """``doc`` with a device renamed in Railyard: the same placement id under another name."""
    for kind, cols in (
        ("devices", ("name",)),
        ("interfaces", ("device",)),
        ("rear-ports", ("device",)),
        ("front-ports", ("device",)),
        ("power-outlets", ("device",)),
        ("power-ports", ("device",)),
        ("cables", ("side_a_device", "side_b_device")),
    ):
        for r in rows(doc, kind):
            for col in cols:
                if r.get(col) == old:
                    r[col] = new
    return doc


def another_project(doc: dict, pid: str, name: str, *, site: str, suffix: str) -> dict:
    """``doc`` as another Railyard project: its own id and name, its site renamed and every device suffixed."""
    doc["project"].update(id=pid, name=name)
    old_site = row(doc, "sites")["name"]
    for r in rows(doc, "sites"):
        r.update(name=site, slug=site.lower())
    for kind in ("locations", "racks", "devices"):
        for r in rows(doc, kind):
            if r.get("site") == old_site:
                r["site"] = site
            if kind == "locations":
                r["slug"] = f"{r['slug']}-{suffix.lower().strip('-')}"
    for r in list(rows(doc, "devices")):
        rename_device(doc, r["name"], r["name"] + suffix)
    return doc


def plugin_settings(**overrides) -> dict:
    """``PLUGINS_CONFIG`` with the plugin's settings replaced by its defaults plus ``overrides``."""
    cfg = copy.deepcopy(settings.PLUGINS_CONFIG)
    base = {
        "railyard_url": RAILYARD_URL,
        "railyard_token": "ry_integration",
        "railyard_token_file": None,
        "railyard_org": None,
        "railyard_allowed_orgs": [],
        "railyard_allowed_projects": [],
        "devicetype_library_ref": None,
        "devicetype_cache_dir": None,
        "import_components": False,
        "netbox_url": None,
    }
    base.update(overrides)
    cfg["netbox_railyard"] = base
    return cfg


class SyncTestCase(TestCase):
    """A NetBox TestCase with ``self.sync(...)`` to run the sync job synchronously as ``self.user``."""

    def superuser(self) -> User:
        return User.objects.create_user(username=f"admin-{uuid.uuid4().hex[:6]}", is_superuser=True)

    def sync(self, doc=None, *, user=None, settings_overrides=None, project_ref=None, org=None, client=None, **options):
        """Run ``RailyardSyncJob`` and return ``(result, job)``; ``result`` is the ``JobFailed`` on failure."""
        doc = document() if doc is None else doc
        job = Job.objects.create(name="Railyard sync (test)", user=user or self.user, job_id=uuid.uuid4())
        stub = client or mock.MagicMock()
        if client is None:
            stub.netbox_sync_document.return_value = copy.deepcopy(doc)
        with (
            override_settings(PLUGINS_CONFIG=plugin_settings(**(settings_overrides or {}))),
            mock.patch("netbox_railyard.jobs.RailyardClient", return_value=stub) as client_cls,
        ):
            try:
                result = RailyardSyncJob(job).run(project=project_ref or doc["project"]["id"], org=org, **options)
            except JobFailed as exc:
                result = exc
        self.client_cls = client_cls
        self.railyard = stub
        return result, job

    @staticmethod
    def log(job) -> str:
        return "\n".join(entry["message"] for entry in job.log_entries)
