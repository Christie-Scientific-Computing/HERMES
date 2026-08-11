"""
Password hashing and signed account tokens (invite / activate / reset).

Deliberately pure functions with no DB or Request access -- callers (deps.py,
auth.py, and Phase 1's accounts router) own looking up the User row; this
module only ever operates on values passed in. Keeps this file trivially
unit-testable and reusable from the Phase 1 break-glass CLI scripts, which
have no request/session context at all.
"""
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from frontend_fastapi.settings import SECRET_KEY

_hasher = PasswordHasher()


def hash_password(raw_password: str) -> str:
    return _hasher.hash(raw_password)


def verify_password(raw_password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, raw_password)
    except VerifyMismatchError:
        return False
    except Exception:
        # argon2 raises InvalidHash for a malformed/foreign hash (e.g. a
        # fixture typo, an unusable-password sentinel, or a row from a
        # hashing scheme this app never wrote) -- treat any of that as
        # "doesn't match", not a 500. This is also what makes
        # unusable_password()'s sentinel actually unusable: it isn't a
        # valid argon2 hash, so verification always lands here.
        return False


_UNUSABLE_PASSWORD_PREFIX = "!"


def unusable_password() -> str:
    """An invited-but-not-yet-activated account has no password to check
    against yet -- this sentinel guarantees verify_password() always
    returns False for it (see the except-branch comment above), the same
    role Django's User.set_unusable_password() plays."""
    return _UNUSABLE_PASSWORD_PREFIX + _random_token()


def is_usable_password(password_hash: str) -> bool:
    return not password_hash.startswith(_UNUSABLE_PASSWORD_PREFIX)


# --- Password strength -------------------------------------------------------
#
# Partial parity with Django's AUTH_PASSWORD_VALIDATORS
# (frontend/hermes_frontend/settings.py) -- an explicit, documented decision
# per docs/frontend-rewrite-implementation-plan.md Phase 0's open item, not
# a silent drop. Covers the two checks that need no external data
# (MinimumLengthValidator, and a cheap approximation of
# UserAttributeSimilarityValidator). Deliberately does NOT port
# CommonPasswordValidator (Django's ~20k-entry common-password wordlist) --
# vendoring or replacing it (e.g. with a zxcvbn strength score) is a
# separate decision for whoever owns this deployment's actual password
# policy, not something to guess at here.
_MIN_PASSWORD_LENGTH = 8


def password_strength_errors(password: str, *, username: str = "", email: str = "") -> list[str]:
    errors = []
    if len(password) < _MIN_PASSWORD_LENGTH:
        errors.append(f"This password is too short. It must contain at least {_MIN_PASSWORD_LENGTH} characters.")
    lowered = password.lower()
    if username and username.lower() in lowered:
        errors.append("This password is too similar to the username.")
    email_local = email.split("@")[0] if email else ""
    if email_local and len(email_local) > 3 and email_local.lower() in lowered:
        errors.append("This password is too similar to the email address.")
    return errors


def _random_token() -> str:
    return secrets.token_urlsafe(32)


def new_session_id() -> str:
    return _random_token()


def new_csrf_token() -> str:
    return _random_token()


# --- Account tokens (invite / activate / password reset) --------------------
#
# One signed, timed token format for every "prove you own this account"
# flow. Mirrors Django's default_token_generator (uidb64 + token, two path
# segments) as a single opaque token instead, carrying the user id itself.
#
# The token embeds a short fingerprint of the user's CURRENT password_hash
# at signing time, not just the user id -- this is what makes a token
# single-use in practice: once activate/reset actually changes the
# password, the fingerprint on file no longer matches, so a re-submitted or
# intercepted-but-already-used token stops verifying. Without this, the
# token would keep working (until expiry) even after the account it grants
# access to has already been activated.
_ACCOUNT_TOKEN_SALT = "hermes-account-token"
_ACCOUNT_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24 * 3  # 3 days

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt=_ACCOUNT_TOKEN_SALT)


def _password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:12]


def make_account_token(user_id: int, password_hash: str) -> str:
    return _serializer.dumps({"uid": user_id, "pwfp": _password_fingerprint(password_hash)})


def read_account_token(token: str) -> dict | None:
    """Returns the signed {"uid", "pwfp"} payload if `token`'s signature is
    valid and it hasn't expired -- None otherwise. Does NOT check the
    fingerprint against a live user (this module has no DB access); pair
    with account_token_matches() once the caller has looked the user up."""
    try:
        return _serializer.loads(token, max_age=_ACCOUNT_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def account_token_matches(token_data: dict, current_password_hash: str) -> bool:
    """True if `token_data` (from read_account_token) was issued against the
    password hash the account currently has -- i.e. the account hasn't been
    activated/reset since this token was minted."""
    return token_data.get("pwfp") == _password_fingerprint(current_password_hash)
