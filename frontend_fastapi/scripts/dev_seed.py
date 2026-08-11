"""
python -m frontend_fastapi.scripts.dev_seed

Idempotent dev-only bootstrap for the local docker-compose.dev.yml stack:
creates the first admin account (and one ordinary user) directly in this
project's own local user table.

Unlike Django's frontend/, this project has no admin panel and no
createsuperuser equivalent -- and its own break-glass scripts
(reset_password.py, set_staff.py) explicitly say bootstrapping the very
first account is "a separate, open problem neither script solves" (see
scripts/set_staff.py's docstring). This is that missing piece, scoped to
local dev only -- it does not follow scripts/_common.py's
mutate-existing-user pattern, since there is deliberately no existing user
to mutate the first time this runs.

Runs migrations itself first: frontend_fastapi.main's lifespan normally
does this, but this script runs as a separate process before uvicorn
starts (see docker-compose.dev.yml's frontend_fastapi command chain), so
the users table may not exist yet otherwise.

`alice` is deliberately the same username backend/scripts/dev_seed.py seeds
as a project_memberships row in HermesDB, so logging in as her exercises
the ethics gate against real membership data.
"""
from frontend_fastapi.database import SessionLocal
from frontend_fastapi.migrations import run_migrations
from frontend_fastapi.models import User
from frontend_fastapi.security import hash_password
from frontend_fastapi.settings import DATABASE_URL

# username, password, first_name, last_name, email, department, is_staff, is_superuser
_USERS = [
    ("admin", "admin123", "Dev", "Admin", "admin@example.test", "Dev Seed", True, True),
    ("alice", "alice123", "Alice", "Researcher", "alice@example.test", "Dev Seed", False, False),
]


def main() -> None:
    run_migrations(DATABASE_URL)

    db = SessionLocal()
    try:
        for username, password, first_name, last_name, email, department, is_staff, is_superuser in _USERS:
            if db.query(User).filter_by(username=username).one_or_none() is not None:
                print(f"User {username!r} already exists; skipping.")
                continue
            db.add(User(
                username=username,
                email=email,
                first_name=first_name,
                last_name=last_name,
                department=department,
                password_hash=hash_password(password),
                is_staff=is_staff,
                is_superuser=is_superuser,
                is_active=True,
            ))
            db.commit()
            print(f"Created user {username!r}.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
