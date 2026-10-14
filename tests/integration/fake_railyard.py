"""Just enough of the Railyard API for the import job, behind railyard-sync's real ``RailyardClient``.

``FakeRailyard().session`` is handed to the client (``railyard_client``), so the job's requests go through
railyard-sync's own error handling: the catalogue search (always empty, so every device type is built from
NetBox's templates), ``/api/validate``, a project read with its revision (ETag) and a save (PUT, ``If-Match``).
"""

from __future__ import annotations

import copy
import json
from typing import Any
from urllib.parse import unquote, urlsplit

from railyard_sync.client import RailyardClient


class FakeResponse:
    def __init__(self, status: int, payload: Any = None, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.headers = dict(headers or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeRailyard:
    def __init__(self) -> None:
        self.projects: dict[str, dict] = {}
        self.revisions: dict[str, int] = {}
        self.calls: list[dict] = []
        self.put_response: FakeResponse | None = None  # answers every PUT when set
        self.problems: list[dict] = []  # what /api/validate finds

    # -- the client's transport ---------------------------------------------------------------------

    @property
    def session(self):
        return self

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, **kwargs):
        path = unquote(urlsplit(url).path)
        self.calls.append({"method": method, "path": path, "headers": dict(headers or {}), "json": json})
        if path == "/api/catalogue/search":
            return FakeResponse(200, {"revision": "1", "entries": []})
        if path == "/api/validate" and method == "POST":
            return FakeResponse(200, {"problems": self.problems, "truncated": False})
        if path.startswith("/api/projects/"):
            ref = path.removeprefix("/api/projects/")
            if method == "GET":
                project = self.find(ref)
                if project is None:
                    return FakeResponse(404, {"error": "project not found", "code": "not_found"})
                return FakeResponse(200, copy.deepcopy(project), {"ETag": f'"{self.revisions[project["id"]]}"'})
            if method == "PUT":
                if self.put_response is not None:
                    return self.put_response
                if_match = (headers or {}).get("If-Match")
                if ref in self.projects and if_match != f'"{self.revisions[ref]}"':
                    return FakeResponse(412, {"error": "revision conflict"})
                self.projects[ref] = copy.deepcopy(json)
                self.revisions[ref] = self.revisions.get(ref, 0) + 1
                return FakeResponse(200, {"id": ref}, {"ETag": f'"{self.revisions[ref]}"'})
        return FakeResponse(404, {"error": f"no route for {method} {path}"})

    # -- helpers for tests --------------------------------------------------------------------------

    def find(self, ref: str) -> dict | None:
        for pid, project in self.projects.items():
            if ref in (pid, project.get("name", "").lower().replace(" ", "-")):
                return project
        return None

    def puts(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "PUT"]

    def client(self, base_url: str, token: str, *, org: str | None = None) -> RailyardClient:
        """A stand-in for ``RailyardClient(base_url, token, org=…)`` that talks to this fake."""
        return RailyardClient(base_url, token, org=org, session=self)
