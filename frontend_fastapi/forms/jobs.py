"""
WTForms forms for routers/jobs.py. Port of jobs/forms.py (Django) -- see
that file for the fields being matched.

`file` is deliberately NOT a field on JobSubmissionForm, for the same reason
forms/research_projects.py keeps document uploads out of WTForms entirely:
a single file field is simpler handled directly via FastAPI's UploadFile/raw
multipart form in the router than routed through WTForms. The "file
required for a batch job" cross-field check Django's clean() does is
instead expressed here as a `file_provided` flag the router sets on the
form instance (`form.file_provided = bool(...)`) before calling validate()
-- mirroring how set_project_choices/set_destination_choices/
set_collection_choices already have to be set fresh by the router on every
request, per ProjectScopedForm's own docstring below. validate() populates
`file_errors` (a plain list, since there's no real field to attach errors
to) which the template renders next to the file input the same way every
other field's errors are shown.

CSRF is handled separately and globally (deps.csrf_protect), same as every
other form module here -- nothing to disable.
"""
from wtforms import BooleanField, Form, IntegerField, RadioField, SelectField, StringField
from wtforms.validators import DataRequired, Length, NumberRange
from wtforms.validators import Optional as OptionalField

IMPORT_LEVEL_CHOICES = [
    ("Planning data", "Planning data"),
    ("Images only", "Images only"),
    ("Everything", "Everything"),
]


class _OptionalSelectField(SelectField):
    """
    A SelectField whose choices are only checked against a submitted value
    when a value was actually submitted -- WTForms' own SelectField.pre_validate
    always requires membership (even for an empty/unselected value, and even
    combined with an Optional() validator, since pre_validate runs before the
    validators chain regardless). Django's ChoiceField, by contrast, only
    checks `if value and not self.valid_value(value)` -- this replicates
    that. Needed because destination/collection legitimately go unselected
    in many valid submissions (do_export unchecked, or the irrelevant half
    of export_kind) while still carrying live-fetched choices for the
    JS-toggled half of the form.
    """

    def pre_validate(self, form):
        if not self.data:
            return
        super().pre_validate(form)


class ProjectScopedForm(Form):
    """
    Base for every form that starts a job. `project_id`'s choices must be
    set by the router, freshly, on every request -- both the GET render and
    the POST reconstruction-for-validation -- from
    backend_client.list_user_active_projects(...) (plus
    backend_client.ensure_superuser_bypass_project(...) first, for
    superusers). Never populate this from session or reuse choices computed
    earlier in the request: this ChoiceField's own validation against
    freshly-fetched choices IS the live re-check that a submitted
    project_id is one the user currently has active access to.
    """

    project_id = SelectField("Project", choices=[])

    def set_project_choices(self, projects: list[dict]) -> None:
        self.project_id.choices = [(p["project_id"], p["title"]) for p in projects]

    def _set_choices(self, field, values: list[str]) -> None:
        field.choices = [(v, v) for v in values]


class JobSubmissionForm(ProjectScopedForm):
    """
    One form for every import/export combination: single patient or batch
    (CSV), import and/or export, DICOM or ProKnow. Every field stays
    optional at the WTForms level -- the hidden half of the form is still
    present in the DOM (just CSS-hidden by submit_job.html's JS toggles) and
    so still gets submitted, and a SelectField with dynamically-populated
    choices can legitimately be empty (Orthanc/ProKnow unreachable) even
    when it *is* the relevant half. validate() below enforces what's
    actually required given the scope/do_import/do_export combination,
    mirroring the server-side check in
    backend/src/retrieve/endpoints.py's batch_import_file.
    """

    scope = RadioField(choices=[("single", "Single patient"), ("batch", "Batch (CSV)")], default="single")
    mrn = StringField("Patient ID (anon)", validators=[OptionalField(), Length(max=100)])

    do_import = BooleanField("Import", default=True)
    import_level = SelectField(choices=IMPORT_LEVEL_CHOICES, default="Planning data", validators=[OptionalField()])

    do_export = BooleanField(
        "Also export", description="Runs automatically once each patient's import succeeds.",
    )
    export_kind = RadioField(
        choices=[("dicom_move", "DICOM"), ("proknow_upload", "ProKnow")],
        default="dicom_move", validators=[OptionalField()],
    )
    destination = _OptionalSelectField("Orthanc modality AE title", choices=[], validators=[OptionalField()])
    # Optional DICOM C-MOVE Message ID -- forwarded to Orthanc as
    # MoveOriginatorID (see backend/src/export/logic.py's
    # Exporter.dicom_c_move) so a receiving anonymising node on the DMZ can
    # pick the right pseudonymisation table (e.g. for clinical-trial
    # patients, who need a different PatientID mapping than routine
    # pseudo-anonymisation). Left blank for an ordinary export. min/max match
    # DICOM's Message ID VR (US, unsigned 16-bit): 0-65535.
    message_id = IntegerField(
        "Message ID (optional)", validators=[OptionalField(), NumberRange(min=0, max=65535)],
        description="For clinical-trial patients: the DICOM Message ID the receiving "
                     "anonymising node uses to pick a pseudonymisation table. Leave blank "
                     "for an ordinary export.",
    )
    collection = _OptionalSelectField("ProKnow collection", choices=[], validators=[OptionalField()])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set by the router before validate() is called -- see module
        # docstring. Defaulting to False here (rather than leaving it unset)
        # means a form that's only ever rendered (GET, never validated)
        # still has a sane value if a template checks it.
        self.file_provided = False
        self.form_errors: list[str] = []
        self.file_errors: list[str] = []

    def set_destination_choices(self, modalities: list[str]) -> None:
        self._set_choices(self.destination, modalities)

    def set_collection_choices(self, collections: list[str]) -> None:
        self._set_choices(self.collection, collections)

    def validate(self, extra_validators=None) -> bool:
        self.form_errors = []
        self.file_errors = []
        valid = super().validate(extra_validators=extra_validators)

        if not self.do_import.data and not self.do_export.data:
            # Matches Django's clean(), which `raise`s here -- stopping
            # immediately rather than also evaluating the mrn/file/export
            # checks below. Per-field errors already recorded by
            # super().validate() (e.g. an invalid project_id) still stand;
            # only the cross-field checks below are skipped.
            self.form_errors.append("Choose to import, export, or both.")
            return False

        if self.scope.data == "single" and not self.mrn.data:
            self.mrn.errors.append("Required for a single patient.")
            valid = False
        elif self.scope.data == "batch" and not self.file_provided:
            self.file_errors.append("Required for a batch (CSV) job.")
            valid = False

        if self.do_export.data:
            if not self.export_kind.data:
                self.export_kind.errors.append("Choose a destination type.")
                valid = False
            elif self.export_kind.data == "dicom_move" and not self.destination.data:
                self.destination.errors.append("Required when exporting via DICOM.")
                valid = False
            elif self.export_kind.data == "proknow_upload" and not self.collection.data:
                self.collection.errors.append("Required when exporting via ProKnow.")
                valid = False

        return valid


class JobLookupForm(Form):
    job_id = StringField("Job ID", validators=[DataRequired(), Length(max=100)])


class PatientLookupForm(Form):
    mrn = StringField("Patient ID (anon)", validators=[DataRequired(), Length(max=100)])
    job_id = StringField(
        "Job ID", validators=[OptionalField(), Length(max=100)],
        description="Leave blank to search across all jobs.",
    )
