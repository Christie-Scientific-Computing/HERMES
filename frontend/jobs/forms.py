from django import forms

IMPORT_LEVEL_CHOICES = [
    ("Planning data", "Planning data"),
    ("Images only", "Images only"),
    ("Everything", "Everything"),
]


class SingleImportForm(forms.Form):
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100)
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")


class BatchImportForm(forms.Form):
    file = forms.FileField(label="CSV file (patient_id column)")
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")


class DicomExportForm(forms.Form):
    file = forms.FileField(label="CSV file (patient_id column)")
    destination = forms.CharField(label="Orthanc modality AE title", max_length=200)


class ProKnowExportForm(forms.Form):
    file = forms.FileField(label="CSV file (patient_id column)")
    collection = forms.CharField(label="ProKnow collection", max_length=200)


class JobLookupForm(forms.Form):
    job_id = forms.CharField(max_length=100)


class PatientLookupForm(forms.Form):
    mrn = forms.CharField(label="Patient ID (anon)", max_length=100)
    job_id = forms.CharField(max_length=100, required=False, help_text="Leave blank to search across all jobs.")
