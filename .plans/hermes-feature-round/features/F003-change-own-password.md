# Feature F003: Change your own password

## Purpose

Every password-setting path in `frontend_fastapi` today is admin/token-style
(activation, invite, break-glass CLI reset) — none check a current password,
because identity is proven a different way in each. There's no way for a
logged-in user to change their own password. This feature adds that
self-service path, gated by the current password since the requester is
already authenticated by a session rather than a token.

## Behaviour

The username in the navbar becomes a link to a new page (`/accounts/me`)
containing a bare change-password form: current password, new password,
confirm new password. Submitting a correct current password and a
new password that passes the existing strength rules updates the stored
hash and logs out every other active session belonging to that user — the
current session stays logged in.

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/routers/accounts.py` — existing route patterns to
  follow: `login` (~88-118), `activate_submit` (~227-241, calls
  `security.password_strength_errors(...)` **after** `form.validate()` —
  note the comment there explaining why it must run exactly once, since
  WTForms rebuilds `Field.errors` on each `.validate()` call).
- `frontend_fastapi/forms/accounts.py` — `ActivateForm` (~63-75, hand-rolled
  `password1`/`password2` with `EqualTo`, no old-password field) is the
  closest existing precedent, minus the old-password check this feature
  adds.
- `frontend_fastapi/security.py` — `password_strength_errors` (~71-89, min
  length 8, similarity-to-username/email/name) and `hash_password`/argon2
  verification helpers (~13, 19, 22-23) — reuse both directly.
- `frontend_fastapi/models.py` — `User` model (~40-53), `password_hash:
  Mapped[str]`.
- `frontend_fastapi/scripts/reset_password.py` (~16-18) — reuses the same
  `security.hash_password` the web flow will use; confirms hashing is
  consistent across break-glass and self-service paths.
- `frontend_fastapi/templates/base.html` (~line 132) — plain `<span>` for
  the username; the link target for this feature.
- `frontend_fastapi/deps.py` — `require_login` dependency to gate the new
  routes.

### Likely changes

- `frontend_fastapi/forms/accounts.py` — new `ChangePasswordForm`:
  `old_password`, `password1`, `password2` (`EqualTo`), running
  `password_strength_errors` against the new password.
- `frontend_fastapi/routers/accounts.py` — new `GET/POST /accounts/me`,
  gated by `require_login`. `POST` verifies `old_password` against
  `user.password_hash` via the existing argon2 verify call, validates the
  new password, hashes and saves it, then invalidates every other session
  row for that user.
- A new method on whatever module owns session persistence (likely near
  `frontend_fastapi/session_middleware.py` or a `SessionsDB`-equivalent) to
  delete/invalidate all session rows for a username except the current
  session's own row. **Verify the sessions table's exact schema (does it
  store `username`/`user_id` directly?) before implementing this** — see
  Open issues in `plan.md`.
- `frontend_fastapi/templates/accounts/change_password.html` — new
  template, can largely copy `activate.html`'s form markup.
- `frontend_fastapi/templates/base.html` — turn the username `<span>` into
  `<a href="{{ url_for('accounts.me') }}">`.

## Acceptance criteria

### Scenario: Successful password change

**Given** a logged-in user on `/accounts/me`

**When** they submit their correct current password and a new password that
passes strength validation, matching on both new-password fields

**Then** their stored password hash is updated, they see a success message,
and remain logged in on the current session

### Scenario: Wrong current password is rejected

**Given** a logged-in user on `/accounts/me`

**When** they submit an incorrect current password

**Then** the form re-renders with a field-level error and no password
change occurs

### Scenario: Weak new password is rejected

**Given** a logged-in user submits a correct current password

**When** the new password fails `password_strength_errors` (e.g. too short,
too similar to their username)

**Then** the form re-renders with the same strength errors `ActivateForm`
would show, and no password change occurs

### Scenario: Other sessions are logged out

**Given** a user is logged in from two different browsers (two session rows)

**When** they change their password successfully from browser A

**Then** browser B's session is invalidated (its next request is treated as
logged out), while browser A remains logged in

### Scenario: Navbar link reaches the form

**Given** any logged-in user viewing any page

**When** they click their username in the navbar

**Then** they land on `/accounts/me` and see the change-password form

## Edge cases

- User has only one active session (their own) — the "invalidate other
  sessions" step is a no-op; should not error.
- New password identical to the old one — decide whether this is rejected
  by strength/similarity rules as-is (likely yes, since it's maximally
  "similar") or needs an explicit check; verify current
  `password_strength_errors` behaviour before assuming.

## Success

A user can successfully change their password end-to-end via the UI, the
new password authenticates on the next login, the old one no longer does,
and a second, previously-logged-in session for that user is rejected on its
next authenticated request.

## Failure behaviour

Both "wrong current password" and "weak new password" are ordinary
validation failures — re-render the form with field-level errors, no
partial state change (password hash and other sessions are untouched unless
the entire operation succeeds).

## Testing considerations

- Follow `frontend_fastapi/tests/test_accounts_activate.py`'s structure for
  the new change-password tests — same app/test-client setup, same
  assertion style for strength-validation failures.
- Add a session-invalidation test: log in as the same user in two separate
  test-client sessions, change password in one, assert the other's next
  request is rejected (401/redirect-to-login, matching however
  `require_login` currently handles an invalid session elsewhere).

## Implementation notes

Scope is intentionally narrow: a bare change-password form, nothing else on
`/accounts/me` yet (no broader profile/account page — that's explicitly out
of scope per the grilling session).

## Out of scope

- Any other content on the user's own account page beyond the
  change-password form.
- Password reset for a user who has forgotten their password (that's the
  existing break-glass `reset_password.py` CLI path, unchanged).
