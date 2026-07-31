from django.urls import path

from research_projects import views

app_name = "research_projects"

urlpatterns = [
    path("", views.project_list, name="list"),
    path("new/", views.project_create, name="create"),
    path("review/", views.review_queue, name="review_queue"),
    path("all/", views.all_projects, name="all_projects"),
    path("<str:project_id>/", views.project_detail, name="detail"),
    path("<str:project_id>/submit/", views.project_submit, name="submit"),
    path("<str:project_id>/review/", views.project_review, name="review"),
    path("<str:project_id>/revoke/", views.project_revoke, name="revoke"),
    path("<str:project_id>/members/add/", views.project_add_member, name="add_member"),
    path("<str:project_id>/members/<str:username>/remove/", views.project_remove_member, name="remove_member"),
    path("<str:project_id>/documents/upload/", views.upload_document, name="upload_document"),
]
