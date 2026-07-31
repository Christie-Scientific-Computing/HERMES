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


class SingleImportForm(ProjectScopedForm):
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100)
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")


class BatchImportForm(ProjectScopedForm):
    file = forms.FileField(label="CSV file (patient_id column)")
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")


class DicomExportForm(ProjectScopedForm):
    file = forms.FileField(label="CSV file (patient_id column)")
    destination = forms.CharField(label="Orthanc modality AE title", max_length=200)


class ProKnowExportForm(ProjectScopedForm):
    file = forms.FileField(label="CSV file (patient_id column)")
    collection = forms.CharField(label="ProKnow collection", max_length=200)


class JobLookupForm(forms.Form):
    job_id = forms.CharField(max_length=100)


class PatientLookupForm(forms.Form):
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100)
    job_id = forms.CharField(max_length=100, required=False, help_text="Leave blank to search across all jobs.")
