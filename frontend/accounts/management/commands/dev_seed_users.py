"""
python manage.py dev_seed_users

Idempotent dev-only bootstrap for the local docker-compose.dev.yml stack:
creates the first admin account (and one ordinary user) directly in
Django's own user table. Needed because accounts/'s invite flow
(accounts/views.py:invite_user) requires an existing staff user to invite
anyone from -- with zero users, there's no way in through the app itself,
and Django admin isn't registered as a fallback here (see CLAUDE.md /
docs/frontend-migration.md 2.4 on that being dropped in the FastAPI rewrite;
this command exists for local dev regardless of which frontend is running).

Run automatically by docker-compose.dev.yml's `frontend` service on every
container start -- cheap, and a no-op once the users already exist.

`alice` is deliberately the same username backend/scripts/dev_seed.py seeds
as a project_memberships row in HermesDB, so logging in as her exercises
the ethics gate against real membership data.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Profile

# username, password, first_name, last_name, email, is_staff, is_superuser
_USERS = [
    ("admin", "admin123", "Dev", "Admin", "admin@example.test", True, True),
    ("alice", "alice123", "Alice", "Researcher", "alice@example.test", False, False),
]


class Command(BaseCommand):
    help = "Create the dev-only seed users (admin/admin123, alice/alice123) if they don't already exist."

    def handle(self, *args, **options):
        for username, password, first_name, last_name, email, is_staff, is_superuser in _USERS:
            if User.objects.filter(username=username).exists():
                self.stdout.write(f"User {username!r} already exists; skipping.")
                continue
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username,
                    password=password,
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    is_staff=is_staff,
                    is_superuser=is_superuser,
                )
                Profile.objects.create(user=user, department="Dev Seed")
            self.stdout.write(self.style.SUCCESS(f"Created user {username!r}."))
