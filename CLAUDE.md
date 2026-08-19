# netbox-plugin-railyard — repo guide for Claude

A **NetBox 4.6+ plugin** that syncs a **Railyard** project (racks, devices, cables) into NetBox using
**DiffSync**. Railyard (https://railyard.sh, repo `railyard-sh/railyard`) is the design tool and the
**source of truth**; this plugin is a one-way **Railyard → NetBox** reconciler. A sibling
`nautobot-plugin-railyard` (Nautobot SSoT) will come later and reuse the core here.

## The one rule that shapes everything: three layers, one dependency direction

```
netbox_railyard/railyard/   ← "core": pure Python, NO netbox/django imports. Unit-tested here.
        ▲
        │ imports
netbox_railyard/*.py         ← "glue": NetBox target adapter + Job + UI. Needs a live NetBox to run.
```

`netbox_railyard/railyard/` must **never** import Django/NetBox — it is meant to be lifted out verbatim
into a shared `railyard-diffsync` PyPI package so the Nautobot plugin reuses it. If you need NetBox in
there, you're in the wrong layer.

## Core layer (`netbox_railyard/railyard/`)

| File | What it is |
|---|---|
| `client.py` | `RailyardClient` — talks to the Railyard REST API. Auth `Authorization: Bearer ry_…`; org selected via `X-Org-Id: <org id>`. Endpoints: `GET /api/orgs`, `GET /api/projects`, `GET /api/projects/{id-or-slug}` (the full Project JSON). Inject a `transport` for tests. |
| `project.py` | Thin typed helpers over the Project JSON dict (index DCs/rows/racks/catalogue, `resolve_dc`, `device_type_by_key`, iterate placements). Mirrors `railyard/backend/internal/model`. |
| `mappings.py` | **Faithful port** of `railyard/backend/internal/export/netbox.go` + `cabling.go` helpers: `slugify`, `width_inches` (mm→19/23), `face_for_netbox`, `interface_type`, `port_connector`, `outlet_type`, `inlet_type`, status defaults. Keep these in lockstep with the Go originals. |
| `cabling.py` | Port of `buildCablingPlan` (cabling.go): resolves `cables` + `powerLinks` into cable specs with resolved terminations (interface / front-port / rear-port / power-port / power-outlet), synthesizing interfaces for port-less devices exactly as the Go export does. |
| `models.py` | The **canonical** `DiffSyncModel`s: `Manufacturer, DeviceType, DeviceRole, Site, Rack, Device, Cable`. Both DCIM targets subclass these and add CRUD, so the diff surface is shared. `top_level` order encodes create-dependencies. |
| `devicetype_library.py` | Resolves a Railyard catalogue entry → a full device-type spec: fetch `device-types/<Manufacturer>/<Model>.yaml` from netbox-community/devicetype-library (pinned ref, on-disk cache), else build a minimal spec from the Railyard catalogue. |
| `source.py` | `RailyardAdapter(Adapter).load(project)` — walks the Project JSON and populates every canonical model. This is the Python analogue of `netbox.go`'s builder. |

## Glue layer (`netbox_railyard/`) — needs NetBox to run

| File | What it is |
|---|---|
| `__init__.py` | `PluginConfig` (`RailyardConfig`): `name="netbox_railyard"`, `min_version`/`max_version`, `default_settings`. |
| `target.py` | `NetBoxAdapter` + `NetBox*` models subclassing the canonical ones, doing create/update/delete via the NetBox **ORM** (`dcim.models`). Stamps a `railyard_id` custom field for stable identity. |
| `devicetypes.py` | Ensures a Manufacturer + DeviceType (with component templates) exists in NetBox from a `devicetype_library` spec, before a device is created. |
| `jobs.py` | `RailyardSyncJob(JobRunner)` — loads both adapters, runs `target.sync_from(source, flags=…)`. Default `SKIP_UNMATCHED_DST` (no deletes); `allow_deletes` drops it. |
| `navigation.py` / `forms.py` / `views.py` / `urls.py` | Sidebar entry + trigger form that enqueues the Job and redirects to the Job detail page. |

## Provenance of the mappings (read this before touching `mappings.py`/`cabling.py`)

The Railyard→NetBox object/field mapping is **already implemented in Go** and is the authoritative
reference. Port from, and diff against:
- `railyard/backend/internal/export/netbox.go` — sites/racks/device-types/roles/devices + slugify/width/face/dedup.
- `railyard/backend/internal/export/cabling.go` — the cabling/power planner + media→type maps.
- `railyard/backend/internal/model/model.go` — the Project struct (the JSON contract).
- `railyard/schema/project.schema.json` — the JSON Schema.
If the Go export changes, update the Python port to match.

## Sync semantics

- One-way, Railyard → NetBox. Default **create/update only** (`DiffSyncFlags.SKIP_UNMATCHED_DST`); the
  trigger form's **Allow deletes** switch removes that flag for a true mirror.
- Device identity is by **name** (matching the Go export's globally-de-duped names). A `railyard_id`
  custom field is stamped for future rename-safe matching. Renames currently read as create+leave-old
  in no-delete mode — documented limitation.
- **Create ordering** is encoded by `models.top_level`: manufacturers → device types → roles → sites →
  racks → devices → cables. Cable terminations are ensured get-or-create on the device.

## Build / test

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest          # core suite — NO NetBox needed (client transport is injected; source runs on fixtures)
ruff check .
```
Target runtime is **NetBox 4.6+ / Python 3.12+ / Django 6**. The glue layer (`target.py`, `jobs.py`,
UI) can only be exercised inside a real NetBox — keep it thin and push logic into the tested core.

## Gotchas

- The core stays import-clean of NetBox — CI/tests import only `netbox_railyard.railyard.*`.
- Device types are the **netbox-community** ones (full component templates), not Railyard's partial
  port list; devices inherit components from the type. Cables ensure their specific terminations exist.
- The Railyard catalogue `key` == the devicetype-library **slug** — that's the join to the YAML file.
