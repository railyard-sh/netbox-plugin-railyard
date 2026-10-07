"""NetBox → Railyard: the ORM reader reads what the REST loader reads, and the import job runs the command
line's import flow (dry run, create, refresh, conflicts, plan limits) against a fake Railyard."""

from __future__ import annotations

import uuid
from unittest import mock
from urllib.parse import urlsplit

from core.exceptions import JobFailed
from core.models import Job, ObjectType
from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    FrontPort,
    FrontPortTemplate,
    Interface,
    InterfaceTemplate,
    Location,
    Manufacturer,
    PortTemplateMapping,
    PowerFeed,
    PowerOutlet,
    PowerOutletTemplate,
    PowerPanel,
    PowerPort,
    PowerPortTemplate,
    Rack,
    RackRole,
    RackType,
    RearPort,
    RearPortTemplate,
    Region,
    Site,
)
from django.test import override_settings
from django.urls import reverse
from extras.models import CustomField, Tag
from railyard_sync.dcim.netbox import load_netbox_snapshot
from railyard_sync.importer.run import recorded_source
from users.models import ObjectPermission, Token

from netbox_railyard.import_job import RailyardImportJob
from netbox_railyard.loader import ORMReader
from netbox_railyard.policy import import_permissions

from .base import SyncTestCase, plugin_settings
from .fake_railyard import FakeRailyard, FakeResponse

NETBOX = "https://netbox.example.com"
URL = "plugins:netbox_railyard:import"


