from django.urls import path

from jobs import views

app_name = "jobs"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("import/single/", views.import_single, name="import_single"),
    path("import/batch/", views.import_batch, name="import_batch"),
    path("export/dicom/", views.export_dicom, name="export_dicom"),
    path("export/proknow/", views.export_proknow, name="export_proknow"),
    path("jobs/<str:job_id>/watch/", views.job_watch, name="job_watch"),
    path("jobs/<str:job_id>/stream/", views.job_stream, name="job_stream"),
    path("jobs/<str:job_id>/cancel/", views.cancel_job, name="cancel_job"),
    path("jobs/<str:job_id>/", views.job_detail, name="job_detail"),
    path("results/", views.results_lookup, name="results_lookup"),
]
