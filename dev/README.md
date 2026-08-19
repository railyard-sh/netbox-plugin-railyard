# Local NetBox for testing the plugin

Spins up a real NetBox (via [netbox-docker](https://github.com/netbox-community/netbox-docker)) with
this plugin installed, so you can run a Railyard → NetBox sync end-to-end.

## Prerequisites

- **Docker Desktop running.**
- A **Railyard personal access token** (`ry_…`) with access to the org/project you want to sync
  (Railyard → User settings → Personal access tokens).

## Bring it up

```bash
./dev/setup.sh                       # clones netbox-docker next door and overlays the plugin
cd ../netbox-docker-railyard         # the work dir setup.sh prints

export RAILYARD_TOKEN=ry_your_token_here
export RAILYARD_URL=https://railyard.sh      # or dev.railyard.sh, or your own instance

docker compose build
docker compose up -d
# first boot runs migrations (~1-2 min). Follow along with:
docker compose logs -f netbox | grep -m1 "Application ready"

# create a login:
docker compose exec netbox /opt/netbox/netbox/manage.py createsuperuser
```

Then open **http://localhost:8000**, sign in, and go to **Plugins → Railyard → Run sync**. Enter a
project id/slug (and org if needed), keep **Dry run** ticked for the first pass to preview the diff in
the job log, then untick it to apply.

## Iterating on the plugin

After editing the plugin, re-sync the source and rebuild:

```bash
./dev/setup.sh && (cd ../netbox-docker-railyard && docker compose build && docker compose up -d)
```

## Tear down

```bash
cd ../netbox-docker-railyard
docker compose down            # keep data
docker compose down -v         # wipe the database/volumes too
```

## What the overlay does

`dev/setup.sh` clones netbox-docker, copies the plugin source into its build context, and drops in:

- `Dockerfile-Plugins` — `FROM` the pinned NetBox image, `pip install` the plugin
- `docker-compose.override.yml` — builds netbox/worker/housekeeping from that image, maps port 8000,
  and passes `RAILYARD_*` env through
- `configuration/plugins.py` — enables `netbox_railyard` and wires `PLUGINS_CONFIG` from the env

Nothing here is needed to *use* the plugin in a real NetBox — it's only the local test harness.