def build_site() -> dict:
    """LDN1 (in UK, in Europe): Hall 1 > Cage A holding two racks; a switch, a patch panel, a server and a 0U
    PDU in A01, cabled (data through the patch panel, power to the PDU); a power feed to A01; and a cable from
    the switch to a device in another site, MAN1."""
    europe = Region.objects.create(name="Europe", slug="europe")
    uk = Region.objects.create(name="UK", slug="uk", parent=europe)
    ldn1 = Site.objects.create(name="LDN1", slug="ldn1", region=uk, facility="Equinix LD8", status="active")
    man1 = Site.objects.create(name="MAN1", slug="man1")
    hall = Location.objects.create(site=ldn1, name="Hall 1", slug="hall-1")
    cage = Location.objects.create(site=ldn1, parent=hall, name="Cage A", slug="cage-a", status="planned")

    acme = Manufacturer.objects.create(name="Acme", slug="acme")
    rack_type = RackType.objects.create(
        manufacturer=acme,
        model="Cab 42",
        slug="cab-42",
        form_factor="4-post-cabinet",
        width=19,
        u_height=42,
        outer_width=600,
        outer_depth=1200,
        outer_unit="mm",
    )
    role = RackRole.objects.create(name="Compute", slug="compute")
    build = Tag.objects.create(name="Build 7", slug="build-7")
    a01 = Rack.objects.create(
        site=ldn1, location=cage, name="A01", rack_type=rack_type, role=role, max_weight=1000, weight_unit="kg"
    )
    a01.tags.add(build)
    Rack.objects.create(
        site=ldn1, location=cage, name="A02", u_height=48, width=23, outer_width=24, outer_unit="in", desc_units=True
    )

    switch = DeviceType.objects.create(manufacturer=acme, model="SW48", slug="sw48", u_height=1)
    InterfaceTemplate.objects.create(device_type=switch, name="Eth1", type="10gbase-x-sfpp")
    InterfaceTemplate.objects.create(device_type=switch, name="Eth2", type="10gbase-x-sfpp")
    InterfaceTemplate.objects.create(device_type=switch, name="mgmt0", type="1000base-t", mgmt_only=True)
    panel = DeviceType.objects.create(manufacturer=acme, model="PP24", slug="pp24", u_height=1, is_full_depth=False)
    rear = RearPortTemplate.objects.create(device_type=panel, name="R1", type="lc", positions=1)
    front = FrontPortTemplate.objects.create(device_type=panel, name="F1", type="lc", positions=1)
    PortTemplateMapping.objects.create(
        device_type=panel, front_port=front, front_port_position=1, rear_port=rear, rear_port_position=1
    )
    server = DeviceType.objects.create(manufacturer=acme, model="SRV2", slug="srv2", u_height=2)
    InterfaceTemplate.objects.create(device_type=server, name="eth0", type="10gbase-x-sfpp")
    psu = PowerPortTemplate.objects.create(device_type=server, name="PSU1", type="iec-60320-c14", maximum_draw=450)
    pdu = DeviceType.objects.create(manufacturer=acme, model="PDU8", slug="pdu8", u_height=0, is_full_depth=False)
    for n in (1, 2):
        PowerOutletTemplate.objects.create(device_type=pdu, name=f"OUT{n}", type="iec-60320-c13", feed_leg="A")
    del psu

    roles = {name: DeviceRole.objects.create(name=name, slug=name.lower()) for name in ("Leaf", "Patch", "Server")}
    sw1 = Device.objects.create(
        name="sw1",
        site=ldn1,
        rack=a01,
        position=40,
        face="front",
        device_type=switch,
        role=roles["Leaf"],
        serial="SW-0001",
    )
    sw1.tags.add(build)
    pp1 = Device.objects.create(
        name="pp1", site=ldn1, rack=a01, position=42, face="front", device_type=panel, role=roles["Patch"]
    )
    srv1 = Device.objects.create(
        name="srv1",
        site=ldn1,
        rack=a01,
        position=1,
        face="rear",
        device_type=server,
        role=roles["Server"],
        comments="Database host",
    )
    pdu1 = Device.objects.create(name="pdu1", site=ldn1, rack=a01, device_type=pdu, role=roles["Server"])
    Device.objects.create(name="spare", site=ldn1, device_type=server, role=roles["Server"])  # not racked
    far = Device.objects.create(name="far1", site=man1, device_type=switch, role=roles["Leaf"])

    def cable(a, b, **kw):
        c = Cable(a_terminations=[a], b_terminations=[b], **kw)
        c.save()
        return c

    cable(Interface.objects.get(device=sw1, name="Eth1"), FrontPort.objects.get(device=pp1), label="L1", type="mmf")
    cable(
        RearPort.objects.get(device=pp1),
        Interface.objects.get(device=srv1, name="eth0"),
        label="L2",
        color="ff0000",
        length=3,
        length_unit="m",
    )
    cable(PowerPort.objects.get(device=srv1), PowerOutlet.objects.get(device=pdu1, name="OUT1"), type="power")
    cable(Interface.objects.get(device=sw1, name="Eth2"), Interface.objects.get(device=far, name="Eth1"), label="WAN")

    panel_ = PowerPanel.objects.create(site=ldn1, location=hall, name="PP-A")
    PowerFeed.objects.create(power_panel=panel_, rack=a01, name="Feed A", voltage=230, amperage=32)
    return {"ldn1": ldn1, "man1": man1, "sw1": sw1, "srv1": srv1}


class TestClientSession:
    """``requests.Session.request`` over Django's test client, so railyard-sync's REST loader reads this test's
    NetBox through its real REST API."""

    def __init__(self, client):
        self.client = client

    def request(self, method, url, headers=None, params=None, timeout=None, verify=None, **kwargs):
        assert method == "GET"
        auth = (headers or {}).get("Authorization", "")
        resp = self.client.get(urlsplit(url).path, data=params or [], HTTP_AUTHORIZATION=auth)
        return _Response(resp)


class _Response:
    def __init__(self, resp):
        self.status_code = resp.status_code
        self.text = resp.content.decode("utf-8", "replace")
        self.headers = dict(resp.headers)
        self._resp = resp

    def json(self):
        return self._resp.json()


