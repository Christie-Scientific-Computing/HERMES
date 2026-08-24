"""
WTForms forms for routers/research_projects.py. Port of research_projects/forms.py
(Django) -- see that file for the fields being matched. No ProjectDocumentForm
here: file uploads are handled directly via FastAPI's UploadFile in the
router, which is simpler than routing a single file field through WTForms.

CSRF is handled separately and globally (deps.csrf_protect), same as
forms/accounts.py -- nothing to disable here either.
"""
from wtforms import DateField, Form, RadioField, SelectField, StringField, TextAreaField, ValidationError
from wtforms.validators import DataRequired, Length
from wtforms.validators import Optional as OptionalField


class CreateProjectForm(Form):
    title = StringField("Title", validators=[DataRequired(), Length(max=255)])
    description = TextAreaField("Description", validators=[OptionalField()])
    ethics_reference = StringField(
        "Ethics/IRAS reference", validators=[OptionalField(), Length(max=255)],
        description="e.g. an IRAS or REC approval number, if already issued.",
    )


class ReviewProjectForm(Form):
    DECISION_CHOICES = [("approve", "Approve"), ("reject", "Reject")]

    decision = RadioField("Decision", choices=DECISION_CHOICES, validators=[DataRequired()])
    comment = TextAreaField("Comment", validators=[OptionalField()])
    # No Optional()/DataRequired() here, deliberately: wtforms.validators.Optional
    # raises StopValidation on an empty field, which would skip the inline
    # validate_expiry_date validator below too (it's appended to the same
    # per-field chain) -- exactly the case that needs to run. Leaving this
    # field's own validators empty and doing the "required iff approving"
    # check entirely in validate_expiry_date covers both directions: empty
    # is fine when rejecting, required when approving.
    expiry_date = DateField(
        "Expiry date",
        description="Required when approving. Leave blank for no expiry.",
    )

    def validate_expiry_date(self, field):
        if self.decision.data == "approve" and not field.data:
            raise ValidationError("An expiry date is required when approving a project.")


class AddMemberForm(Form):
    username = StringField("Username", validators=[DataRequired(), Length(max=150)])
    role = SelectField("Role", choices=[("member", "Member"), ("owner", "Owner")], default="member")
