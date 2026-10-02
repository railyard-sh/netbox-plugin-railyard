"""A sync needs NetBox permissions — checked by the view, re-checked by the job, and applied per object."""

from core.exceptions import JobFailed
from core.models import Job
from dcim.models import Device, Site
from django.urls import reverse
from extras.models import Tag
from users.models import ObjectPermission

from .base import APPLY, BOOTSTRAP, DELETE, VIEW, SyncTestCase, plugin_settings

URL = "plugins:netbox_railyard:sync"


class ViewPermissionTests(SyncTestCase):
    def test_page_is_forbidden_without_view_permissions(self):
        self.assertEqual(self.client.get(reverse(URL)).status_code, 403)

    def test_page_opens_with_view_permissions(self):
        self.add_permissions(*VIEW)
        self.assertEqual(self.client.get(reverse(URL)).status_code, 200)

    def test_applying_needs_add_and_change(self):
        self.add_permissions(*VIEW)
        with self.settings(PLUGINS_CONFIG=plugin_settings()):
            resp = self.client.post(reverse(URL), {"org": "", "project": "prj_it", "confirm": "on"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("dcim.add_device", resp.content.decode())
        self.assertFalse(Job.objects.exists())

    def test_deleting_needs_delete(self):
        self.add_permissions(*APPLY)
        with self.settings(PLUGINS_CONFIG=plugin_settings()):
            resp = self.client.post(
                reverse(URL), {"org": "", "project": "prj_it", "allow_deletes": "on", "confirm": "on"}
            )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("dcim.delete_device", resp.content.decode())
        self.assertFalse(Job.objects.exists())

    def test_applying_needs_the_confirmation(self):
        self.add_permissions(*APPLY)
        with self.settings(PLUGINS_CONFIG=plugin_settings()):
            resp = self.client.post(reverse(URL), {"org": "", "project": "prj_it"})
        self.assertEqual(resp.status_code, 200)  # form re-rendered with the error
        self.assertFalse(Job.objects.exists())


class JobPermissionTests(SyncTestCase):
    def test_job_refuses_a_user_without_permissions(self):
        self.add_permissions(*VIEW)  # enough to dry-run, not to apply
        result, job = self.sync(dry_run=False)
        self.assertIsInstance(result, JobFailed)
        self.assertIn("dcim.add_device", self.log(job))
        self.client_cls.assert_not_called()  # refused before Railyard is even contacted
        self.assertFalse(Device.objects.exists())

    def test_job_refuses_deletes_without_delete_permission(self):
        self.add_permissions(*APPLY, *BOOTSTRAP)
        result, job = self.sync(dry_run=False, allow_deletes=True)
        self.assertIsInstance(result, JobFailed)
        self.assertIn("dcim.delete_", self.log(job))

    def test_dry_run_needs_only_view(self):
        self.add_permissions(*VIEW)
        result, _ = self.sync(dry_run=True)
        self.assertNotIsInstance(result, JobFailed)
        self.assertEqual(result["diff"]["create"], 19)

    def test_apply_with_model_permissions(self):
        self.add_permissions(*APPLY, *DELETE, *BOOTSTRAP)
        result, _ = self.sync(dry_run=False, allow_deletes=True)
        self.assertNotIsInstance(result, JobFailed)
        self.assertEqual(Device.objects.count(), 3)

    def test_creating_the_tag_needs_permission(self):
        self.add_permissions(*APPLY, "extras.add_customfield", "extras.change_customfield")
        result, job = self.sync(dry_run=False)
        self.assertIsInstance(result, JobFailed)
        self.assertIn("extras.add_tag", self.log(job))
        self.assertFalse(Tag.objects.exists())
        self.assertFalse(Site.objects.exists())

    def test_object_level_constraints_apply_and_roll_back(self):
        # The user may add sites only when the site is called "Elsewhere": creating DC1 is refused, and
        # everything created before that point is rolled back with it.
        self.add_permissions(*[p for p in APPLY if p != "dcim.add_site"], *BOOTSTRAP)
        perm = ObjectPermission.objects.create(
            name="constrained site add", actions=["add"], constraints={"name": "Elsewhere"}
        )
        perm.object_types.add(*perm.object_types.model.objects.filter(app_label="dcim", model="site"))
        perm.users.add(self.user)

        result, job = self.sync(dry_run=False)
        self.assertIsInstance(result, JobFailed)
        self.assertIn("may not add site", self.log(job))
        self.assertIn("rolled back", self.log(job))
        self.assertFalse(Site.objects.exists())
        self.assertFalse(Tag.objects.exists())

    def test_inactive_user_is_refused(self):
        user = self.superuser()
        user.is_active = False
        user.save()
        result, _ = self.sync(user=user, dry_run=True)
        self.assertIsInstance(result, JobFailed)
