"""What the sync document carries beyond racks and devices: locations, design tags, serials, comments, cable
type and colour, and the Railyard identity that renames a device in place."""

from core.exceptions import JobFailed
from dcim.models import Cable, Device, Interface, Location, Rack, Site
from extras.models import Tag

from .base import RAILYARD_URL, SyncTestCase, document, rename_device, row
from .test_ownership import operator_site


class SyncDocumentTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.superuser()

    def apply(self, doc=None, **options):
        result, job = self.sync(doc, dry_run=False, **options)
        self.assertNotIsInstance(result, JobFailed, self.log(job))
        return result, job

    def test_devices_carry_their_railyard_id_serial_and_comments(self):
        self.apply()
        srv = Device.objects.get(name="SRV-1")
        self.assertEqual(srv.custom_field_data["railyard_id"], "p_srv")
        self.assertEqual((srv.serial, srv.comments), ("SN-0001", "Database host"))
        self.assertEqual(srv.location.name, "A")

    def test_a_device_renamed_in_railyard_is_renamed_in_place(self):
        self.apply()
        before = Device.objects.get(name="SW-1")
        cable = Cable.objects.get(label="L1")
        doc = rename_device(document(), "SW-1", "SW-1A")

        dry, job = self.sync(doc, dry_run=True)
        self.assertEqual((dry["renamed"], dry["diff"]["create"]), (1, 0), self.log(job))
        self.assertIn("rename: device SW-1 → SW-1A", self.log(job))
        self.assertTrue(Device.objects.filter(name="SW-1").exists())

        result, job = self.apply(doc, allow_deletes=True)
        after = Device.objects.get(pk=before.pk)
        self.assertEqual(after.name, "SW-1A")
        self.assertTrue(Interface.objects.filter(device=after, name="Eth1").exists())
        self.assertTrue(Cable.objects.filter(pk=cable.pk).exists())  # its cable was not re-made
        self.assertEqual(result["diff"]["delete"], 0, self.log(job))
        again, job = self.sync(doc, dry_run=True)
        self.assertEqual(again["diff"]["create"] + again["diff"]["update"], 0, self.log(job))

    def test_a_rename_onto_an_operator_device_is_a_conflict(self):
        self.apply()
        site = Site.objects.get(name="DC1")
        from .test_ownership import operator_device

        operator_device("SW-1A", site)
        result, job = self.apply(rename_device(document(), "SW-1", "SW-1A"))
        self.assertEqual(result["renamed"], 0)
        self.assertIn("device SW-1A in site DC1 already exists", self.log(job))
        self.assertTrue(Device.objects.filter(name="SW-1").exists())

    def test_design_tags_are_created_and_follow_railyard(self):
        doc = document()
        doc["objects"]["tags"] = [{"name": "Build 1", "slug": "build-1", "color": "ff0000"}]
        row(doc, "racks", name="R1")["tags"] = "build-1"
        row(doc, "devices", name="SW-1")["tags"] = "build-1"
        self.apply(doc)
        build = Tag.objects.get(slug="build-1")
        self.assertEqual((build.name, build.color), ("Build 1", "ff0000"))
        rack = Rack.objects.get(name="R1")
        self.assertEqual(sorted(t.slug for t in rack.tags.all())[0], "build-1")
        self.assertTrue(Device.objects.get(name="SW-1").tags.filter(slug="build-1").exists())

        row(doc, "racks", name="R1")["tags"] = ""
        result, job = self.apply(doc)
        self.assertEqual(result["diff"]["update"], 1, self.log(job))
        self.assertFalse(rack.tags.filter(slug="build-1").exists())
        self.assertTrue(rack.tags.filter(slug__startswith="ry-").exists())  # still owned

    def test_a_dry_run_does_not_create_design_tags(self):
        doc = document()
        doc["objects"]["tags"] = [{"name": "Build 1", "slug": "build-1"}]
        row(doc, "racks", name="R1")["tags"] = "build-1"
        _, job = self.sync(doc, dry_run=True)
        self.assertIn("Tag 'build-1' doesn't exist in NetBox; a real run creates it.", self.log(job))
        self.assertFalse(Tag.objects.exists())

    def test_cable_type_status_and_colour_follow_railyard(self):
        self.apply()
        cable = Cable.objects.get(label="L1")
        self.assertEqual((cable.type, cable.status, cable.color), ("", "connected", ""))
        doc = document()
        row(doc, "cables", label="L1").update(type="dac-active", color="00ff00", status="planned")
        result, job = self.apply(doc)
        self.assertEqual(result["diff"]["update"], 1, self.log(job))
        cable.refresh_from_db()
        self.assertEqual((cable.type, cable.status, cable.color), ("dac-active", "planned", "00ff00"))
        power = Cable.objects.get(terminations___device__name="PDU-1")
        self.assertEqual(power.type, "power")

    def test_an_operator_location_is_used_as_it_is(self):
        site = operator_site()
        theirs = Location.objects.create(site=site, name="A", slug="a", description="the operator's")
        _, job = self.apply()
        theirs.refresh_from_db()
        self.assertFalse(theirs.tags.exists())
        self.assertEqual(theirs.description, "the operator's")
        self.assertEqual(Rack.objects.get(name="R1").location, theirs)
        self.assertIn("location DC1/A (exists; used as-is", self.log(job))

    def test_a_rack_moved_to_another_location(self):
        self.apply()
        doc = document()
        doc["objects"]["locations"].append(
            {"name": "B", "slug": "b-1", "site": "DC1", "parent": "", "status": "active", "facility": ""}
        )
        row(doc, "racks", name="R1")["location"] = "B"
        for device in doc["objects"]["devices"]:
            device["location"] = "B"
        result, job = self.apply(doc)
        rack = Rack.objects.get(name="R1")
        self.assertEqual(rack.location.name, "B", self.log(job))
        self.assertEqual(set(Device.objects.values_list("location__name", flat=True)), {"B"})

    def test_the_sync_and_the_command_line_share_the_ownership_tag(self):
        from railyard_sync.export.policy import ownership_tag

        self.apply()
        spec = ownership_tag(RAILYARD_URL, "prj_it", "Integration")
        self.assertTrue(Device.objects.filter(tags__slug=spec.slug).count() == 3)
