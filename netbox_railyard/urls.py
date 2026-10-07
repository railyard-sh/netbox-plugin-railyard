from django.urls import path

from . import views

urlpatterns = [
    path("sync/", views.RailyardSyncView.as_view(), name="sync"),
    path("import/", views.RailyardImportView.as_view(), name="import"),
]
