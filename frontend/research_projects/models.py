from django.db import models


def ethics_document_upload_path(instance, filename: str) -> str:
    return f"ethics_documents/{instance.project_id}/{filename}"


class ProjectDocument(models.Model):
    """
    Ethics-certificate upload for a research project. Django-local by
    necessity, not choice: the project's *workflow state* (status,
    membership, expiry) is backend-owned (HermesDB, via the /projects API),
    but nothing in backend/ has a file-storage story, so the document
    itself lives here. `project_id` is a plain CharField, not a real FK --
    same cross-database reasoning as project_memberships.username in
    backend/src/projects/db_client.py: there's no local Project model to
    point a ForeignKey at.
    """

    project_id = models.CharField(max_length=64, db_index=True)
    file = models.FileField(upload_to=ethics_document_upload_path)
    uploaded_by = models.CharField(max_length=150)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return f"Document for {self.project_id} ({self.file.name})"
