"""
WTForms forms for routers/research_projects.py. Port of research_projects/forms.py
(Django) -- see that file for the fields being matched. No ProjectDocumentForm
here: file uploads are handled directly via FastAPI's UploadFile in the
router, which is simpler than routing a single file field through WTForms.

CSRF is handled separately and globally (deps.csrf_protect), same as
forms/accounts.py -- nothing to disable here either.
"""
from wtforms import (
    BooleanField, DateField, Form, IntegerField, RadioField, SelectField,
    SelectMultipleField, StringField, TextAreaField, ValidationError,
)
from wtforms.validators import DataRequired, Length, NumberRange
from wtforms.validators import Optional as OptionalField


class _DestinationPickerForm(Form):
    """Shared by CreateProjectForm and AmendProjectForm -- both need the
    identical two-step destination picker (a type toggle revealing the
    matching multi-select; selections from both types can coexist per
    GRILLING.md #11) plus the project-level message_id field."""

    use_dicom = BooleanField("Export via DICOM")
    dicom_destinations = SelectMultipleField("Orthanc modality AE title(s)", choices=[], validators=[OptionalField()])
    use_proknow = BooleanField("Export via ProKnow")
    proknow_destinations = SelectMultipleField("ProKnow collection(s)", choices=[], validators=[OptionalField()])
    # Project-level, not per-job (GRILLING.md #4) -- see F001's own
    # JobSubmissionForm, which no longer carries this field at all.
    message_id = IntegerField(
        "Message ID (optional)", validators=[OptionalField(), NumberRange(min=0, max=65535)],
        description="For clinical-trial patients: the DICOM Message ID the receiving "
                     "anonymising node uses to pick a pseudonymisation table. Requires "
                     "data-custodian approval, same as the project itself.",
    )

    def set_destination_choices(self, modalities: list[str], collections: list[str]) -> None:
        self.dicom_destinations.choices = [(v, v) for v in modalities]
        self.proknow_destinations.choices = [(v, v) for v in collections]

    def validate(self, extra_validators=None) -> bool:
        valid = super().validate(extra_validators=extra_validators)
        if self.use_dicom.data and not self.dicom_destinations.data:
            self.dicom_destinations.errors.append("Choose at least one Orthanc destination.")
            valid = False
        if self.use_proknow.data and not self.proknow_destinations.data:
            self.proknow_destinations.errors.append("Choose at least one ProKnow collection.")
            valid = False
        return valid

    def destinations(self) -> list[dict]:
        """The picker's current selections, in the shape
        backend_client.create_project/propose_amendment expect."""
        picked = []
        if self.use_dicom.data:
            picked += [{"destination_type": "dicom", "destination_value": v} for v in self.dicom_destinations.data]
        if self.use_proknow.data:
            picked += [{"destination_type": "proknow", "destination_value": v} for v in self.proknow_destinations.data]
        return picked


class CreateProjectForm(_DestinationPickerForm):
    title = StringField("Title", validators=[DataRequired(), Length(max=255)])
    description = TextAreaField("Description", validators=[DataRequired()])
    ethics_reference = StringField(
        "Ethics/IRAS reference", validators=[OptionalField(), Length(max=255)],
        description="e.g. an IRAS or REC approval number, if already issued.",
    )


class AmendProjectForm(_DestinationPickerForm):
    """Proposes a full replacement destination set (not an incremental
    diff) plus/or a new message_id -- see ProjectsDB.propose_amendment's
    own docstring."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_errors: list[str] = []

    def validate(self, extra_validators=None) -> bool:
        self.form_errors = []
        valid = super().validate(extra_validators=extra_validators)
        # Unlike CreateProjectForm (where "no destinations/message_id yet,
        # add them later" is a legitimate empty project), an amendment
        # that proposes nothing has no meaning -- without this, submitting
        # both toggles off and a blank message_id silently "succeeds" with
        # nothing for a reviewer to ever see or decide on.
        if valid and not self.destinations() and self.message_id.data is None:
            self.form_errors.append("Propose at least one destination change or a new message ID.")
            valid = False
        return valid


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


class AmendmentDecisionForm(Form):
    """Approve/reject a pending amendment -- simpler than ReviewProjectForm:
    an amendment has no expiry_date of its own to set."""

    DECISION_CHOICES = [("approve", "Approve"), ("reject", "Reject")]

    decision = RadioField("Decision", choices=DECISION_CHOICES, validators=[DataRequired()])
    comment = TextAreaField("Comment", validators=[OptionalField()])
