from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password

from accounts.models import Profile

User = get_user_model()


class HermesAuthenticationForm(AuthenticationForm):
    remember_me = forms.BooleanField(required=False, initial=True, label="Remember me")


class _UserIdentityForm(forms.Form):
    """Shared identity fields for both account-creation paths."""

    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
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

    def _create_user(self, **extra) -> User:
        user = User.objects.create(
            username=self.cleaned_data["username"],
            email=self.cleaned_data["email"],
            first_name=self.cleaned_data["first_name"],
            last_name=self.cleaned_data["last_name"],
            is_staff=self.cleaned_data["is_staff"],
            is_active=True,
            **extra,
        )
        Profile.objects.create(user=user, department=self.cleaned_data["department"])
        return user


class InviteUserForm(_UserIdentityForm):
    """Creates an unusable-password account and an activation link the
    invitee uses to set their own password. Needs a real address to be any
    use at all, so overrides email back to required (subclass field
    declarations replace the inherited one -- doesn't affect CreateUserForm)."""

    email = forms.EmailField()

    def save(self) -> User:
        user = self._create_user()
        user.set_unusable_password()
        user.save()
        return user


class CreateUserForm(_UserIdentityForm):
    """Admin sets the password directly -- no email, no activation link,
    account usable immediately."""

    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("The two password fields didn't match.")
        if password1:
            # An unsaved instance so validators like UserAttributeSimilarityValidator
            # can check the password against the identity fields above.
            temp_user = User(
                username=self.cleaned_data.get("username", ""),
                email=self.cleaned_data.get("email", ""),
                first_name=self.cleaned_data.get("first_name", ""),
                last_name=self.cleaned_data.get("last_name", ""),
            )
            validate_password(password1, temp_user)
        return password2

    def save(self) -> User:
        user = self._create_user()
        user.set_password(self.cleaned_data["password1"])
        user.save()
        return user


class AddDestinationForm(forms.Form):
    """
    Add one destination to a user's export allow-list (accounts/views.py's
    user_access, safety-plan §A). A single field, not a type radio +
    separate dropdown -- the view populates choices from live
    backend_client.get_orthanc_modalities/get_proknow_collections calls
    (same source the export forms themselves use, so an admin only ever
    picks a real, currently-registered destination), grouped into two
    optgroups. The value encodes "<destination_type>:<destination>" so a
    single ChoiceField can cover both kinds without any JS to swap a second
    field's options based on the first.
    """
    destination = forms.ChoiceField(label="Destination", choices=[])

    def set_destination_choices(self, modalities: list[str], collections: list[str]) -> None:
        self.fields["destination"].choices = [
            ("DICOM modalities", [(f"dicom_modality:{m}", m) for m in modalities]),
            ("ProKnow collections", [(f"proknow_collection:{c}", c) for c in collections]),
        ]

    def clean_destination(self):
        value = self.cleaned_data["destination"]
        destination_type, sep, destination = value.partition(":")
        if not sep or destination_type not in ("dicom_modality", "proknow_collection") or not destination:
            raise forms.ValidationError("Invalid destination selection.")
        return value

    @property
    def destination_type(self) -> str:
        return self.cleaned_data["destination"].split(":", 1)[0]

    @property
    def destination_value(self) -> str:
        return self.cleaned_data["destination"].split(":", 1)[1]
