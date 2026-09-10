"""
WTForms forms for local-PACS (Conquest) features: F002's browse/search form
and F003's destination add/edit form.

CSRF is handled separately and globally (deps.csrf_protect), same as every
other form module here -- nothing to disable.
"""
import re

from wtforms import DateField, Form, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Regexp
from wtforms.validators import Optional as OptionalField

# DICOM AE titles: up to 16 characters, printable ASCII excluding backslash
# and control characters (PS3.5 Annex E) -- leading/trailing spaces aren't
# meaningful here since this is a title a human picks, not a padded DIMSE
# field. StringField's own Length + this pattern together are stricter than
# needed for every legal AE title byte value, but cover every title anyone
# would actually type.
_AE_TITLE_RE = re.compile(r"^[A-Za-z0-9 _\-.]{1,16}$")


class LocalPacsSearchForm(Form):
    """F002's search form. At least one field must be provided -- an
    unconstrained browse-everything query isn't the goal (see F002's
    Behaviour) -- enforced by _validate_at_least_one_field below rather than
    per-field DataRequired, since every individual field is independently
    optional."""

    patient_id = StringField("Anonymised patient ID", validators=[OptionalField(), Length(max=64)])
    study_date_from = DateField("Study date from", validators=[OptionalField()])
    study_date_to = DateField("Study date to", validators=[OptionalField()])
    study_description = StringField("Study description", validators=[OptionalField(), Length(max=200)])
    modalities_in_study = StringField(
        "Modalities (e.g. CT, RTSTRUCT)", validators=[OptionalField(), Length(max=64)],
        description="Comma-separated DICOM modality codes.",
    )

    def validate(self, extra_validators=None) -> bool:
        ok = super().validate(extra_validators=extra_validators)
        if not ok:
            return False
        if not any([
            self.patient_id.data, self.study_date_from.data, self.study_date_to.data,
            self.study_description.data, self.modalities_in_study.data,
        ]):
            self.patient_id.errors.append("Provide at least one search field.")
            return False
        if self.study_date_from.data and self.study_date_to.data and self.study_date_from.data > self.study_date_to.data:
            self.study_date_to.errors.append("End date must not be before the start date.")
            return False
        return True


class LocalPacsDestinationForm(Form):
    ae_title = StringField(
        "AE title", validators=[
            DataRequired(), Length(max=16),
            Regexp(_AE_TITLE_RE, message="AE titles may only contain letters, digits, spaces, '_', '-' and '.', up to 16 characters."),
        ],
        description="Conquest must already be configured to reach this AE title -- adding it here only adds it to this list.",
    )
    display_name = StringField("Display name", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("Description (optional)", validators=[OptionalField(), Length(max=500)])
