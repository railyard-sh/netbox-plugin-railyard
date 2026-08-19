"""``RailyardSyncJob`` — the NetBox background Job that runs the sync.

It shows up in NetBox's core Jobs UI (progress, log, result). It loads the Railyard project into the
source adapter and the scoped NetBox state into the target adapter, then reconciles with DiffSync —
create/update only by default (``SKIP_UNMATCHED_DST``), or a full mirror when ``allow_deletes`` is set.
"""

from __future__ import annotations

from diffsync.enum import DiffSyncFlags
from netbox.jobs import JobRunner

from .config import railyard_settings
from .railyard.client import RailyardClient
from .railyard.devicetype_library import DeviceTypeLibrary
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


class RailyardSyncJob(JobRunner):
    class Meta:
        name = "Railyard → NetBox sync"

    def run(self, *, project, org=None, dry_run=False, allow_deletes=False, **kwargs):
        cfg = railyard_settings()
        if not cfg["token"]:
            raise ValueError("Set 'railyard_token' in PLUGINS_CONFIG['netbox_railyard'] first.")

        self.logger.info(f"Fetching Railyard project {project!r}…")
        client = RailyardClient(cfg["url"], cfg["token"], org=org or cfg["org"])
        doc = client.get_project(project)

        dtl = DeviceTypeLibrary(ref=cfg["devicetype_library_ref"], cache_dir=cfg["devicetype_cache_dir"])
        source = RailyardAdapter(doc, devicetype_library=dtl, name="railyard")
        source.load()
        for warning in source.warnings:
            self.logger.warning(warning)

        site_names = {s.name for s in source.get_all(source.site)}
        ensure_custom_field()

        target = NetBoxAdapter(
            site_names=site_names, import_components=cfg["import_components"], name="netbox"
        )
        target.load()

        flags = DiffSyncFlags.CONTINUE_ON_FAILURE
        if not allow_deletes:
            flags |= DiffSyncFlags.SKIP_UNMATCHED_DST

        diff = target.diff_from(source, flags=flags)
        summary = diff.summary()
        self.logger.info(f"Diff: {summary}")

        if dry_run:
            self.logger.warning("Dry run — no changes were applied.")
            return {"dry_run": True, "diff": summary, "sites": sorted(site_names)}

        target.sync_from(source, flags=flags)
        self.logger.info(f"Sync complete: {summary}")
        return {"dry_run": False, "diff": summary, "sites": sorted(site_names)}
