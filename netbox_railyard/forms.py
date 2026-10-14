"""The trigger forms: sync a Railyard estate into NetBox, and import NetBox sites into Railyard."""

from __future__ import annotations

from django import forms

from .policy import SyncTargets, TargetNotAllowed, validate_estate_name


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


class RailyardImportForm(forms.Form):
    """Import NetBox sites into Railyard: a new estate, or a refresh of one imported before."""

    sites = forms.ModelMultipleChoiceField(
        label="Sites",
        queryset=None,  # the sites the user may view; set in __init__
        required=False,
        help_text="The NetBox sites to import. They become one Railyard estate.",
    )
    all_sites = forms.BooleanField(
        label="All sites",
        required=False,
        help_text="Import every site you may view instead (a refresh then also picks up sites added since).",
    )
    org = forms.ChoiceField(
        label="Organisation",
        required=False,
        help_text="The Railyard organisation to import into.",
    )
    name = forms.CharField(
        label="New estate",
        required=False,
        max_length=200,
        help_text="Name of a new Railyard estate to create from the sites.",
    )
    project = forms.CharField(
        label="Existing estate to refresh",
        required=False,
        help_text="Or: the id or URL slug of an estate imported from this NetBox before. Its Railyard design is kept.",
    )
    dry_run = forms.BooleanField(
        label="Dry run",
        required=False,
        initial=True,
        help_text="Show the import report and the changes in the job log without saving anything to Railyard.",
    )
    allow_deletes = forms.BooleanField(
        label="Allow deletes",
        required=False,
        initial=False,
        help_text="On a refresh, also remove imported objects that are gone from NetBox. Objects designed in "
        "Railyard are never removed. Off by default: they are kept and reported as stale.",
    )

    def __init__(self, *args, targets: SyncTargets, sites, **kwargs):
        super().__init__(*args, **kwargs)
        self.targets = targets
        self.fields["sites"].queryset = sites
        self.fields["org"].choices = targets.org_choices()
        self.fields["org"].initial = targets.orgs[0]
        if targets.projects:
            self.fields["project"] = forms.ChoiceField(
                label="Existing estate to refresh",
                required=False,
                choices=[("", "—")] + [(p, p) for p in targets.projects],
                help_text="Or: one of the Railyard estates this NetBox is configured to reach.",
            )
            self.order_fields(["sites", "all_sites", "org", "name", "project", "dry_run", "allow_deletes"])

    def clean_org(self):
        try:
            return self.targets.check_org(self.cleaned_data.get("org")) or ""
        except TargetNotAllowed as exc:
            raise forms.ValidationError(str(exc)) from None

    def clean(self):
        data = super().clean()
        if bool(data.get("sites")) == bool(data.get("all_sites")):
            self.add_error("sites", "Choose the sites to import, or tick All sites (not both).")
        name, project = (data.get("name") or "").strip(), (data.get("project") or "").strip()
        if bool(name) == bool(project):
            self.add_error("name", "Enter a name for a new estate, or the estate to refresh (not both).")
            return data
        try:
            if project:
                data["project"], data["name"] = self.targets.check_project(project), ""
            else:
                data["name"], data["project"] = validate_estate_name(name), ""
        except TargetNotAllowed as exc:
            self.add_error("project" if project else "name", str(exc))
        return data
