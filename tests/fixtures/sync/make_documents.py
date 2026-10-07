"""Regenerate the NetBox sync-document fixtures with Railyard's own exporter.

The documents are exactly what ``POST /api/projects/{id}/deliverables/netbox-sync`` serves (the source of the
plugin's sync), written offline by the Railyard CLI. Build it from the railyard repository and run this script
from the plugin's root:

    (cd ../railyard/backend && go build -o /tmp/railyard ./cmd/railyard)
    python tests/fixtures/sync/make_documents.py --railyard-cli /tmp/railyard

It exports each ``<name>-project.json`` here with ``railyard export --format netbox-sync`` and writes
``netbox-sync-<name>.json`` beside it. The server adds ``project.revision``, which an offline export cannot
know; the fixtures carry one so they look like the served document. The integration suite sets
``netboxVersion`` to the NetBox it runs on.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import tempfile

HERE = pathlib.Path(__file__).parent
DOCUMENTS = ("integration", "patch")
NETBOX_VERSION = "4.6"  # front ports carry `positions` (4.5 and later), as on every NetBox the plugin supports


def build(cli: str, stem: str) -> dict:
    project_path = HERE / f"{stem}-project.json"
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "doc.json"
        subprocess.run(
            [cli, "export", "--format", "netbox-sync", "--netbox-version", NETBOX_VERSION, str(project_path)]
            + ["-o", str(out)],
            check=True,
            capture_output=True,
            text=True,
        )
        doc = json.loads(out.read_text())
    doc["project"].setdefault("revision", 1)
    return doc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--railyard-cli", required=True, help="path to a built railyard CLI binary")
    args = parser.parse_args()
    for stem in DOCUMENTS:
        doc = build(args.railyard_cli, stem)
        path = HERE / f"netbox-sync-{stem}.json"
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
