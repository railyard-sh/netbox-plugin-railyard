"""The job only reaches orgs/projects an administrator allowed, and explains token problems."""

from unittest import mock

from core.exceptions import JobFailed
from dcim.models import Device

from netbox_railyard.railyard.errors import RailyardTokenRejectedError

from .base import SyncTestCase


class TargetTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.superuser()

    def test_org_outside_the_allowlist_is_refused_before_contacting_railyard(self):
        result, job = self.sync(
            dry_run=True,
            org="someone-else",
            settings_overrides={"railyard_org": "acme", "railyard_allowed_orgs": ["beta"]},
        )
        self.assertIsInstance(result, JobFailed)
        self.assertIn("railyard_allowed_orgs", self.log(job))
        self.client_cls.assert_not_called()

    def test_allowlisted_org_is_passed_to_the_client(self):
        result, _ = self.sync(
            dry_run=True, org="BETA", settings_overrides={"railyard_org": "acme", "railyard_allowed_orgs": ["beta"]}
        )
        self.assertNotIsInstance(result, JobFailed)
        self.assertEqual(self.client_cls.call_args.kwargs["org"], "beta")

    def test_blank_org_means_the_default(self):
        self.sync(dry_run=True, settings_overrides={"railyard_org": "acme"})
        self.assertEqual(self.client_cls.call_args.kwargs["org"], "acme")

    def test_any_named_org_is_refused_when_none_is_configured(self):
        result, _ = self.sync(dry_run=True, org="acme")
        self.assertIsInstance(result, JobFailed)
        self.client_cls.assert_not_called()

    def test_project_outside_the_allowlist_is_refused(self):
        result, job = self.sync(
            dry_run=True, project_ref="prj_other", settings_overrides={"railyard_allowed_projects": ["prj_it"]}
        )
        self.assertIsInstance(result, JobFailed)
        self.assertIn("railyard_allowed_projects", self.log(job))
        self.client_cls.assert_not_called()

    def test_expired_token_fails_with_rotation_advice(self):
        client = mock.MagicMock()
        client.get_project.side_effect = RailyardTokenRejectedError("Railyard rejected … expired …", status=401)
        result, job = self.sync(dry_run=False, client=client)
        self.assertIsInstance(result, JobFailed)
        self.assertIn("rotate the token", self.log(job))
        self.assertFalse(Device.objects.exists())

    def test_token_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".token") as fh:
            fh.write("ry_fromfile\n")
            fh.flush()
            self.sync(dry_run=True, settings_overrides={"railyard_token": "", "railyard_token_file": fh.name})
        self.assertEqual(self.client_cls.call_args.args[1], "ry_fromfile")

    def test_plain_http_is_warned_about(self):
        _, job = self.sync(dry_run=True, settings_overrides={"railyard_url": "http://railyard.internal"})
        self.assertIn("not https", self.log(job))
