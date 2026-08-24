"""
Shared scaffolding for break-glass scripts (reset_password.py, set_staff.py):
the "load a user by username, mutate it, commit" pattern every one of them
needs, so a future third script doesn't paste a fourth copy of the same
five lines.
"""
from typing import Callable

from frontend_fastapi.database import SessionLocal
from frontend_fastapi.models import User


def mutate_user_by_username(username: str, mutate: Callable[[User], None]) -> bool:
    """Loads the user, applies `mutate` in place, commits. Returns False
    (a no-op, nothing committed) if no such user exists."""
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(username=username).one_or_none()
        if user is None:
            return False
        mutate(user)
        db.commit()
        return True
    finally:
        db.close()