class ReaderTests(SyncTestCase):
    """The ORM reader and railyard-sync's REST loader read the same NetBox into the same snapshot."""

    def setUp(self):
        super().setUp()
        self.user = self.superuser()
        CustomField.objects.create(name="asset_owner", type="text").object_types.set(
            [ObjectType.objects.get_for_model(Device)]
        )
        self.objects = build_site()
        self.objects["srv1"].custom_field_data["asset_owner"] = "DB team"
        self.objects["srv1"].save()

    def rest(self, sites):
        self.client.logout()  # the REST API authenticates the token's user, not the test client's session
        token = Token.objects.create(user=self.user)
        secret = f"nbt_{token.key}.{token.token}"
        return load_netbox_snapshot("http://testserver", secret, sites, session=TestClientSession(self.client))

    def test_one_site_reads_the_same_as_over_rest(self):
        orm = ORMReader(self.user, "http://testserver").load(["ldn1"])
        self.assertEqual(orm.to_dict(), self.rest(["ldn1"]).to_dict())
        # and it is the whole site, not two empty snapshots
        self.assertEqual([r.slug for r in orm.regions], ["europe", "uk"])
        self.assertEqual(len(orm.racks), 2)
        self.assertEqual({d.name for d in orm.devices}, {"sw1", "pp1", "srv1", "pdu1", "spare"})
        self.assertEqual(len(orm.cables), 4)
        self.assertTrue(any("outside the loaded sites" in w for w in orm.warnings))
        a02 = next(r for r in orm.racks if r.name == "A02")
        self.assertEqual((a02.width_in, a02.outer_width_mm, a02.desc_units), (23, 609.6, True))
        front = next(c for c in orm.components if c.kind == "front-port")
        self.assertEqual(front.rear_port_id, str(RearPort.objects.get(device__name="pp1").pk))
        srv = next(d for d in orm.devices if d.name == "srv1")
        self.assertEqual(
            (srv.face, srv.comments, srv.custom_fields["asset_owner"]), ("rear", "Database host", "DB team")
        )

    def test_sites_by_id_and_every_site_read_the_same_as_over_rest(self):
        ids = [str(self.objects["ldn1"].pk), str(self.objects["man1"].pk)]
        self.assertEqual(ORMReader(self.user, "http://testserver").load(ids).to_dict(), self.rest(ids).to_dict())
        self.assertEqual(ORMReader(self.user, "http://testserver").load(None).to_dict(), self.rest(None).to_dict())

    def test_only_what_the_user_may_view_is_read(self):
        viewer = self.superuser()
        viewer.is_superuser = False
        viewer.save()
        perm = ObjectPermission.objects.create(name="all but srv1", actions=["view"], constraints={"name__in": ["sw1"]})
        perm.object_types.add(*perm.object_types.model.objects.filter(app_label="dcim", model="device"))
        perm.users.add(viewer)
        others = ObjectPermission.objects.create(name="the rest", actions=["view"])
        others.object_types.add(*others.object_types.model.objects.filter(app_label="dcim").exclude(model="device"))
        others.users.add(viewer)
        snapshot = ORMReader(viewer, NETBOX).load(["ldn1"])
        self.assertEqual([d.name for d in snapshot.devices], ["sw1"])


