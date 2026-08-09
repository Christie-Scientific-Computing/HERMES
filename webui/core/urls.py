from django.urls import path

from . import views

app_name = "core"
urlpatterns = [
    path("", views.home, name="home"),
    path("import/", views.import_view, name="import"),
    path("export/", views.export_view, name="export"),
    path("results/", views.results_view, name="results"),
]
