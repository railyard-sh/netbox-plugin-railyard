"""``RailyardImportJob`` — the NetBox background Job that imports NetBox sites into Railyard.

The other direction from ``RailyardSyncJob``: it reads the chosen sites from NetBox's own database into a
railyard-sync ``Snapshot`` (``loader.ORMReader``, field for field what ``railyard-sync import netbox`` reads
over the REST API) and runs railyard-sync's ``ImportRun``, the command's own import flow:

- **A new estate** (a name): built from the sites, with device types matched against Railyard's catalogue.
- **A refresh** of an estate imported before (an estate id or URL slug): the import is merged into it as a
  baseline. What was designed in Railyard is kept; objects gone from NetBox are kept and reported as stale
  unless *Allow deletes* is ticked. Conflicts between the design and NetBox stop the save, and an estate
  imported from another NetBox, or from more sites than this import reads, is refused.

A dry run shows the import report, the re-import diff and Railyard's check of the document, and saves
nothing. A real run is saved only when Railyard's check passes, and a refresh names the revision it replaces,
so a change made in Railyard meanwhile is never overwritten. Railyard caps racks per estate by plan: a save
past the cap is refused (HTTP 402) and the job fails with the upgrade message.

The job writes nothing to NetBox. It needs ``view`` on what it reads (``policy.import_permissions``); what the
user may not view is not imported.
"""

from __future__ import annotations

from railyard_sync.dcim.errors import DCIMError
from railyard_sync.errors import RailyardAPIError, RailyardPlanError, RailyardPlanLimitError
from railyard_sync.importer.run import (
    ImportRefused,
    ImportRun,
    check_failure,
    conflicts_message,
    import_summary,
    snapshot_counts,
)
from railyard_sync.plans import plan_limit_message, sentence, upgrade_options

from .config import railyard_settings, sync_targets
from .jobs import RailyardJob, railyard_sync_logs
from .loader import ORMReader
from .policy import TargetNotAllowed, missing_import_permissions, validate_estate_name

#: The id prefix of everything imported from NetBox: the same as the command line's, so an estate imported by
#: either can be refreshed by the other.
PREFIX = "nb"

#: What to do about Railyard's check, worded for the job rather than the command line.
CHECK_FIX = "Fix them in NetBox (or in Railyard, for objects designed there) and import again."


def plan_message(e: RailyardPlanError, *, refreshing: bool) -> str:
    """The upgrade message for a plan refusal during an import."""
    if isinstance(e, RailyardPlanLimitError):
        return plan_limit_message(e, refreshing=refreshing)
    options = upgrade_options(e.required_plans, e.project_pass)
    return f"{e}" + (f" {sentence(options)}" if options else "") + " Nothing was saved."


class RailyardImportJob(RailyardJob):
    class Meta:
        name = "Import into Railyard"

    def run(
        self,
        *,
        sites=None,
        org=None,
        project=None,
        name=None,
        dry_run=True,
        allow_deletes=False,
        netbox_url="",
        **kwargs,
    ):
        cfg = railyard_settings()
        user = self._check_user(lambda u: missing_import_permissions(u.has_perm), "import")

        # The job is the enforcement point for the allowlist, whatever enqueued it.
        targets = sync_targets()
        try:
            org = targets.check_org(org)
            if project:
                project = targets.check_project(project)
            else:
                name = validate_estate_name(name)
        except TargetNotAllowed as exc:
            self._fail(str(exc))

        site_refs = [str(int(pk)) for pk in sites] if sites else None
        url = cfg["netbox_url"] or netbox_url or ""
        client = self._client(cfg, org)
        run = None
        try:
            with railyard_sync_logs(self.logger):
                snapshot = ORMReader(user, url).load(site_refs)
                self.logger.info(f"Read {snapshot_counts(snapshot)}.")
                self._log_list("NetBox warnings", snapshot.warnings, level="warning")

                run = ImportRun(
                    client,
                    snapshot,
                    project=project,
                    name=name,
                    prefix=PREFIX,
                    allow_deletes=allow_deletes,
                    source_url=url,
                    name_taken_hint="choose it as the estate to refresh instead, or choose another name",
                )
                run.fetch()
                built = run.build()
                for line in import_summary(snapshot, built.project, built.report).splitlines():
                    self.logger.info(line)
                self._log_list("Import warnings", built.report.warnings, level="warning")
                diff = run.merge()
                if diff is not None:
                    for line in diff.summary().splitlines():
                        self.logger.info(line)
                    if diff.stale_count and not allow_deletes:
                        self.logger.info("Objects deleted in NetBox were kept; tick Allow deletes to remove them.")
                return self._finish(run, dry_run)
        except ImportRefused as exc:
            self._fail(str(exc))
        except RailyardPlanError as exc:
            self._fail(plan_message(exc, refreshing=bool(project)))
        except DCIMError as exc:
            self._fail(str(exc))
        except (RailyardAPIError, ValueError) as exc:
            self._railyard_failed(exc)

    def _finish(self, run: ImportRun, dry_run: bool) -> dict:
        what = f"{run.name!r} ({run.project_id})" if run.project_id else repr(run.name)
        result = {
            "dry_run": dry_run,
            "project_id": run.project_id,
            "name": run.name,
            "refreshed": run.refreshing,
            "imported": dict(run.built.report.imported),
            "skipped": len(run.built.report.skipped),
            "conflicts": len(run.conflicts),
            "problems": 0,
            "saved": False,
            "revision": run.revision,
        }
        if dry_run:
            problems = run.check()
            result["problems"] = len(problems)
            if problems:
                self.logger.error(check_failure(problems, dry_run=True, fix=CHECK_FIX))
            if run.conflicts:
                self.logger.error(f"{len(run.conflicts)} conflict(s) (listed above) would stop a real import.")
            action = f"refresh {what}" if run.refreshing else f"create {what}"
            self.logger.warning(f"Dry run: would {action}. Nothing was saved to Railyard.")
            return result
        if run.conflicts:
            self._fail(conflicts_message(len(run.conflicts)))
        if run.up_to_date():
            self.logger.info(f"{run.name!r} is already up to date with NetBox; nothing was saved.")
            return result
        problems = run.check()
        if problems:
            self._fail(check_failure(problems, dry_run=False, fix=CHECK_FIX))
        revision = run.save()
        verb = "Refreshed" if run.refreshing else "Created"
        self.logger.info(f"{verb} {what} at revision {revision}.")
        result.update(saved=True, revision=revision)
        return result
