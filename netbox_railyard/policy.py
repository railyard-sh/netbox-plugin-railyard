"""Policy decisions that don't need NetBox: the NetBox permissions each job needs, which Railyard orgs and
projects a job may reach, and how the plugin's token is read.

Kept free of Django/NetBox imports so it is unit-tested in ``tests/``; the NetBox glue (``forms``, ``views``,
``jobs``) calls into it and enforces the result. The ownership tag (``ownership_tag``) and the clear-text URL
check (``insecure_url``) are railyard-sync's, shared with its command-line export so both recognise the same
objects; they are re-exported here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from railyard_sync.export.policy import TagSpec, insecure_url, instance_key, ownership_tag

__all__ = [
    "IMPORT_MODELS",
    "SYNC_MODELS",
    "SyncTargets",
    "TagSpec",
    "TargetNotAllowed",
    "as_tuple",
    "import_permissions",
    "insecure_url",
    "instance_key",
    "missing_import_permissions",
    "missing_permissions",
    "ownership_tag",
    "read_token",
    "required_permissions",
    "validate_estate_name",
    "validate_project_ref",
]

#: A project id or URL slug. Railyard ids and slugs are ASCII words joined by ``-``/``_``/``.``.
_PROJECT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,199}$")

#: The dcim models the Railyard → NetBox sync reads and writes (``<app>.<action>_<model>`` codenames).
SYNC_MODELS = (
    "manufacturer",
    "devicetype",
    "devicerole",
    "site",
    "location",
    "rack",
    "device",
    "interface",
    "frontport",
    "rearport",
    "portmapping",
    "poweroutlet",
    "powerport",
    "cable",
)


def required_permissions(*, dry_run: bool, allow_deletes: bool) -> frozenset[str]:
    """The NetBox permissions a user needs to run a sync with these options.

    A dry run reads NetBox (and shows what it found in the job log), so it needs ``view`` on every
    synced model. Applying needs ``add`` and ``change`` as well, and ``delete`` when deletes are on.
    Object-level constraints on those permissions are enforced separately, per object, by the target
    adapter (``.restrict()``).
    """
    actions = ["view"]
    if not dry_run:
        actions += ["add", "change"]
        if allow_deletes:
            actions.append("delete")
    perms = {f"dcim.{action}_{model}" for action in actions for model in SYNC_MODELS}
    perms.add("extras.view_tag")
    return frozenset(perms)


def missing_permissions(has_perm, *, dry_run: bool, allow_deletes: bool) -> list[str]:
    """The required permissions ``has_perm(codename)`` denies, sorted (empty when all are held)."""
    return sorted(p for p in required_permissions(dry_run=dry_run, allow_deletes=allow_deletes) if not has_perm(p))


#: The dcim models the NetBox → Railyard import reads (it writes nothing to NetBox).
IMPORT_MODELS = (
    "region",
    "site",
    "location",
    "racktype",
    "rack",
    "manufacturer",
    "devicetype",
    "devicerole",
    "device",
    "interfacetemplate",
    "frontporttemplate",
    "rearporttemplate",
    "powerporttemplate",
    "poweroutlettemplate",
    "consoleporttemplate",
    "interface",
    "frontport",
    "rearport",
    "portmapping",
    "powerport",
    "poweroutlet",
    "consoleport",
    "cable",
    "powerpanel",
    "powerfeed",
)


def import_permissions() -> frozenset[str]:
    """The NetBox permissions a user needs to import into Railyard: ``view`` on everything the import reads.

    The import only reads NetBox, so a dry run and a real run need the same; what the user may not view
    (including object-level constraints) is simply not imported."""
    return frozenset(f"dcim.view_{model}" for model in IMPORT_MODELS)


def missing_import_permissions(has_perm) -> list[str]:
    return sorted(p for p in import_permissions() if not has_perm(p))


class TargetNotAllowed(ValueError):
    """The requested org or project is not one this NetBox is configured to sync from."""


def as_tuple(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """A setting given as a list, tuple, set or comma-separated string, as a tuple of stripped,
    non-empty strings in order, without case-insensitive duplicates."""
    if value is None:
        return ()
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return tuple(out)


def validate_project_ref(ref: str | None) -> str:
    text = (ref or "").strip()
    if not _PROJECT_REF_RE.match(text):
        raise TargetNotAllowed("Enter a Railyard project id or URL slug (letters, digits, '-', '_', '.' or '~').")
    return text


#: Railyard's limit on a project name.
MAX_ESTATE_NAME = 200


def validate_estate_name(name: str | None) -> str:
    """A new estate's name: stripped, non-empty, at most ``MAX_ESTATE_NAME`` characters, no control characters."""
    text = " ".join((name or "").split())
    if not text:
        raise TargetNotAllowed("Enter a name for the new Railyard estate.")
    if len(text) > MAX_ESTATE_NAME:
        raise TargetNotAllowed(f"An estate name has at most {MAX_ESTATE_NAME} characters.")
    return text


@dataclass(frozen=True)
class SyncTargets:
    """The orgs and projects a sync may read from Railyard.

    ``orgs`` are org references exactly as an administrator configured them (id, slug or name);
    ``""`` stands for the token owner's personal org and is only permitted when no org is configured
    at all. ``projects`` is empty when any project in a permitted org may be synced.
    """

    orgs: tuple[str, ...]
    projects: tuple[str, ...] = ()
    default: str = ""

    @classmethod
    def from_settings(
        cls,
        *,
        default_org: str | None,
        allowed_orgs: str | Iterable[str] | None,
        allowed_projects: str | Iterable[str] | None,
    ) -> SyncTargets:
        default = (default_org or "").strip()
        orgs = as_tuple(([default] if default else []) + list(as_tuple(allowed_orgs)))
        if not orgs:
            orgs = ("",)
        return cls(orgs=orgs, projects=as_tuple(allowed_projects), default=default)

    def org_choices(self) -> list[tuple[str, str]]:
        return [(org, org or "Personal organisation of the token's user") for org in self.orgs]

    def check_org(self, ref: str | None) -> str | None:
        """The configured org reference matching ``ref`` (``None`` for the personal org). A blank
        ``ref`` means the default org. Anything not configured raises ``TargetNotAllowed``."""
        wanted = (ref or "").strip() or self.default
        for org in self.orgs:
            if org.lower() == wanted.lower():
                return org or None
        raise TargetNotAllowed(
            f"Organisation {wanted or '(personal)'!r} is not in this NetBox's railyard_allowed_orgs."
        )

    def check_project(self, ref: str | None) -> str:
        text = validate_project_ref(ref)
        if self.projects:
            for project in self.projects:
                if project.lower() == text.lower():
                    return project
            raise TargetNotAllowed(f"Project {text!r} is not in this NetBox's railyard_allowed_projects.")
        return text


def read_token(token: str | None, token_file: str | None) -> str:
    """The Railyard token: the contents of ``token_file`` when set (a mounted secret), else ``token``."""
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"Can't read railyard_token_file {token_file!r}: {exc.strerror or exc}") from None
    return (token or "").strip()
