"""The trigger form for a manual sync run."""

from __future__ import annotations

from django import forms

from .policy import SyncTargets, TargetNotAllowed


class RailyardSyncForm(forms.Form):
    org = forms.ChoiceField(
        label="Organisation",
        required=False,  # "" is the personal-org choice; clean_org() still enforces the allowlist
        help_text="The Railyard organisations this NetBox is configured to sync from.",
    )
    project = forms.CharField(
        label="Railyard project",
        help_text="Project id or URL slug to sync from.",
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
        help_text="Also delete objects this sync created that are no longer in Railyard. Objects the sync "
        "did not create are never deleted. Off by default — Railyard only creates and updates.",
    )
    confirm = forms.BooleanField(
        label="I have reviewed a dry run and backed up NetBox",
        required=False,
        help_text="Required to apply changes (when Dry run is off).",
    )

    def __init__(self, *args, targets: SyncTargets, **kwargs):
        super().__init__(*args, **kwargs)
        self.targets = targets
        self.fields["org"].choices = targets.org_choices()
        self.fields["org"].initial = targets.orgs[0]
        if targets.projects:
            self.fields["project"] = forms.ChoiceField(
                label="Railyard project",
                choices=[(p, p) for p in targets.projects],
                help_text="The Railyard projects this NetBox is configured to sync from.",
            )
            self.order_fields(["org", "project", "dry_run", "allow_deletes", "confirm"])

    def clean_org(self):
        try:
            return self.targets.check_org(self.cleaned_data.get("org")) or ""
        except TargetNotAllowed as exc:
            raise forms.ValidationError(str(exc)) from None

    def clean_project(self):
        try:
            return self.targets.check_project(self.cleaned_data.get("project"))
        except TargetNotAllowed as exc:
            raise forms.ValidationError(str(exc)) from None

    def clean(self):
        data = super().clean()
        if not data.get("dry_run") and not data.get("confirm"):
            self.add_error("confirm", "Run a dry run first, back up NetBox, then tick this to apply changes.")
        return data
