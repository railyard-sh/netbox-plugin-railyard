# netbox-plugin-railyard — repo guide for Claude

A **NetBox 4.5–4.7 plugin** that connects NetBox with **Railyard** (https://railyard.sh, repo
`railyard-sh/railyard`), the data-centre design tool, in both directions:

- **Sync from Railyard** (`RailyardSyncJob`, Railyard → NetBox): reconciles an estate into NetBox with
  **DiffSync**, from Railyard's server-side **NetBox sync document**.
- **Import into Railyard** (`RailyardImportJob`, NetBox → Railyard): reads NetBox sites into a snapshot and
  creates or refreshes a Railyard estate. Writes nothing to NetBox.

## The one rule that shapes everything: the Railyard side is railyard-sync

```
railyard-sync (dependency, repo railyard-sh/railyard-sync)   ← NO NetBox/Django. Client, sync-document adapter,
        ▲                                                       canonical models, importer, ownership tag, plans.
        │ imports                                               Shared with the `railyard-sync` CLI.
netbox_railyard/*.py   ← "glue": ORM target + ORM reader + Jobs + UI + NetBox-specific policy. Needs a live NetBox.
```

There is no Railyard core in this repo any more (0.1's `netbox_railyard/railyard/` moved to railyard-sync).
If a change is NetBox-API-agnostic (Railyard's API, the document's shape, the importer, ownership bookkeeping,
messages), make it in **railyard-sync** and bump the pin here; the plugin keeps only what needs NetBox's ORM:

| railyard-sync | what the plugin uses it for |
|---|---|
| `railyard_sync.client.RailyardClient`, `.errors` | `netbox_sync_document()` (sync), `get_project_with_revision`/`validate`/`put_project` (import, via `ImportRun`); typed errors incl. `RailyardPlanError` (402) |
| `railyard_sync.export.sync_document.SyncDocumentAdapter` | the sync's DiffSync source |
| `railyard_sync.export.models` (`TOP_LEVEL`) | the canonical models `target.py` subclasses |
| `railyard_sync.export.ownership` | `OwnershipReport`, `OwnershipMixin` (delete candidates, rename detection/re-keying), planned-change lines |
| `railyard_sync.export.policy` | `ownership_tag`, `insecure_url` (re-exported by `netbox_railyard.policy`) |
| `railyard_sync.export.devicetype_library` | `DeviceTypeSpec`, `DeviceTypeLibrary` (opt-in component templates) |
| `railyard_sync.dcim.netbox.NetBoxReader` | the import's reader; `loader.ORMReader` answers its `fetch`/`fetch_all` from the ORM |
| `railyard_sync.importer.run.ImportRun` | the import flow (fetch + same-source check, build, merge, check, save) |
| `railyard_sync.plans` | the 402 upgrade messages both jobs show |

**The dependency is pinned to a git commit** in `pyproject.toml` (`railyard-sync @ git+https://…@<sha>`)
until railyard-sync is on PyPI. `dev/setup.sh` reads the pin from there. When railyard-sync changes, push it,
move the pin, and re-run the integration suite.

`netbox_railyard/__init__.py` guards `from netbox.plugins import PluginConfig` so `policy.py` imports (and is
unit-tested) without NetBox.

See `netbox_railyard/CLAUDE.md` for the glue (target, reader, jobs, UI) and the hard-won NetBox lessons.

## Directory map

| Path | What it is |
|---|---|
| `netbox_railyard/{target,devicetypes}.py` | Sync target: NetBox ORM writes under the ownership rules. REST twin: `railyard_sync/export/netbox_rest.py` — keep them in step |
| `netbox_railyard/loader.py` | Import source: `ORMReader(NetBoxReader)` |
| `netbox_railyard/{jobs,import_job}.py` | `RailyardSyncJob` (+ shared `RailyardJob`, prerequisites), `RailyardImportJob` |
| `netbox_railyard/{config,policy,forms,views,urls,navigation}.py`, `templates/` | Settings, NetBox-free policy, trigger UI |
| `tests/` | NetBox-free suite (`test_policy.py`). `tests/integration/` needs a real NetBox (see below) |
| `tests/fixtures/sync/` | Sync documents written by Railyard's own exporter (`make_documents.py` regenerates them) |
| `dev/` | One-command local NetBox (netbox-docker overlay) — `dev/setup.sh`; `run-integration-tests.sh` |

## Sync semantics (Railyard → NetBox)

- **Source = the sync document.** `RailyardClient.netbox_sync_document(project, netbox_version=<this NetBox's
  major.minor>)` → `POST /api/projects/{id}/deliverables/netbox-sync`. Railyard's Go export writes it, so names,
  slugs (sites/locations carry a hash suffix), statuses, cable labels and the cabling plan are Railyard's; never
  re-derive them here. **Paid deliverable on hosted Railyard**: a 402 becomes `plans.deliverable_plan_message`
  in the job log; a self-hosted server with billing off allows it.
- **One-way.** Default is **create/update only**; **Allow deletes** also removes objects the sync created that
  are no longer in Railyard (otherwise they are logged as stale).
- **Ownership tag** (`ownership_tag`: slug `ry-<project id>-<hash of instance+id>`, name `RY:<project name>
  (<hash>)`, recognised by its exact description). Identical to the CLI's, so both manage the same objects.
  `load()` reads back **only** tagged objects; updates/deletes go by NetBox pk (`nb_id`) + tag. Untagged objects
  are never updated, deleted or adopted: existing shared objects (manufacturer/type/role/site/location/rack) are
  used as-is, conflicting devices/ports/cables (and device types whose slug is taken) are skipped and reported.
  Exception: same-named components on a device the sync owns are adopted. Read the README's "What the sync will
  and won't touch" before changing any of this; it is a promise to users.
- **Identity & renames.** Devices by **name**, plus the `railyard_id` custom field (the placement id, from the
  document's `railyard` identity). An owned device whose `railyard_id` now has another name is **renamed in
  place** (`OwnershipMixin.detect_renames` re-keys it before the diff; `apply_renames()` writes it first in a
  real run). Components by `(device, name)`; cables by their two terminations.
- **Design tags** on racks/devices: found or (real run, `extras.add_tag`) created by `jobs.ensure_user_tags`;
  owned objects carry exactly the ownership tag + their design tags.
- **Permissions.** View/add/change(/delete) on `policy.SYNC_MODELS` (incl. location), checked by the view and
  re-checked by the job; every query is `.restrict()`-ed to the job's user.
- **Targets.** Users pick an org from `railyard_org` + `railyard_allowed_orgs` (no free text) and, optionally, a
  project from `railyard_allowed_projects`; the job re-checks both.
- **Dry run is read-only** (no tag/custom field/design tag creation, rolled-back transaction). **A real run is
  atomic** and change-logged as the queuing user.
- **Ordering.** `models.TOP_LEVEL`: manufacturers → device types → roles → sites → locations → racks → devices
  → rear ports → front ports → interfaces → power → cables. A real run: renames, cable deletes, creates/updates,
  other deletes in reverse.

## Import semantics (NetBox → Railyard)

- `loader.ORMReader(user, url).load(site ids | None)` → `Snapshot`, field for field what railyard-sync's REST
  loader reads (`tests/integration/test_import.py` compares the two on the same NetBox). Then
  `ImportRun(client, snapshot, project=… | name=…, prefix="nb", allow_deletes, source_url=url)`, exactly the
  CLI's flow: dry run → report + diff + `/api/validate`; conflicts or check errors stop the save; an unchanged
  refresh is not saved; 412/409 become `ImportRefused`; a 402 `plan_limit` becomes `plans.plan_limit_message`.
- Ids are `nb-<kind>-<NetBox pk>` (same prefix as the CLI), so the CLI and the plugin can refresh each other's
  estates when they record the same NetBox URL (`netbox_url` setting, else the URL the form was used on).
- Needs `view` on `policy.IMPORT_MODELS` only; the Railyard token's user needs *editor* in the target org.

## Build / test

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"   # railyard-sync from GitHub at the pinned commit
pytest                    # NetBox-free suite
ruff check . && ruff format --check .
NETBOX_DIR=/path/to/netbox ./dev/run-integration-tests.sh   # tests/integration: NetBox + Postgres + Redis
```
CI runs the integration suite against **NetBox 4.5, 4.6 and 4.7** (`.github/workflows/ci.yml` matrix; keep it in
step with `PluginConfig.min_version`/`max_version`). The NetBox `configuration.py` needs `API_TOKEN_PEPPERS`
(the import tests read the REST API with a v2 token). Railyard is never contacted: the sync's client is a stub
serving `tests/fixtures/sync/` documents (mutated per test with `base.py` helpers, as Railyard would export
them), the import's a `FakeRailyard` behind the real `RailyardClient`.

## Local NetBox + deploying

- **Local end-to-end:** `./dev/setup.sh` clones netbox-docker next door, fetches railyard-sync at the pinned
  commit (or `RAILYARD_SYNC_DIR=…` for a local checkout) and overlays the plugin; then `docker compose build &&
  docker compose up -d` in the printed work dir. See `dev/README.md`.
- The sync only sees what Railyard's API serves: a Railyard instance must be **deployed** with the
  `netbox-sync` deliverable (railyard commit "feat: netbox-sync and nautobot-sync…", #1009).

## Provenance

What a sync creates is whatever Railyard's Go export writes (`railyard/backend/internal/export/netbox.go`,
`cabling.go`, `internal/deliverables/sync.go`); the document's shape lives in
`railyard_sync/export/sync_document.py` only. The import's mapping lives in `railyard_sync/importer/`.
