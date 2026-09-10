"""
WTForms form for routers/error_reports.py (item 06).

CSRF is handled separately and globally (deps.csrf_protect), same as every
other form in this project -- nothing to disable here either.
"""
from wtforms import BooleanField, Form, RadioField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length
from wtforms.validators import Optional as OptionalField


class ErrorReportForm(Form):
    CATEGORY_CHOICES = [("feedback", "General feedback / suggestion"), ("error", "Error / problem")]

    category = RadioField("Category", choices=CATEGORY_CHOICES, default="feedback", validators=[DataRequired()])
    urgent = BooleanField(
        "Urgent",
        description="For time-sensitive issues, e.g. a possible data leak. Does not send an email today -- a data custodian follows up separately.",
    )
    job_id = StringField(
        "Job ID (optional)", validators=[OptionalField(), Length(max=255)],
        description="If this relates to a specific job, its ID (visible on the job's page).",
    )
    message = TextAreaField("Message", validators=[DataRequired()])
