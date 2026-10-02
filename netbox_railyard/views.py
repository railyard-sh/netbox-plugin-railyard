"""The sync trigger view: render the form, enqueue the Job, and hand off to the Job result page.

Permissions: opening the form needs the read-only (dry-run) permissions; queueing a sync needs the
permissions for the chosen options (``policy.required_permissions``). The job re-checks them against
its user before it reads or writes anything, and the target adapter applies object-level constraints.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.generic import View

from .config import sync_targets
from .forms import RailyardSyncForm
from .jobs import RailyardSyncJob
from .policy import missing_permissions

TEMPLATE = "netbox_railyard/sync.html"


class RailyardSyncView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and missing_permissions(
            request.user.has_perm, dry_run=True, allow_deletes=False
        ):
            raise PermissionDenied("Running a Railyard sync needs view permission on the synced DCIM models.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        form = RailyardSyncForm(initial={"dry_run": True}, targets=sync_targets())
        return render(request, TEMPLATE, {"form": form})

    def post(self, request):
        form = RailyardSyncForm(request.POST, targets=sync_targets())
        if form.is_valid():
            data = form.cleaned_data
            missing = missing_permissions(
                request.user.has_perm, dry_run=data["dry_run"], allow_deletes=data["allow_deletes"]
            )
            if missing:
                form.add_error(None, "You don't have the NetBox permissions this sync needs: " + ", ".join(missing))
                return render(request, TEMPLATE, {"form": form}, status=403)
            job = RailyardSyncJob.enqueue(
                project=data["project"],
                org=data["org"] or None,
                dry_run=data["dry_run"],
                allow_deletes=data["allow_deletes"],
                user=request.user,
            )
            messages.success(request, f"Queued Railyard sync (job #{job.pk}).")
            return redirect(job.get_absolute_url())
        return render(request, TEMPLATE, {"form": form})
