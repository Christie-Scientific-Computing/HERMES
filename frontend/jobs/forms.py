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


class SingleImportForm(ProjectScopedForm):
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100)
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")


class BatchImportForm(ProjectScopedForm):
    file = forms.FileField(label="CSV file (patient_id column)")
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")


class DicomExportForm(ProjectScopedForm):
    file = forms.FileField(label="CSV file (patient_id column)")
    destination = forms.ChoiceField(label="Orthanc modality AE title", choices=[])
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

    def set_destination_choices(self, modalities: list[str]) -> None:
        self._set_choices("destination", modalities)


class ProKnowExportForm(ProjectScopedForm):
    file = forms.FileField(label="CSV file (patient_id column)")
    collection = forms.ChoiceField(label="ProKnow collection", choices=[])

    def set_collection_choices(self, collections: list[str]) -> None:
        self._set_choices("collection", collections)


class CombinedSingleDicomForm(ProjectScopedForm):
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100)
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")
    destination = forms.ChoiceField(label="Orthanc modality AE title", choices=[])
    message_id = forms.IntegerField(
        label="Message ID (optional)", required=False, min_value=0, max_value=65535,
        help_text="For clinical-trial patients: the DICOM Message ID the receiving "
                   "anonymising node uses to pick a pseudonymisation table. Leave blank "
                   "for an ordinary export.",
    )

    def set_destination_choices(self, modalities: list[str]) -> None:
        self._set_choices("destination", modalities)


class CombinedSingleProKnowForm(ProjectScopedForm):
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100)
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")
    collection = forms.ChoiceField(label="ProKnow collection", choices=[])

    def set_collection_choices(self, collections: list[str]) -> None:
        self._set_choices("collection", collections)


class CombinedBatchDicomForm(ProjectScopedForm):
    file = forms.FileField(label="CSV file (patient_id column)")
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")
    destination = forms.ChoiceField(label="Orthanc modality AE title", choices=[])
    message_id = forms.IntegerField(
        label="Message ID (optional)", required=False, min_value=0, max_value=65535,
        help_text="For clinical-trial patients: the DICOM Message ID the receiving "
                   "anonymising node uses to pick a pseudonymisation table. Leave blank "
                   "for an ordinary export.",
    )

    def set_destination_choices(self, modalities: list[str]) -> None:
        self._set_choices("destination", modalities)


class CombinedBatchProKnowForm(ProjectScopedForm):
    file = forms.FileField(label="CSV file (patient_id column)")
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")
    collection = forms.ChoiceField(label="ProKnow collection", choices=[])

    def set_collection_choices(self, collections: list[str]) -> None:
        self._set_choices("collection", collections)


class JobLookupForm(forms.Form):
    job_id = forms.CharField(max_length=100)


class PatientLookupForm(forms.Form):
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100)
    job_id = forms.CharField(max_length=100, required=False, help_text="Leave blank to search across all jobs.")
