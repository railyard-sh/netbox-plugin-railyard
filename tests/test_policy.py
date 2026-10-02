"""Sync policy: which orgs/projects a sync may reach, token loading and URL safety. NetBox-free."""

import pytest

from netbox_railyard.policy import (
    SyncTargets,
    TargetNotAllowed,
    as_tuple,
    insecure_url,
    read_token,
    validate_project_ref,
)


def targets(default=None, orgs=None, projects=None) -> SyncTargets:
    return SyncTargets.from_settings(default_org=default, allowed_orgs=orgs, allowed_projects=projects)


# ---- org allowlist ----------------------------------------------------------


def test_nothing_configured_allows_only_the_personal_org():
    t = targets()
    assert t.orgs == ("",)
    assert t.check_org(None) is None
    assert t.check_org("") is None
    with pytest.raises(TargetNotAllowed):
        t.check_org("acme")  # free text can no longer reach another org


def test_default_org_only():
    t = targets(default="acme")
    assert t.check_org(None) == "acme"  # blank means the default
    assert t.check_org("ACME") == "acme"  # case-insensitive, returns the configured spelling
    with pytest.raises(TargetNotAllowed):
        t.check_org("other")


def test_allowlist_plus_default():
    t = targets(default="acme", orgs=["org_123", "beta"])
    assert t.orgs == ("acme", "org_123", "beta")
    assert t.check_org("beta") == "beta"
    assert t.check_org("org_123") == "org_123"
    with pytest.raises(TargetNotAllowed):
        t.check_org("gamma")
    with pytest.raises(TargetNotAllowed):
        t.check_org("acme-evil")  # exact match only, never a prefix/substring


def test_allowlist_without_default_has_no_personal_org():
    t = targets(orgs="org_a, org_b")
    assert t.orgs == ("org_a", "org_b")
    with pytest.raises(TargetNotAllowed):
        t.check_org(None)


def test_org_choices_are_the_allowlist():
    assert [v for v, _ in targets(default="acme", orgs=["beta"]).org_choices()] == ["acme", "beta"]
    assert targets().org_choices()[0][0] == ""


# ---- project allowlist / ref validation -------------------------------------


def test_any_well_formed_project_when_unrestricted():
    assert targets().check_project(" prj_abc ") == "prj_abc"
    for bad in ("", "../x", "a/b", "a?b", "a b", "-lead", "x" * 300):
        with pytest.raises(TargetNotAllowed):
            targets().check_project(bad)


def test_project_allowlist():
    t = targets(projects=["prj_1", "dc-build"])
    assert t.check_project("DC-BUILD") == "dc-build"
    with pytest.raises(TargetNotAllowed):
        t.check_project("prj_2")


def test_validate_project_ref_rejects_path_tricks():
    with pytest.raises(TargetNotAllowed):
        validate_project_ref("..%2F")


def test_as_tuple_dedupes_case_insensitively():
    assert as_tuple(["A", "a", " b ", ""]) == ("A", "b")
    assert as_tuple(None) == ()


# ---- token + url ------------------------------------------------------------


def test_token_file_wins_and_is_stripped(tmp_path):
    f = tmp_path / "token"
    f.write_text("ry_fromfile\n")
    assert read_token("ry_inline", str(f)) == "ry_fromfile"
    assert read_token(" ry_inline ", None) == "ry_inline"


def test_unreadable_token_file_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="railyard_token_file"):
        read_token(None, str(tmp_path / "missing"))


def test_insecure_url():
    assert not insecure_url("https://railyard.sh")
    assert not insecure_url("http://localhost:8080")
    assert not insecure_url("http://127.0.0.1:8080")
    assert insecure_url("http://railyard.internal")
    assert insecure_url("ftp://x")


# ---- permissions ------------------------------------------------------------

from netbox_railyard.policy import SYNC_MODELS, missing_permissions, required_permissions  # noqa: E402


def test_dry_run_needs_only_view():
    perms = required_permissions(dry_run=True, allow_deletes=True)  # deletes don't matter for a dry run
    assert perms == {f"dcim.view_{m}" for m in SYNC_MODELS} | {"extras.view_tag"}


def test_apply_needs_add_and_change():
    perms = required_permissions(dry_run=False, allow_deletes=False)
    assert "dcim.add_device" in perms and "dcim.change_cable" in perms
    assert not any(p.startswith("dcim.delete_") for p in perms)


def test_apply_with_deletes_needs_delete():
    perms = required_permissions(dry_run=False, allow_deletes=True)
    assert {f"dcim.delete_{m}" for m in SYNC_MODELS} <= perms


def test_missing_permissions_lists_what_is_denied():
    granted = {f"dcim.view_{m}" for m in SYNC_MODELS} | {"extras.view_tag"}
    assert missing_permissions(granted.__contains__, dry_run=True, allow_deletes=False) == []
    missing = missing_permissions(granted.__contains__, dry_run=False, allow_deletes=False)
    assert "dcim.add_device" in missing and "dcim.view_device" not in missing
    assert missing == sorted(missing)
