# `netbox_railyard/` — the NetBox glue

The NetBox-specific half: it imports `dcim.models` and the NetBox plugin framework, so it **only runs
inside a NetBox 4.6+ instance** and can't be unit-tested here — it's written correct-by-construction and
exercised against a live NetBox via `dev/`. Keep it thin; anything DCIM-agnostic belongs in
`netbox_railyard/railyard/`.

## Files

| File | Responsibility |
|---|---|
| `__init__.py` | `PluginConfig` (`RailyardConfig`): `name="netbox_railyard"`, `min_version="4.6.0"`, `default_settings`. The `from netbox.plugins import PluginConfig` import is **guarded** so the core imports without NetBox. |
| `config.py` | `railyard_settings()` — reads `PLUGINS_CONFIG["netbox_railyard"]` (url, token, org, devicetype ref/cache, import_components). |
| `target.py` | `NetBoxAdapter` + the `NetBox*` models (subclass the canonical ones, add ORM create/update/delete). The heavy file — see the rules below. |
| `devicetypes.py` | `ensure_manufacturer` / `ensure_device_type` — get-or-create a lean DeviceType from a `DeviceTypeSpec` before a device needs it. |
| `jobs.py` | `RailyardSyncJob(JobRunner)` — the sync. Also `ensure_custom_field()` (the `railyard_id` CF on dcim.device) and `ensure_project_tag()` (the `RY:<project>` tag), and `_log_changes()` (the per-object changelog). |
| `forms.py`/`views.py`/`urls.py`/`navigation.py`/`templates/` | Sidebar entry + trigger form that enqueues the Job and redirects to its result page. |

## `target.py` rules (read before editing)

- **Every model needs create *and* update *and* delete.** DiffSync's base `update()` is a **silent
  no-op** on the ORM — a missing `update()` means the field diff shows every run but never persists
  (this exact bug ate cable labels). If a model has attributes that can drift, it needs a real `update()`.
- **create = get-or-create + tag.** Creates adopt an existing object by natural key and then
  `obj.tags.add(adapter.tag)`, so the first sync over pre-existing data tags rather than duplicates.
- **`load()` is tag-scoped.** It reads back only objects carrying `adapter.tag` (`.filter(tags=tag)`),
  across **all** model types incl. components and cables — that's what makes re-syncs diff to no-change
  and keeps deletes off operator-owned objects. If you add a model, load it here too or it will
  perpetually re-"create".
- Cables are matched by their two terminations; `_existing_cable()` finds the NetBox cable via a
  resolved termination's `.cable`. Build cables with `a_terminations`/`b_terminations` then `save()`.

## Hard-won NetBox 4.6 / netbox-docker facts

These cost real debugging time — don't rediscover them:

- **Install with `uv`, not pip.** netbox-docker 5.x ships a uv-managed venv on **Python 3.14** with no
  `pip` in it. `dev/overlay/Dockerfile-Plugins` uses `uv pip install --python /opt/netbox/venv/bin/python`.
- **FrontPort has no `rear_port` FK.** 4.6 couples front↔rear through a **`dcim.PortMapping`** row
  (`device, front_port, rear_port, *_position`). Create the FrontPort with `positions=1`, then a
  PortMapping — passing `rear_port=`/`rear_port_position=` to `FrontPort()` raises `TypeError`.
- **Pydantic eats underscore class attributes.** A `DiffSyncModel` is a Pydantic model, so a class
  attribute like `_nb_model` is treated as a *private attribute* and won't resolve on the subclass
  (`cls._nb_model` → the base `None`). Pass the NetBox model explicitly (module-level helpers), never via
  an `_underscore` class attr.
- **`JobRunner.logger` is a plain `logging.Logger`** — it has `info/warning/error/debug`, **no
  `.success()`**. Using `.success()` errors the job *after* the sync succeeded.
- **DiffSync's own per-object logs go to structlog**, not the NetBox Job log — hence `_log_changes()`
  walks the diff and logs to `self.logger` so runs read as a changelog in the UI.
- **Field names:** Device's role is `role` (not `device_role`); status/face are plain strings
  (`"active"`, `"front"`); a 0U device has `position=None` and **blank** `face`.
- **Custom-field relation moved** across minors: `CustomField.object_types` (4.x) vs `content_types` —
  `ensure_custom_field()` handles both defensively.

## Running it

The Job appears at **Plugins → Railyard → Run sync**; it enqueues `RailyardSyncJob` (visible in NetBox's
core Jobs UI). To drive it from a shell in `dev/`:
`docker compose exec netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell` and enqueue
`RailyardSyncJob`, or build the adapters directly (see the scratch scripts pattern used during dev).
