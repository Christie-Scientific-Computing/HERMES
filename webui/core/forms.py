from django import forms

IMPORT_LEVEL_CHOICES = [
    ("Planning data", "Planning data"),
    ("Images only", "Images only"),
    ("Everything", "Everything"),
]


class SingleImportForm(forms.Form):
    mrn = forms.CharField(label="Patient MRN")
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")


class BatchImportForm(forms.Form):
    file = forms.FileField(label="CSV with a patient_id column")
    import_level = forms.ChoiceField(choices=IMPORT_LEVEL_CHOICES, initial="Planning data")


class ExportForm(forms.Form):
    MODE_CHOICES = [("dicom", "DICOM C-MOVE"), ("proknow", "ProKnow upload")]

    file = forms.FileField(label="CSV with a patient_id column")
    mode = forms.ChoiceField(choices=MODE_CHOICES, widget=forms.RadioSelect, initial="dicom")
    destination = forms.CharField(
        required=False, label="Orthanc AE title", help_text="Required for DICOM C-MOVE mode",
    )
    collection = forms.CharField(
        required=False, label="ProKnow collection", help_text="Required for ProKnow upload mode",
    )

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("mode")
        if mode == "dicom" and not cleaned.get("destination"):
            self.add_error("destination", "Required for DICOM C-MOVE mode")
        if mode == "proknow" and not cleaned.get("collection"):
            self.add_error("collection", "Required for ProKnow upload mode")
        return cleaned


class JobLookupForm(forms.Form):
    job_id = forms.CharField(label="Job ID")


class PatientLookupForm(forms.Form):
    mrn = forms.CharField(label="Patient MRN")
    job_id = forms.CharField(label="Job ID (leave blank to see all jobs)", required=False)
