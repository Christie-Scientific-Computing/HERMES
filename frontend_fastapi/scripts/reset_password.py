"""
python -m frontend_fastapi.scripts.reset_password <username> <new_password>

Break-glass password reset -- for when a data custodian is locked out (or
there isn't one yet, on a fresh deployment) and the normal invite/activate
flow isn't an option. Bypasses the invite/activate flow entirely: sets the
password directly, no email, no token.
"""
import argparse
import sys

from frontend_fastapi import security
from frontend_fastapi.database import SessionLocal
from frontend_fastapi.models import User


def reset_password(username: str, new_password: str) -> bool:
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(username=username).one_or_none()
        if user is None:
            return False
        user.password_hash = security.hash_password(new_password)
        db.commit()
        return True
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("new_password")
    args = parser.parse_args()

    if reset_password(args.username, args.new_password):
        print(f"Password reset for {args.username!r}.")
    else:
        print(f"No such user: {args.username!r}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