class ImportTestCase(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.superuser()
        self.objects = build_site()
        self.railyard = FakeRailyard()

    def run_import(self, *, user=None, settings_overrides=None, **options):
        """Run ``RailyardImportJob`` against the fake Railyard; ``(result, job)`` (``result`` is the ``JobFailed``)."""
        job = Job.objects.create(name="Railyard import (test)", user=user or self.user, job_id=uuid.uuid4())
        options.setdefault("sites", [self.objects["ldn1"].pk])
        options.setdefault("netbox_url", NETBOX)
        with (
            override_settings(PLUGINS_CONFIG=plugin_settings(**(settings_overrides or {}))),
            mock.patch("netbox_railyard.jobs.RailyardClient", side_effect=self.railyard.client) as client_cls,
        ):
            try:
                result = RailyardImportJob(job).run(**options)
            except JobFailed as exc:
                result = exc
        self.client_cls = client_cls
        return result, job

    def create(self, **options):
        result, job = self.run_import(name="LDN1 baseline", dry_run=False, **options)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        return result, self.railyard.projects[result["project_id"]]


class ImportJobTests(ImportTestCase):
    def test_dry_run_reports_and_saves_nothing(self):
        result, job = self.run_import(name="LDN1 baseline", dry_run=True)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.railyard.puts(), [])
        log = self.log(job)
        self.assertIn("Read NetBox (ldn1)", log)
        self.assertIn("devices: 4 imported, 1 skipped", log)  # the unracked spare is reported, not imported
        self.assertIn("Dry run: would create 'LDN1 baseline'", log)
        self.assertEqual(result["imported"]["racks"], 2)

    def test_create_builds_the_estate_from_netbox(self):
        result, estate = self.create()
        self.assertTrue(result["saved"])
        self.assertEqual(estate["name"], "LDN1 baseline")
        self.assertTrue(estate["id"].startswith("ry-"))
        sw1 = self.objects["sw1"]
        placements = {p["id"]: p for rack in estate["racks"] for p in rack["placements"]}
        self.assertEqual(placements[f"nb-dev-{sw1.pk}"]["label"], "sw1")
        self.assertEqual(recorded_source(estate), ("netbox", NETBOX, ["ldn1"]))
        (put,) = self.railyard.puts()
        self.assertNotIn("If-Match", put["headers"])
        self.assertEqual(Device.objects.get(pk=sw1.pk).name, "sw1")  # NetBox is never written

    def test_netbox_url_setting_wins_over_the_url_the_form_was_used_on(self):
        _, estate = self.create(settings_overrides={"netbox_url": "https://netbox.internal/"})
        self.assertEqual(recorded_source(estate)[1], "https://netbox.internal")

    def test_refresh_keeps_the_railyard_design_and_follows_netbox(self):
        result, estate = self.create()
        pid = result["project_id"]
        # Designed in Railyard meanwhile: a device in a free unit of A02.
        a02 = next(r for r in estate["racks"] if r["name"] == "A02")
        a02["placements"].append({"id": "pl_planned", "startU": 10, "heightU": 1, "face": "front", "label": "planned"})
        self.railyard.revisions[pid] += 1
        # Renamed in NetBox meanwhile.
        sw1 = self.objects["sw1"]
        sw1.name = "sw1-new"
        sw1.save()

        result, job = self.run_import(project=pid, dry_run=False)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertTrue(result["saved"] and result["refreshed"])
        put = self.railyard.puts()[-1]
        self.assertEqual(put["headers"]["If-Match"], '"2"')
        placements = {p["id"]: p for rack in put["json"]["racks"] for p in rack["placements"]}
        self.assertEqual(placements[f"nb-dev-{sw1.pk}"]["label"], "sw1-new")
        self.assertIn("pl_planned", placements)
        self.assertIn("Re-import changes:", self.log(job))

    def test_refresh_with_nothing_changed_saves_nothing(self):
        result, _ = self.create()
        again, job = self.run_import(project=result["project_id"], dry_run=False)
        self.assertNotIsInstance(again, JobFailed, self.log(job))
        self.assertFalse(again["saved"])
        self.assertEqual(len(self.railyard.puts()), 1)
        self.assertIn("already up to date", self.log(job))

    def test_conflicts_stop_the_save(self):
        result, estate = self.create()
        a01 = next(r for r in estate["racks"] if r["name"] == "A01")
        a01["placements"].append({"id": "pl_mine", "startU": 40, "heightU": 1, "face": "front", "label": "mine"})
        dry, job = self.run_import(project=result["project_id"], dry_run=True)
        self.assertEqual(dry["conflicts"], 1, self.log(job))
        self.assertIn("would stop a real import", self.log(job))
        real, job = self.run_import(project=result["project_id"], dry_run=False)
        self.assertIsInstance(real, JobFailed)
        self.assertIn("conflict(s) between Railyard's design and NetBox", self.log(job))
        self.assertEqual(len(self.railyard.puts()), 1)

    def test_the_rack_limit_shows_the_upgrade_message(self):
        self.railyard.put_response = FakeResponse(
            402,
            {
                "error": "This estate would have 2 racks; the Community plan allows 1.",
                "code": "plan_limit",
                "plan": "community",
                "resource": "racks",
                "limit": 1,
                "current": 2,
                "scope": "estate",
                "requiredPlans": ["pro"],
                "projectPass": True,
            },
        )
        result, job = self.run_import(name="LDN1 baseline", dry_run=False)
        self.assertIsInstance(result, JobFailed)
        self.assertIn(
            "This import has 2 racks; the Community plan allows 1 per estate. Upgrade to Pro, buy a Project Pass "
            "for this estate, or import fewer sites. Nothing was saved.",
            self.log(job),
        )

    def test_check_errors_stop_the_save(self):
        self.railyard.problems = [
            {"severity": "error", "code": "placement.overlap", "message": "two devices overlap", "rackId": "x"}
        ]
        result, job = self.run_import(name="LDN1 baseline", dry_run=False)
        self.assertIsInstance(result, JobFailed)
        self.assertIn("so it was not saved", self.log(job))
        self.assertEqual(self.railyard.puts(), [])

    def test_an_estate_from_another_netbox_is_refused(self):
        result, _ = self.create()
        again, job = self.run_import(project=result["project_id"], dry_run=True, netbox_url="https://other.example")
        self.assertIsInstance(again, JobFailed)
        self.assertIn("ids from two NetBox instances would collide", self.log(job))

    def test_every_site(self):
        result, estate = self.create(sites=None)
        self.assertEqual(recorded_source(estate)[2], ["ldn1", "man1"])
        self.assertTrue(result["saved"])


