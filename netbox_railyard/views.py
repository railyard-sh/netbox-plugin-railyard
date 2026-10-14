"""The trigger views: render a form, enqueue the Job, and hand off to the Job result page.

Permissions: the sync page needs the read-only (dry-run) permissions, and queueing a sync the permissions for
the chosen options (``policy.required_permissions``); the import page needs ``view`` on everything an import
reads (``policy.import_permissions``). Each job re-checks them against its user before it reads or writes
anything, and every query it makes applies object-level constraints.
"""

from __future__ import annotations

from dcim.models import Site
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.generic import View

from .config import sync_targets
from .forms import RailyardImportForm, RailyardSyncForm
from .import_job import RailyardImportJob
from .jobs import RailyardSyncJob
from .policy import missing_import_permissions, missing_permissions


class RailyardSyncView(LoginRequiredMixin, View):
    template = "netbox_railyard/sync.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and missing_permissions(
            request.user.has_perm, dry_run=True, allow_deletes=False
        ):
            raise PermissionDenied("Running a Railyard sync needs view permission on the synced DCIM models.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        form = RailyardSyncForm(initial={"dry_run": True}, targets=sync_targets())
        return render(request, self.template, {"form": form})

    def post(self, request):
        form = RailyardSyncForm(request.POST, targets=sync_targets())
        if form.is_valid():
            data = form.cleaned_data
            missing = missing_permissions(
                request.user.has_perm, dry_run=data["dry_run"], allow_deletes=data["allow_deletes"]
            )
            if missing:
                form.add_error(None, "You don't have the NetBox permissions this sync needs: " + ", ".join(missing))
                return render(request, self.template, {"form": form}, status=403)
            job = RailyardSyncJob.enqueue(
                project=data["project"],
                org=data["org"] or None,
                dry_run=data["dry_run"],
                allow_deletes=data["allow_deletes"],
                user=request.user,
            )
            messages.success(request, f"Queued Railyard sync (job #{job.pk}).")
            return redirect(job.get_absolute_url())
        return render(request, self.template, {"form": form})


class RailyardImportView(LoginRequiredMixin, View):
    template = "netbox_railyard/import.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and missing_import_permissions(request.user.has_perm):
            raise PermissionDenied("Importing into Railyard needs view permission on the DCIM models it reads.")
        return super().dispatch(request, *args, **kwargs)

    def _form(self, request, data=None):
        sites = Site.objects.restrict(request.user, "view").order_by("name")
        initial = None if data else {"dry_run": True}
        return RailyardImportForm(data, initial=initial, targets=sync_targets(), sites=sites)

    def get(self, request):
        return render(request, self.template, {"form": self._form(request)})

    def post(self, request):
        form = self._form(request, request.POST)
        if form.is_valid():
            data = form.cleaned_data
            job = RailyardImportJob.enqueue(
                sites=None if data["all_sites"] else [site.pk for site in data["sites"]],
                org=data["org"] or None,
                project=data["project"] or None,
                name=data["name"] or None,
                dry_run=data["dry_run"],
                allow_deletes=data["allow_deletes"],
                # This NetBox's address, recorded as the estate's source (PLUGINS_CONFIG netbox_url wins).
                netbox_url=request.build_absolute_uri(reverse("home")).rstrip("/"),
                user=request.user,
            )
            messages.success(request, f"Queued Railyard import (job #{job.pk}).")
            return redirect(job.get_absolute_url())
        return render(request, self.template, {"form": form})
