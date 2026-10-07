#!/usr/bin/env bash
#
# Stand up a local NetBox (via the official netbox-community/netbox-docker) with this plugin
# installed, for end-to-end testing. Idempotent — safe to re-run after editing the plugin.
#
#   ./dev/setup.sh            # clone netbox-docker + overlay the plugin, then print next steps
#   RAILYARD_SYNC_DIR=../railyard-sync ./dev/setup.sh   # …with a local railyard-sync checkout
#   RAILYARD_TOKEN=ry_… docker compose up   (run inside the work dir it prints)
#
# Requires Docker Desktop running.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="${NETBOX_DOCKER_DIR:-$(cd "$PLUGIN_DIR/.." && pwd)/netbox-docker-railyard}"
OVERLAY="$PLUGIN_DIR/dev/overlay"

echo "▸ plugin:   $PLUGIN_DIR"
echo "▸ work dir: $WORK_DIR"

if [ ! -d "$WORK_DIR/.git" ]; then
  echo "▸ cloning netbox-community/netbox-docker (release)…"
  git clone -q -b release https://github.com/netbox-community/netbox-docker.git "$WORK_DIR"
fi

# Detect the NetBox image tag netbox-docker pins, so Dockerfile-Plugins builds FROM the same base.
VERSION="$(grep -oE 'VERSION-v[0-9]+\.[0-9]+-[0-9.]+' "$WORK_DIR/docker-compose.yml" | head -1 | sed 's/VERSION-//')"
VERSION="${VERSION:-v4.6-3.4.0}"
echo "▸ NetBox image tag: $VERSION"

# Copy the plugin source into the build context (exclude local venvs / git / caches).
echo "▸ syncing plugin source into the build context…"
rm -rf "$WORK_DIR/netbox-plugin-railyard"
rsync -a --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '.ruff_cache' --exclude '.devicetype-cache' \
  "$PLUGIN_DIR/" "$WORK_DIR/netbox-plugin-railyard/"

# railyard-sync at the commit pyproject.toml pins, fetched on the host so the image needs no git.
RAILYARD_SYNC_REF="$(grep -oE 'railyard-sync@[0-9a-zA-Z._-]+' "$PLUGIN_DIR/pyproject.toml" | head -1 | cut -d@ -f2)"
echo "▸ railyard-sync: ${RAILYARD_SYNC_DIR:-github.com/railyard-sh/railyard-sync@$RAILYARD_SYNC_REF}"
rm -rf "$WORK_DIR/railyard-sync"
if [ -n "${RAILYARD_SYNC_DIR:-}" ]; then
  # a local checkout instead (to try railyard-sync changes)
  rsync -a --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' \
    --exclude '.ruff_cache' "$RAILYARD_SYNC_DIR/" "$WORK_DIR/railyard-sync/"
else
  git clone -q https://github.com/railyard-sh/railyard-sync.git "$WORK_DIR/railyard-sync"
  git -C "$WORK_DIR/railyard-sync" -c advice.detachedHead=false checkout -q "$RAILYARD_SYNC_REF"
  rm -rf "$WORK_DIR/railyard-sync/.git"
fi

# Overlay the netbox-docker plugin wiring.
cp "$OVERLAY/Dockerfile-Plugins" "$WORK_DIR/Dockerfile-Plugins"
cp "$OVERLAY/docker-compose.override.yml" "$WORK_DIR/docker-compose.override.yml"
mkdir -p "$WORK_DIR/configuration"
cp "$OVERLAY/plugins.py" "$WORK_DIR/configuration/plugins.py"

# Persist the detected version for compose build args.
grep -q '^VERSION=' "$WORK_DIR/.env" 2>/dev/null || echo "VERSION=$VERSION" >> "$WORK_DIR/.env"

cat <<EOF

✅ Overlay ready. Next:

  cd "$WORK_DIR"
  docker compose build
  docker compose up -d
  # wait ~1-2 min for migrations, then create a login:
  docker compose exec netbox /opt/netbox/netbox/manage.py createsuperuser

Open http://localhost:8000  →  Plugins → Railyard → Run sync.

Set your Railyard token first (either edit configuration/plugins.py, or export before 'up'):
  export RAILYARD_TOKEN=ry_your_token_here
  export RAILYARD_URL=https://railyard.sh     # or dev.railyard.sh / your instance
  docker compose up -d

Re-run this script after editing the plugin, then: docker compose build && docker compose up -d
EOF
