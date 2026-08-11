"""
python -m frontend_fastapi.scripts.set_staff <username> [--unset]

Break-glass staff-flag fix -- for when a user's is_staff (data custodian)
flag is wrong and there's no other staff user around who can fix it via
the normal Users page (accounts/users). Grants by default; --unset revokes.

Does NOT create the user if they don't already exist -- see
scripts/reset_password.py's sibling script for the same constraint, and
docs/frontend-rewrite-implementation-plan.md's Phase 1 section for why
bootstrapping a deployment's very first account is a separate, open
problem neither script solves.
"""
import argparse
import sys

from frontend_fastapi.scripts._common import mutate_user_by_username


def set_staff(username: str, is_staff: bool) -> bool:
    return mutate_user_by_username(username, lambda user: setattr(user, "is_staff", is_staff))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--unset", action="store_true", help="Revoke data-custodian access instead of granting it.")
    args = parser.parse_args()

    if set_staff(args.username, is_staff=not args.unset):
        verb = "revoked from" if args.unset else "granted to"
        print(f"Data-custodian access {verb} {args.username!r}.")
    else:
        print(f"No such user: {args.username!r}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
