# HERMES `frontend/` migration: Django → FastAPI + Jinja2 + htmx

**Status:** v3, converged after two rounds of independent critical review (UX
+ practicality), each checking every claim against the live code rather than
taking prose on faith. Round 1 found a load-bearing design error (§2.5's
original document-download access control would have locked ethics reviewers
out of documents they need to read) and a significant unflagged scope gap
(the messages framework, §2.8, absent entirely). Round 2, after those fixes,
found a genuine SQLAlchemy mutation-tracking bug in the round-1 fix for §2.8
(a plain JSON-column `.append()` doesn't persist without `MutableList`), a
factual inconsistency between §2.7 and §2.8 (four tables vs. five), and an
incoherent supporting argument in §1 (reasoning about Flask-Login/Flask-WTF's
thread-locals, when this plan doesn't use either library under any framework
choice). All three are corrected below. Across both rounds, v1's phasing, the
SSE redesign, and the Jinja-macro strategy held up unchanged. Every place this
document responds to a review round is marked inline so the delta is
traceable, not silently folded in.
**Scope:** `frontend/` only. `backend/` and `proxy/` are untouched — this document
never proposes changing them, only cites them as precedent.
**Non-goal:** this is not a htmx-vs-something-else document. htmx (or its absence
in practice — see Finding 0 below) is unchanged. The question is Django-the-framework
vs. a lighter one, with Jinja2 replacing django-cotton templates and hand-rolled
sessions/auth/CSRF replacing Django's contrib apps.

---

## Finding 0, up front: htmx is currently unused

`templates/base.html:10-11` loads `htmx.org@2.0.4` and `htmx-ext-sse@2.2.2` from a
CDN. A repo-wide grep (`grep -rn "hx-\|htmx" frontend/ --include=*.html`) finds
**zero** `hx-*` attributes anywhere. The one piece of live-updating UI —
`templates/cotton/job_progress.html:18-84` — is hand-rolled vanilla JS
(`new EventSource(...)`, manual `addEventListener` per event type), not
`htmx-ext-sse`. Tab-switching in `collect_data.html`/`retrieve_data.html` is a
hand-rolled `hermesShowTab()` function toggling Tailwind classes, not
`hx-get`/`hx-swap`. Everything else is traditional full-page-reload forms/links.

This matters for scoping: there is no existing htmx usage to port. "Keep htmx"
is really "keep the *option* to write forward-looking `hx-*` markup, on top of
plain server-rendered HTML, with no build step" — a standing architectural
choice, not a migration burden. I have **not** treated "port htmx usage" as a
phase below, because there's nothing to port. Whether to actually *start* using
htmx (e.g. swap the hand-rolled tab JS for `hx-get`, or swap
`patient_table`'s filter pills without a full reload) is a separate UX decision
I flag under Open Risks, not one I've made here.

---

## 1. Target stack recommendation

**FastAPI + Jinja2 (via `starlette.templating.Jinja2Templates`) + htmx, run under
uvicorn — the same server binary already running `backend/` and `proxy/`.**

### Why not Starlette directly (FastAPI's own base)?

Real alternative, not a straw man: `frontend/` does almost no request/response
*validation* — it's server-rendered HTML, not a JSON API, so Pydantic request
models buy little. Bare Starlette would be lighter.

I'm recommending FastAPI anyway, for one concrete reason: **the department
already runs FastAPI twice** (`backend/main.py`, `proxy/main.py`). Every
convention a Python-fluent-but-not-frontend-specialist colleague would need to
learn to debug `frontend/` — `Depends()` for request-scoped values, exception
handlers, `BackgroundTasks`, how routers/`APIRouter` compose, how `uvicorn`
is invoked and configured — they've already learned twice. Starlette is FastAPI
minus that shared vocabulary, for a saving that's mostly aesthetic (fewer
decorators). CLAUDE.md's own words about the htmx-over-Vue decision — "colleagues
... need to be able to read and debug the UI code without a second language" —
apply just as much to "a third slightly-different Python web framework." I'm
weighing this as the deciding factor over raw minimalism.

### Why not Flask?

Flask 2.x+ supports `async def` views, but only by wrapping each one in a
threadpool via `asgiref.sync.async_to_sync`-style shims under the hood — it is
not a native ASGI framework the way Starlette/FastAPI are. The one view that
matters most for this migration, `job_stream` (`jobs/views.py:250-285`), is an
**async generator streaming a `StreamingHttpResponse`** that itself awaits
another async generator (`backend_client.stream_sse`, an `httpx.AsyncClient`
stream). Doing that cleanly under Flask's async-via-threadpool model is
friction for no offsetting gain, and it's the opposite direction from where the
rest of this codebase already lives.

### Why this is a real tradeoff — engaging with it directly, not deferring it

**Revised after review.** A first pass of this section set the Flask-ecosystem
argument aside on "one fewer framework for the team" grounds and left it at "a
reviewer could reasonably disagree." That understated the case for Flask, and
I want to correct that rather than restate it more politely.

The strongest version of the Flask argument, made explicit: §2 below is this
document's longest section, and it is an inventory of everything this
migration has to rebuild **regardless of which ASGI micro-framework gets
picked** — sessions, CSRF, password hashing, invite/activation tokens, forms
and their cross-field validation, a second local Alembic project, file-upload
handling with correct access control, flash messaging. None of that is shared
surface with `backend/`/`proxy/` today; those two are stateless JSON/SSE APIs
with no auth of their own. The FastAPI vocabulary genuinely shared with them —
`Depends()`, router composition, how `uvicorn` is invoked — covers routing
plumbing, not the territory where this migration's actual risk lives. Framework
choice is doing less real work than "one fewer framework" implies, and for a
system gating hospital-adjacent workflows, a framework choice that measurably
reduces exactly the surface this document itself calls its biggest risk
(§6 item 2: hand-rolled sessions/CSRF as new attack surface) is not a soft
tiebreaker against team-legibility — Flask-Login/Flask-WTF/Flask-Migrate being
more battle-tested than the FastAPI-world equivalents is a real, security-relevant
argument, not just an ecosystem-size footnote.

Having stated that as strongly as I can, here's why I still land on FastAPI —
and this needed a second look too: a version of this section previously
argued Flask-Login's/Flask-WTF's synchronous thread-locals would be a poor
fit for `job_stream`'s async generator. On review, that argument doesn't
actually hold together against **this document's own design**: §2.1 already
hand-rolls sessions via a custom `request.state.session` dependency rather
than Flask-Login, and §2.6 picks WTForms specifically *because* it's
framework-agnostic ("no Flask-WTF needed"). Neither Flask-Login nor Flask-WTF
was ever going to be used here under either framework choice — so an argument
about their thread-locals being awkward under async is arguing against a
version of "pick Flask" that this plan wouldn't have implemented anyway. Worth
correcting rather than leaving in: it read as a decisive technical point and
wasn't one.

The argument that actually holds is the one already made under "Why not
Flask?" above: `job_stream` is a native `async def` generator relaying
another async generator, and Flask can only run that by bridging back through
a thread via an async-compatibility shim rather than running it natively the
way Starlette/FastAPI do. That's a real, if narrower, technical reason to
prefer a native-ASGI framework for specifically this one view — it doesn't
extend to a broader claim about Flask's ecosystem being poorly suited
elsewhere in the app, because (per the point above) this plan doesn't lean on
Flask's ecosystem-specific pieces regardless of which framework wins.

So, the honest position, stated once rather than re-argued: "one fewer
framework for the team" (§1's original framing) is a real but secondary
factor; the primary technical reason to prefer FastAPI is that its one
native-async view is more naturally expressed without a sync-bridge shim;
and Flask's ecosystem maturity (Flask-Login/WTF/Migrate) is a real advantage
this plan doesn't actually capture either way, since it deliberately avoids
those specific libraries in favor of framework-agnostic and hand-rolled
alternatives (§2.1, §2.6). A reviewer who weighs "battle-tested contrib
libraries exist for Flask, even if this plan doesn't currently propose using
Flask's own" more heavily than "one view streams more naturally under native
ASGI" could reasonably choose Flask. This is a close call, not a settled one.

### Supporting libraries (concrete list)

| Concern | Library | Notes |
|---|---|---|
| ASGI server | `uvicorn[standard]` | already a repo dependency (backend, proxy) |
| Templates | `Jinja2` | via `fastapi.templating.Jinja2Templates`; autoescape on by default for `.html` |
| Sessions | hand-rolled server-side table (see §3) + a plain signed cookie for the opaque session id | see §2.1 for why not `itsdangerous`-cookie-only |
| CSRF | `starlette-csrf` (or hand-rolled double-submit cookie) | see §2.2 |
| Password hashing | `passlib[argon2]` or `argon2-cffi` directly | modern default, no real reason to match Django's PBKDF2 exactly |
| Forms/validation | `WTForms` (framework-agnostic, no Flask-WTF needed) | closest analog to Django `forms.Form` — see §2.6 |
| File uploads | FastAPI's `UploadFile` (backed by `python-multipart`) | already a repo-wide dependency per README |
| Local DB + migrations | `SQLAlchemy` + `Alembic` | mirrors backend's own pattern (`backend/alembic/`); **a separate Alembic project, separate DB file, never touching HermesDB** |
| Backend HTTP client | `httpx` (async) | already what `hermes_frontend/backend_client.py` uses — carries over almost unchanged |
| Signed tokens (invite/activate links) | `itsdangerous` | `URLSafeTimedSerializer`, same library Flask uses internally for this exact purpose |

---

## 2. What's lost by dropping Django, item by item

### 2.1 Sessions

**Today:** `django.contrib.sessions` (`hermes_frontend/settings.py:43,54`),
default `SESSION_ENGINE` = DB-backed (`django_session` table in the local
`db.sqlite3`). Two real uses:
- Login state (`request.user`, set by `AuthenticationMiddleware`).
- The two-phase job pattern: `jobs/views.py:93-97` writes
  `request.session[f"pending_job:{job_id}"] = {...}` (kind, staged file path,
  project_id, username, extra) after staging an upload; `job_watch` and
  `job_stream` read it back by key.

**Replacement:** a small local `sessions` table (`id` opaque token PK, `user_id`
nullable, `csrf_token`, `created_at`, `expires_at`) plus a `hermes_session`
cookie (`httponly`, `secure` in prod, `samesite=lax`) holding *only* the opaque
session id — structurally identical to what Django's DB session backend already
does, just hand-rolled. A small ASGI middleware (or a FastAPI dependency run
first in every router) loads the row by cookie value, exposes it as
`request.state.session`, and creates one if absent.

**Pending-job staging specifically:** I recommend a *dedicated* `pending_jobs`
table (`job_id` PK, `session_id` FK, `kind`, `tmp_path`, `project_id`,
`username`, `extra` JSON, `created_at`) rather than cramming this into a
generic session JSON blob. Concretely:

```python
class PendingJob(Base):
    __tablename__ = "pending_jobs"
    job_id = Column(String, primary_key=True)          # uuid4, same as today
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)               # "import_batch" | "export_dicom" | "export_proknow"
    tmp_path = Column(String, nullable=False)
    project_id = Column(String, nullable=False)
    username = Column(String, nullable=False)
    extra = Column(JSON, nullable=False, default=dict)  # import_level | destination | collection
    created_at = Column(DateTime, default=datetime.utcnow)
```

This preserves the exact security property the docstring at `jobs/views.py:17-22`
calls out: `job_stream` can only act on a `job_id` if `pending_jobs.session_id`
matches the requesting browser's own session cookie — a third party who gets a
victim to open a crafted `/jobs/<guessed-or-leaked-id>/stream` URL still can't
trigger anything, because there's nothing to act on unless *their own* session
staged it. Same invariant, different storage.

**Genuine gap, not just relocation:** Django's session backend gets free
garbage collection *infrastructure* (a `clearsessions` management command) —
though notably Django doesn't run it automatically either; an operator has to
cron it. A hand-rolled table needs the same "expire old rows" habit explicitly
decided and scheduled (e.g. a startup task or a simple cron hitting a
`/internal/gc` route), and `pending_jobs` rows for abandoned uploads (staged,
then the tab closed) need the same cleanup `_cleanup_pending_job` already does
on the happy/error path today, plus a sweep for the "never came back" path
that arguably isn't even handled by the current Django app.

**"Remember me" — specified, not left implicit.** `HermesLoginView.form_valid`
(`accounts/views.py:26-34`) calls `request.session.set_expiry(0)` when the
checkbox is unchecked. What that actually does in Django: it does **not**
shorten the server-side session row's lifetime — it changes the *cookie* Django
sends to a browser-session cookie (no `Max-Age`/`Expires`, so the browser
discards it on close), while the stored session data itself is still governed
by `SESSION_COOKIE_AGE` (Django's default, 2 weeks) as an upper bound. Checked
(the default), the cookie gets an explicit `Max-Age` of ~2 weeks and survives
browser restarts.

The new design should copy this split exactly, since it maps cleanly onto the
two independent knobs already in play — the `sessions` row's `expires_at`, and
the cookie's own `Max-Age` attribute:
- `sessions.expires_at` is **always** set to `now + 2 weeks` at login,
  regardless of the checkbox — this is the server-side cap, same role
  `SESSION_COOKIE_AGE` plays today.
- The `Set-Cookie` header's `Max-Age` is what the checkbox actually controls:
  present (`Max-Age=1209600`) when "remember me" is checked, **omitted**
  entirely when unchecked — an omitted `Max-Age`/`Expires` is what makes a
  cookie a genuine browser-session cookie, cleared at browser close, exactly
  matching `set_expiry(0)`'s effect today.

This is worth being this precise about for a hospital deployment specifically:
shared clinical terminals and personal workstations have different "stay
logged in" expectations, and getting this wrong silently is either an
annoying mid-shift logout (including mid-way through watching a live job, per
UX-4's original framing) or a shared terminal left signed in indefinitely — a
real access-control concern for a system fronting patient-identifiable
workflows, not a nicety.

### 2.2 CSRF protection

**Today:** `django.middleware.csrf.CsrfViewMiddleware` (`settings.py:56`) +
`{% csrf_token %}` in every POST form (10 templates use it, verified by grep:
`templates/base.html`, `jobs/templates/jobs/{collect_data,retrieve_data}.html`,
`templates/registration/login.html`,
`accounts/templates/accounts/{invite,create_user,activate}.html`,
`templates/cotton/job_progress.html`,
`research_projects/templates/research_projects/{detail,create}.html`). Django's
implementation ties a per-session secret to a per-form token and validates on
every unsafe method.

**Replacement:** `starlette-csrf` (cookie + hidden-field double-submit) is the
safer default recommendation over hand-rolling — this is exactly the kind of
security-sensitive, easy-to-subtly-break code (constant-time comparison,
`SameSite` correctness, token-session binding) I'd rather not have this
migration hand-write from scratch without a security-focused review pass. If a
third-party dependency is unwanted, the fallback is: a `csrf_token` column on
the `sessions` row from §2.1, a Jinja global function `csrf_token()` returning
it, a hidden `<input name="csrf_token">` in every form (mechanical
find-and-replace of `{% csrf_token %}`), and a dependency that 403s any POST
where the form field doesn't match the session's stored value. Either way,
budget explicit review time for this file specifically — see Open Risks.

### 2.3 Auth system

**Today (`accounts/`):**
- `django.contrib.auth`'s `User` model, `login()`/`logout()`, PBKDF2 password
  hashing, `login_required`/`user_passes_test` decorators.
- `accounts/forms.py`: `HermesAuthenticationForm` (adds `remember_me`),
  `_UserIdentityForm` (shared username/email/name/department/`is_staff`
  fields, with a `clean_username` uniqueness check), `InviteUserForm`
  (`set_unusable_password()` + activation link), `CreateUserForm`
  (`password1`/`password2` + `validate_password()` against the 4 validators in
  `settings.py:100-105`).
- `accounts/views.py:82-101`: `activate_account` uses
  `default_token_generator` (HMAC over user pk + password hash + a timestamp,
  Django's `PASSWORD_RESET_TIMEOUT`-bounded) + `urlsafe_base64_encode/decode`
  of the user pk, then Django's `SetPasswordForm`.
- `accounts/models.py:5-13`: `Profile` (`OneToOneField` to `User`, one extra
  `department` field) — the only reason a separate table exists is that
  Django's `User` model can't be extended in place without a custom user model
  from day one, which this app didn't start with.

**Replacement:**
- One local `users` table (SQLAlchemy model) — since there's no Django `User`
  to extend around, **merge `Profile` into it directly**: `id, username,
  email, first_name, last_name, department, is_staff, is_superuser, is_active,
  password_hash, created_at`. One fewer table than today, and one fewer join.
- Password hashing: `passlib[argon2]` — a genuine, deliberate upgrade over
  Django's PBKDF2 default, not a parity requirement.
- `login_required` → a FastAPI dependency:
  ```python
  async def require_login(request: Request, db=Depends(get_db)) -> User:
      session = request.state.session
      if not session.user_id:
          raise RedirectException(f"/accounts/login?next={request.url.path}")
      return db.get(User, session.user_id)
  ```
  (`RedirectException` here is a tiny custom `HTTPException` subclass paired
  with an exception handler that returns a `RedirectResponse` — FastAPI has no
  built-in "redirect on auth failure" the way Django's `login_required` does,
  this is the one place that needs a small adapter.)
- `user_passes_test(_is_data_custodian)` → a second dependency,
  `require_data_custodian`, wrapping `require_login`. **Worth doing during the
  port, not after:** `_is_data_custodian` is defined *twice* today, verbatim,
  in `accounts/views.py:18-19` and `research_projects/views.py:10-11` — this
  migration is a natural point to collapse it to one shared dependency.
- Invite/activate token: `itsdangerous.URLSafeTimedSerializer`, signing
  `{"uid": user.id, "pwhash_fingerprint": sha256(user.password_hash)[:12]}`
  with an expiry — the `pwhash_fingerprint` component is what gives the
  "invalidated once the password is actually set" property Django's
  `default_token_generator` has (it includes the password hash in what it
  signs, so a used token stops validating once `SetPasswordForm.save()` changes
  the hash). This is a faithful behavioral port, not just "some token scheme."
- Password validators (`settings.py:100-105`): **not a free port.** Django's
  `CommonPasswordValidator` ships a bundled ~20,000-entry common-password
  wordlist; `UserAttributeSimilarityValidator` does fuzzy-matching against
  username/email/name. Replicating exactly means vendoring Django's own
  wordlist (it's just a gzipped text file, could be copied) or accepting a
  different library (`zxcvbn` gives a strength *score* rather than a pass/fail
  against Django's specific rules — not equivalent, just different-but-reasonable).
  I'm flagging this as a **conscious decision the team needs to make**, not
  silently substituting one for the other.
- Django admin's `HermesUserAdmin`/`ProfileInline` (`accounts/admin.py`): see
  §2.4 — dropped, no replacement, see the break-glass caveat there.

### 2.4 Django admin

Checked all three `admin.py` files:
- `accounts/admin.py` registers a real customization (`HermesUserAdmin` +
  `ProfileInline`), reachable via the "Admin" nav link superusers see in
  `base.html:67-69` (`{% if user.is_superuser %}`).
- `research_projects/admin.py` and `jobs/admin.py` are both the unmodified
  scaffold (`# Register your models here.`) — **nothing registered, unused.**

**Verdict: droppable, with one caveat.** `accounts/` already has dedicated
`invite_user`/`create_user`/`user_list` views (`accounts/views.py`) that
duplicate everything the admin User CRUD offers — so no real workflow depends
on `/admin/`. What's lost is Django admin's role as a **free emergency escape
hatch**: today, if a user account gets into a bad state (locked out, wrong
`is_staff` flag, whatever), a superuser can fix it by hand in `/admin/` with no
code deployed. Dropping Django admin removes that safety valve. I'd recommend
budgeting for a couple of small "break-glass" CLI scripts (e.g. a
`reset_password.py <username>` one-liner against the new `users` table) rather
than assuming ops will be comfortable writing raw SQL under pressure — cheap
insurance, not a full admin-panel replacement.

### 2.5 `ProjectDocument` — Django's `FileField`/storage

**Today (`research_projects/models.py:8-29`):** a `FileField(upload_to=
ethics_document_upload_path)` where `ethics_document_upload_path` builds
`ethics_documents/<project_id>/<filename>` under `MEDIA_ROOT`. Django handles
multipart parsing, filename collision-avoidance (appends a random suffix if
the target path exists), and serving via `MEDIA_URL` in dev
(`hermes_frontend/urls.py:14-19`).

**Replacement:**
```python
class ProjectDocument(Base):
    __tablename__ = "project_documents"
    id = Column(Integer, primary_key=True)
    project_id = Column(String, index=True, nullable=False)  # plain string, not a real FK — no local Project model, same reasoning as today
    file_path = Column(String, nullable=False)               # relative to MEDIA_ROOT
    original_filename = Column(String, nullable=False)
    uploaded_by = Column(String, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
```
Upload handling: FastAPI's `UploadFile`, write to
`MEDIA_ROOT/ethics_documents/<project_id>/<uuid4>_<sanitized_filename>` (the
`uuid4` prefix replaces Django's collision-avoidance suffix, and is simpler).

**Corrected after review — the first draft of this design was wrong, and its
own citation didn't support it.** The original text here proposed gating
`GET /projects/{project_id}/documents/{doc_id}/download` by `require_login` +
a project-membership check, citing `research_projects/views.py:60` as
precedent. Checked directly, that line doesn't support the proposal:
`project_detail` (`research_projects/views.py:52-75`) has only
`@login_required` — no membership or staff gate on the view itself.
`is_member` (line 60) is computed and passed to the template, but it's used
*only* to show/hide the **mutation** forms — "add member"
(`detail.html:114-124`) and "upload document" (`detail.html:136-144`). The
document list (`detail.html:131`, `<a href="{{ doc.file.url }}">`) renders
unconditionally regardless of `is_member` — any logged-in user can already see
and click every document link on any project's detail page today.

More importantly, a bare membership check would actively break a real
workflow: the ethics-review flow (`review_queue`/`project_review`,
`research_projects/views.py:90-117`) is gated purely on `_is_data_custodian`
(`is_staff`), **with no membership check anywhere** — a data custodian
reviewing a submitted project is structurally an outside reviewer, not a
project member; that separation is the entire point of the role. The review
form they use to approve or reject a project sits on the same page
(`detail.html:45-69`) as the document list they need to read *before* making
that decision. A membership-only gate on document downloads would 403 exactly
the person whose job requires reading the ethics certificate, unless they
happened to also be a member of every project they review — defeating the
separation the roles exist to enforce.

**Corrected design:** gate the download route on `require_login` **and**
(`is_member` **or** `is_staff`) — mirroring the pattern `project_list` already
uses for the equivalent "who gets to see this" question one page over ("staff
see every project regardless of status/membership," `research_projects/views.py:16-17`):

```python
@router.get("/projects/{project_id}/documents/{doc_id}/download")
async def download_document(project_id: str, doc_id: int, db=Depends(get_db),
                             user=Depends(require_login)):
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or doc.project_id != project_id:
        raise HTTPException(404)
    project = await backend_client.get_project(project_id)
    is_member = any(m["username"] == user.username for m in project["members"])
    if not (is_member or user.is_staff):
        raise HTTPException(403)
    return FileResponse(Path(settings.MEDIA_ROOT) / doc.file_path,
                         filename=doc.original_filename)
```

This still closes the real, pre-existing gap (today: no access check at all,
just "know the URL") without introducing a new one in its place — it's a
genuine improvement over today's behavior (member and reviewer can both open
it; a random logged-in-but-unrelated user now cannot), rather than the
draft's first pass, which would have been a regression for reviewers
specifically while looking like a strict improvement on paper.

### 2.6 Django forms/validation

**Today:** 15 `forms.Form`/`forms.ModelForm` classes total across
`accounts/forms.py` (4), `research_projects/forms.py` (4), and `jobs/forms.py`
(7) — verified by direct count of `^class ` in the three files. 13 of those
are ever instantiated directly; the other 2 (`_UserIdentityForm`,
`ProjectScopedForm`) are shared base classes subclassed by others, never used
on their own. Two have real cross-field logic worth preserving exactly:
`ReviewProjectForm.clean()`
(`research_projects/forms.py:26-30`, requires `expiry_date` when
`decision == "approve"`) and `CreateUserForm.clean_password2`
(`accounts/forms.py:71-86`, matches passwords *and* runs
`validate_password()` against a throwaway `User` instance for the similarity
check).

**Replacement:** `WTForms` — declarative fields, per-field validators, a
`form.errors` dict, and a `validate()` hook for cross-field logic, e.g.:
```python
class ReviewProjectForm(FlaskForm):  # WTForms works standalone; naming kept for familiarity
    decision = RadioField(choices=[("approve", "Approve"), ("reject", "Reject")])
    comment = TextAreaField()
    expiry_date = DateField(validators=[Optional()])

    def validate_expiry_date(self, field):
        if self.decision.data == "approve" and not field.data:
            raise ValidationError("An expiry date is required when approving a project.")
```
`ProjectScopedForm`'s pattern of populating `project_id` choices **fresh, every
request, from a live backend call** (`jobs/forms.py:10-21`) is the one thing
that needs to survive verbatim — it's not just form plumbing, it's the *live
re-check* that a submitted `project_id` is currently one the user has active
access to (the docstring is explicit about this). WTForms' `SelectField`
supports dynamically-set `.choices` the same way Django's `ChoiceField` does,
so this ports directly: set `.choices` from `backend_client.list_user_active_projects(...)`
on both the GET render and the POST-validation reconstruction, same as today.

**What's lost:** Django's `ModelForm` auto-generating a form from a model
(`ProjectDocumentForm(forms.ModelForm)`, `research_projects/forms.py:38-41`) —
used exactly once, for a single `file` field. Trivial to hand-write; not worth
mourning.

### 2.7 Django's migration system for the two local models

**Today:** Django migrations (auto-generated, `python manage.py makemigrations`)
for `accounts.Profile` and `research_projects.ProjectDocument`, applied via
`python manage.py migrate`.

**Replacement:** Alembic against the new local DB — same tool, same directory
convention (`alembic/versions/`) that `backend/` already uses
(`backend/alembic/versions/`), just a **second, entirely separate Alembic
project** pointed at the frontend's own local DB file/DSN, never at
`DATABASE_URL`/HermesDB. This is explicitly not "reuse backend's Alembic
config" — CLAUDE.md is emphatic that HermesDB and any frontend-local storage
must stay separate, and that discipline should carry over unchanged.

**Judgment call on whether Alembic is even warranted:** the new local schema
is small (`users`, `sessions`, `pending_jobs`, `project_documents` — four
tables; flash-message storage per §2.8 is a `flash_messages` column on
`sessions`, not a fifth table) and low-risk. A simpler `Base.metadata.create_all()`
on startup would work today.
I recommend Alembic anyway, on the grounds that schema *will* evolve (new user
fields, etc.) and "we already have the pattern in `backend/`, just reuse the
convention" is cheaper than reintroducing ad-hoc migrations later — but this
is genuinely optional for a first cut, not load-bearing for the migration to
succeed.

### 2.8 `django.contrib.messages` — added after review; missing from the first draft entirely

**This was a real gap, not a nitpick.** The first draft's §2 inventoried seven
things lost by dropping Django and never once mentioned the messages
framework — despite it being wired into `MIDDLEWARE` (`settings.py:58`) and
`INSTALLED_APPS` (`settings.py:44`) as first-class infrastructure, and used at
**25 call sites** across `jobs/views.py`, `accounts/views.py`, and
`research_projects/views.py` (verified by grep) for essentially every
success/error acknowledgment in the app — job-cancel failures, "could not load
job," every project create/submit/review/revoke confirmation, and one flow
that isn't cosmetic at all:

`settings.py:142-149`'s own comment states there's no SMTP configured for this
deployment (`EMAIL_BACKEND` defaults to the console backend). The way an admin
actually retrieves a new hire's activation link, in a real working deployment,
is a flash message:
```python
messages.success(request, f"Invited {user.username}. Activation link: {activate_url}")
```
(`accounts/views.py:59`), rendered by the styled message block in
`templates/base.html:98-106`. **This is the only working path for the one
onboarding flow this deployment actually has** — if the messages equivalent is
rebuilt casually, or deferred to "later," the concrete failure mode isn't a
cosmetic regression: an admin invites someone, the page reloads with no
confirmation and no link, and there is no fallback (no real email either).
Discovered by an actual admin trying to add an actual member of staff, not by
a developer reading the code — exactly the kind of gap a "cut over once" phase
plan (§5) has no room to absorb gracefully.

**Replacement design:** a one-shot flash mechanism riding on the same
`sessions` row from §2.1 — the natural place for it, since Django's own
message storage is itself session-backed by default (`SessionStorage`/
`FallbackStorage`). Concretely, a `flash_messages` JSON column on `sessions`
(`[{"tag": "success", "text": "..."}, ...]`):

**Caught on the second review pass, and worth stating plainly since it's
exactly the kind of bug this section exists to prevent:** a naive
`request.state.session.flash_messages.append(...)` **will not persist.**
SQLAlchemy's unit-of-work does not detect in-place mutation of a plain
`JSON`-typed column — `.append()` on the loaded Python list is invisible to
`db.commit()` unless the column is explicitly wrapped so SQLAlchemy can track
mutation. Two correct options, either is fine:

```python
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy import JSON

class Session(Base):
    __tablename__ = "sessions"
    # ...
    flash_messages = Column(MutableList.as_mutable(JSON), nullable=False, default=list)
```
(`MutableList.as_mutable` makes `.append()`/`.pop()` on the loaded value
register as a change, same as any other tracked attribute) — **or**, without
the extra import, always reassign the whole list rather than mutating it:
```python
def flash(request: Request, tag: str, text: str) -> None:
    request.state.session.flash_messages = request.state.session.flash_messages + [{"tag": tag, "text": text}]
```
Either is correct; `MutableList` is the more idiomatic SQLAlchemy fix and
avoids anyone re-introducing the same bug later by calling `.append()` out of
habit, so it's the recommended default. **This is the one piece of this
entire document that was asserted to work without being checked against
SQLAlchemy's actual change-tracking semantics — flagged explicitly, per the
same caution already applied to "remember me" (§6 item 9): a design reading
correctly on paper is not the same as it being verified.**

A template block then pops (reads-then-clears) the list on the next render —
a direct Jinja port of `templates/base.html:98-106`'s existing
`{% for message in messages %}` block, including the same per-tag styling
(`message.tags == 'success'/'warning'/'error'`). "Popped on next render," not
"popped on next request," matters here for the exact same reason Django's does:
a message survives one redirect (POST → flash → redirect → GET renders and
clears it) but not a page refresh after that.

**Folded into the estimate, not left implicit:** this is genuinely Phase 0
scope (it's cross-cutting infrastructure every subsequent phase's views call
into, the same way sessions/CSRF are) rather than a Phase 1/2/3 line item —
Phase 0's description below has been updated accordingly, and it's small
relative to sessions/CSRF/auth (one column, one helper, one template block)
but real enough that skipping it silently was a genuine miss, not a rounding
error.

### 2.9 The "active projects" context processor — a cross-cutting per-request call, also missing from the first pass

`hermes_frontend/context_processors.py`'s `active_projects` runs on **every**
authenticated page render (registered in `TEMPLATES.OPTIONS.context_processors`,
`settings.py:78`) and makes a live `backend_client.list_user_active_projects`
call purely to populate the "no active projects" nav banner
(`templates/base.html:88-96`). Mechanically simple to replicate — a FastAPI
dependency run on every authenticated route (or a small before-request hook)
that stashes `nav_active_projects` into the template context, mirroring what
the context processor does today — but it's genuine per-request behavior that
belongs in the same inventory as sessions/CSRF/auth, and the first draft's
phase plan didn't allocate any time to it. Low severity on its own (it's a
few lines), flagged mainly so Phase 0's scaffolding estimate accounts for it
rather than it surfacing as a surprise once every page in Phase 1 turns out to
need it.

### 2.10 What this inventory adds up to — a tension worth naming directly

Read straight, §2.1 through §2.9 — sessions, CSRF, auth (including
invite/activate tokens), Django admin's break-glass value, file storage with
correct access control, forms with real cross-field validation, a second local
migrations project, the messages framework, and a cross-cutting per-request
context call — is a fairly strong argument that Django was **not** overkill
for an app shaped like this one: session-authenticated, form-heavy,
file-upload-having, multi-role (member vs. data-custodian vs. superuser), with
a real (if small) invite/activation flow. Every one of those is a solved
problem in Django that this migration re-solves by hand, in each case with a
smaller and less battle-tested library standing in, or with no library at all.

That's not, on its own, a reason to block this migration — "one fewer
framework for the department to know" (§1) and "no build step, stays
Python-only" are legitimate priorities to optimize for, and the team has
already made the analogous call once (cotton+htmx over Vue, per CLAUDE.md) on
comparable grounds. But it is a real tension this document's own analysis
surfaces without ever naming outright in the first draft, and whoever makes
the final call on this migration should see it stated plainly rather than
have to infer it from the length of §2: **this migration trades a framework
that already solved these nine problems for one where solving all nine
correctly, and keeping them correct as the app evolves, becomes this team's
own ongoing responsibility.** If that trade is made with that framing in view,
I think the recommendation in §1 still holds. If it's made because "Django
feels heavy" without weighing what's actually being taken on in exchange, it
isn't being made on the right basis.

---

## 3. The two-phase SSE job pattern — concrete design

This is `jobs/views.py`'s most architecturally important piece
(`jobs/views.py:1-30`'s own docstring says as much), so a full sketch rather
than a summary.

### What it does today

1. `collect_data`/`retrieve_data` (`jobs/views.py:101-204`, ordinary CSRF-POST
   views) call `_stage_batch_job` (`jobs/views.py:85-98`): mint a `uuid4`
   `job_id`, write the uploaded file to
   `MEDIA_ROOT/tmp_uploads/<job_id>_<filename>`, store a dict under
   `session[f"pending_job:{job_id}"]`, redirect to `job_watch`.
2. `job_watch` (`jobs/views.py:207-212`, sync) checks the session for that key
   and 404s if absent, then renders a page embedding the `c-job-progress`
   component, whose inline `<script>` (`templates/cotton/job_progress.html:18-84`)
   opens `new EventSource("/jobs/<id>/stream/")`.
3. `job_stream` (`jobs/views.py:250-285`) is **the one async view in the whole
   app**. It has to jump through `sync_to_async` twice
   (`_load_pending_job`, `jobs/views.py:237-247`, and the initial
   `list_user_active_projects` re-check at line 257) specifically because
   Django's `request.session`/`request.user` are lazy objects that trigger a
   **synchronous** DB read on first touch, which is disallowed inside a native
   `async def` view (`SynchronousOnlyOperation`). It re-checks live project
   membership (defense in depth — staging already checked once, but
   membership could be revoked in between), builds the multipart POST to the
   backend (`_build_stream_request`, `jobs/views.py:215-227`), and relays
   `backend_client.stream_sse`'s raw bytes, re-framing each `data: {...}` line
   with a matching `event: <type>` line by peeking at the JSON body's own
   `"type"` field — so the browser's plain `EventSource.addEventListener(
   'progress', ...)` works without client-side dispatch logic. A `finally`
   block (`jobs/views.py:282-283`) always cleans up the session key and the
   staged tmp file, on both the success and error paths.

### FastAPI equivalent

```python
# jobs/routes.py
import json, uuid
from pathlib import Path
from fastapi import APIRouter, Depends, Request, UploadFile, Form
from fastapi.responses import StreamingResponse, RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

router = APIRouter()

def stage_batch_job(db, session_id: str, kind: str, filename: str, content: bytes,
                     extra: dict, project_id: str, username: str) -> str:
    job_id = str(uuid.uuid4())
    tmp_dir = Path(settings.MEDIA_ROOT) / "tmp_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{filename}"
    tmp_path.write_bytes(content)
    db.add(PendingJob(job_id=job_id, session_id=session_id, kind=kind,
                       tmp_path=str(tmp_path), project_id=project_id,
                       username=username, extra=extra))
    db.commit()
    return job_id


@router.post("/collect-data/")
async def collect_data_submit(
    request: Request, db=Depends(get_db), user=Depends(require_login),
    mode: str = Form(...), project_id: str = Form(...),
    import_level: str = Form("Planning data"),
    file: UploadFile | None = None, mrn: str | None = Form(None),
):
    active = await backend_client.list_user_active_projects(user.username)  # live re-check, same as today
    if project_id not in {p["project_id"] for p in active}:
        raise HTTPException(400, "Not an active member of that project")

    if mode == "single":
        filename, content = "single_patient.csv", f"patient_id\n{mrn}\n".encode()
        job_id = stage_batch_job(db, request.state.session.id, "import_batch",
                                  filename, content, {"import_level": import_level},
                                  project_id, user.username)
        # Faithful port of jobs/views.py:117-128, not the redirect-always
        # shape this sample used in the first draft: single-patient import
        # deliberately stays on this page, with a fresh single_form and the
        # just-started job's progress rendered inline via job_id, so staff
        # working down a short list of MRNs (e.g. off a phone call) can
        # submit one after another without a page navigation between each.
        # Batch/DICOM/ProKnow (the `else` branch below) always redirect to a
        # dedicated watch page instead — that distinction is preserved, not
        # collapsed.
        single_form, batch_form = SingleImportForm(), BatchImportForm()
        single_form.set_project_choices(active)  # matches §2.6's ProjectScopedForm pattern, not a constructor kwarg
        batch_form.set_project_choices(active)
        return templates.TemplateResponse("jobs/collect_data.html", {
            "request": request, "single_form": single_form, "batch_form": batch_form,
            "job_id": job_id, "has_projects": bool(active), "active_tab": "single",
        })

    filename, content = file.filename, await file.read()
    job_id = stage_batch_job(db, request.state.session.id, "import_batch",
                              filename, content, {"import_level": import_level},
                              project_id, user.username)
    return RedirectResponse(f"/jobs/{job_id}/watch/", status_code=HTTP_303_SEE_OTHER)


@router.get("/jobs/{job_id}/stream/")
async def job_stream(job_id: str, request: Request, db=Depends(get_db),
                      user=Depends(require_login)):
    pending = db.get(PendingJob, job_id)
    if pending is None or pending.session_id != request.state.session.id:
        raise HTTPException(404, "Unknown or already-completed job")

    active = await backend_client.list_user_active_projects(user.username)
    if not any(p["project_id"] == pending.project_id for p in active):
        raise HTTPException(404, "No longer an active member of this project")

    async def relay():
        try:
            path, data, files = _build_stream_request(pending, job_id)
            buffer = b""
            async for chunk in backend_client.stream_sse(path, data=data, files=files):
                buffer += chunk
                while b"\n\n" in buffer:
                    raw_event, buffer = buffer.split(b"\n\n", 1)
                    line = raw_event.decode(errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    try:
                        event_type = json.loads(payload).get("type", "message")
                    except Exception:
                        event_type = "message"
                    yield f"event: {event_type}\ndata: {payload}\n\n".encode()
        except backend_client.BackendError as e:
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': e.detail})}\n\n".encode()
            yield b'event: done\ndata: {"type": "done"}\n\n'
        finally:
            Path(pending.tmp_path).unlink(missing_ok=True)
            db.delete(pending)
            db.commit()

    return StreamingResponse(relay(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache"})
```

### Genuine simplification, not just a rewrite

The `sync_to_async` double-wrap in today's code (`jobs/views.py:237-247`)
exists *specifically* because Django's request object is sync-first and this
is an async view bolted onto that. FastAPI's `Depends()` resolves
`request.state.session`/`user` **before** the handler body runs, and the
`httpx.AsyncClient` calls in `backend_client` are natively awaitable — there is
no async/sync boundary to paper over at all. This is one of the few places in
this migration where I'm confident the new code is simpler, not merely
different.

### What must not regress

- The "third party can't trigger a job via a crafted `GET`" property (§3's
  `pending.session_id != request.state.session.id` check) — this is the whole
  reason the pattern is two-phase in the first place.
- Cleanup-on-every-exit-path (`finally:` in both versions) — a missed case
  here means orphaned files under `tmp_uploads/` and permanently-4xx'ing
  `pending_jobs` rows that never get GC'd (see §2.1's session-GC gap — the
  same operational habit needs to cover this table too).
- The live membership re-check at stream time, not just at stage time — this
  is a deliberate defense-in-depth choice in the current code (comment at
  `jobs/views.py:9-10`), not an accident, and easy to "simplify away" by
  mistake during a rewrite.
- **The single-patient inline-progress flow, added to this list after
  review.** `collect_data` (`jobs/views.py:101-143`) deliberately does not
  redirect on a single-MRN submission — it re-renders the same page with a
  reset form and the new `job_id`, so `c-job-progress` appears inline and
  staff can submit MRN after MRN without navigating away
  (`jobs/views.py:127-128`, `collect_data.html:62-66`). Batch/DICOM/ProKnow
  imports always redirect to a dedicated `job_watch` page instead
  (`jobs/views.py:138,187,198`) — that distinction is real UX, not
  incidental, and the reference implementation above now preserves it
  explicitly rather than collapsing every mode to the same redirect (the
  first draft's sample did the latter, unintentionally regressing the
  rapid-entry workflow it claimed to port faithfully).

### A cutover-continuity detail: trailing slashes

Every current Django route ends in a trailing slash (`jobs/urls.py`:
`/jobs/<job_id>/watch/`, `/collect-data/`, etc.) — standard Django convention,
enforced by `APPEND_SLASH`. The code samples in this document now match that
convention (`/collect-data/`, `/jobs/{job_id}/stream/`) rather than the
slash-less form the first draft used. Starlette's router redirects between the
slash and no-slash form of a registered path by default
(`redirect_slashes=True`), so a bookmark or saved shortcut pointing at the old
`.../watch/` URL should resolve correctly against a FastAPI app that registers
routes the same way — but given §5 is a hard cutover with no incremental
routing or side-by-side period, and I have not run this specific scenario
against a live FastAPI app to confirm it end-to-end, this belongs on Phase 0's
scaffolding checklist as something to verify empirically (it's a five-minute
check) rather than something Phase 4 discovers for the first time via a staff
member's stale bookmark.

---

## 4. Component strategy: Jinja macros as the cotton equivalent

### What django-cotton actually gives you here

Auditing all 8 components under `templates/cotton/`, the feature surface
actually used is narrow:
- `<c-vars name=default .../>` — prop declarations with defaults
  (`patient_table.html:3`, `source_badges.html:1`, etc.)
- `:prop="python_expr"` — binds the *real* Python value (a list, a `True`/
  `False`/`None` tri-state, a dict) from the caller's context, e.g.
  `<c-source-badges :mosaiq="row.in_mosaiq" ... />`
  (`templates/cotton/patient_table.html:36`) — critically, `row.in_mosaiq` is
  a tri-state, and cotton passes the actual `None`/`True`/`False`, not a
  stringified `"None"`.
- Plain `prop="{{ template_var }}"` — string interpolation, used where a
  string is genuinely wanted, e.g. `<c-status-badge status="{{ plan.status }}" />`
  (`jobs/templates/jobs/patient_detail.html:66`).
- Nesting: `patient_table` calls `c-source-badges` inside its loop;
  `project_card` calls `c-status-badge`.
- **No named slots anywhere** — every component is purely prop/data-driven, no
  `{{ slot }}` usage in this codebase. This meaningfully simplifies the
  translation: there's no "wrapper with arbitrary inner content" pattern to
  replicate.

### Jinja translation: macros, explicitly imported

```jinja2
{# templates/components/badges.html #}
{% macro source_badges(mosaiq=None, pinnacle=None, proknow=None) %}
<div class="flex items-center gap-3 text-xs whitespace-nowrap">
  {% for label, value in [("Mosaiq", mosaiq), ("Pinnacle", pinnacle), ("ProKnow", proknow)] %}
  <span class="flex items-center gap-1 text-gray-600">
    {{ label }}
    {% if value is true %}<span class="text-green-600 font-bold">&#10003;</span>
    {% elif value is false %}<span class="text-red-600 font-bold">&times;</span>
    {% else %}<span class="text-gray-400">&mdash;</span>{% endif %}
  </span>
  {% endfor %}
</div>
{% endmacro %}
```

```jinja2
{# templates/components/patient_table.html #}
{% from "components/badges.html" import source_badges %}
{% macro patient_table(rows=[], pills=None, job_id="", total=0, base_query="") %}
{% if pills %}
<div class="flex flex-wrap gap-2 mb-4">
  {% for pill in pills %}
  <a href="?{{ base_query }}filter={{ pill.key }}"
     class="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium border
       {{ 'bg-hermes-700 text-white border-hermes-700' if pill.active
          else 'bg-white text-gray-600 border-gray-200 hover:border-hermes-300' }}">
    {{ pill.label }}
    <span class="{{ 'text-hermes-100' if pill.active else 'text-gray-400' }} tabular-nums">{{ pill.count }}</span>
  </a>
  {% endfor %}
</div>
{% endif %}
<div class="overflow-x-auto">
  <table class="min-w-full text-sm divide-y divide-gray-100">
    <thead><tr>
      <th class="text-left py-1 font-medium text-gray-500">Patient</th>
      <th class="text-left py-1 font-medium text-gray-500">Sources</th>
      <th class="text-left py-1 font-medium text-gray-500">Outcome</th>
    </tr></thead>
    <tbody class="divide-y divide-gray-50">
      {% for row in rows %}
      <tr>
        <td class="py-2 pr-4">
          {% if job_id %}
          <a class="text-hermes-700 hover:underline font-mono"
             href="/jobs/{{ job_id }}/patients/{{ row.mrn }}/">{{ row.mrn }}</a>
          {% else %}
          <span class="font-mono">{{ row.mrn }}</span>
          {% endif %}
        </td>
        <td class="py-2 pr-4">{{ source_badges(mosaiq=row.in_mosaiq, pinnacle=row.in_pinnacle, proknow=row.in_proknow) }}</td>
        <td class="py-2">
          {% if row.outcome == "success" %}
            <span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-green-100 text-green-800">Success</span>
          {% elif row.outcome == "failure" %}
            <span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-red-100 text-red-800">Failed</span>
            {% if row.error_message %}<p class="text-xs text-red-600 mt-1 max-w-md break-words">{{ row.error_message }}</p>{% endif %}
          {% elif row.outcome == "running" %}
            <span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-yellow-100 text-yellow-800">In progress</span>
          {% else %}
            <span class="text-gray-400">&mdash;</span>
          {% endif %}
        </td>
      </tr>
      {% else %}
      <tr><td colspan="3" class="py-6 text-center text-gray-400">
        {% if total %}No patients match this filter.{% else %}No patients recorded.{% endif %}
      </td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endmacro %}
```

Used from a page:
```jinja2
{% extends "base.html" %}
{% from "components/patient_table.html" import patient_table %}
{% block content %}
...
{{ patient_table(rows=rows, pills=pills, job_id=job_id, total=total) }}
{% endblock %}
```

### Honest assessment: simpler, or just different?

**Genuinely simpler, in one specific way:** cotton has two calling
conventions to remember (`prop="literal"` vs `:prop="expr"`), and picking the
wrong one silently passes a string where a bool/list was wanted. A macro call
`source_badges(mosaiq=row.in_mosaiq)` has exactly one calling convention —
every argument is "the real Python value," full stop. For a tri-state
(`True`/`False`/`None`) boolean like `in_mosaiq`, that's one fewer way to get
it subtly wrong.

**Genuinely more boilerplate, in one specific way:** cotton auto-discovers
every file under `templates/cotton/` as a globally-available `<c-name>` tag —
no import statement anywhere. Jinja macros need an explicit `{% from "..."
import name %}` at the top of *every* template that uses them. This could be
papered over by registering commonly-used macros as Jinja globals
(`env.globals["patient_table"] = patient_table`) in the FastAPI app setup, but
I'd recommend **against** doing that: explicit imports mean you can see
exactly where `patient_table` comes from by reading the top of the file, which
is more debuggable for a team without deep templating background — arguably
cotton's implicit global namespace was already a minor "magic" tax on the same
audience, and this migration is a chance to trade it for grep-ability.

**A real, if narrow, capability gap:** cotton's `{{ slot }}` (unused today, so
zero migration cost) would be needed if the team ever wants a wrapper
component with arbitrary inner markup (a modal shell, a card wrapper). Jinja's
equivalent is `{% call component() %}...{{ caller() }}...{% endcall %}` —
functionally equivalent but more awkward syntax than cotton's. Not a blocker,
just worth knowing before someone reaches for it and is surprised.

**Must-verify, not a design choice:** Jinja2 autoescapes `{{ }}` by default
only when the environment is configured with `select_autoescape` (FastAPI's
`Jinja2Templates` does this by default for `.html`, but it's exactly the kind
of default that's easy to accidentally override while wiring up the new app).
This app renders free-text fields sourced from the backend
(`error_message`, `comment` — see CLAUDE.md's own anonymisation-boundary
section, which already worries about these exact fields carrying real MRNs in
prose) — autoescaping must be confirmed on, explicitly, in the new setup, not
assumed.

---

## 5. Migration phasing

No existing infra exists to route some paths to Django and others to a new
FastAPI process (no reverse proxy sits in front of `frontend/` today — `proxy/`
is DMZ-only, between browsers and `backend/`, not in front of the internal
frontend). Building that just to enable an incremental "strangle" migration
isn't worth it for an app this size. I'm recommending **build-in-parallel,
cut over once, keep the old code as a rollback reference** instead of an
in-place incremental rewrite — with phases ordered by risk so the hardest,
least-tested piece (`jobs/`) is verified last, with the most context behind it.

### Phase 0 — Scaffolding (foundational, medium effort, low *isolated* risk)

Build the new app skeleton with nothing user-facing yet: config loading
(mirror `hermes_frontend/settings.py`'s env-var conventions so `.env` doesn't
need to change), `Jinja2Templates` wiring + `base.html` port (nav, Tailwind
CDN, htmx CDN — carried forward unchanged per §0), the local SQLAlchemy models
+ Alembic setup (§2.7), the session middleware + CSRF utility (§2.1, §2.2),
`require_login`/`require_data_custodian` dependencies (§2.3), **the flash-message
mechanism (§2.8) and the active-projects context dependency (§2.9)** — both
added to this phase after review; the first draft omitted them entirely and
they're cross-cutting infrastructure every later phase's views depend on, the
same way sessions/CSRF are. Also: empirically confirm trailing-slash redirect
behavior (see §3's cutover-continuity note) before Phase 1 starts building on
top of the routing convention.

*This is where security regressions are most likely to be introduced silently*
(session/CSRF/hashing/flash-token code, all new) — recommend running the
`security-review` skill against this phase specifically before building
anything on top of it, rather than waiting for a final end-to-end review.

### Phase 1 — `accounts/` (medium-high risk, small surface)

Port `users` model, hashing, login/logout, invite/activate (§2.3), user list,
the flash-message calls at every `accounts/views.py` success/error path
(§2.8). **`accounts/tests.py` is currently 3 lines (empty scaffold)** — there
is no regression net here today. Recommend writing characterization tests
against the *current* Django app first, so the port has something concrete to
match, rather than porting five auth views on faith. This is more tractable
than it might sound: `accounts/views.py` makes **zero** `backend_client`
calls (verified by grep — every view there is pure local-DB logic), so
Django's own built-in per-test transactional database is sufficient, no real
Postgres or mocked backend needed; and `jobs/tests.py`'s existing pattern
(`django.test.TestCase` + `self.client` + `mock.patch("jobs.views.backend_client")`,
182 lines, already proven out) is the template to follow for the handful of
`research_projects/` tests in Phase 2 that *do* need backend mocking — this
isn't open-ended new tooling, it's copying a pattern the repo already has.
Small surface area, but auth bugs are high-blast-radius — treat the
risk/effort ratio as worse than the line count suggests.

### Phase 2 — `research_projects/` (low-medium risk)

Port `ProjectDocument` and the corrected member-or-staff download gate (§2.5),
all 10 views (mostly thin `backend_client` wrappers — low logic density), the
4 WTForms equivalents (§2.6), the flash-message calls at every view's
success/error path (§2.8). **`research_projects/tests.py` is also a 3-line
empty scaffold** — same "write characterization tests first" recommendation
as Phase 1, and the same mitigation applies: `research_projects/views.py`
makes 22 `backend_client` calls across its 10 views (verified by grep), so
these need the mocking approach rather than Phase 1's plain-DB one — but
that's exactly what `jobs/tests.py` already demonstrates
(`mock.patch("jobs.views.backend_client")` + `TestCase`), so this is "apply an
existing, proven pattern to a second app," not new test infrastructure. No
async complexity here. The approve/reject/revoke review flow is
business-critical enough (it's the ethics gate) to warrant manual QA against a
real backend before sign-off, independent of automated coverage — and per
§2.5, that QA pass should specifically include a data custodian (not a project
member) opening a submitted project's ethics documents, since that's the
scenario the corrected access-control design exists to keep working.

### Phase 3 — `jobs/` (high risk, highest value, hardest piece)

Port dashboard, the tab forms, the `PendingJob` staging table (§3), `job_watch`,
`job_stream` (the async relay — the single highest-risk piece in this entire
migration), `cancel_job`, `job_detail`, `patient_detail`, `results_lookup`.

`jobs/tests.py` is a real 182-line suite (`Django TestCase` + `self.client` +
`mock.patch("jobs.views.backend_client")`), but it **does not test
`job_stream` itself** — only the synchronous views (`job_detail`,
`patient_detail`). Port the existing tests first (structurally translatable:
FastAPI's `TestClient`/`httpx.AsyncClient` + dependency-override mocking of
`backend_client` plays the same role as Django's `mock.patch` +
`force_login`), confirm they pass against the port, **then** manually verify
the SSE relay end-to-end against a real backend: start a batch job, confirm
live progress renders, cancel mid-job, confirm both the tmp file and the
`pending_jobs` row are gone afterward on both the success and error paths.
This manual step exists because no automated test — old or new — currently
covers it; "the code looks right" is not enough confidence for this one view
given that. The manual pass should also explicitly click through the
single-patient tab on `collect_data` twice in a row without navigating away
(§3's "what must not regress" note) — this is exactly the kind of behavior a
line-by-line code review can miss, since the bug in the first draft's own
reference implementation (an unconditional redirect) *looked* like a
reasonable simplification and would have passed casual review.

### Phase 4 — Cutover (low effort, medium risk)

Full cutover (stop the Django process, start the FastAPI one) rather than a
gradual split, once Phase 3 is manually verified. Migrate `db.sqlite3`'s
`auth_user`/`accounts_profile` rows into the new `users` table via a one-off
script — dry-run it first (report counts/diffs, don't write) given data
migrations' well-earned reputation for surprises. `ProjectDocument` files can
stay exactly where they are under `MEDIA_ROOT` if the new app points at the
same directory — no file copy needed, only the DB rows describing them.

### Phase 5 — Decommission

Remove Django-specific dependencies, `db.sqlite3`, `manage.py`, the Django
migrations directory, once a defined burn-in period passes with no rollback
needed — same spirit as `webui/` being kept around rather than deleted
outright after its own deprecation (per CLAUDE.md).

---

## 6. Open risks / judgment calls

Being explicit about where this is a call rather than a clear win, rather
than presenting a false consensus — each of these survived two rounds of
independent critical review as a genuine, unresolved judgment call, not an
oversight:

1. **Framework choice itself — closer than the first draft presented it.**
   §1's revised "why this is a real tradeoff" section now engages directly
   with the strongest form of the Flask-ecosystem argument (Flask-Login/
   Flask-WTF/Flask-Migrate being more battle-tested than the FastAPI-world
   equivalents, on the security-critical path, for a system gating
   hospital-adjacent workflows) rather than deferring it in one sentence. My
   revised position: FastAPI still wins on the highest-risk single view
   (`job_stream`, native-async), but I now state explicitly that Flask's
   ecosystem would likely reduce risk on the other ~90% of the app more than
   FastAPI's thinner one does. This is a genuinely close call, not a settled
   one — a reviewer weighing ecosystem maturity on the security path above
   the async-native argument could reasonably choose Flask, and I would not
   consider that an unreasonable call to make differently than I did.

2. **Hand-rolled sessions/CSRF/messages is new attack surface for this
   codebase.** Django's contrib apps are extremely well-reviewed; anything
   hand-rolled to replace them, however small, is code this team now owns the
   security properties of — and per §2.8, this list is now three items, not
   two (messages piggybacks on the same `sessions` row, so a bug in session
   handling now also risks flash-message integrity). §2.1/§2.2/§2.8 all flag
   specific mitigations (use a real library for CSRF, budget explicit review
   time, keep the flash mechanism as simple as possible) — but the honest
   framing, per §2.10, is "we are choosing to take on this maintenance burden
   for team-legibility reasons," not "this is free," and §2.10 now says that
   plainly rather than leaving it implicit.

3. **Password validator parity (§2.3) is explicitly *not* solved** —
   vendoring Django's wordlist vs. accepting different validation behavior is
   left as an open decision, not resolved in this draft.

4. **Django admin's break-glass value (§2.4)** — I've proposed CLI scripts as
   a cheap mitigation but haven't sized that work; it could be skipped
   entirely and reactively built only if it's ever actually needed.

5. **Session/pending-job garbage collection (§2.1, §3)** is a new operational
   habit this team needs to adopt (a cron or startup sweep) that Django's
   contrib app made easy to forget about but didn't actually solve for free
   either — worth being honest that this isn't strictly a regression, but it
   is new surface to remember to operate.

6. **Whether to actually start using htmx** (§0) is scope-adjacent but
   deliberately not decided here — e.g. replacing the hand-rolled
   `hermesShowTab()` tab-switch JS with `hx-get`/`hx-target`, or making the
   patient-table filter pills swap without a full page reload. Flagging it as
   a natural opportunity during this rewrite, not recommending it, since it's
   a UX scope question outside "migrate the framework."

7. **Production static-file serving is not actually configured today either**
   (`hermes_frontend/urls.py:14-19` only wires up static serving under
   `DEBUG`; there's no `WhiteNoise` or reverse-proxy config for a real
   deployment in this repo as it stands). This migration shouldn't silently
   inherit that gap — recommend deciding once (e.g. `starlette.staticfiles
   .StaticFiles` mount, acceptable for two SVG files and unlikely to ever need
   more) rather than carrying forward an implicit "works in dev, undefined in
   prod" state without anyone noticing it was never solved.

8. **Test-writing-first for `accounts/`/`research_projects/` (Phases 1-2) is
   real, uncounted scope — though more tractable than it first looked.** Both
   apps currently ship with empty test scaffolds. Writing characterization
   tests before porting them is the right call for an NHS
   ethics-approval-gate and account-provisioning system, but it means the
   migration estimate should include "write tests for code that was never
   tested," not just "port five views." Per §5's Phase 1/2 updates, this is
   less open-ended than the first draft implied: `accounts/` needs no backend
   mocking at all (zero `backend_client` calls), and `research_projects/` can
   directly reuse `jobs/tests.py`'s already-proven `TestCase` +
   `mock.patch("jobs.views.backend_client")` pattern rather than inventing a
   new testing approach. Real scope, not zero scope — but "apply an existing
   pattern twice," not "design testing strategy from scratch."

9. **"Remember me" session-lifetime semantics — resolved in design, not yet
   verified in practice.** §2.1 now specifies the exact split (server-side
   `expires_at` always ~2 weeks; cookie `Max-Age` present only when "remember
   me" is checked) that reproduces `set_expiry(0)`'s behavior. This is a
   design, not an implementation — it should be explicitly exercised in
   Phase 1 QA (log in unchecked, close the browser, confirm the session is
   gone; log in checked, confirm it survives a restart) rather than assumed
   correct because the design reads correctly on paper.

10. **Trailing-slash URL continuity across cutover** — per §3's
    cutover-continuity note, Starlette's default `redirect_slashes` behavior
    should absorb the Django-to-FastAPI URL convention difference
    transparently, but this is stated from documented framework behavior, not
    from having run it against this app. Cheap to verify (a five-minute
    manual check in Phase 0), disproportionately annoying to discover for the
    first time via a staff member's stale bookmark during a hard cutover with
    no fallback period.
