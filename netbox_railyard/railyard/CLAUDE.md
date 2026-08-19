# `netbox_railyard/railyard/` — the core (Railyard side)

Pure Python, **zero NetBox/Django imports**. This is the DCIM-agnostic half: it fetches a Railyard
project and turns it into the canonical DiffSync object graph. It is unit-tested here (no NetBox), and
is meant to be lifted out verbatim into a shared `railyard-diffsync` package for the future Nautobot
plugin. **If you need NetBox in here, you're in the wrong layer** — put it in `netbox_railyard/` instead.

## Files

| File | Responsibility |
|---|---|
| `client.py` | `RailyardClient` — the REST client. Auth `Authorization: Bearer ry_…`; org selected via `X-Org-Id: <org id>` (the id, not the slug). Endpoints: `GET /api/orgs`, `GET /api/projects`, `GET /api/projects/{id-or-slug}` (full Project JSON). A `session` is injectable (a `requests.Session`-shaped object) so tests never hit the network. Typed errors: `RailyardAuthError` (401/403), `RailyardNotFoundError` (404). |
| `project.py` | `Project` — a thin wrapper over the decoded Project JSON `dict`. Indexes DCs/rows/racks/catalogue and answers the same lookups the Go export uses: `resolve_dc` (rack.dcId, else its row's dcId), `device_type_by_key`, `iter_placements`, plus per-rack defaults. Field names are the JSON (camelCase) tags exactly. |
| `mappings.py` | Value maps ported **1:1** from the Go export: `slugify`, `width_inches` (≥700mm→23, else 19), `face_for_netbox` (rear/front; full→front), `interface_type`, `port_connector`, `outlet_type`, `inlet_type`, status/role/site defaults. Each fn cites its Go source line. |
| `cabling.py` | `build_cabling_plan` — a port of `buildCablingPlan` (cabling.go). Resolves `cables` + `powerLinks` into a plan of components (interfaces, front/rear ports, power ports/outlets) and cable links, synthesising interfaces for port-less devices exactly as Go does. Also **synthesises a cable label** from the two ends when Railyard supplies none (`_endpoint_label`). |
| `devicetype_library.py` | `DeviceTypeLibrary.resolve(catalogue_entry)` → a `DeviceTypeSpec`. Fetches `device-types/<Manufacturer>/<Model>.yaml` from netbox-community/devicetype-library (pinned `ref`, in-memory + optional on-disk cache), falling back to a minimal spec from the Railyard catalogue. The fetcher is injectable for tests. |
| `models.py` | The **canonical** `DiffSyncModel`s (the shared diff surface): `Manufacturer, DeviceType, DeviceRole, Site, Rack, Device, Interface, RearPort, FrontPort, PowerOutlet, PowerPort, Cable`. Each DCIM target subclasses these and adds create/update/delete — identical `_identifiers`/`_attributes` keep the diff aligned across backends. `TOP_LEVEL` is the create order. |
| `source.py` | `RailyardAdapter(Adapter).load()` — the Python analogue of `netbox.go`'s builder. Walks the project (sites/racks/devices), resolves device types via `DeviceTypeLibrary`, then `_load_cabling()` turns the cabling plan into models. Records `unresolved`/`warnings`. |
| `errors.py` | The typed client exceptions. |

## Design decisions worth knowing before you edit

- **The Go export is the authority.** `mappings.py` and `cabling.py` mirror
  `railyard/backend/internal/export/{netbox.go,cabling.go}`; keep them in lockstep, and re-derive from
  there (not from memory) when Railyard changes.
- **Device types are lean.** `models.DeviceType` carries identity + u_height/is_full_depth/part_number;
  it does **not** carry component templates into the diff. The cabling models create the specific ports
  the wiring needs. The library YAML's full component lists are captured on the model (`components`) only
  for a future opt-in "import full template" mode.
- **The catalogue `key` is the join.** `Placement.deviceTypeRef` → catalogue entry by exact `key`, and
  that `key` is the devicetype-library slug — the handle to the YAML file.
- **Determinism.** Everything is emitted in project order (racks, then placements), so device names,
  synthesised interface names (`iface1`, `iface2`, …), and cable identities are stable across runs —
  which is what makes the sync idempotent.
- **Ports vs cables.** A cable terminates on a *component*, never a bare device. `cabling.py` resolves
  each end to an interface / front-port / rear-port / power-port / power-outlet, emits every component a
  cable touches, and synthesises an interface for a port-less device (e.g. a plain server).

## Testing

The whole layer is exercised without NetBox: `client.py` via an injected fake session, `source.py`
against `tests/fixtures/example-project.json` and an inline `cabled_project` fixture (switch + port-less
server + PDU power link), `mappings.py`/`devicetype_library.py` directly. Add tests here, not in the
glue — the glue needs a live NetBox.
