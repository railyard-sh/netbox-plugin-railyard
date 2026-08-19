"""Typed errors for the Railyard client, so callers can distinguish auth/not-found from
other failures without string-matching."""

from __future__ import annotations


class RailyardAPIError(Exception):
    """A Railyard API request failed. Carries the HTTP status when there was a response."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RailyardAuthError(RailyardAPIError):
    """401/403 — the personal access token is missing, wrong, or lacks access."""


class RailyardNotFoundError(RailyardAPIError):
    """404 — the org or project reference did not resolve."""
