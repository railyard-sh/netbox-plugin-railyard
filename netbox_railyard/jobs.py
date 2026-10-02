"""``RailyardSyncJob`` — the NetBox background Job that runs the sync.

It shows up in NetBox's core Jobs UI (progress, log, result). It loads the Railyard project into the
source adapter and the scoped NetBox state into the target adapter, then reconciles with DiffSync —
create/update only by default (``SKIP_UNMATCHED_DST``), or a full mirror when ``allow_deletes`` is set.

A dry run is read-only: it never creates the ownership tag or custom field, and runs inside a
transaction that is always rolled back as a backstop. A real run applies everything in one
transaction (all or nothing) with NetBox's request processors active, so every change is recorded in
the changelog against the user who queued the job and event rules fire only once it has committed.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

from core.exceptions import JobFailed
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectCrudException
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from netbox.jobs import JobRunner
from utilities.request import NetBoxFakeRequest, apply_request_processors

from .config import railyard_settings, railyard_token, sync_targets
from .policy import TagSpec, TargetNotAllowed, insecure_url, missing_permissions, ownership_tag
from .railyard.client import RailyardClient
from .railyard.devicetype_library import DeviceTypeLibrary
from .railyard.errors import RailyardAuthError, RailyardNotFoundError, RailyardTokenRejectedError
from .railyard.source import RailyardAdapter
from .target import _NB_MODEL, CUSTOM_FIELD, NetBoxAdapter


class _Rollback(Exception):
    """Raised to roll back a dry run's transaction."""


@contextmanager
def _rolled_back():
    """Run a block in a transaction that is always rolled back (a dry run's backstop against writes)."""
    try:
        with transaction.atomic():
            yield
            raise _Rollback
    except _Rollback:
        pass


def _label(model) -> str:
    return _NB_MODEL[model.get_type()]._meta.label_lower


def _change_logging(user):
    """NetBox's request processors (change logging, event rules) for a job running as ``user``."""
    request = NetBoxFakeRequest(
        {
            "META": {},
            "COOKIES": {},
            "POST": {},
            "GET": {},
            "FILES": {},
            "user": user,
            "method": "POST",
            "path": "",
            "id": uuid.uuid4(),
        }
    )
    return apply_request_processors(request)


class SyncRefused(Exception):
    """The sync can't safely continue; the message says why (shown in the job log)."""


def ensure_custom_field(user) -> None:
    """Ensure the ``railyard_id`` text custom field exists on dcim.device (stable identity across
    re-syncs). Written defensively because the CustomField relation was renamed across NetBox minors
    (``content_types`` -> ``object_types``)."""
    from django.contrib.contenttypes.models import ContentType
    from extras.models import CustomField

    cf = CustomField.objects.filter(name=CUSTOM_FIELD).first()
    if cf is None:
        if not user.has_perm("extras.add_customfield"):
            raise SyncRefused(
                f"The {CUSTOM_FIELD!r} custom field doesn't exist yet and {user} may not create it: ask an "
                "administrator to run the first sync, or to create it (text, on dcim.device)."
            )
        cf = CustomField.objects.create(
            name=CUSTOM_FIELD, type="text", label="Railyard ID", description="Railyard placement id"
        )
    ct = ContentType.objects.get(app_label="dcim", model="device")
    relation = getattr(cf, "object_types", None) or getattr(cf, "content_types", None)
    if relation is not None and not relation.filter(pk=ct.pk).exists():
        if not user.has_perm("extras.change_customfield"):
            raise SyncRefused(f"The {CUSTOM_FIELD!r} custom field isn't enabled on devices and {user} may not fix it.")
        relation.add(ct)


def find_ownership_tag(spec: TagSpec):
    """The project's ownership tag if it exists, verified, without creating or changing anything.

    A tag with the expected slug but a different description isn't ours (an operator's tag, or a tag
    someone edited), so the sync refuses to treat the objects carrying it as Railyard-owned.
    """
    from extras.models import Tag

    tag = Tag.objects.filter(slug=spec.slug).first()
    if tag is not None and tag.description != spec.description:
        raise SyncRefused(
            f"Tag {tag.name!r} (slug {spec.slug!r}) is not this project's Railyard ownership tag: its "
            f"description has changed. If it is, restore the description to {spec.description!r}."
        )
    return tag


def ensure_ownership_tag(spec: TagSpec, user):
    """Get-or-create the project's ownership tag. The name follows project renames when allowed."""
    from extras.models import Tag

    tag = find_ownership_tag(spec)
    if tag is None:
        if not user.has_perm("extras.add_tag"):
            raise SyncRefused(f"{user} may not create this project's ownership tag {spec.name!r} (extras.add_tag).")
        if Tag.objects.filter(name=spec.name).exists():
            raise SyncRefused(f"A different tag is already named {spec.name!r}; rename it and re-run.")
        tag = Tag(slug=spec.slug, name=spec.name, description=spec.description, color="1f8bff")
        tag.full_clean()
        tag.save()
        return tag
    if (
        tag.name != spec.name
        and user.has_perm("extras.change_tag")
        and not Tag.objects.filter(name=spec.name).exclude(pk=tag.pk).exists()
    ):
        tag.name = spec.name
        tag.save()
    return tag


