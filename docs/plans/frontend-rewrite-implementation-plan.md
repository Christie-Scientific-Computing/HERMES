# HERMES frontend rewrite — implementation plan

**Status:** approved architecture, ready to implement. Builds on `docs/frontend-migration.md` (the original Django→FastAPI analysis) and `docs/worker-queue-design.md` (the queue architecture this plan's `jobs/` phase depends on). Written against `main` @ the worker-queue cutover (PRs #23-#26 merged). This document is the execution-level companion to those two: where they analyze *whether* and *what*, this specifies *how*, file by file, phase by phase, with testing and risk call-outs. Every claim below was checked directly against the live code on this branch, not assumed from the earlier docs — see each phase's "Validated against the codebase" note.

**Scope confirmed with the user before writing this**: FastAPI + Jinja2, htmx adopted now (not deferred) for tab-switching and filter pills, minimal break-glass CLI scripts in Phase 1, and four new features folded into the rewrite: a combined import→export page, a cohort/data-availability browser (Orthanc + the destination PACS), an admin compliance dashboard, and in-app notifications. Two decisions from architecture review, carried forward here: the document-access-control gap (see Phase 2) is fixed as part of Phase 2, not patched standalone first; backend-side role enforcement stays frontend-only, consistent with the existing architecture (see Phase 4).

---

## 0. Dependencies

Checked directly against the venv (`pip show`) and `requirements.txt` — some things assumed missing were already installed (just undeclared), some assumed present are genuinely missing:

| Package | Status | Action |
|---|---|---|
| `fastapi`, `uvicorn[standard]`, `httpx`, `psycopg2-binary`, `python-dotenv`, `pydantic` | Installed, declared | none |
| `python-multipart` | **Installed (0.0.32), undeclared** | add to `requirements.txt` (needed for `UploadFile`) |
| `alembic` (1.18.5), `SQLAlchemy` (2.0.51) | **Installed, undeclared** — `backend/` already depends on both, silently | add to `requirements.txt` explicitly; the frontend's own local Alembic project uses the same libraries |
| `Jinja2` | **Not installed** | add — `fastapi.templating.Jinja2Templates` needs it explicitly, don't rely on a transitive pull |
| `passlib[argon2]` or `argon2-cffi` | **Not installed** | add (`argon2-cffi` directly is simpler than `passlib`'s wrapper layer for a single hashing scheme) |
| `WTForms` | **Not installed** | add |
| `itsdangerous` | **Not installed** | add |
| `starlette-csrf` (or hand-rolled) | **Not installed** | add if going the library route (recommended — see Phase 0 §CSRF) |

`requirements.txt`'s header comment ("Covers both the backend (FastAPI) and gateway (FastAPI + Streamlit) packages") is already stale (`gateway/` was deleted); update it to also cover the new frontend when these are added, and drop the now-dead `streamlit` line once `Home.py`/`gateway/` are formally retired (out of scope for this plan — flagging only).

---

## 1. Repository layout

```
frontend_fastapi/                  # new, sibling to frontend/ (Django stays untouched until Phase 5 cutover)
    main.py                        # FastAPI app, mirrors backend/main.py's shape
    settings.py                    # env-var loading, mirrors hermes_frontend/settings.py's variable NAMES exactly
    db.py                          # SQLAlchemy engine/session for this project's OWN local DB (users/sessions/etc.) -- NOT HermesDB
    alembic/                       # this project's own migrations -- separate from backend/alembic/, never the same DATABASE_URL
    models/                        # SQLAlchemy models: User, Session, PendingJob(removed, see Phase 3a), ProjectDocument
    deps.py                        # require_login, require_data_custodian, get_db, csrf dependencies
    backend_client.py              # near-identical port of frontend/hermes_frontend/backend_client.py (httpx-based, same functions)
    templates/                     # Jinja2, mirrors frontend/templates/ + frontend/*/templates/ structure
    static/
    routers/
        accounts.py
        research_projects.py
        jobs.py
        cohort.py                  # new
        admin.py                   # new
    forms/                         # WTForms, one module per router roughly matching today's forms.py files
```

This mirrors `frontend/`'s existing app-per-concern split closely enough that porting is mechanical where possible, while collapsing Django's `app/{models,views,forms,urls,templates}.py` scatter into `routers/`+`forms/`+`models/` (no per-router `urls.py` needed — FastAPI routers declare their own paths inline).

---

## 2. Phase 0 — Scaffolding

**Goal:** everything that has no user-visible behavior of its own but that every later phase depends on. Nothing in this phase is portable from Django 1:1 — it's all hand-rolled replacement for what Django's contrib apps gave for free.

### 2.1 Tasks

1. **`frontend_fastapi/main.py`, `settings.py`** — mirror `backend/main.py`'s startup shape (load `.env` from repo root, then a local one) and `hermes_frontend/settings.py`'s exact variable names (`BACKEND_URI`/`BACKEND_PORT`/`BACKEND_URL`, `HERMES_INTERNAL_KEY`, `DJANGO_SECRET_KEY`→rename to e.g. `HERMES_FRONTEND_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`→similar rename, `DJANGO_EMAIL_*`→keep as-is or rename consistently) so `.env` files need minimal changes on cutover. Confirmed via `settings.py:28-29`: it loads `BASE_DIR.parent / ".env"` (repo root) then a local one — replicate this exact two-file load order.
2. **Local DB models** (`models/`, SQLAlchemy): `User` (username, email, first_name, last_name, department, is_staff, is_superuser, is_active, password_hash, created_at — merges Django's `User`+`Profile` into one table per `docs/frontend-migration.md` §2.3, since there's no Django `User` to extend around), `Session` (id, user_id nullable, csrf_token, flash_messages JSON via `MutableList.as_mutable`, expires_at, created_at), `ProjectDocument` (project_id str — not a FK, matching `research_projects/models.py:20`'s own comment about there being no local Project model — file_path, original_filename, uploaded_by, uploaded_at).
3. **Local Alembic project** (`frontend_fastapi/alembic/`) — run `alembic init` inside `frontend_fastapi/`, then edit `env.py` to mirror `backend/alembic/env.py`'s pattern (`DATABASE_URL` env override wins over `alembic.ini`, `disable_existing_loggers=False`) but point at a **separate** DSN (e.g. `HERMES_FRONTEND_DATABASE_URL`, defaulting to a local sqlite file matching today's `db.sqlite3` convenience, or a Postgres DSN if preferred). **Do not point this at `DATABASE_URL`/HermesDB** — this is a hard rule already established for `backend/`'s two-database split (CLAUDE.md's "Two entirely separate Postgres databases" section) and applies identically here: a third, entirely separate local DB, never conflated with HermesDB or the anon-mapping DB.
4. **Sessions** — a small dependency (`deps.get_session`) that reads an opaque cookie (`hermes_session`, httponly, `secure` in prod, `samesite=lax`), loads the `Session` row, creates one if absent, exposes it as `request.state.session`. Two independent lifetime knobs, replicating `HermesLoginView.form_valid`'s exact `set_expiry(0)` semantics (`accounts/views.py:26-34`): `Session.expires_at` always ~2 weeks server-side regardless of "remember me"; the cookie's `Max-Age` is what "remember me" actually controls — present (2 weeks) when checked, **omitted** entirely when unchecked (an omitted `Max-Age` is what makes a cookie a true browser-session cookie).
5. **CSRF** — recommend `starlette-csrf` over hand-rolling (this is exactly the kind of security-sensitive, easy-to-subtly-break code — constant-time comparison, `SameSite` correctness, token-session binding — not worth writing from scratch without a dedicated security pass). Fallback if a third-party dependency is unwanted: a `csrf_token` column on `Session` (already planned above), a Jinja global `csrf_token()`, a hidden `<input name="csrf_token">` replacing every `{% csrf_token %}` (mechanical find-and-replace across the ported templates), a dependency 403ing any POST where the field doesn't match.
6. **`require_login`/`require_data_custodian`** dependencies. Confirmed the exact function this replaces is defined **twice, verbatim**, in `accounts/views.py:18-19` and `research_projects/views.py:10-11` (`user.is_active and user.is_staff`) — collapse to one shared dependency here, don't port the duplication.
7. **Flash messages** — `flash_messages` as a `MutableList.as_mutable(JSON)` column on `Session` (not a plain `JSON` column — SQLAlchemy's unit-of-work doesn't track in-place `.append()` on a bare JSON column, this was caught in the original doc's own review pass). A `flash(request, tag, text)` helper + a Jinja template block that pops (reads-then-clears) on render, porting `templates/base.html`'s existing message block including its per-tag styling.
8. **Active-projects context dependency** — port `hermes_frontend/context_processors.py`'s `active_projects` (11 lines, trivial) as a FastAPI dependency injected into every authenticated route's template context, same `nav_active_projects` key, same "staff have no special case here, it's literally just their own active projects" behavior.
9. **Password hashing** — `argon2-cffi` (or `passlib[argon2]`), a deliberate upgrade over Django's PBKDF2 default, not a parity requirement.
10. **Invite/activate tokens** — `itsdangerous.URLSafeTimedSerializer`, signing `{"uid": user.id, "pwhash_fingerprint": sha256(user.password_hash)[:12]}` with an expiry. The fingerprint component is what gives "invalidated once the password is actually set" (Django's `default_token_generator` gets this by including the password hash in what it signs — replicate that property, don't drop it).
11. **Static files** — `starlette.staticfiles.StaticFiles` mount. Confirmed via `hermes_frontend/urls.py:14-19`: production static serving isn't actually configured today either (`runserver` auto-serves in `DEBUG`, nothing wires it up for a real deployment) — this rewrite shouldn't silently inherit that gap. Decide once here.

### 2.2 Testing

- Unit tests for the session lifetime split (create with "remember me" checked → cookie has `Max-Age`; unchecked → no `Max-Age`; `Session.expires_at` is ~2 weeks either way) — this is a design that reads correctly on paper and needs actually exercising, not assuming.
- Unit tests for the flash-message `MutableList` mutation tracking specifically (`.append()` then `db.commit()` then re-fetch the row in a fresh session — confirm the append persisted). This exact bug (plain `JSON` column, mutation invisible to the ORM) was caught in `docs/frontend-migration.md`'s own second review pass; a regression test here is cheap insurance against reintroducing it.
- CSRF: a test POST without the token 403s; with a stale/mismatched token 403s; with a valid one succeeds.
- Invite/activate token: sign → verify succeeds; verify after the user's `password_hash` changes fails (this is the property that makes a used token stop working).

### 2.3 Risks

- **This is where security regressions are most likely to be introduced silently** (session/CSRF/hashing/flash-token code, all genuinely new — Django's contrib apps are extremely well-reviewed, this replacement isn't yet). Run the `security-review` skill against this phase specifically before Phase 1 builds anything on top of it, per the original doc's own recommendation.
- Trailing-slash convention: every current Django route ends in `/` (`APPEND_SLASH`). Starlette's default `redirect_slashes=True` should absorb this transparently for old bookmarks, but this is stated from documented framework behavior, not from having run it against this app — verify empirically here (a five-minute check), not discovered for the first time via a stale bookmark during the Phase 5 hard cutover.
- Password validator parity is explicitly **not** solved by this plan (same as the original doc's open item #3) — Django's `CommonPasswordValidator` ships a ~20k-entry wordlist and `UserAttributeSimilarityValidator` does fuzzy-matching against username/email/name (`accounts/forms.py:71-86`'s `clean_password2` exercises exactly this). Vendoring Django's wordlist vs. accepting different validation behavior (e.g. `zxcvbn`'s strength score) is a decision to make explicitly in this phase, not silently drop.

### 2.4 Validated against the codebase

Confirmed by direct read (not assumed from the original doc): `_is_data_custodian` really is duplicated verbatim in two files; `settings.py`'s two-stage `.env` load order; `HermesLoginView.form_valid`'s exact "remember me" logic; `context_processors.py` really is as trivial as described (11 lines); static serving really is `DEBUG`-only today (`urls.py:14-19`).

---

## 3. Phase 1 — `accounts/`

**Goal:** port `accounts/` (login, invite, create-user, activate, user list) plus the new break-glass CLI scripts. `accounts/tests.py` is confirmed empty (a 3-line scaffold) — no regression net exists today, so this phase includes writing characterization tests against the *current* Django app first.

### 3.1 Tasks — direct port checklist

Every route in `accounts/urls.py`, confirmed by direct read, maps to a new FastAPI route:

| Django route (name) | New route | Notes |
|---|---|---|
| `login/` (`login`) | `GET/POST /accounts/login` | `HermesAuthenticationForm`'s `remember_me` field → `CombinedLoginForm` (WTForms), sets session per Phase 0 §2.4 |
| `logout/` (`logout`) | `POST /accounts/logout` | clears session |
| `invite/` (`invite`) | `GET/POST /accounts/invite`, `require_data_custodian` | `InviteUserForm` → WTForms equivalent; **the flash-message activation-link fallback (`views.py:59`) is load-bearing, not cosmetic** — this is the only working path for onboarding today since no SMTP is configured; do not drop it in the port |
| `users/` (`user_list`) | `GET /accounts/users`, `require_data_custodian` | |
| `users/create/` (`create_user`) | `GET/POST /accounts/users/create`, `require_data_custodian` | `CreateUserForm`'s cross-field password-match + Django-validator check (`forms.py:71-86`) — port the *cross-field* logic; the *validator* itself is Phase 0's open item |
| `activate/<uidb64>/<token>/` (`activate`) | `GET/POST /accounts/activate/{token}` | `itsdangerous` token replaces `uidb64`+`default_token_generator`'s two-part scheme — one opaque token, not two path segments |

**Break-glass CLI scripts** (new, no Django precedent — confirmed via `find frontend -path "*management/commands*"`: zero existing management commands anywhere in this repo, so this is genuinely the first one): a small script invoked directly (`python -m frontend_fastapi.scripts.reset_password <username>`, since there's no Django `manage.py` equivalent to hook into — a plain argparse script against the same SQLAlchemy session Phase 0 sets up is simplest). Minimum viable set: `reset_password.py` (set a new password directly, bypassing the invite/activate flow), `set_staff.py` (toggle `is_staff` — the operational scenario this replaces is "someone's `is_staff` flag is wrong and they can't self-serve a fix").

### 3.2 Testing

- **Write characterization tests against the *current* Django app first** (it has none today), then confirm the port matches. Confirmed via grep: `accounts/views.py` makes **zero** `backend_client` calls — every view here is pure local-DB logic, so Django's own per-test transactional database is sufficient, no real Postgres or backend mocking needed. This makes the characterization-test-first step more tractable than it sounds, not open-ended.
- Ported tests: login (with/without "remember me", asserting the cookie `Max-Age` behavior from Phase 0), invite (asserts the flash message contains the activation link, given no real email will be received in a test), activate (valid token succeeds, expired/tampered/already-used token fails — "already-used" specifically exercises the `pwhash_fingerprint` property), create_user (password mismatch, password validator rejection), user_list staff-only gate.
- Break-glass scripts: a test invoking each script's `main()` directly against a test DB, asserting the intended row change.

### 3.3 Risks

Small surface area, but auth bugs are high-blast-radius — treat the risk/effort ratio as worse than the line count suggests (same framing as the original doc). The invite-email fallback message is the single highest-consequence thing to get right in this phase: if it's silently dropped or broken, the only working onboarding path in a deployment with no SMTP disappears with no error and no obvious symptom until someone tries to invite a colleague.

### 3.4 Validated against the codebase

Confirmed the exact route table above by reading `accounts/urls.py` and `accounts/views.py` directly (not from the earlier summary). Confirmed zero `management/commands` directories exist anywhere in `frontend/`. Confirmed `accounts/views.py` genuinely makes no `backend_client` calls (visually scanned the full file — only `django.contrib.auth`/local DB operations).

---

## 4. Phase 2 — `research_projects/`

**Goal:** port `research_projects/` (10 views, list/create/detail/submit/review/revoke/membership/documents) plus the corrected document-access-control gate and the ethics-workflow polish (live "expiring soon" indicator, light document-handling improvements). `research_projects/tests.py` is also confirmed empty.

### 4.1 Tasks — direct port checklist

Every route in `research_projects/urls.py`, confirmed by direct read:

| Django route (name) | New route | Notes |
|---|---|---|
| `` (`list`) | `GET /projects` | staff see everything (`?status=` filter available), everyone else sees only their own — `project_list`'s exact branch (`views.py:15-27`) |
| `new/` (`create`) | `GET/POST /projects/new` | `CreateProjectForm` → WTForms |
| `review/` (`review_queue`) | `GET /projects/review`, `require_data_custodian` | |
| `<project_id>/` (`detail`) | `GET /projects/{project_id}` | assembles project + `is_member` + documents + jobs + three embedded forms — port as one view building one template context, same shape |
| `<project_id>/submit/` (`submit`) | `POST /projects/{project_id}/submit` | |
| `<project_id>/review/` (`review`) | `POST /projects/{project_id}/review`, `require_data_custodian` | `ReviewProjectForm`'s cross-field "expiry required when approving" (`forms.py:26-30`) — port as a WTForms `validate_expiry_date` |
| `<project_id>/revoke/` (`revoke`) | `POST /projects/{project_id}/revoke`, `require_data_custodian` | |
| `<project_id>/members/add/` (`add_member`) | `POST /projects/{project_id}/members` | |
| `<project_id>/members/<username>/remove/` (`remove_member`) | `POST /projects/{project_id}/members/{username}/remove` | |
| `<project_id>/documents/upload/` (`upload_document`) | `POST /projects/{project_id}/documents/upload` | |
| **(none today)** | `GET /projects/{project_id}/documents/{doc_id}/download` | **new** — see §4.2 |

### 4.2 The document-access-control fix — the actual work in this phase

**Confirmed, not assumed**: `research_projects/views.py` has no download view at all. Documents are currently reached only via `{{ doc.file.url }}` in the detail template, served through Django's raw `MEDIA_URL` with **zero** access control — `upload_document` (`views.py:164-173`) is `@login_required` only, and there is nothing gating read access. Any logged-in user (or anyone who obtains a document's URL by any means) can download any project's ethics-approval documents today.

**Fix** (per `docs/frontend-migration.md` §2.5, corrected during that doc's own review — the first draft's proposal would have locked out data-custodian reviewers, who are structurally *not* project members): gate the new download route on `require_login` **and** (`is_member` OR `is_staff`), mirroring the same pattern `project_list` already uses for the analogous "who sees this" question. `is_member` is computed exactly as `project_detail` already does it (`views.py:60`: `any(m["username"] == request.user.username for m in project["members"])`), fetched fresh from the backend, not cached.

```python
@router.get("/{project_id}/documents/{doc_id}/download")
async def download_document(project_id: str, doc_id: int, db=Depends(get_db), user=Depends(require_login)):
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or doc.project_id != project_id:
        raise HTTPException(404)
    project = await backend_client.get_project(project_id)
    is_member = any(m["username"] == user.username for m in project["members"])
    if not (is_member or user.is_staff):
        raise HTTPException(403)
    return FileResponse(Path(settings.MEDIA_ROOT) / doc.file_path, filename=doc.original_filename)
```

**Decided during architecture review**: this waits for this phase, not a standalone patch to the currently-running Django app. Noted here so this phase's own task list and code review treat it as closing a known, real gap — not a routine template-and-view port where a reviewer might reasonably assume there's nothing security-sensitive to check twice.

### 4.3 Ethics-workflow polish (new, this plan's addition)

- **Live "expiring soon" indicator** — a per-request check (reusing Phase 0's active-projects dependency pattern) calling a new `ProjectsDB.list_expiring_projects(within_days=30)` (backend change — see the architecture plan's Feature 4 for the exact query), filtered to the current user's own projects, rendered on `project_list`/`project_detail`. No new scheduler needed — this is computed live on page load, deliberately not a persisted/pushed notification (see Phase 4).
- **Document-handling polish**: show uploader + upload date on each document row (data already captured, `ProjectDocument.uploaded_by`/`uploaded_at` — likely just not surfaced in the current template; confirm during port), and a delete action gated the same way as the new download route (member or staff) plus additionally restricted to the uploader or staff specifically (a delete is more consequential than a read).

### 4.4 Testing

- Characterization tests first (empty scaffold today). `research_projects/views.py` makes **22 `backend_client` calls across its 10 views** (confirmed by grep) — these need the mocking approach, not Phase 1's plain-DB one, but `frontend/jobs/tests.py`'s already-proven pattern (`TestCase` + `mock.patch("jobs.views.backend_client")`, 182 lines) is the template to copy, not new tooling to invent.
- **New tests for the document-access fix specifically**: a non-member, non-staff user gets 403 on download; a project member succeeds; a staff data-custodian who is *not* a member succeeds (this is the scenario the corrected design exists to keep working — the original draft's bug would have failed exactly this case); an unauthenticated request redirects/401s.
- Manual QA pass (business-critical, ethics gate): a data custodian who is *not* a member of a submitted project opens that project's documents from the review queue before approving/rejecting — confirm this specific flow works, since it's the one a naive membership-only gate would have silently broken.
- "Expiring soon" indicator: a project with `expiry_date` inside vs. outside the window renders/doesn't render the banner; a project with no `expiry_date` (open-ended approval) never shows it.

### 4.5 Risks

The approve/reject/revoke review flow is business-critical enough (it's the ethics gate) to warrant the manual QA pass above independent of automated coverage, regardless of how much automated coverage exists.

### 4.6 Validated against the codebase

Read `research_projects/models.py`, `views.py`, `forms.py`, `urls.py` in full directly for this plan (not from an earlier summary) — confirmed the 10-route table, confirmed `upload_document`'s exact gate (`@login_required` only), confirmed no download view exists anywhere, confirmed `is_member`'s exact computation in `project_detail`.

---

## 5. Phase 3a — `jobs/` core parity

**Goal:** a faithful port of *today's* behavior only. Deliberately nothing from this plan's new features lands here — see Phase 3b — so a bug in this phase can never be confused with a parity regression, and vice versa.

### 5.1 What's dramatically simpler now than when `docs/frontend-migration.md` was written

That doc called this *"the single highest-risk piece in this entire migration"* because of the two-phase `PendingJob` staging mechanism. **That mechanism no longer exists** — the worker-queue rewrite deleted it. Confirmed via fresh read of the current `frontend/jobs/views.py`: `collect_data`/`retrieve_data` now call `_enqueue_batch_job` directly, which POSTs straight to the backend and gets a `job_id` back immediately; nothing is staged to `MEDIA_ROOT` or session. There is **no `PendingJob` model to port**, no staging-security-invariant to replicate, no `pending_jobs` GC cron to invent (the original doc's open item #5 is now moot for this reason). `job_watch`/`job_stream` do a live visibility re-check (`_user_can_watch_job`) on every request instead.

### 5.2 Tasks — direct port checklist

| Django view | New route | Notes |
|---|---|---|
| `dashboard` | `GET /` | two nav cards → **three**, once Phase 3b's combined page lands (see below) |
| `collect_data` | `GET/POST /collect-data` | single/batch tabs; single mode re-renders inline with `job_id` set (does **not** redirect — `views.py:135-136`, this is deliberate rapid-entry UX, confirmed still true post-queue-rewrite); batch mode redirects to `job_watch` |
| `retrieve_data` | `GET/POST /retrieve-data` | dicom/proknow tabs, both always redirect (no single-mode) |
| `job_watch` | `GET /jobs/{job_id}/watch` | `_user_can_watch_job` port: live `job_summary` + `_job_is_visible_to` check, no session trust |
| `job_stream` | `GET /jobs/{job_id}/stream` | **the one async view** — now just a plain relay of the backend's `GET /results/job/{job_id}/stream`, re-framing `data:` lines as named `event:` lines. Confirmed via direct read: this is now ~30 lines, not the load-bearing security mechanism the original doc worried about |
| `cancel_job` | `POST /jobs/{job_id}/cancel` | |
| `job_detail` | `GET /jobs/{job_id}` | patient table + filter pills |
| `patient_detail` | `GET /jobs/{job_id}/patients/{mrn}` | |
| `results_lookup` | `GET /results` | job-id or patient-mrn lookup |

**htmx work in this phase** (per the confirmed decision to adopt now): replace the hand-rolled `hermesShowTab()` JS — confirmed duplicated verbatim across `collect_data.html`/`retrieve_data.html` — with `hx-get`/`hx-target` tab switching (one shared partial instead of two copies of the same script), and make `patient_table`'s filter pills swap via `hx-get` instead of a full page reload.

### 5.3 Testing

- Port `jobs/tests.py`'s existing 182 lines (confirmed real, not a scaffold) — structurally translatable: FastAPI's `TestClient`/`httpx.AsyncClient` + dependency-override mocking of `backend_client` plays the same role Django's `mock.patch("jobs.views.backend_client")` + `force_login` does.
- **Manual SSE pass, end-to-end against a real backend** — this is still necessary; confirmed no automated test (old or new) covers `job_stream` itself, even post-simplification: start a batch job, confirm live progress renders, cancel mid-job, confirm the observer stream reports `cancelled` then `done` correctly.
- Explicitly re-verify the single-patient inline-progress behavior (`collect_data`'s single mode) — click through it twice in a row without navigating away. This is exactly the kind of behavior a line-by-line code review can miss (flagged in the original doc: an unconditional redirect *looks* like a reasonable simplification and would pass casual review while silently breaking the rapid-entry workflow).

### 5.4 Risks

Low risk relative to the original doc's assessment, specifically *because* the hardest part (two-phase staging) is gone — but this is still the highest-traffic app in the whole system, so port discipline (matching today's behavior exactly, no incidental changes) matters more here than anywhere else in the migration.

### 5.5 Validated against the codebase

Re-read `frontend/jobs/views.py` fresh for this plan (not from pre-worker-queue-rewrite memory) — confirmed the current, post-queue shape of every view in the table above, confirmed the single-mode-doesn't-redirect behavior is still present, confirmed `job_stream`'s current ~30-line size.

---

## 6. Phase 3b — new jobs-adjacent features

**Goal:** the combined import→export page and the cohort/data-availability browser. Started once Phase 3a is manually verified (so a bug here is never confused with a parity regression). These share no code path with 3a's port and both sit on infrastructure that already exists — could run in parallel with 3a if resourcing allows.

### 6.1 Combined import→export page

Full backend design (chaining mechanism, race-condition fix, new endpoint, SSE vocabulary extensions) is in the architecture plan already agreed — reproduced here as an execution checklist:

**Backend tasks** (`backend/`, separate PRs from the frontend work, same review discipline as the worker-queue PRs):
1. `backend/worker.py`: `_maybe_chain_export` + reordered `_handle_one` (chain-enqueue **before** `mark_succeeded` — this ordering is load-bearing, not stylistic, see the architecture plan's race-condition writeup) + the try/except guard around the chain call specifically (confirmed via direct read: `main()`'s claim loop has no try/except around `_handle_one`, so an unguarded chain-enqueue failure would crash the whole worker process, not just fail one task).
2. `backend/src/status/tasks_db.py`: add `kind`/`stage` to `job_progress`'s `SELECT`.
3. `backend/src/results/endpoints.py`: `_observe_job` — add `"stage"` to `progress`/`success`/`error` payloads; add the new `total` event (emitted whenever `len(rows)` changes, not just once at `start`).
4. `backend/src/status/db_client.py`: `count_completed_by_stage(job_id, stage, success_detail_key=None)`, generalizing `count_imported_patients`; `get_latest_event_per_patient(job_id, stage=None)` gains the optional stage filter.
5. `backend/src/results/endpoints.py`: `job_summary` gains `exported_count`; `job_patients_summary` gains `import_outcome`/`import_error_message`/`export_outcome`/`export_error_message`.
6. `backend/src/retrieve/endpoints.py`: new `combined_import_export_file` endpoint (full code in the architecture plan).

**Frontend tasks** (`frontend_fastapi/`):
1. `routers/jobs.py`: `combined_import_export` view (GET/POST), `backend_client.combined_import_export_file(...)` wrapper.
2. `forms/jobs.py`: `CombinedImportExportForm` — mrn-or-file, `import_level`, `export_kind` choice, `destination_or_collection` populated live per `export_kind` (same live-population pattern `retrieve_data` already uses for modalities/collections).
3. New template, reachable as a **prominent third nav card on the dashboard** — this is meant to be the primary entry point, not buried.
4. **New progress component** — not a port of `cotton/job_progress.html` (single-stage by design: one `EventSource`, one bar, one log). Needs two progress bars (Import/Export), a per-patient table with two status badges routed by the new `stage` field, and explicit handling of the "import terminal but not `imported=True` → no export task will ever exist for this patient" state (render as "Not exported — not found on import," not a perpetual spinner — this is a real UI state that will otherwise look like a bug).
5. Final summary reads `job_summary`'s new `exported_count`/`submitted_count`: `"Requested: {submitted_count}, Exported: {exported_count}. View Errors."`, linking to `job_detail` filtered on `export_outcome == 'failure'`.

### 6.2 Cohort/data-availability browser

**Backend tasks:**
1. `backend/src/studies/endpoints.py`: add `limit`/`offset` params to `GET /studies` (Orthanc's `/tools/find` already supports `Limit`/`Since` — this is additive, not a rewrite).
2. `dicom_move_uids_file` needs **no backend change** — already exists, tested, queue-converted; this phase is simply its first real caller.
3. **Destination-PACS querying** — recovered from git history (`gateway/pacs_client.py` at commit `4b968b4`, deleted at `7116708`). Port near-verbatim as a framework-agnostic module (`pacs_client.py`: `is_configured`, `echo`, `query_series_batch`, `query_studies_batch`, the `_convert_uid`/Conquest `dgate64.exe` passthrough logic) so it can be wired into the new frontend directly *or* wrapped by a small standalone service, depending on where the deployed frontend turns out to sit relative to the destination PACS's network — **this placement decision is deferred to this phase specifically**, when the frontend's actual deployment host is known (see the architecture plan's open-question resolution). Whichever way it's wired, wrap the synchronous `pynetdicom` calls in `asyncio.to_thread`.

**Frontend tasks:**
1. `routers/cohort.py`, template `cohort/browse.html`: filter form (patient_id/study_date/modality) → paginated `/studies` results, checkboxes per study, a selection bar with project+destination pickers feeding `dicom_move_uids_file` (build the CSV client-side matching `_build_uid_items`'s expected columns, POST through a new `backend_client.dicom_move_uids_file(...)` wrapper).
2. **Client-side guard**: require at least one filter before allowing submission of an unfiltered query (a paginated-but-unfiltered query against a full PACS-scale Orthanc is still a bad first experience) — note in the UI that the result count is "how many came back this page," not a true total match count (Orthanc's find API doesn't expose one independent of `Limit`).
3. Destination-PACS badge per result row ("on destination PACS: yes/no/unknown"), fetched via one batch `query_studies_batch` call for the visible page (not N+1 per-row queries).

### 6.3 Testing

- `_maybe_chain_export`: mirror `test_worker.py`'s existing fake-kind pattern. Critically, **assert the ordering**: enqueue an import task with `chain_export` params, drive it through the worker, and confirm the export task row exists (queryable via `TasksDB.get_task`/`job_progress`) at the moment *just before* the import task's own `mark_succeeded` call resolves — not just "eventually both exist." A test that only checks final state would pass even with the buggy (post-`mark_succeeded`) ordering; the whole point is closing a race that only shows up under polling timing, so the test needs to assert the ordering directly, not just the end state.
- `count_completed_by_stage`, the new `total` SSE event (assert it's emitted exactly when task count changes, not every tick), `/studies`' new `limit`/`offset`.
- Manual pass: run a real combined job end-to-end (a CSV with a mix of patients that will and won't be found), confirm the two-column progress UI and final summary match reality, including the "not exported" case.
- `pacs_client.py`: unit tests against a mocked `pynetdicom` association (mirroring however the old `gateway/` tests — if any existed — approached this; if none did, this is new coverage) — `is_configured()` false when unset, `echo()`/`query_*_batch` handling of a failed association gracefully (`None` results, not an exception escaping to the caller).

### 6.4 Risks

The chaining race condition (§6.1 point 1) is the single most important correctness detail in this whole plan — it's subtle (only manifests under specific poll timing), silent when wrong (the user just sees the stream end early, no error), and was found by design review rather than being obvious from the code shape. Treat it as the first thing verified, not an afterthought.

### 6.5 Validated against the codebase

`pacs_client.py`/`routers/pacs.py`/`gateway/main.py` recovered and read in full from git history (commit `4b968b4`, before deletion commit `7116708`) — the code excerpts in this plan and the architecture plan are the actual recovered source, not a reconstruction from memory. Confirmed `main()`'s claim loop has no try/except around `_handle_one` by direct read of current `backend/worker.py`.

---

## 7. Phase 4 — Admin dashboard + notifications

**Goal:** net-new functionality (no Django precedent to port, so near-zero regression risk, but real new build effort). Placed after Phase 3 since it reads job/task data Phase 3's port is what establishes as reliable.

### 7.1 Tasks

Backend (per the architecture plan's full design):
1. `backend/src/admin/endpoints.py` — new `/admin` router, `GET /admin/overview`, same `verify_internal_key` gate as every other project-gated router, **no staff check inside the backend** (see §7.2).
2. `ProjectsDB.list_expiring_projects(within_days=30)`, `StatusDB.list_recent_jobs_with_counts(limit=50)`.
3. Audit-chain periodic check: reuse `backend/scripts/verify_audit_chain.py`'s existing pure functions from a new timer in `worker.py`'s `main()` loop (alongside the existing `reap_stale_claims` timer, much longer interval — e.g. daily), persisting to a new `audit_chain_checks` table via a new `audit_chain_db.py` client.
4. `notifications` table + `backend/src/notifications/db_client.py` + router. Population hooks: `TasksDB.job_is_complete` + a race-safe `jobs.completed_notified_at` marker called from `worker.py`'s `_handle_one` after every terminal write; `review_project`'s endpoint notifying every current member on a decision.

Frontend:
1. `routers/admin.py` — `admin_overview` view, **gated with `require_data_custodian`** (Phase 0's dependency).
2. Dashboard template: project-status counts, expiring-soon list, recent-jobs table, audit-chain status ("last verified: ..., OK" / "never verified yet" for a fresh deployment).
3. Notification dropdown (persisted job-done/approval-decision rows + a visually-distinct "live" section for the current user's own expiring-soon projects, reusing the same query Phase 2's project-list banner already uses).

### 7.2 Backend authorization — the constraint, stated precisely

**The backend gains no role-checking of its own here** — this was reviewed explicitly and is a deliberate continuation of the existing architecture, not an oversight: HermesDB has no user or role table, by design (`backend/src/projects/db_client.py`'s own docstring: Django — soon the new frontend — is the sole source of truth for user identity). This is true of *every* project-gated backend endpoint today, including the equally-sensitive ethics-review approve/reject/revoke actions, and giving HermesDB an actual role table would be a real architecture change, not a small addition.

**This is not a gap that reappears when Django is removed** — it's the same single point of enforcement, relocated from Django's `user_passes_test(_is_data_custodian)` to this rewrite's `require_data_custodian` dependency (built in Phase 0). The actual risk is the ordinary implementation-discipline risk of a route forgetting to attach that dependency — which is why it's called out as a **requirement**, not a suggestion, checked explicitly in this phase's code review: the frontend route serving `/admin/overview` must use `require_data_custodian`, the same way `research_projects`' review views already use `_is_data_custodian` today.

### 7.3 Testing

- `list_expiring_projects`: a project inside/outside/at-the-boundary of the window, a project with no `expiry_date` never appears.
- `list_recent_jobs_with_counts`: matches what N separate `job_summary` calls would have returned, for a handful of test jobs with mixed success/failure counts (a correctness check on the `JOIN`+`GROUP BY` against the N+1 approach it replaces).
- Audit-chain periodic check: a healthy chain records `ok=True`; a deliberately tampered `events` row (same technique `test_hash_chain.py` already uses) records `ok=False` with a `bad_event_id`; the check running against an empty `events` table (fresh deployment) doesn't error.
- **Explicit frontend-gate test**: an authenticated-but-non-staff user gets 403/redirected from `/admin`, confirming `require_data_custodian` is actually attached — this is the test that would catch the "forgot the dependency" risk named above.
- Notifications: job completion creates exactly one notification even if `job_is_complete` is checked from multiple concurrent terminal writes (the race-safe marker's whole point); an approval decision notifies every member, not just the submitter.

### 7.4 Risks

Silent-failure risk is structurally different from every other phase: a missing `require_data_custodian` dependency, a worker.py audit-check timer that's wired but never actually scheduled, or a notification hook that silently no-ops all have **no visible symptom** until someone notices data they shouldn't see, or an incident where "was this ever actually checked" matters and the honest answer is no one knows. Test for the absence of things, not just the presence.

### 7.5 Validated against the codebase

Confirmed `ProjectsDB`'s docstring language on user-identity sourcing, confirmed `worker.py`'s existing `reap_stale_claims` timer pattern is a real, working precedent to extend (not a hypothetical), confirmed `verify_audit_chain.py`'s functions are cleanly separable (no CLI-only state) via direct read in the earlier research pass.

---

## 8. Phase 5 — Cutover

Full cutover (stop Django, start the FastAPI app) rather than a gradual split — no reverse proxy exists in front of `frontend/` today to enable a strangler-fig approach, and building one just for this migration isn't worth it for an app this size (same reasoning the original doc gives).

1. Migrate `db.sqlite3`'s `auth_user`/`accounts_profile` rows into the new `users` table via a one-off script. **Dry-run first** (report counts/diffs, don't write) — data migrations have a well-earned reputation for surprises, and this one is irreversible against a production user base if wrong.
2. `ProjectDocument` files stay exactly where they are under `MEDIA_ROOT` if the new app points at the same directory — no file copy needed, only the DB rows describing them (which the migration script above also needs to carry over, since `ProjectDocument` moves from Django's ORM to the new SQLAlchemy model).
3. **Confirm, don't just merge**, that `worker.py`'s new periodic hooks (audit check, job-completion notification marker) are actually running in the deployed environment — neither has any frontend-visible symptom if the worker process silently isn't executing them (e.g. an old worker process still running pre-Phase-4 code after a partial deploy).
4. Re-verify the trailing-slash behavior from Phase 0 one more time against the actual production URL set, not just the dev check.

---

## 9. Phase 6 — Decommission

Remove Django-specific dependencies, `db.sqlite3`, `manage.py`, the Django migrations directory, once a defined burn-in period passes with no rollback needed — same posture as `webui/` being kept around rather than deleted outright after its own deprecation.

---

## 10. Cross-cutting notes

- **`docker-compose.yml`** will need a new service for `frontend_fastapi/` once Phase 3a is ready to run standalone (mirroring how the worker-queue work added a `worker` service to the same file) — not urgent before then, but don't let it be a Phase-5 surprise.
- **`CLAUDE.md`** describes the current 3-component architecture (backend/frontend/proxy) and the synchronous SSE model in several places (Architecture diagram, "SSE streaming" pattern description) that are already slightly stale post-worker-queue and will need a real update once this rewrite lands too — flagged as a known follow-up, not part of this plan's own scope (consistent with how the worker-queue work itself flagged the same gap without fixing it).
- **Two open questions carried over from architecture review**, not yet decided, worth resolving before the relevant phase starts rather than blocking this document: (1) the new `total` SSE event's scope — unconditional for every job (recommended) vs. gated behind a "combined job" flag; (2) the notifications dropdown's merge of persisted rows with the live-computed "expiring soon" section vs. keeping the latter as a banner only.

---

## 11. Verification summary (consolidated from each phase)

| Phase | Automated | Manual |
|---|---|---|
| 0 | Session lifetime split, flash-message mutation tracking, CSRF accept/reject, token sign/verify/invalidate-on-password-change | `security-review` skill against this phase specifically; trailing-slash empirical check |
| 1 | Characterization tests first (none exist today), then port; login/invite/activate/create_user; break-glass scripts | — |
| 2 | Characterization tests first (none exist today); **document-access 403/200 matrix** (non-member, member, non-member-staff, unauthenticated) | Data custodian opening a non-member project's documents before a review decision |
| 3a | Port existing 182-line suite | Full SSE pass (start/progress/cancel/done); single-patient rapid-entry flow twice in a row |
| 3b | Chaining ordering (not just end-state), `total` event emission, `/studies` pagination, `pacs_client.py` against a mocked association | Full combined-job run with a mixed found/not-found patient list |
| 4 | Expiry-window edges, job-count-rollup correctness, audit-chain tamper detection + empty-table case, **frontend gate test (403 for non-staff)**, notification race-safety | — |
| 5 | Dry-run user migration diff | Confirm worker hooks are actually running post-deploy; production trailing-slash re-check |
