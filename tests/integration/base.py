"""Shared helpers for the NetBox integration suite.

These tests need a real NetBox (they import ``dcim``/``extras`` and use its database), so they run under
NetBox's own test runner rather than pytest — see ``dev/run-integration-tests.sh``. Railyard itself is
never contacted: the client is replaced by a stub that returns a project document, and the device-type
library is offline.
"""

from __future__ import annotations

import copy
import logging
import uuid
from unittest import mock

import structlog
from core.exceptions import JobFailed
from core.models import Job
from django.conf import settings
from django.test import override_settings
from users.models import User
from utilities.testing import TestCase

from netbox_railyard.jobs import RailyardSyncJob
from netbox_railyard.policy import SYNC_MODELS
from netbox_railyard.railyard.devicetype_library import DeviceTypeLibrary

# DiffSync logs every step through structlog at debug level; keep the test output readable.
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

VIEW = [f"dcim.view_{m}" for m in SYNC_MODELS] + ["extras.view_tag"]
APPLY = VIEW + [f"dcim.{a}_{m}" for a in ("add", "change") for m in SYNC_MODELS]
DELETE = [f"dcim.delete_{m}" for m in SYNC_MODELS]
BOOTSTRAP = ["extras.add_tag", "extras.change_tag", "extras.add_customfield", "extras.change_customfield"]


def project(pid: str = "prj_it", name: str = "Integration") -> dict:
    """One rack in DC1 holding a switch (2 SFP+ ports) and a port-less server, cabled together, plus a
    0U PDU feeding the server — the same shape as the core suite's ``cabled_project`` fixture."""
    return {
        "schemaVersion": "1",
        "id": pid,
        "name": name,
        "dataCentres": [{"id": "dc1", "name": "DC1", "status": "Active"}],
        "rows": [{"id": "row1", "name": "A", "dcId": "dc1", "rackIds": ["r1"]}],
        "racks": [
            {
                "id": "r1",
                "name": "R1",
                "uHeight": 42,
                "rowId": "row1",
                "dcId": "dc1",
                "status": "Active",
                "placements": [
                    {
                        "id": "p_sw",
                        "startU": 10,
                        "heightU": 1,
                        "face": "front",
                        "label": "SW-1",
                        "deviceTypeRef": "sw",
                        "role": "leaf",
                    },
                    {
                        "id": "p_srv",
                        "startU": 1,
                        "heightU": 2,
                        "face": "front",
                        "label": "SRV-1",
                        "deviceTypeRef": "srv",
                        "role": "compute",
                    },
                    {
                        "id": "p_pdu",
                        "startU": 0,
                        "heightU": 0,
                        "face": "front",
                        "mount": "zeroU",
                        "side": "left",
                        "label": "PDU-1",
                        "deviceTypeRef": "pdu",
                        "role": "power",
                    },
                ],
            }
        ],
        "catalogue": [
            {
                "key": "sw",
                "manufacturer": "Acme",
                "model": "SW1",
                "uHeight": 1,
                "fullDepth": True,
                "ports": {
                    "front": 2,
                    "passThrough": False,
                    "media": "SFP+",
                    "frontPorts": [{"name": "Eth1", "type": "SFP+"}, {"name": "Eth2", "type": "SFP+"}],
                },
            },
            {"key": "srv", "manufacturer": "Acme", "model": "SRV1", "uHeight": 2, "fullDepth": True},
            {
                "key": "pdu",
                "manufacturer": "Acme",
                "model": "PDU1",
                "uHeight": 0,
                "fullDepth": False,
                "outlets": {"count": 2, "type": "C13", "feeds": 1},
            },
        ],
        "cables": [
            {
                "id": "c1",
                "a": {"rackId": "r1", "placementId": "p_sw", "side": "front", "portIndex": 1},
                "b": {"rackId": "r1", "placementId": "p_srv"},
                "media": "SFP+",
                "label": "L1",
            }
        ],
        "powerLinks": [
            {
                "id": "pw1",
                "device": {"rackId": "r1", "placementId": "p_srv"},
                "pdu": {"rackId": "r1", "placementId": "p_pdu", "outlet": 1},
            }
        ],
    }


def plugin_settings(**overrides) -> dict:
    """``PLUGINS_CONFIG`` with the plugin's settings replaced by its defaults plus ``overrides``."""
    cfg = copy.deepcopy(settings.PLUGINS_CONFIG)
    base = {
        "railyard_url": "https://railyard.example",
        "railyard_token": "ry_integration",
        "railyard_token_file": None,
        "railyard_org": None,
        "railyard_allowed_orgs": [],
        "railyard_allowed_projects": [],
        "devicetype_library_ref": None,
        "devicetype_cache_dir": None,
        "import_components": False,
    }
    base.update(overrides)
    cfg["netbox_railyard"] = base
    return cfg


class SyncTestCase(TestCase):
    """A NetBox TestCase with ``self.sync(...)`` to run the job synchronously as ``self.user``."""

    def superuser(self) -> User:
        return User.objects.create_user(username=f"admin-{uuid.uuid4().hex[:6]}", is_superuser=True)

    def sync(self, doc=None, *, user=None, settings_overrides=None, project_ref=None, org=None, client=None, **options):
        """Run ``RailyardSyncJob`` and return ``(result, job)``; ``result`` is the ``JobFailed`` on failure."""
        doc = project() if doc is None else doc
        job = Job.objects.create(name="Railyard sync (test)", user=user or self.user, job_id=uuid.uuid4())
        stub = client or mock.MagicMock()
        if client is None:
            stub.get_project.return_value = copy.deepcopy(doc)
        offline = lambda **kw: DeviceTypeLibrary(fetcher=lambda url: None)  # noqa: E731
        with (
            override_settings(PLUGINS_CONFIG=plugin_settings(**(settings_overrides or {}))),
            mock.patch("netbox_railyard.jobs.RailyardClient", return_value=stub) as client_cls,
            mock.patch("netbox_railyard.jobs.DeviceTypeLibrary", offline),
        ):
            try:
                result = RailyardSyncJob(job).run(project=project_ref or doc["id"], org=org, **options)
            except JobFailed as exc:
                result = exc
        self.client_cls = client_cls
        return result, job

    @staticmethod
    def log(job) -> str:
        return "\n".join(entry["message"] for entry in job.log_entries)
