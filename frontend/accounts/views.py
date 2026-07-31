from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from accounts.forms import CreateUserForm, InviteUserForm

User = get_user_model()


def _is_data_custodian(user) -> bool:
    return user.is_active and user.is_staff


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
