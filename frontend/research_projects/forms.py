from django import forms

from research_projects.models import ProjectDocument


class CreateProjectForm(forms.Form):
    title = forms.CharField(max_length=255)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), required=False)
    ethics_reference = forms.CharField(
        max_length=255, required=False,
        label="Ethics/IRAS reference",
        help_text="e.g. an IRAS or REC approval number, if already issued.",
    )


class ReviewProjectForm(forms.Form):
    DECISION_CHOICES = [("approve", "Approve"), ("reject", "Reject")]

    decision = forms.ChoiceField(choices=DECISION_CHOICES, widget=forms.RadioSelect)
    comment = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)
    expiry_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Required when approving. Leave blank for no expiry.",
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("decision") == "approve" and not cleaned.get("expiry_date"):
            raise forms.ValidationError("An expiry date is required when approving a project.")
        return cleaned


class AddMemberForm(forms.Form):
    username = forms.CharField(max_length=150)
    role = forms.ChoiceField(choices=[("member", "Member"), ("owner", "Owner")], initial="member")


class ProjectDocumentForm(forms.ModelForm):
    class Meta:
        model = ProjectDocument
        fields = ["file"]
