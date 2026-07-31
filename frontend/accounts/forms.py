from django import forms
from django.contrib.auth import get_user_model

from accounts.models import Profile

User = get_user_model()


class InviteUserForm(forms.Form):
    username = forms.CharField(max_length=150)
    email = forms.EmailField()
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    department = forms.CharField(max_length=200, required=False)
    is_staff = forms.BooleanField(
        required=False,
        label="Data custodian (can review ethics projects)",
        help_text="Grants access to the project-review queue. Does not affect the ethics gate itself.",
    )

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("A user with that username already exists.")
        return username

    def save(self) -> User:
        user = User.objects.create(
            username=self.cleaned_data["username"],
            email=self.cleaned_data["email"],
            first_name=self.cleaned_data["first_name"],
            last_name=self.cleaned_data["last_name"],
            is_staff=self.cleaned_data["is_staff"],
            is_active=True,
        )
        user.set_unusable_password()
        user.save()
        Profile.objects.create(user=user, department=self.cleaned_data["department"])
        return user
