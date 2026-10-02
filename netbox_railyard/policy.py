"""Sync policy decisions that don't need NetBox: which Railyard orgs and projects a sync may target,
how the plugin's token is read, and whether the Railyard URL is safe to send it to.

Kept free of Django/NetBox imports (like the ``railyard`` core) so it is unit-tested in ``tests/``;
the NetBox glue (``forms``, ``views``, ``jobs``) calls into it and enforces the result.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

#: A project id or URL slug. Railyard ids and slugs are ASCII words joined by ``-``/``_``/``.``.
_PROJECT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,199}$")

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


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


def insecure_url(url: str) -> bool:
    """True when ``url`` would send the token in clear text (plain http to anything but loopback)."""
    parts = urlsplit(url or "")
    if parts.scheme == "https":
        return False
    return not (parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS)
