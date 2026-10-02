# netbox-plugin-railyard

A [NetBox](https://netbox.dev) plugin that syncs a **[Railyard](https://railyard.sh)** data-centre
design into NetBox — **racks, devices and cables** — using [DiffSync](https://github.com/networktocode/diffsync).

Railyard is the *design* tool (sketch an estate: locations → data centres → rows → racks → devices,
plan structured cabling and power). This plugin reconciles a Railyard **project** into NetBox as the
system of record, so the sketch becomes real DCIM data without a manual CSV import.

> [!WARNING]
> **Pre-1.0 software that writes to your NetBox. Back up your NetBox database before every sync.**
> Run a **dry run** first and read its job log before applying anything, and try it on a copy of your
> NetBox before pointing it at production. The plugin is provided under the Apache-2.0 licence
> **without warranty** (see [LICENSE](LICENSE)).

> Status: **early development (0.1).** The Railyard-side core (client, canonical model, source adapter,
> mappings, device-type resolution) is implemented and unit-tested, and the NetBox side (target adapter,
> sync Job, trigger UI) has an integration suite that runs against NetBox 4.6 — see
> [Development](#development). See [Roadmap](#roadmap).

## How it works

```
Railyard REST API  ──►  RailyardAdapter (source)  ─┐
                                                    ├─►  DiffSync diff ──►  create/update[/delete]
NetBox ORM         ──►  NetBoxAdapter (target)   ──┘
```

- Railyard is the **source of truth**. The sync runs one way: **Railyard → NetBox**.
- Default is **create/update only**. An explicit **"Allow deletes"** option also removes objects *the
  sync created* that are no longer in Railyard.
- A **dry run** (the default) only reads NetBox: it writes nothing, not even the ownership tag, and
  lists what a real run would create, update, delete, skip and keep.
- A real run is **all or nothing**: it applies every change in one database transaction, so if
  anything fails, nothing is changed. Every change appears in NetBox's changelog under the user who
  ran the sync.

### What the sync will and won't touch

Every object the sync creates is tagged with its project's **ownership tag**, named
`RY:<project name> (<id>)`. The tag is specific to one Railyard project on one Railyard instance (it is
keyed by the project's id, not its name), and the sync recognises it by its exact description.

- **It only ever updates or deletes objects that carry its ownership tag.** It finds them by NetBox
  id and tag, never by name.
- **It never takes over objects you created.** If NetBox already has a manufacturer, device type,
  role, site or rack with the same name, the sync *uses* it (for example, it places its devices in
  your existing rack) but never tags, changes or deletes it.
- **Conflicts are skipped and reported.** If your NetBox already has a device with the same name in
  the same site, the sync doesn't touch it; it skips that device, its ports and cables, and lists them in
  the job log. The same applies to a port that is already cabled.
- **One exception: components of a device the sync created.** When the sync creates a device whose
  device type has component templates, NetBox creates those components too. The sync adopts (tags)
  the ones its design uses, and the job log lists them.
- **Deletes (opt-in) never reach beyond what the sync owns.** An owned object is kept, and
  reported, if deleting it would also delete or change something the sync doesn't own (for example
  your device in its rack, or your cable on its port). Deleting an owned device removes its
  components with it, as NetBox always does.
- If you remove the ownership tag from an object, the sync stops managing it.

Devices also carry a `railyard_id` custom field (the Railyard placement id) for reference. Devices
are matched by name, so renaming a device in Railyard creates a new NetBox device (and, with deletes
on, removes the old one).

### Device types

Device types come from the [netbox-community device-type library](https://github.com/netbox-community/devicetype-library).
When a device needs a type NetBox doesn't have yet, the plugin fetches the matching YAML (by the
Railyard catalogue `key`, which is the library slug) at a **pinned library commit** and creates a lean
device type (no component templates; the sync creates exactly the ports its cabling needs). If the
library has no match, it builds a minimal type from the Railyard catalogue. An existing device type
with the same manufacturer and model is used as it is.

## Architecture (three layers, one repo)

| Path | Layer | NetBox import? |
|---|---|---|
| `netbox_railyard/railyard/` | **Railyard core** — API client, canonical DiffSync models, source adapter, Go-parity mappings, device-type-library resolver | **No** — pure Python, unit-tested, extractable to a shared `railyard-diffsync` package for the future Nautobot plugin |
| `netbox_railyard/policy.py` | **Sync policy** — permissions, org/project allowlist, ownership tag, token loading | **No** — unit-tested |
| `netbox_railyard/target.py`, `devicetypes.py` | **NetBox target** — ORM adapter + create/update/delete + ownership rules | Yes |
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
        "railyard_url": "https://railyard.sh",  # your Railyard base URL (use https)
        # The token: prefer a file (e.g. a mounted secret) to putting it in configuration.py.
        "railyard_token_file": "/run/secrets/railyard_token",
        # "railyard_token": "ry_...",
        # Which Railyard organisations and projects may be synced. Users pick from these; nothing
        # else is reachable. With none configured, only the token user's personal org is used.
        "railyard_org": "my-org",  # the default org (id, slug or name)
        "railyard_allowed_orgs": [],  # further orgs users may choose
        "railyard_allowed_projects": [],  # optional: only these project ids/slugs; empty = any
        # Device-type library commit; defaults to a pinned SHA. Override only after reviewing upstream.
        # "devicetype_library_ref": "<40-character commit SHA>",
    },
}
```

Requires **NetBox 4.6+** (Python 3.12+, Django 6).

### The Railyard token: least privilege, expiry and rotation

Every sync uses the one token configured here, whichever NetBox user runs it, and it can read
**everything its Railyard user can see**. Railyard tokens aren't scoped yet, so limit what that user
can see:

1. **Use a dedicated Railyard user for the sync**, not a person's account.
2. **Give it the *viewer* role, only in the organisations you want to sync.** The sync only reads from
   Railyard, so it never needs editor or owner rights.
3. Create its token under **User settings → Personal access tokens**. Tokens **expire** (90 days by
   default, 7–365 days if you choose); choose the shortest expiry you are prepared to rotate.
4. Store it in a file readable only by NetBox (`railyard_token_file`), not in `configuration.py` or in
   version control.
5. **Rotate it before it expires.** Create a new token, replace the file, then revoke the old token in
   Railyard. NetBox reads the token at the start of each sync, so no restart is needed for
   `railyard_token_file`.

Also set `railyard_org` / `railyard_allowed_orgs` (and, if you like, `railyard_allowed_projects`), so
that only the orgs and projects you choose can be synced, even if the token's user can see others.

If the token is wrong, revoked or **expired**, Railyard returns HTTP 401 and the sync fails with a
message saying so. Rotate the token as above. A 403 means the token works but its user can't read
that org or project.

### NetBox permissions

Syncing changes your DCIM data, so it uses NetBox's own permissions. The **job re-checks them** when
it runs, and applies object-level constraints to every object it reads or writes.

| To… | the user needs |
|---|---|
| open the sync page and run a **dry run** | `view` on manufacturer, device type, device role, site, rack, device, interface, front port, rear port, port mapping, power outlet, power port and cable, plus `extras.view_tag` |
| **apply** a sync | the above, plus `add` and `change` on the same DCIM models |
| apply with **Allow deletes** | the above, plus `delete` on the same DCIM models |
| run the **first** sync of a project | `extras.add_tag` (for the ownership tag); the very first sync on a NetBox also needs `extras.add_customfield` and `extras.change_customfield` (for `railyard_id`). An administrator can run that first sync. |

## Usage

1. **Back up NetBox.**
2. Open **Plugins → Railyard → Run sync**, pick the organisation and project, and run a **dry run**
   (ticked by default).
3. Read the job log: what would be created, updated and deleted; existing objects it would use as
   they are; what it would skip because it doesn't own it; and deletes it would refuse.
4. To apply, untick *Dry run*, tick *Allow deletes* only if you want a mirror, and confirm that
   you've reviewed the dry run and backed up NetBox.

The run is a native NetBox background Job; watch progress and the diff on the Job detail page.

## Upgrading from 0.1

This release changes behaviour on purpose:

- **Ownership tag.** 0.1 tagged objects `RY:<project name>` (slug `ry-<project-name>`). Objects with
  that tag are **no longer recognised**: the sync won't update or delete them, and it reports
  same-named devices as conflicts. To hand them over, run a dry run. Its log prints the new tag's
  slug, name and description. Edit the old tag in NetBox to exactly those values, then sync.
- **Organisation** is no longer free text. Configure `railyard_org` and/or `railyard_allowed_orgs`
  (and optionally `railyard_allowed_projects`). Without them, only the token user's personal org can
  be synced.
- **Permissions.** Users need the NetBox permissions above to open and run a sync.
- **Adoption.** Existing objects are no longer silently tagged and taken over (see
  [What the sync will and won't touch](#what-the-sync-will-and-wont-touch)).
- **Device-type library** defaults to a pinned commit instead of `master`.
- Applying a sync needs the form's confirmation tick.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest            # runs the NetBox-independent core suite
ruff check .
```

The core test-suite (`tests/`) needs **no NetBox install**. It exercises the client (with an injected
transport), the mappings, the sync policy, and the source adapter against
`tests/fixtures/example-project.json`.

`tests/integration/` exercises the NetBox side (permissions, ownership, dry runs, the allowlist)
against a real NetBox and PostgreSQL, under NetBox's test runner:

```bash
NETBOX_DIR=/path/to/netbox ./dev/run-integration-tests.sh
```

See the script for what `NETBOX_DIR` needs; CI runs it against NetBox 4.6. For a clickable local
NetBox, use [`dev/setup.sh`](dev/README.md).

## Roadmap

- [x] Railyard API client (PAT auth, `X-Org-Id`, list orgs/projects, fetch project JSON)
- [x] Canonical DiffSync models + Railyard source adapter (sites, racks, manufacturers, device types, roles, devices, cables, power)
- [x] Go-parity mappings + device-type-library resolver
- [x] NetBox ORM target adapter (create/update/delete)
- [x] `RailyardSyncJob` (`JobRunner`) + trigger form + sidebar UI
- [x] Local NetBox test harness (`dev/setup.sh`)
- [x] Integration test suite against NetBox 4.6
- [ ] Field testing against real estates
- [ ] `nautobot-plugin-railyard` (Nautobot SSoT) reusing this core

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability.

## Licence

Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)). Device-type definitions are © their
respective vendors, via the Apache-2.0
[netbox-community/devicetype-library](https://github.com/netbox-community/devicetype-library).
