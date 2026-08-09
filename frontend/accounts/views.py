from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import LoginView
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from hermes_frontend import backend_client
from accounts.forms import AddDestinationForm, CreateUserForm, HermesAuthenticationForm, InviteUserForm

User = get_user_model()


def _is_data_custodian(user) -> bool:
    return user.is_active and user.is_staff


class HermesLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = HermesAuthenticationForm

    def form_valid(self, form):
        response = super().form_valid(form)
        # Django's SESSION_COOKIE_AGE default (2 weeks) already persists
        # sessions regardless of this checkbox -- "remember me" unchecked is
        # the behavior that needs adding (expire when the browser closes),
        # not the reverse.
        if not form.cleaned_data.get("remember_me"):
            self.request.session.set_expiry(0)
        return response


@login_required
@user_passes_test(_is_data_custodian)
def invite_user(request):
    if request.method == "POST":
        form = InviteUserForm(request.POST)
        if form.is_valid():
            user = form.save()
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            activate_url = request.build_absolute_uri(
                reverse("accounts:activate", kwargs={"uidb64": uid, "token": token})
            )
            send_mail(
                subject="You've been invited to HERMES",
                message=(
                    f"An account has been created for you on HERMES.\n\n"
                    f"Set your password to activate it: {activate_url}"
                ),
                from_email=None,
                recipient_list=[user.email],
                fail_silently=True,
            )
            messages.success(request, f"Invited {user.username}. Activation link: {activate_url}")
            return redirect("accounts:invite")
    else:
        form = InviteUserForm()
    return render(request, "accounts/invite.html", {"form": form})


@login_required
@user_passes_test(_is_data_custodian)
def create_user(request):
    """Admin sets username+password directly -- no email, no activation
    link, active immediately. Sits alongside invite_user, doesn't replace it."""
    if request.method == "POST":
        form = CreateUserForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f"Created account for {user.username}. They can sign in immediately.")
            return redirect("accounts:user_list")
    else:
        form = CreateUserForm()
    return render(request, "accounts/create_user.html", {"form": form})


def activate_account(request, uidb64, token):
    try:
        uid = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        return render(request, "accounts/activate_invalid.html", status=400)

    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            login(request, user)
            messages.success(request, "Your password has been set. Welcome to HERMES.")
            return redirect("jobs:dashboard")
    else:
        form = SetPasswordForm(user)
    return render(request, "accounts/activate.html", {"form": form})


@login_required
@user_passes_test(_is_data_custodian)
def user_list(request):
    users = User.objects.select_related("profile").order_by("username")
    return render(request, "accounts/user_list.html", {"users": users})


@login_required
@user_passes_test(_is_data_custodian)
def user_access(request, username):
    """
    Staff-only page (safety-plan §A): manage a single user's export
    destination allow-list, independent of project membership. Zero rows
    for this user means unrestricted (see AccessDB.is_allowed on the
    backend) -- the table below is empty in exactly that case, not an
    error state.
    """
    target_user = get_object_or_404(User, username=username)

    modalities, modalities_error = [], None
    collections, collections_error = [], None
    try:
        modalities = backend_client.list_orthanc_modalities_for_admin()
    except backend_client.BackendError as e:
        modalities_error = e.detail
    try:
        collections = backend_client.list_proknow_collections_for_admin()
    except backend_client.BackendError as e:
        collections_error = e.detail

    if request.method == "POST":
        form = AddDestinationForm(request.POST)
        form.set_destination_choices(modalities, collections)
        if form.is_valid():
            try:
                backend_client.add_access(
                    username, form.destination_type, form.destination_value,
                    added_by=request.user.username,
                )
            except backend_client.BackendError as e:
                messages.error(request, f"Could not add destination: {e.detail}")
            else:
                messages.success(request, "Destination added.")
            return redirect("accounts:user_access", username=username)
    else:
        form = AddDestinationForm()
        form.set_destination_choices(modalities, collections)

    try:
        destinations = backend_client.list_access(username)
    except backend_client.BackendError as e:
        messages.error(request, f"Could not load destinations: {e.detail}")
        destinations = []

    return render(request, "accounts/user_access.html", {
        "target_user": target_user,
        "destinations": destinations,
        "form": form,
        "modalities_error": modalities_error,
        "collections_error": collections_error,
    })


@login_required
@user_passes_test(_is_data_custodian)
def user_access_remove(request, username, destination_id):
    if request.method == "POST":
        try:
            backend_client.remove_access(username, destination_id)
        except backend_client.BackendError as e:
            messages.error(request, f"Could not remove destination: {e.detail}")
        else:
            messages.success(request, "Destination removed.")
    return redirect("accounts:user_access", username=username)
