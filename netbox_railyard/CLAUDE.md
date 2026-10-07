# `netbox_railyard/` — the NetBox glue

The NetBox-specific half: it imports `dcim.models` and the NetBox plugin framework, so it **only runs inside a
NetBox 4.5–4.7 instance**; `tests/integration/` exercises it under NetBox's test runner. Keep it thin: anything
that doesn't need NetBox's ORM belongs in **railyard-sync** (`railyard_sync.*`), which the CLI shares.

## Files

| File | Responsibility |
|---|---|
| `__init__.py` | `PluginConfig` (`RailyardConfig`): `name="netbox_railyard"`, `min_version="4.5.0"`, `max_version="4.7.99"` (the CI matrix), `default_settings`. The `PluginConfig` import is **guarded** so `policy.py` imports without NetBox. |
| `config.py` | `railyard_settings()` — reads `PLUGINS_CONFIG["netbox_railyard"]` (url, token/token file, org, devicetype ref/cache, `import_components`, `netbox_url`); `sync_targets()` (the org/project allowlist) and `railyard_token()`. |
| `policy.py` | **NetBox-free** decisions, unit-tested in `tests/`: `SYNC_MODELS` + `required_permissions` (sync), `IMPORT_MODELS` + `import_permissions` (import), `SyncTargets`, `validate_estate_name`, `read_token`. Re-exports railyard-sync's `ownership_tag`/`insecure_url`. |
| `target.py` | Sync target: `NetBoxAdapter(OwnershipMixin, Adapter)` + the `NetBox*` models (subclass `railyard_sync.export.models`, add ORM create/update/delete), the ownership pre-pass (`reconcile`), renames (`apply_renames`) and the deletion guard (`deletion_blockers`). The heavy file — rules below. |
| `devicetypes.py` | `create_device_type` — a lean DeviceType from a `DeviceTypeSpec` (0U allowed), plus the library's templates with `import_components`. |
| `loader.py` | Import source: `ORMReader(NetBoxReader)` answers railyard-sync's reader from the ORM in the REST shape. |
| `jobs.py` | `RailyardJob` (shared: `_fail`, user/permission re-check, Railyard client, list logging), `RailyardSyncJob`, `railyard_sync_logs()` (forwards railyard-sync's INFO+ logs into the job log), the sync's prerequisites (`ensure_custom_field`, `find_/ensure_ownership_tag`, `ensure_user_tags`) and `netbox_release()`. |
| `import_job.py` | `RailyardImportJob`: allowlist + permission re-check, `ORMReader`, then railyard-sync's `ImportRun` with the CLI's rules; 402 → `plan_message`. |
| `forms.py`/`views.py`/`urls.py`/`navigation.py`/`templates/` | Two sidebar entries (*Sync from Railyard* at `sync/`, *Import into Railyard* at `import/`) + forms that enqueue the jobs and redirect to their result pages. |

## `target.py` rules (read before editing)

- **Same semantics as `railyard_sync/export/netbox_rest.py`** (the REST twin the CLI uses). A behaviour change
  goes in both; API-agnostic logic goes in `railyard_sync/export/ownership.py`.
- **Every model needs create *and* update *and* delete.** DiffSync's base `update()` is a **silent no-op** on
  the ORM — a missing `update()` means the field diff shows every run but never persists. Every attribute in a
  canonical model's `_attributes` must be written by `create` and `update` and read back by `load()`, or the
  sync re-"updates" forever (the idempotence tests catch this).
- **Never adopt, never touch untagged objects.** `create` refuses if the natural key already exists
  (`ObjectNotCreated`, which rolls the run back); `reconcile()` runs first and drops such objects from the
  source — shared ones (manufacturer/type/role/site/location/rack) are *referenced* as-is, devices/ports/cables
  become *conflicts*. The one exception: a same-named component on a device the sync owns is adopted.
- **Writes go by pk + tag.** `load()` stores each object's NetBox pk on the model (`nb_id`, the name the shared
  `OwnershipMixin` expects); `update`/`delete` re-fetch with `_get_owned()`/`_owned()` =
  `.restrict(user, action).filter(tags=tag, pk=nb_id)`, never by name. Creates/updates re-check object-level
  permissions with `_require()`.
