"""The job only reaches orgs/projects an administrator allowed, and explains token problems."""

from unittest import mock

from core.exceptions import JobFailed
from dcim.models import Device
from railyard_sync.errors import RailyardPlanLimitError, RailyardPlanRequiredError, RailyardTokenRejectedError

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
        client.netbox_sync_document.side_effect = RailyardTokenRejectedError(
            "Railyard rejected … expired …", status=401
        )
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

    def test_the_document_is_asked_for_this_netbox_release(self):
        from netbox_railyard.jobs import netbox_release

        self.sync(dry_run=True)
        self.railyard.netbox_sync_document.assert_called_once_with("prj_it", netbox_version=netbox_release())

    def test_a_plan_without_deliverables_shows_the_upgrade_message(self):
        client = mock.MagicMock()
        client.netbox_sync_document.side_effect = RailyardPlanRequiredError(
            "The netbox-sync deliverable needs a paid plan.",
            plan="community",
            feature="deliverables",
            deliverable="netbox-sync",
            required_plans=["pro", "team"],
            project_pass=True,
        )
        result, job = self.sync(dry_run=True, client=client)
        self.assertIsInstance(result, JobFailed)
        self.assertIn(
            "Exporting to NetBox needs a plan with deliverables: the Community plan does not include them. "
            "Upgrade to Pro or Team, or buy a Project Pass for this estate. Nothing was written to NetBox.",
            self.log(job),
        )
        self.assertFalse(Device.objects.exists())

    def test_an_estate_past_the_deliverable_rack_limit_shows_it(self):
        client = mock.MagicMock()
        client.netbox_sync_document.side_effect = RailyardPlanLimitError(
            "too many racks", plan="pro", resource="racks", limit=100, current=140, required_plans=["team"]
        )
        result, job = self.sync(dry_run=True, client=client)
        self.assertIsInstance(result, JobFailed)
        self.assertIn(
            "This estate has 140 racks; the Pro plan exports deliverables for estates of up to 100", self.log(job)
        )
