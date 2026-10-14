"""A dry run reports but never writes; a real run is change-logged as the user who queued it."""

from core.models import ObjectChange
from dcim.models import Device, Manufacturer, Site
from extras.models import CustomField, Tag

from .base import RAILYARD_URL, SyncTestCase, creates, document, drop_devices, drop_racks, row


class DryRunTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.superuser()

    def test_first_dry_run_writes_nothing_at_all(self):
        result, job = self.sync(dry_run=True, allow_deletes=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["diff"]["create"], creates(document()))
        self.assertFalse(Tag.objects.exists())
        self.assertFalse(CustomField.objects.exists())
        self.assertFalse(Device.objects.exists())
        self.assertFalse(ObjectChange.objects.exists())
        self.assertIn("nothing was written", self.log(job))
        self.assertIn("Upgrading from plugin 0.1", self.log(job))  # tells the operator the new tag's identity

    def test_converting_a_0_1_tag_hands_its_objects_over(self):
        from extras.models import Tag

        from netbox_railyard.policy import ownership_tag

        self.sync(dry_run=False)
        tag = Tag.objects.get()
        # simulate 0.1: the tag was keyed by project name, without the description
        tag.slug, tag.name, tag.description = "ry-integration", "RY:Integration", ""
        tag.save()
        result, _ = self.sync(dry_run=True)
        self.assertEqual(result["conflicts"], 3)  # its devices are no longer recognised as owned
        # the documented migration: give the old tag the new identity
        spec = ownership_tag(RAILYARD_URL, "prj_it", "Integration")
        tag.slug, tag.name, tag.description = spec.slug, spec.name, spec.description
        tag.save()
        result, _ = self.sync(dry_run=True)
        self.assertEqual((result["conflicts"], result["diff"]["create"]), (0, 0))

    def test_dry_run_after_a_sync_reports_without_applying(self):
        self.sync(dry_run=False)
        changes = ObjectChange.objects.count()
        doc = drop_devices(document(), "PDU-1")
        row(doc, "sites", name="DC1")["status"] = "planned"
        result, job = self.sync(doc, dry_run=True, allow_deletes=True)
        self.assertEqual(result["diff"]["update"], 1)
        self.assertGreaterEqual(result["diff"]["delete"], 1)
        self.assertIn("delete: device [PDU-1]", self.log(job))
        self.assertEqual(Site.objects.get(name="DC1").status, "active")
        self.assertTrue(Device.objects.filter(name="PDU-1").exists())
        self.assertEqual(ObjectChange.objects.count(), changes)

    def test_dry_run_reports_conflicts_and_references(self):
        Manufacturer.objects.create(name="Acme", slug="acme")
        result, job = self.sync(dry_run=True)
        self.assertEqual(result["referenced"], 1)
        self.assertIn("manufacturer Acme", self.log(job))
        self.assertFalse(Manufacturer.objects.get(name="Acme").tags.exists())

    def test_dry_run_previews_kept_deletes(self):
        self.sync(dry_run=False)
        from .test_ownership import operator_device  # noqa: PLC0415

        rack = Device.objects.get(name="SW-1").rack
        operator_device("OPS-1", rack.site, rack=rack)
        _, job = self.sync(drop_racks(document()), dry_run=True, allow_deletes=True)
        log = self.log(job)
        self.assertIn("delete: rack", log)
        self.assertIn("would be kept: still in use by 1 device", log)
        self.assertNotIn("delete: device [SW-1] — would be kept", log)

    def test_apply_is_change_logged_as_the_job_user(self):
        self.sync(dry_run=False)
        changes = ObjectChange.objects.filter(changed_object_type__model="device")
        self.assertEqual(changes.count(), 3)
        self.assertEqual({c.user_id for c in changes}, {self.user.pk})