- **`load()` is tag-scoped** across **all** model types incl. locations, components and cables. If you add a
  model, load it here too or it will perpetually re-"create".
- **Renames before the diff.** `reconcile()` calls `detect_renames()` first (re-keys an owned device and its
  components/cables to its new Railyard name); the job calls `apply_renames()` before `sync_from`.
- **Deletes are the job's, not DiffSync's.** The diff runs with `SKIP_UNMATCHED_DST`; the job deletes
  `delete_candidates()` itself (cables first, then the rest in reverse `TOP_LEVEL` order), and
  `deletion_blockers()` keeps anything whose deletion NetBox would protect, cascade or SET_NULL into untagged
  objects. Add new bookkeeping models to `_BOOKKEEPING` only if nobody enters them by hand.
- **A racked device takes its rack's location** (NetBox's `Device.save`). In a rack used as-is, `reconcile()`
  sets the device's location to that rack's real one so the diff doesn't fight NetBox.
- Cables are created only between free ports on owned devices. Build them with `a_terminations`/
  `b_terminations` then `save()`.

## `loader.py` rules

- It answers `NetBoxReader.fetch`/`fetch_all` for exactly the endpoints and filters the reader asks for, with
  the REST representation's fields (choice fields as values, blank as `None`; nested objects as `{"id"}` or
  `{"id", "name"}`; 4.5+ `rear_ports` mapping lists). Anything else raises — so a reader change in railyard-sync
  fails the integration suite instead of silently reading less.
- **Never override the reader's own methods**: `NetBoxReader._site`, `_rack`, `_component`… are its mappers.
  Serialisers here are `_api_*`.
- Every queryset is `.restrict(user, "view")`, like the REST API. `test_import.ReaderTests` compares the ORM
  snapshot with railyard-sync's REST loader reading the same NetBox through Django's test client: keep it green.

## Hard-won NetBox / netbox-docker facts

These cost real debugging time — don't rediscover them:

- **Install with `uv`, not pip, in netbox-docker.** netbox-docker 5.x ships a uv-managed venv on Python 3.14
  with no `pip` (and no `git`): `dev/overlay/Dockerfile-Plugins` installs railyard-sync from the build context.
- **FrontPort has no `rear_port` FK (4.5+).** Front↔rear is a **`dcim.PortMapping`** row (`device, front_port,
  rear_port, *_position`, unique per front position and per rear position). Create the FrontPort with
  `positions=1`, then a PortMapping. Swapping two ports' rear ports needs the clashing (owned) mapping removed
  first (`_map_front_port`).
- **Pydantic eats underscore class attributes.** A `DiffSyncModel` is a Pydantic model, so a class attribute
  like `_nb_model` is a *private attribute* and won't resolve on the subclass. Use module-level helpers/maps.
- **`JobRunner.logger` is a plain `logging.Logger`** — no `.success()`.
- **DiffSync's own per-object logs go to structlog**, not the Job log — hence `_log_changes()` logs the diff.
- **Field names:** Device's role is `role`; status/face are plain strings; a 0U device has `position=None` and
  **blank** `face`; 0U device types (`u_height=0`) are valid.
- **Custom-field relation moved** across minors: `CustomField.object_types` vs `content_types` —
  `ensure_custom_field()` handles both.
- **v2 API tokens need `API_TOKEN_PEPPERS`** in `configuration.py` (the import tests use one).

## Running it

The jobs appear at **Plugins → Railyard → Sync from Railyard** and **→ Import into Railyard**; they enqueue
`RailyardSyncJob` / `RailyardImportJob` (visible in NetBox's core Jobs UI). From a shell in `dev/`:
`docker compose exec netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell` and enqueue a job
with `user=`, or build the adapters directly.
