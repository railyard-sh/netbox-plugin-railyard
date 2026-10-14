"""The sync only updates or deletes objects carrying its ownership tag, never adopts operator objects,
and refuses deletes that would reach beyond what it owns."""

from core.exceptions import JobFailed
from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    FrontPort,
    Interface,
    InterfaceTemplate,
    Location,
    Manufacturer,
    PortMapping,
    Rack,
    RearPort,
    Site,
)
from extras.models import Tag
from users.models import ObjectPermission

from netbox_railyard.policy import ownership_tag

from .base import (
    RAILYARD_URL,
    SyncTestCase,
    another_project,
    creates,
    document,
    drop_all_devices,
    drop_devices,
    drop_racks,
    row,
)


def operator_site(name="DC1", **kw):
    return Site.objects.create(name=name, slug=name.lower(), **kw)


def operator_device(name, site, *, rack=None, model="OP1", **kw):
    mfr, _ = Manufacturer.objects.get_or_create(name="OperatorCo", slug="operatorco")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=model, slug=model.lower())
    role, _ = DeviceRole.objects.get_or_create(name="operator", slug="operator")
    return Device.objects.create(name=name, site=site, rack=rack, device_type=dt, role=role, **kw)


class OwnershipTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.superuser()

    def tag(self, doc=None):
        doc = doc or document()
        return Tag.objects.get(slug=ownership_tag(RAILYARD_URL, doc["project"]["id"], doc["project"]["name"]).slug)

    def test_first_sync_creates_and_tags_everything(self):
        result, job = self.sync(dry_run=False)
        self.assertEqual(result["diff"]["create"], creates(document()), self.log(job))
        tag = self.tag()
        self.assertEqual(Device.objects.filter(tags=tag).count(), 3)
        self.assertEqual(Cable.objects.filter(tags=tag).count(), 2)
        self.assertEqual(Location.objects.get(tags=tag).name, "A")
        self.assertEqual(Rack.objects.get(tags=tag).location.name, "A")
        again, job = self.sync(dry_run=False)
        self.assertEqual(again["diff"]["create"] + again["diff"]["update"], 0, self.log(job))  # idempotent

    def test_existing_shared_objects_are_used_but_never_tagged_or_changed(self):
        site = operator_site(status="planned")  # Railyard says "active"
        rack = Rack.objects.create(site=site, name="R1", u_height=48)  # Railyard says 42U
        mfr = Manufacturer.objects.create(name="Acme", slug="acme", description="operator's")

        result, job = self.sync(dry_run=False)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        site.refresh_from_db(), rack.refresh_from_db(), mfr.refresh_from_db()
        self.assertEqual(site.status, "planned")
        self.assertEqual(rack.u_height, 48)
        self.assertEqual(mfr.description, "operator's")
        for obj in (site, rack, mfr):
            self.assertFalse(obj.tags.exists(), obj)
        # the sync's own devices went into the operator's rack
        self.assertEqual(Device.objects.filter(rack=rack, tags=self.tag()).count(), 3)
        self.assertIn("used as they are", self.log(job))

        # and a later sync still doesn't touch them
        self.sync(dry_run=False, allow_deletes=True)
        site.refresh_from_db()
        self.assertEqual(site.status, "planned")
        self.assertTrue(Site.objects.filter(pk=site.pk).exists())

    def test_same_named_device_in_the_same_site_is_a_conflict_not_an_adoption(self):
        site = operator_site()
        theirs = operator_device("SW-1", site, serial="KEEP-ME")

        result, job = self.sync(dry_run=False)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        theirs.refresh_from_db()
        self.assertFalse(theirs.tags.exists())
        self.assertEqual(theirs.device_type.model, "OP1")
        self.assertEqual(theirs.serial, "KEEP-ME")
        self.assertIn("device SW-1 in site DC1 already exists", self.log(job))
        # its component and cable were skipped; the other devices and the power cable were created
        self.assertFalse(Interface.objects.filter(device=theirs).exists())
        self.assertEqual(Device.objects.filter(tags=self.tag()).count(), 2)
        self.assertEqual(Cable.objects.count(), 1)
        self.assertEqual(result["conflicts"], 1)

    def test_same_named_device_in_another_site_is_untouched_by_updates_and_deletes(self):
        elsewhere = operator_site("Elsewhere")
        theirs = operator_device("SW-1", elsewhere, serial="KEEP-ME")
        self.sync(dry_run=False)
        ours = Device.objects.get(name="SW-1", tags=self.tag())
        self.assertNotEqual(ours.pk, theirs.pk)

        # Railyard moves SW-1 up a unit: only the sync's SW-1 changes
        doc = document()
        row(doc, "devices", name="SW-1")["position"] = "12"
        self.sync(doc, dry_run=False)
        ours.refresh_from_db(), theirs.refresh_from_db()
        self.assertEqual(ours.position, 12)
        self.assertIsNone(theirs.position)

        # Railyard drops SW-1: only the sync's SW-1 is deleted
        drop_devices(doc, "SW-1")
        result, job = self.sync(doc, dry_run=False, allow_deletes=True)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertFalse(Device.objects.filter(pk=ours.pk).exists())
        self.assertTrue(Device.objects.filter(pk=theirs.pk).exists())

    def test_untagged_objects_are_never_deleted(self):
        self.sync(dry_run=False)
        ours = Device.objects.get(name="SW-1")
        ours.tags.clear()  # e.g. an operator takes it over by removing the tag
        empty = drop_all_devices(document())
        result, job = self.sync(empty, dry_run=False, allow_deletes=True)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertTrue(Device.objects.filter(pk=ours.pk).exists())
        # its rack and site are kept too, because the untagged device still uses them
        self.assertTrue(Rack.objects.filter(name="R1").exists())
        self.assertIn("still in use by 1 device", self.log(job))

    def test_delete_refused_when_it_would_cascade_to_operator_data(self):
        self.sync(dry_run=False)
        rack = Rack.objects.get(name="R1")
        theirs = operator_device("OPS-1", rack.site, rack=rack)
        result, job = self.sync(drop_racks(document()), dry_run=False, allow_deletes=True)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertTrue(Device.objects.filter(pk=theirs.pk).exists())
        self.assertTrue(Rack.objects.filter(pk=rack.pk).exists())  # kept: it holds an operator device
        self.assertFalse(Device.objects.filter(tags=self.tag()).exists())  # the sync's devices went
        self.assertGreaterEqual(result["kept"], 1)

    def test_device_with_an_operator_cable_is_kept(self):
        self.sync(dry_run=False)
        srv = Device.objects.get(name="SRV-1")
        other = operator_device("OPS-1", srv.site)
        mine = Interface.objects.create(device=srv, name="mgmt0", type="1000base-t")
        theirs = Interface.objects.create(device=other, name="eth0", type="1000base-t")
        cable = Cable(a_terminations=[mine], b_terminations=[theirs])
        cable.save()
        result, job = self.sync(drop_devices(document(), "SRV-1"), dry_run=False, allow_deletes=True)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertTrue(Device.objects.filter(pk=srv.pk).exists())
        self.assertTrue(Cable.objects.filter(pk=cable.pk).exists())
        self.assertIn("not managed by this sync", self.log(job))

    def test_port_already_cabled_by_an_operator_is_skipped(self):
        self.sync(dry_run=False)
        Cable.objects.get(label="L1").delete()  # someone re-patches SW-1:Eth1 by hand
        sw_eth1 = Interface.objects.get(device__name="SW-1", name="Eth1")
        other = operator_device("OPS-1", sw_eth1.device.site)
        port = Interface.objects.create(device=other, name="eth0", type="1000base-t")
        theirs = Cable(a_terminations=[sw_eth1], b_terminations=[port], label="operator")
        theirs.save()

        result, job = self.sync(dry_run=False, allow_deletes=True)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        theirs.refresh_from_db()
        self.assertEqual(theirs.label, "operator")
        self.assertFalse(theirs.tags.exists())
        self.assertIn("already cabled", self.log(job))
        self.assertFalse(Cable.objects.filter(label="L1").exists())

    def test_template_components_on_an_owned_device_are_adopted(self):
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        dt = DeviceType.objects.create(manufacturer=mfr, model="SW1", slug="sw1")  # the operator's type
        InterfaceTemplate.objects.create(device_type=dt, name="Eth1", type="10gbase-x-sfpp")
        InterfaceTemplate.objects.create(device_type=dt, name="Eth48", type="10gbase-x-sfpp")

        result, job = self.sync(dry_run=False)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        tag = self.tag()
        sw = Device.objects.get(name="SW-1", tags=tag)
        self.assertTrue(Interface.objects.get(device=sw, name="Eth1").tags.filter(pk=tag.pk).exists())
        self.assertFalse(Interface.objects.get(device=sw, name="Eth48").tags.exists())  # not in Railyard
        self.assertIn("Adopted", self.log(job))
        # a mirror sync leaves the template interface the plan doesn't mention alone
        self.sync(dry_run=False, allow_deletes=True)
        self.assertTrue(Interface.objects.filter(device=sw, name="Eth48").exists())

    def test_projects_with_the_same_name_get_separate_tags(self):
        a = document()
        a["project"].update(id="prj_a", name="Same name")
        b = another_project(document(), "prj_b", "Same name", site="DC2", suffix="-B")
        self.sync(a, dry_run=False)
        self.sync(b, dry_run=False)
        tag_a, tag_b = self.tag(a), self.tag(b)
        self.assertNotEqual(tag_a.pk, tag_b.pk)
        self.assertEqual(Device.objects.filter(tags=tag_a).count(), 3)

        # mirroring project B (now empty) never deletes project A's objects
        result, job = self.sync(drop_racks(b), dry_run=False, allow_deletes=True)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertEqual(Device.objects.filter(tags=tag_a).count(), 3)
        self.assertFalse(Device.objects.filter(tags=tag_b).exists())

    def test_same_project_on_another_instance_gets_another_tag(self):
        self.sync(dry_run=False)
        self.sync(dry_run=False, settings_overrides={"railyard_url": "https://other.example"})
        self.assertEqual(Tag.objects.count(), 2)

    def test_a_lookalike_tag_is_refused(self):
        spec = ownership_tag(RAILYARD_URL, "prj_it", "Integration")
        Tag.objects.create(slug=spec.slug, name="lookalike", description="someone else's")
        result, job = self.sync(dry_run=False)
        self.assertIsInstance(result, JobFailed)
        self.assertIn("is not this project's Railyard ownership tag", self.log(job))
        self.assertFalse(Device.objects.exists())
        dry, job = self.sync(dry_run=True)
        self.assertIsInstance(dry, JobFailed)

    def test_failure_rolls_back_the_whole_sync(self):
        site = operator_site()
        rack = Rack.objects.create(site=site, name="R1")
        operator_device("OPS-1", site, rack=rack, position=10, face="front")  # where SW-1 must go
        result, job = self.sync(dry_run=False)
        self.assertIsInstance(result, JobFailed)
        self.assertIn("rolled back", self.log(job))
        self.assertEqual(Device.objects.count(), 1)  # only the operator's
        self.assertFalse(Tag.objects.exists())


class PatchPanelTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.superuser()

    def test_patch_panel_ports_and_mappings(self):
        doc = document("patch")
        result, job = self.sync(doc, dry_run=False)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertEqual(RearPort.objects.filter(device__name="PP-1").count(), 2)
        self.assertEqual(FrontPort.objects.filter(device__name="PP-1").count(), 2)
        mapping = PortMapping.objects.get(device__name="PP-1", front_port__name="2")
        self.assertEqual((mapping.rear_port.name, mapping.rear_port_position), ("2", 1))
        front = FrontPort.objects.get(device__name="PP-1", name="1")
        self.assertEqual(front.cable.a_terminations[0].name, "Eth2")
        again, job = self.sync(doc, dry_run=False)
        self.assertEqual(again["diff"]["create"] + again["diff"]["update"], 0, self.log(job))

    def test_a_remapped_front_port_is_remapped_in_place(self):
        doc = document("patch")
        self.sync(doc, dry_run=False)
        row(doc, "front-ports", device="PP-1", name="1")["rear_port"] = "2"
        row(doc, "front-ports", device="PP-1", name="2")["rear_port"] = "1"
        result, job = self.sync(doc, dry_run=False)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertEqual(result["diff"]["update"], 2, self.log(job))
        mapping = PortMapping.objects.get(device__name="PP-1", front_port__name="1")
        self.assertEqual(mapping.rear_port.name, "2")


class DeleteConstraintTests(SyncTestCase):
    def test_a_delete_the_user_may_not_make_is_kept(self):
        from netbox_railyard.policy import SYNC_MODELS

        from .base import APPLY, BOOTSTRAP

        self.add_permissions(*APPLY, *BOOTSTRAP, *[f"dcim.delete_{m}" for m in SYNC_MODELS if m != "device"])
        # devices: may delete only those named SRV-*
        perm = ObjectPermission.objects.create(
            name="srv only", actions=["delete"], constraints={"name__startswith": "SRV-"}
        )
        perm.object_types.add(*perm.object_types.model.objects.filter(app_label="dcim", model="device"))
        perm.users.add(self.user)
        self.sync(dry_run=False)

        result, job = self.sync(drop_all_devices(document()), dry_run=False, allow_deletes=True)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        self.assertFalse(Device.objects.filter(name="SRV-1").exists())
        self.assertTrue(Device.objects.filter(name="SW-1").exists())
        self.assertIn("may not delete it", self.log(job))
