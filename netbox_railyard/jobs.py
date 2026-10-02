"""``RailyardSyncJob`` — the NetBox background Job that runs the sync.

It shows up in NetBox's core Jobs UI (progress, log, result). It loads the Railyard project into the
source adapter and the scoped NetBox state into the target adapter, then reconciles with DiffSync —
create/update only by default (``SKIP_UNMATCHED_DST``), or a full mirror when ``allow_deletes`` is set.
"""

from __future__ import annotations

from core.exceptions import JobFailed
from diffsync.enum import DiffSyncFlags
from netbox.jobs import JobRunner

from .config import railyard_settings, railyard_token, sync_targets
from .policy import TargetNotAllowed, insecure_url
from .railyard.client import RailyardClient
from .railyard.devicetype_library import DeviceTypeLibrary
from .railyard.errors import RailyardAuthError, RailyardNotFoundError, RailyardTokenRejectedError
from .railyard.mappings import slugify
from .railyard.source import RailyardAdapter
from .target import CUSTOM_FIELD, NetBoxAdapter


def ensure_custom_field() -> None:
    """Ensure the ``railyard_id`` text custom field exists on dcim.device (stable identity across
    re-syncs). Written defensively because the CustomField relation was renamed across NetBox minors
    (``content_types`` -> ``object_types``)."""
    from django.contrib.contenttypes.models import ContentType
    from extras.models import CustomField

    cf, _ = CustomField.objects.get_or_create(
        name=CUSTOM_FIELD,
        defaults={"type": "text", "label": "Railyard ID", "description": "Railyard placement id"},
    )
    ct = ContentType.objects.get(app_label="dcim", model="device")
    relation = getattr(cf, "object_types", None) or getattr(cf, "content_types", None)
    if relation is not None and not relation.filter(pk=ct.pk).exists():
        relation.add(ct)


def ensure_project_tag(project_name: str):
    """Get-or-create the ``RY:<project>`` tag that marks every object this sync owns. Keyed by slug so
    the tag is stable; the display name follows a project rename."""
    from extras.models import Tag

    name = f"RY:{project_name}"
    slug = slugify(f"ry-{project_name}") or "ry-railyard"
    tag, _ = Tag.objects.get_or_create(slug=slug, defaults={"name": name, "color": "1f8bff"})
    if tag.name != name:
        tag.name = name
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
        except RailyardTokenRejectedError as exc:
            self._fail(f"{exc} Ask a NetBox administrator to rotate the token.")
        except (RailyardAuthError, RailyardNotFoundError, ValueError) as exc:
            self._fail(str(exc))

        dtl = DeviceTypeLibrary(ref=cfg["devicetype_library_ref"], cache_dir=cfg["devicetype_cache_dir"])
        source = RailyardAdapter(doc, devicetype_library=dtl, name="railyard")
        source.load()
        for warning in source.warnings:
            self.logger.warning(warning)

        ensure_custom_field()
        tag = ensure_project_tag(source.project.name or project)
        self.logger.info(f"Tracking Railyard-owned objects with tag {tag.name!r}.")

        target = NetBoxAdapter(tag=tag, import_components=cfg["import_components"], name="netbox")
        target.load()

        flags = DiffSyncFlags.CONTINUE_ON_FAILURE
        if not allow_deletes:
            flags |= DiffSyncFlags.SKIP_UNMATCHED_DST

        diff = target.diff_from(source, flags=flags)
        summary = diff.summary()
        self.logger.info(f"Diff: {summary}")
        verb = "Would apply" if dry_run else "Applying"
        self._log_changes(diff, verb)

        if dry_run:
            self.logger.warning("Dry run — no changes were applied.")
            return {"dry_run": True, "diff": summary, "tag": tag.name}

        target.sync_from(source, flags=flags)
        self.logger.info(f"Sync complete: {summary}")
        return {"dry_run": False, "diff": summary, "tag": tag.name}

    # Per-object create/update/delete gets logged so a run reads as a changelog, not just a count.
    # Creates on a first sync can number in the thousands, so each action is capped; updates show the
    # field-level before→after.
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
            for row in rows[: self._LOG_CAP]:
                self.logger.info(f"  · {action}: {row}")
            if len(rows) > self._LOG_CAP:
                self.logger.info(f"  · … and {len(rows) - self._LOG_CAP} more {action}s")
