# netbox-plugin-railyard

A [NetBox](https://netbox.dev) plugin that syncs a **[Railyard](https://railyard.sh)** data-centre
design into NetBox — **racks, devices and cables** — using [DiffSync](https://github.com/networktocode/diffsync).

Railyard is the *design* tool (sketch an estate: locations → data centres → rows → racks → devices,
plan structured cabling and power). This plugin reconciles a Railyard **project** into NetBox as the
system of record, so the sketch becomes real DCIM data without a manual CSV import.

> Status: **early development (0.1).** The Railyard-side core (API client, canonical model, source
> adapter, mapping, device-type resolution) is implemented and unit-tested. The NetBox-side target
> adapter, sync Job and UI are landing next — see [Roadmap](#roadmap).

## How it works

```
Railyard REST API  ──►  RailyardAdapter (source)  ─┐
                                                    ├─►  DiffSync diff ──►  create/update[/delete]
NetBox ORM         ──►  NetBoxAdapter (target)   ──┘
```

- Railyard is the **source of truth**. The sync runs one way: **Railyard → NetBox**.
- Default is **create/update only** (DiffSync `SKIP_UNMATCHED_DST`) — it never deletes objects an
  operator added in NetBox. An explicit **"Allow deletes"** toggle turns the sync into a full mirror.
- **Device types come from the [netbox-community device-type library](https://github.com/netbox-community/devicetype-library)**.
  When a device needs a type NetBox doesn't have yet, the plugin fetches the matching YAML (by the
  Railyard catalogue `key`, which is the library slug) and creates the Manufacturer + Device Type
  (with its component templates) before creating the device — falling back to a minimal type built
  from the Railyard catalogue when the library has no match.

## Architecture (three layers, one repo)

| Path | Layer | NetBox import? |
|---|---|---|
| `netbox_railyard/railyard/` | **Railyard core** — API client, canonical DiffSync models, source adapter, Go-parity mappings, device-type-library resolver | **No** — pure Python, unit-tested, extractable to a shared `railyard-diffsync` package for the future Nautobot plugin |
| `netbox_railyard/target.py`, `devicetypes.py` | **NetBox target** — ORM adapter + create/update/delete | Yes |
| `netbox_railyard/jobs.py`, `navigation.py`, `forms.py`, `views.py` | **NetBox glue** — the `JobRunner`, the trigger form and the sidebar entry | Yes |

The mappings in `railyard/mappings.py` and `railyard/cabling.py` are a faithful Python port of
Railyard's Go export engine (`backend/internal/export/netbox.go` + `cabling.go`), so the objects this
plugin creates match Railyard's own NetBox CSV export.

## Install

```bash
pip install netbox-plugin-railyard        # (once published)
# or, from a checkout, into the NetBox virtualenv:
pip install -e /opt/netbox-plugin-railyard
```

Enable it in `configuration.py`:

```python
PLUGINS = ["netbox_railyard"]

PLUGINS_CONFIG = {
    "netbox_railyard": {
        "railyard_url": "https://railyard.sh",   # your Railyard base URL
        "railyard_token": "ry_...",              # a Railyard personal access token
        "railyard_org": "my-org",                # optional default org (id, slug or name)
        # device-type library source (pinned):
        "devicetype_library_ref": "master",
    },
}
```

Requires **NetBox 4.6+** (Python 3.12+, Django 6).

## Usage

Run **Plugins → Railyard → Run sync**, pick the org + project, choose *dry run* / *allow deletes*, and
submit. The run is a native NetBox background Job — watch progress and the diff on the Job detail page.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest            # runs the NetBox-independent core suite
ruff check .
```

The core test-suite (`tests/`) needs **no NetBox install** — it exercises the client (with an injected
transport), the mappings, and the source adapter against `tests/fixtures/example-project.json`.

## Roadmap

- [x] Railyard API client (PAT auth, `X-Org-Id`, list orgs/projects, fetch project JSON)
- [x] Canonical DiffSync models + Railyard source adapter (sites, racks, manufacturers, device types, roles, devices, cables, power)
- [x] Go-parity mappings + device-type-library resolver
- [ ] NetBox ORM target adapter (create/update/delete)
- [ ] `RailyardSyncJob` (`JobRunner`) + trigger form + sidebar UI
- [ ] Integration tests against a live NetBox
- [ ] `nautobot-plugin-railyard` (Nautobot SSoT) reusing this core

## Licence

Apache-2.0. Device-type definitions are © their respective vendors, via the Apache-2.0
[netbox-community/devicetype-library](https://github.com/netbox-community/devicetype-library).
