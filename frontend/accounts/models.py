from django.conf import settings
from django.db import models


class Profile(models.Model):
    """Extra, non-auth fields for a Django user. Django's own auth/session
    storage (see hermes_frontend/settings.py) -- not backend/HermesDB data."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    department = models.CharField(max_length=200, blank=True, default="")

    def __str__(self) -> str:
        return f"Profile({self.user.username})"
