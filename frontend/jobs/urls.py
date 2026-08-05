from django.urls import path

from jobs import views

app_name = "jobs"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("collect-data/", views.collect_data, name="collect_data"),
    path("retrieve-data/", views.retrieve_data, name="retrieve_data"),
    path("jobs/<str:job_id>/watch/", views.job_watch, name="job_watch"),
    path("jobs/<str:job_id>/stream/", views.job_stream, name="job_stream"),
    path("jobs/<str:job_id>/cancel/", views.cancel_job, name="cancel_job"),
    path("jobs/<str:job_id>/patients/<str:mrn>/", views.patient_detail, name="patient_detail"),
    path("jobs/<str:job_id>/", views.job_detail, name="job_detail"),
    path("results/", views.results_lookup, name="results_lookup"),
]
