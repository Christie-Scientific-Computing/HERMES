from django import forms

IMPORT_LEVEL_CHOICES = [
    ("Planning data", "Planning data"),
    ("Images only", "Images only"),
    ("Everything", "Everything"),
]


class ProjectScopedForm(forms.Form):
    """
    Base for every form that starts a job. `project_id`'s choices must be
    set by the view, freshly, on every request -- both the GET render and
    the POST reconstruction-for-validation -- from
    backend_client.list_user_active_projects(request.user.username) (plus
    backend_client.ensure_superuser_bypass_project(...) first, for
    superusers). Never populate this from session or reuse choices computed
    earlier in the request: Django's own ChoiceField validation against
    freshly-fetched choices IS the live re-check that a submitted
    project_id is one the user currently has active access to.
    """
    project_id = forms.ChoiceField(label="Project", choices=[])

    def set_project_choices(self, projects: list[dict]) -> None:
        self.fields["project_id"].choices = [(p["project_id"], p["title"]) for p in projects]

    def _set_choices(self, field_name: str, values: list[str]) -> None:
        self.fields[field_name].choices = [(v, v) for v in values]


class JobSubmissionForm(ProjectScopedForm):
    """
    One form for every import/export combination: single patient or batch
    (CSV), import and/or export, DICOM or ProKnow. The view only ever uses
    the fields relevant to what was actually chosen (do_import/do_export),
    but every field stays `required=False` at the Django level -- the
    hidden half of the form is still present in the DOM (just CSS-hidden by
    submit_job.html's JS toggles) and so still gets submitted, and a
    ChoiceField with dynamically-populated choices can legitimately be
    empty (Orthanc/ProKnow unreachable) even when it *is* the relevant
    half. clean() below enforces what's actually required given the
    scope/do_import/do_export combination, mirroring the server-side check
    in backend/src/retrieve/endpoints.py's batch_import_file.
    """
    scope = forms.ChoiceField(
        choices=[("single", "Single patient"), ("batch", "Batch (CSV)")],
        widget=forms.RadioSelect, initial="single",
    )
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100, required=False)
    file = forms.FileField(label="CSV file (patient_id column)", required=False)

    do_import = forms.BooleanField(label="Import", required=False, initial=True)
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, required=False, initial="Planning data")

    do_export = forms.BooleanField(
        label="Also export", required=False,
        help_text="Runs automatically once each patient's import succeeds.",
    )
    export_kind = forms.ChoiceField(
        choices=[("dicom_move", "DICOM"), ("proknow_upload", "ProKnow")],
        widget=forms.RadioSelect, required=False, initial="dicom_move",
    )
    destination = forms.ChoiceField(label="Orthanc modality AE title", choices=[], required=False)
    # Optional DICOM C-MOVE Message ID -- forwarded to Orthanc as
    # MoveOriginatorID (see backend/src/export/logic.py's
    # Exporter.dicom_c_move) so a receiving anonymising node on the DMZ can
    # pick the right pseudonymisation table (e.g. for clinical-trial
    # patients, who need a different PatientID mapping than routine
    # pseudo-anonymisation). Left blank for an ordinary export. min_value/
    # max_value match DICOM's Message ID VR (US, unsigned 16-bit): 0-65535
    # -- Django's NumberInput widget renders these as the input's HTML
    # min/max attributes for free, on top of the server-side check.
    message_id = forms.IntegerField(
        label="Message ID (optional)", required=False, min_value=0, max_value=65535,
        help_text="For clinical-trial patients: the DICOM Message ID the receiving "
                   "anonymising node uses to pick a pseudonymisation table. Leave blank "
                   "for an ordinary export.",
    )
    collection = forms.ChoiceField(label="ProKnow collection", choices=[], required=False)

    def set_destination_choices(self, modalities: list[str]) -> None:
        self._set_choices("destination", modalities)

    def set_collection_choices(self, collections: list[str]) -> None:
        self._set_choices("collection", collections)

    def clean(self):
        cleaned_data = super().clean()
        do_import = cleaned_data.get("do_import")
        do_export = cleaned_data.get("do_export")
        if not do_import and not do_export:
            raise forms.ValidationError("Choose to import, export, or both.")

        scope = cleaned_data.get("scope")
        if scope == "single" and not cleaned_data.get("mrn"):
            self.add_error("mrn", "Required for a single patient.")
        elif scope == "batch" and not cleaned_data.get("file"):
            self.add_error("file", "Required for a batch (CSV) job.")

        if do_export:
            export_kind = cleaned_data.get("export_kind")
            if not export_kind:
                self.add_error("export_kind", "Choose a destination type.")
            elif export_kind == "dicom_move" and not cleaned_data.get("destination"):
                self.add_error("destination", "Required when exporting via DICOM.")
            elif export_kind == "proknow_upload" and not cleaned_data.get("collection"):
                self.add_error("collection", "Required when exporting via ProKnow.")

        return cleaned_data


class JobLookupForm(forms.Form):
    job_id = forms.CharField(max_length=100)


class PatientLookupForm(forms.Form):
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100)
    job_id = forms.CharField(max_length=100, required=False, help_text="Leave blank to search across all jobs.")
