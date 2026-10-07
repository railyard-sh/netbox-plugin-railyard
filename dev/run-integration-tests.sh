#!/usr/bin/env bash
#
# Run tests/integration against a real NetBox checkout, without Docker.
#
#   NETBOX_DIR=/path/to/netbox ./dev/run-integration-tests.sh [extra manage.py test args]
#
# NETBOX_DIR is a netbox-community/netbox checkout (matching the plugin's supported version) whose
# virtualenv is at $NETBOX_DIR/.venv, with a netbox/netbox/configuration.py that points at a
# PostgreSQL database and Redis, and that enables the plugin:
#
#   PLUGINS = ["netbox_railyard"]
#   DEBUG = False  # the debug toolbar refuses to run under tests
#   API_TOKEN_PEPPERS = {1: "<50 or more characters>"}  # the import tests use a v2 API token
#
# The plugin is installed (editable) into that virtualenv, with railyard-sync from GitHub at the commit
# pyproject.toml pins. To test against a local railyard-sync checkout instead, install it into the same
# virtualenv afterwards: uv pip install --python "$PY" -e ../railyard-sync Django creates and destroys its own test
# database, so the configured database is not modified.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
: "${NETBOX_DIR:?set NETBOX_DIR to a NetBox checkout}"
PY="${NETBOX_PYTHON:-$NETBOX_DIR/.venv/bin/python}"

if command -v uv >/dev/null 2>&1; then
  uv pip install -q --python "$PY" -e "$PLUGIN_DIR"
else
  "$PY" -m pip install -q -e "$PLUGIN_DIR"
fi

cd "$NETBOX_DIR/netbox"
exec "$PY" manage.py test "$PLUGIN_DIR/tests/integration" -t "$PLUGIN_DIR/tests" --parallel 1 --noinput "$@"