class ImportPermissionTests(ImportTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.superuser()
        self.user.is_superuser = False
        self.user.save()

    def test_the_job_refuses_a_user_who_may_not_view_what_it_reads(self):
        self.add_permissions(*[p for p in import_permissions() if p != "dcim.view_cable"])
        result, job = self.run_import(name="x", dry_run=True)
        self.assertIsInstance(result, JobFailed)
        self.assertIn("dcim.view_cable", self.log(job))
        self.client_cls.assert_not_called()

    def test_an_estate_outside_the_allowlist_is_refused(self):
        self.add_permissions(*import_permissions())
        result, job = self.run_import(
            project="prj_other", dry_run=True, settings_overrides={"railyard_allowed_projects": ["prj_it"]}
        )
        self.assertIsInstance(result, JobFailed)
        self.assertIn("railyard_allowed_projects", self.log(job))
        self.client_cls.assert_not_called()

    def test_page_needs_view_permissions(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse(URL)).status_code, 403)
        self.add_permissions(*import_permissions())
        with self.settings(PLUGINS_CONFIG=plugin_settings()):
            self.assertEqual(self.client.get(reverse(URL)).status_code, 200)

    def test_the_form_queues_the_job(self):
        self.add_permissions(*import_permissions())
        self.client.force_login(self.user)
        site = self.objects["ldn1"]
        with (
            self.settings(PLUGINS_CONFIG=plugin_settings()),
            mock.patch.object(RailyardImportJob, "enqueue") as enqueue,
        ):
            enqueue.return_value = Job.objects.create(name="queued", user=self.user, job_id=uuid.uuid4())
            resp = self.client.post(reverse(URL), {"sites": [site.pk], "org": "", "name": "LDN1", "dry_run": "on"})
        self.assertEqual(resp.status_code, 302, resp.content.decode()[:2000])
        kwargs = enqueue.call_args.kwargs
        queued = (kwargs["sites"], kwargs["name"], kwargs["project"], kwargs["dry_run"])
        self.assertEqual(queued, ([site.pk], "LDN1", None, True))
        self.assertTrue(kwargs["netbox_url"].startswith("http://testserver"))

    def test_the_form_needs_sites_and_one_estate(self):
        self.add_permissions(*import_permissions())
        self.client.force_login(self.user)
        with self.settings(PLUGINS_CONFIG=plugin_settings()):
            resp = self.client.post(reverse(URL), {"org": "", "name": "LDN1", "project": "prj_x", "dry_run": "on"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Choose the sites to import", resp.content.decode())
        self.assertIn("not both", resp.content.decode())
