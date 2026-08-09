from django.contrib.auth import views as auth_views
from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.HermesLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("invite/", views.invite_user, name="invite"),
    path("users/", views.user_list, name="user_list"),
    path("users/create/", views.create_user, name="create_user"),
    path("users/<str:username>/access/", views.user_access, name="user_access"),
    path("users/<str:username>/access/<int:destination_id>/remove/", views.user_access_remove, name="user_access_remove"),
    path("activate/<uidb64>/<token>/", views.activate_account, name="activate"),
]
