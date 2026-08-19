"""The sync trigger view: render the form, enqueue the Job, and hand off to the Job result page."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views.generic import View

from .forms import RailyardSyncForm
from .jobs import RailyardSyncJob

TEMPLATE = "netbox_railyard/sync.html"


class RailyardSyncView(LoginRequiredMixin, View):
    def get(self, request):
        return render(request, TEMPLATE, {"form": RailyardSyncForm(initial={"dry_run": True})})

    def post(self, request):
        form = RailyardSyncForm(request.POST)
        if form.is_valid():
            job = RailyardSyncJob.enqueue(
                project=form.cleaned_data["project"],
                org=form.cleaned_data["org"] or None,
                dry_run=form.cleaned_data["dry_run"],
                allow_deletes=form.cleaned_data["allow_deletes"],
                user=request.user,
            )
            messages.success(request, f"Queued Railyard sync (job #{job.pk}).")
            return redirect(job.get_absolute_url())
        return render(request, TEMPLATE, {"form": form})
