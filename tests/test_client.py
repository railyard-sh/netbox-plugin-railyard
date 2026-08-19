"""RailyardClient talks to the API with Bearer auth + X-Org-Id, and resolves org refs."""

import pytest
from conftest import FakeResponse, FakeSession

from netbox_railyard.railyard.client import RailyardClient
from netbox_railyard.railyard.errors import RailyardAuthError, RailyardNotFoundError

ORGS = [
    {"id": "org_personal", "name": "Personal", "slug": "personal", "personal": True},
    {"id": "org_acme", "name": "Acme Corp", "slug": "acme", "personal": False},
]


def make_client(handler, **kw):
    return RailyardClient("https://railyard.sh/", "ry_secret", session=FakeSession(handler), **kw)


def test_requires_ry_prefix():
    with pytest.raises(ValueError):
        RailyardClient("https://railyard.sh", "not-a-railyard-token", session=FakeSession(lambda *a: None))


def test_list_orgs_sends_bearer_auth():
    def handler(method, url, headers, params):
        assert headers["Authorization"] == "Bearer ry_secret"
        assert url == "https://railyard.sh/api/orgs"
        return FakeResponse(200, ORGS)

    client = make_client(handler)
    assert [o["slug"] for o in client.list_orgs()] == ["personal", "acme"]


def test_resolve_org_by_slug_name_or_id():
    client = make_client(lambda *a: FakeResponse(200, ORGS))
    assert client.resolve_org("acme") == "org_acme"
    assert client.resolve_org("Acme Corp") == "org_acme"
    assert client.resolve_org("org_personal") == "org_personal"
    assert client.resolve_org(None) is None
    with pytest.raises(RailyardNotFoundError):
        client.resolve_org("nope")


def test_get_project_sends_org_header():
    captured = {}

    def handler(method, url, headers, params):
        if url.endswith("/api/orgs"):
            return FakeResponse(200, ORGS)
        captured["url"] = url
        captured["org"] = headers.get("X-Org-Id")
        return FakeResponse(200, {"id": "prj1", "name": "Build", "racks": []})

    client = make_client(handler, org="acme")
    doc = client.get_project("prj1")
    assert doc["name"] == "Build"
    assert captured["url"] == "https://railyard.sh/api/projects/prj1"
    assert captured["org"] == "org_acme"  # resolved from the "acme" ref


def test_auth_error_maps_to_typed_exception():
    client = make_client(lambda *a: FakeResponse(401, {"error": "bad token"}, text="bad token"))
    with pytest.raises(RailyardAuthError) as exc:
        client.whoami()
    assert exc.value.status == 401


def test_not_found_maps_to_typed_exception():
    def handler(method, url, headers, params):
        if url.endswith("/api/orgs"):
            return FakeResponse(200, ORGS)
        return FakeResponse(404, {"error": "no such project"})

    client = make_client(handler)
    with pytest.raises(RailyardNotFoundError):
        client.get_project("ghost")
