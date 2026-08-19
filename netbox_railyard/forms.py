"""The trigger form for a manual sync run."""

from __future__ import annotations

from django import forms


class RailyardSyncForm(forms.Form):
    project = forms.CharField(
        label="Railyard project",
        help_text="Project id or URL slug to sync from.",
    )
    org = forms.CharField(
        label="Organisation",
        required=False,
        help_text="Org id, slug or name. Leave blank to use the plugin's default (or your personal org).",
    )
    dry_run = forms.BooleanField(
        label="Dry run",
        required=False,
        initial=True,
        help_text="Preview the diff in the job log without changing NetBox.",
    )
    allow_deletes = forms.BooleanField(
        label="Allow deletes",
        required=False,
        initial=False,
        help_text="Also delete NetBox objects (within the project's sites) that are no longer in Railyard. "
        "Off by default — Railyard only creates and updates.",
    )