class RailyardSyncJob(JobRunner):
    class Meta:
        name = "Railyard → NetBox sync"

    def _fail(self, message: str):
        self.logger.error(message)
        raise JobFailed(message)

    def run(self, *, project, org=None, dry_run=False, allow_deletes=False, **kwargs):
        cfg = railyard_settings()

        # The job acts as the user who queued it, and re-checks their permissions now: they may have
        # changed since it was queued, and a job can be queued by other means than the form.
        user = self.job.user
        if user is None or not user.is_active:
            self._fail("A Railyard sync must be queued by an active NetBox user.")
        missing = missing_permissions(user.has_perm, dry_run=dry_run, allow_deletes=allow_deletes)
        if missing:
            self._fail(f"{user} lacks the NetBox permissions this sync needs: {', '.join(missing)}")

        # Re-check the target against the allowlist here too: the job is the enforcement point, whatever
        # enqueued it (the form, a shell, a re-run of an older job).
        targets = sync_targets()
        try:
            org = targets.check_org(org)
            project = targets.check_project(project)
        except TargetNotAllowed as exc:
            self._fail(str(exc))

        try:
            token = railyard_token()
        except ValueError as exc:
            self._fail(str(exc))
        if not token:
            self._fail("Set 'railyard_token' or 'railyard_token_file' in PLUGINS_CONFIG['netbox_railyard'] first.")
        if insecure_url(cfg["url"]):
            self.logger.warning(
                f"railyard_url {cfg['url']!r} is not https: the Railyard token is sent in clear text. Use https."
            )

        self.logger.info(f"Fetching Railyard project {project!r} from org {org or '(personal)'!r}…")
        try:
            client = RailyardClient(cfg["url"], token, org=org)
            doc = client.get_project(project)
            dtl = DeviceTypeLibrary(ref=cfg["devicetype_library_ref"], cache_dir=cfg["devicetype_cache_dir"])
        except RailyardTokenRejectedError as exc:
            self._fail(f"{exc} Ask a NetBox administrator to rotate the token.")
        except (RailyardAuthError, RailyardNotFoundError, ValueError) as exc:
            self._fail(str(exc))

        source = RailyardAdapter(doc, devicetype_library=dtl, name="railyard")
        source.load()
        for warning in source.warnings:
            self.logger.warning(warning)
        try:
            spec = ownership_tag(cfg["url"], source.project.id, source.project.name)
        except ValueError as exc:
            self._fail(str(exc))

        try:
            if dry_run:
                with _rolled_back():
                    return self._dry_run(source, spec, user, cfg, allow_deletes)
            with _change_logging(user), transaction.atomic():
                return self._apply(source, spec, user, cfg, allow_deletes)
        except (SyncRefused, PermissionDenied, ObjectCrudException, ValidationError, IntegrityError) as exc:
            self.logger.error("Nothing was changed in NetBox: the sync was rolled back.")
            self._fail(str(exc))

    def _plan(self, target, source, allow_deletes: bool):
        """Decide what to skip/use as-is, then diff. Read-only."""
        target.load()
        report = target.reconcile(source, allow_deletes=allow_deletes)
        # Creates/updates come from the diff; deletes are handled separately, in dependency order.
        diff = target.diff_from(source, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        deletes = target.delete_candidates(source) if allow_deletes else []
        return report, diff, deletes

    def _dry_run(self, source, spec, user, cfg, allow_deletes):
        tag = find_ownership_tag(spec)
        if tag is None:
            self.logger.info(
                f"Ownership tag {spec.name!r} doesn't exist yet: this sync owns nothing in NetBox, so nothing "
                "can be updated or deleted."
            )
            self.logger.info(
                "Upgrading from plugin 0.1 (which tagged objects 'RY:<project name>')? Edit that tag before "
                f"syncing so it becomes this project's: slug {spec.slug!r}, name {spec.name!r}, "
                f"description {spec.description!r}."
            )
        target = NetBoxAdapter(tag=tag, user=user, import_components=cfg["import_components"], name="netbox")
        report, diff, deletes = self._plan(target, source, allow_deletes)
        summary = diff.summary()
        summary["delete"] = len(deletes)
        self.logger.info(f"Diff: {summary}")
        self._log_report(report, adopt_verb="Would adopt")
        self._log_changes(diff, "Would apply")
        scheduled = {(_label(m), m.nb_pk) for m in deletes}
        self._log_deletes(deletes, "Would delete", lambda m: target.preview_delete(m, scheduled))
        self.logger.warning("Dry run — nothing was written to NetBox.")
        return self._result(True, summary, report, spec.name)

    def _apply(self, source, spec, user, cfg, allow_deletes):
        ensure_custom_field(user)
        tag = ensure_ownership_tag(spec, user)
        self.logger.info(f"Railyard-owned objects carry the tag {tag.name!r} (slug {tag.slug!r}).")

        target = NetBoxAdapter(tag=tag, user=user, import_components=cfg["import_components"], name="netbox")
        report, diff, deletes = self._plan(target, source, allow_deletes)
        summary = diff.summary()
        summary["delete"] = len(deletes)
        self.logger.info(f"Diff: {summary}")
        self._log_changes(diff, "Applying")
        self._log_deletes(deletes, "Deleting", None)

        # Cables first, so a re-patched port is free before its new cable is created; then creates and
        # updates; then everything else, dependents before what they depend on.
        for model in deletes:
            if model.get_type() == "cable":
                model.delete()
        target.sync_from(source, flags=DiffSyncFlags.SKIP_UNMATCHED_DST, diff=diff)
        for model in deletes:
            if model.get_type() != "cable":
                model.delete()

        self._log_report(report, adopt_verb="Adopted")
        self.logger.info(f"Sync complete: {summary}")
        return self._result(False, summary, report, tag.name)

    @staticmethod
    def _result(dry_run, summary, report, tag_name):
        return {
            "dry_run": dry_run,
            "diff": summary,
            "tag": tag_name,
            "referenced": len(report.referenced),
            "conflicts": len(report.conflicts),
            "dependents_skipped": report.dependents_skipped,
            "adopted": len(report.adopted),
            "kept": len(report.kept),
        }

    def _log_list(self, heading: str, rows: list[str], *, level: str = "info") -> None:
        if not rows:
            return
        log = getattr(self.logger, level)
        log(f"{heading} ({len(rows)}):")
        for row in rows[: self._LOG_CAP]:
            log(f"  · {row}")
        if len(rows) > self._LOG_CAP:
            log(f"  · … and {len(rows) - self._LOG_CAP} more")

    def _log_report(self, report, *, adopt_verb: str) -> None:
        self._log_list("Not managed by this sync — used as they are", report.referenced)
        self._log_list("Skipped — would touch objects this sync doesn't own", report.conflicts, level="warning")
        if report.dependents_skipped:
            self.logger.warning(f"Also skipped {report.dependents_skipped} components/cables of skipped objects.")
        self._log_list(f"{adopt_verb} — existing components of devices this sync owns", report.adopted)
        self._log_list("Kept — not deleted, because NetBox would delete or change more", report.kept, level="warning")
        for warning in report.warnings:
            self.logger.warning(warning)

    def _log_deletes(self, deletes, verb: str, why_kept) -> None:
        if not deletes:
            return
        self.logger.info(f"{verb} ({len(deletes)}) objects this sync created that are no longer in Railyard:")
        for model in deletes:  # never capped: every delete is named before it happens
            line = f"  · delete: {model.get_type()} [{model.get_unique_id()}]"
            reasons = why_kept(model) if why_kept else []
            if reasons:
                line += f" — would be kept: {'; '.join(reasons)}"
            self.logger.info(line)

    # Per-object create/update/delete gets logged so a run reads as a changelog, not just a count.
    # Creates on a first sync can number in the thousands, so creates and updates are capped; deletes
    # are always listed in full so nothing is removed without being named first.
    _LOG_CAP = 250

    def _log_changes(self, diff, verb: str) -> None:
        from collections import defaultdict

        lines: dict[str, list[str]] = {"create": [], "update": [], "delete": []}
        counts: dict[str, dict[str, int]] = {a: defaultdict(int) for a in lines}

        def ident(el) -> str:
            return " ".join(f"{k}={v}" for k, v in (getattr(el, "keys", {}) or {}).items())

        def walk(elements) -> None:
            for el in elements:
                action = el.action
                if action in lines:
                    counts[action][el.type] += 1
                    line = f"{el.type} [{ident(el)}]"
                    if action == "update":
                        d = el.get_attrs_diffs()
                        old, new = d.get("-", {}), d.get("+", {})
                        changed = ", ".join(f"{k}: {old.get(k)!r}→{new.get(k)!r}" for k in new)
                        if changed:
                            line += f" — {changed}"
                    lines[action].append(line)
                walk(el.get_children())  # flat models have none, but recurse to be safe

        walk(diff.get_children())

        for action in ("create", "update", "delete"):
            rows = lines[action]
            if not rows:
                continue
            by_type = ", ".join(f"{n} {t}" for t, n in sorted(counts[action].items()))
            self.logger.info(f"{verb} — {action} ({len(rows)}): {by_type}")
            cap = len(rows) if action == "delete" else self._LOG_CAP
            for row in rows[:cap]:
                self.logger.info(f"  · {action}: {row}")
            if len(rows) > cap:
                self.logger.info(f"  · … and {len(rows) - cap} more {action}s")
