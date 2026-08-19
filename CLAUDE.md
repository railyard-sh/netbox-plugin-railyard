# netbox-plugin-railyard — repo guide for Claude

A **NetBox 4.6+ plugin** that syncs a **Railyard** project (racks, devices, structured cabling, power)
into NetBox using **DiffSync**. Railyard (https://railyard.sh, repo `railyard-sh/railyard`) is the
design tool and the **source of truth**; this plugin is a one-way **Railyard → NetBox** reconciler. A
sibling `nautobot-plugin-railyard` (Nautobot SSoT) is planned and will reuse the core here.

## The one rule that shapes everything: three layers, one dependency direction

```
netbox_railyard/railyard/   ← "core": pure Python, NO netbox/django imports. Unit-tested here.
        ▲
        │ imports
netbox_railyard/*.py         ← "glue": NetBox ORM target adapter + Job + UI. Needs a live NetBox.
```

`netbox_railyard/railyard/` must **never** import Django/NetBox — it is meant to be lifted out verbatim
into a shared `railyard-diffsync` package so the Nautobot plugin reuses it. If you reach for NetBox in
there, you're in the wrong layer. The `PluginConfig` import in `netbox_railyard/__init__.py` is guarded
(`try: from netbox.plugins import PluginConfig`) precisely so importing the core outside NetBox works.

See the per-layer guides:
- `netbox_railyard/railyard/CLAUDE.md` — the extractable core (client, models, source, mappings, cabling).
- `netbox_railyard/CLAUDE.md` — the NetBox glue (target adapter, job, UI) + the hard-won 4.6 lessons.

## Directory map

| Path | What it is |
|---|---|
| `netbox_railyard/railyard/` | **Core** — Railyard API client, canonical DiffSync models, source adapter, Go-parity mappings, cabling/power planner, device-type-library resolver. |
| `netbox_railyard/{target,devicetypes,jobs,config,forms,views,urls,navigation}.py` | **Glue** — NetBox ORM writes, the `RailyardSyncJob`, and the trigger UI. |
| `tests/` | Core suite — needs **no NetBox** (injected client transport; source runs on `fixtures/example-project.json`). |
| `dev/` | One-command local NetBox (netbox-docker overlay) for end-to-end testing — `dev/setup.sh`. |

## Sync semantics (what actually happens)

- **One-way, Railyard → NetBox.** Default is **create/update only** (`DiffSyncFlags.SKIP_UNMATCHED_DST`);
  the trigger form's **Allow deletes** switch drops that flag for a full mirror.
- **Ownership tag.** Every object the sync creates/adopts is tagged **`RY:<project name>`**, and the
  target adapter loads back **only** tagged objects. Consequences: re-runs diff to **no-change**
  (idempotent), operator-added objects are never touched, and a mirror-delete only ever removes
  Railyard-owned objects. Creates are get-or-create, so the first run over pre-existing data **adopts
  and tags** it instead of duplicating.
- **Identity.** Devices by **name** (matching the Go export's globally-de-duped names) + a `railyard_id`
  custom field (the placement id) for rename-stable matching; components by `(device, name)`; cables by
  their two terminations.
- **Cable labels.** Railyard labels are kept; when absent, a label is synthesised from the ends —
  data `A:port <-> B:port`, power `device:PSU -> pdu:OUT`.
- **Ordering.** `models.TOP_LEVEL` encodes create-dependencies: manufacturers → device types → roles →
  sites → racks → devices → rear ports → front ports → interfaces → power → cables.
- **Logging.** The Job logs each create/update/delete (per-type summary + one line per object, with
  field-level before→after on updates), capped so a first sync doesn't flood the log.

## Device types & the netbox-community dependency

Device types are the **netbox-community** ones, resolved by the Railyard catalogue `key` (== the
devicetype-library **slug**). But they are created **lean** — manufacturer/model/slug/u_height/
is_full_depth/part_number only, matching Railyard's Go export — and the cabling models create **exactly
the ports the cabling needs**, so nothing double-creates. Importing the library's full component
templates is an opt-in future mode (`import_components`, off by default).

## Build / test

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest          # core suite — NO NetBox needed
ruff check .
```
Target runtime is **NetBox 4.6+ / Python 3.12+ / Django 6**. The glue can only be exercised inside a
real NetBox — keep it thin, push logic into the tested core.

## Local NetBox + deploying

- **Local end-to-end:** `./dev/setup.sh` clones netbox-docker next door and overlays the plugin; then
  `docker compose build && docker compose up -d` in the printed work dir. See `dev/README.md`. Default
  login is what you `createsuperuser`; the sync needs `RAILYARD_TOKEN` in that dir's `.env`.
- **The plugin only sees data the REST API returns.** Railyard's `GET /api/projects/{id}` now serves
  live collab-room state (fixed in the railyard repo, commit "api: GET … serves live collab state") —
  but a Railyard instance must be **deployed** with that change for edits to show up promptly. Prod/dev
  deploy runs on the lab box `kc-ry-01` (`make deploy-dev`), not from here.

## Provenance — the Go export is the authority

The Railyard→NetBox object/field mapping is already implemented in Go; the Python `mappings.py` /
`cabling.py` are a faithful port. If the Go export changes, update the port to match. Reference:
`railyard/backend/internal/export/{netbox.go,cabling.go}`, `.../model/model.go`,
`railyard/schema/project.schema.json`.
