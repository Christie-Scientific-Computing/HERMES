"""
WTForms forms for routers/accounts.py. Port of accounts/forms.py (Django).

CSRF is handled separately and globally (deps.csrf_protect), not by these
forms -- plain wtforms.Form has no CSRF machinery of its own to disable
(that's a Flask-WTF feature), so there's nothing to turn off here.
"""
from wtforms import BooleanField, Form, PasswordField, StringField, ValidationError
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional

from frontend_fastapi import security


class LoginForm(Form):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember_me = BooleanField("Remember me", default=True)


class _IdentityForm(Form):
    """Shared identity fields for both account-creation paths -- mirrors
    accounts/forms.py's _UserIdentityForm."""

    username = StringField("Username", validators=[DataRequired(), Length(max=150)])
    email = StringField("Email", validators=[Optional(), Email()])
    first_name = StringField("First name", validators=[Optional(), Length(max=150)])
    last_name = StringField("Last name", validators=[Optional(), Length(max=150)])
    department = StringField("Department", validators=[Optional(), Length(max=200)])
    is_staff = BooleanField(
        "Data custodian (can review ethics projects)",
        description="Grants access to the project-review queue. Does not affect the ethics gate itself.",
    )


class InviteUserForm(_IdentityForm):
    """Creates an unusable-password account and an activation link the
    invitee uses to set their own password. Needs a real address to be any
    use at all, so overrides email back to required."""

    email = StringField("Email", validators=[DataRequired(), Email()])


def _validate_password_strength(form: Form, field) -> None:
    errors = security.password_strength_errors(
        field.data or "", username=form.username.data or "", email=form.email.data or ""
    )
    if errors:
        raise ValidationError(errors[0])


class CreateUserForm(_IdentityForm):
    """Admin sets the password directly -- no email, no activation link,
    account usable immediately."""

    password1 = PasswordField("Password", validators=[DataRequired(), _validate_password_strength])
    password2 = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password1", message="The two password fields didn't match.")],
    )


class ActivateForm(Form):
    """Sets a password on an invited (unusable-password) account. No
    username/email fields here (the token already identifies the account)
    -- routers/accounts.py's activate_account runs the same
    security.password_strength_errors check directly against the target
    user's own username/email after this form's own validation passes."""

    password1 = PasswordField("New password", validators=[DataRequired()])
    password2 = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password1", message="The two password fields didn't match.")],
    )
