# Implementation Plan — UI polish round

## Goal

A batch of usability tweaks to `frontend_fastapi/`, the production frontend: surface admin-actionable
items (projects awaiting review, unaddressed error reports) in the nav instead of requiring a page visit
to notice them; tidy up three pages (Results, Projects list/review queue, the admin overview's report
ordering); rename the homepage; and make every timestamp in the UI human-readable instead of raw ISO 8601.

## What we discussed

- The app already has a live, non-persisted "badge" pattern (`nav_expiring_soon` in
  [`frontend_fastapi/deps.py`](../../frontend_fastapi/deps.py)) alongside a persisted one (`nav_notifications`,
  backed by `backend/src/notifications/`). For "a project needs reviewing," the live pattern is the right
  fit: `review_queue`'s own route already fetches exactly this data (submitted projects + pending
  amendments) fresh on every request, and a persisted-notification alternative would need `backend/` to know
  the staff username list, which it structurally doesn't (no user table in HermesDB).
- `backend/src/error_reports/` (added recently, per `docs/plans/feature-round-implementation-plan.md` item
  06) has no "addressed/resolved" concept yet — just `category`/`urgent`/`message`. Item 2 needs a schema
  change.
- **`backend/alembic/versions/` currently has two migration heads** (`ddccca9b1fca` and `a7c9e2f4b1d3`,
  both descending from `3cea980979d2` — see `git log`/the versions directory). This predates this plan and
  isn't something any of these features caused, but the first migration added here must merge them first,
  or `alembic upgrade head` will fail. Flagged in F003's implementation notes.
- `templating.py`'s `hermes_timestamp` Jinja filter (`YYYY-MM-DD at HH:MM:SS`) already exists and is already
  wired up — but only one template (`jobs/patient_detail.html`) actually uses it. Every other raw
  `created_at`/`ts`/`checked_at` render in the UI still shows the raw ISO string. `expiry_date` is the one
  exception: despite the column name, it's stored as `TIMESTAMP(timezone=True)` and set via a date-only
  picker, so it's always midnight — formatting it through `hermes_timestamp` would show a meaningless
  "at 00:00:00" on every project. See F001.
- The existing `status_badge` Jinja macro (`research_projects/_macros.html`) already colour-codes the full
  project lifecycle (draft / pending review / approved / rejected / revoked / expired) — reused as-is for
  the new table view rather than inventing a collapsed draft/active/expired scheme, so no status information
  is lost (in particular, the review queue itself needs to distinguish "submitted" specifically).
- "Admin", "staff", and "data custodian" are the same role in this codebase (`User.is_staff`,
  `require_data_custodian`) — no separate role model to account for.

## Decisions

- **D001 — Review-needed alert is a live count, not a persisted notification:** badge on the renamed
  "Review Projects" nav link, computed fresh each render from `list_projects(status="submitted")` +
  `list_pending_amendments()`, staff-only. No new backend endpoint, no "mark read" — it clears itself.
- **D002 — A second, independent alert source: unaddressed urgent error reports**, shown as a badge on the
  "Admin" nav link (separate badge from D001's, on a different link). This is the "any other situations?"
  answer — no other trigger is in scope for this round.
- **D003 — Error reports gain a persisted "addressed" state** (`resolved_at`/`resolved_by`, mirroring
  `notifications.read_at`'s shape). Addressed reports drop out of the admin page's default list behind a
  toggle (reusing the existing filter-pill convention from `jobs/_macros.html`'s `patient_table`), rather
  than staying visible-but-greyed.
- **D004 — The projects table (list + review queue) reuses the full existing status set** via `status_badge`,
  not a collapsed draft/active/expired scheme (see "What we discussed" above).
- **D005 — "Dashboard" is renamed to "Home"** everywhere user-facing (nav link, `<title>`, `<h1>`). Internal
  names (route name `dashboard`, template file `jobs/dashboard.html`) are left as-is — purely an
  implementation detail with no user-facing consequence.
- **D006 — `expiry_date` is excluded from F001's blanket "add `hermes_timestamp`" treatment** and instead
  gets a date-only rendering (see "What we discussed" above) — an assumption, not a hard requirement; easy
  to revisit if a reviewer wants the time shown too.

## Implementation overview

1. **F001 — Human-readable timestamps everywhere**
   ([`features/F001-uniform-timestamp-formatting.md`](features/F001-uniform-timestamp-formatting.md)):
   apply the existing `hermes_timestamp` filter to every remaining raw timestamp render in the UI, and give
   `expiry_date` its own date-only rendering.
2. **F002 — "Review Projects" nav badge**
   ([`features/F002-review-projects-nav-badge.md`](features/F002-review-projects-nav-badge.md)): rename the
   "Review Queue" nav link to "Review Projects" and add a live red count badge for pending review items.
3. **F003 — Mark error reports as addressed**
   ([`features/F003-mark-error-reports-addressed.md`](features/F003-mark-error-reports-addressed.md)):
   persisted addressed/resolved state, an amber/red indicator and mark-addressed action on the admin page,
   a show/hide-addressed toggle, and moving the error-reports section above "Recent jobs" (below "Expiring
   soon").
4. **F004 — Admin nav alert badge**
   ([`features/F004-admin-nav-alert-badge.md`](features/F004-admin-nav-alert-badge.md)): a red badge on the
   "Admin" nav link when an unaddressed urgent error report exists.
5. **F005 — Results page reorganisation**
   ([`features/F005-results-page-reorganisation.md`](features/F005-results-page-reorganisation.md)): move
   job/patient search to the top of the page, and cap + reformat the "Your jobs" table to match the Home
   page's table (max 25 rows, with counts columns).
6. **F006 — Rename Dashboard to Home**
   ([`features/F006-rename-dashboard-to-home.md`](features/F006-rename-dashboard-to-home.md)): user-facing
   rename only.
7. **F007 — Projects table view**
   ([`features/F007-projects-table-view.md`](features/F007-projects-table-view.md)): replace the project
   card grid with a table (name, description, created by, created at, status, expiry date) on both the
   projects list and the review queue.

## Dependencies

```text
F001 (independent)
F002 (independent)
F003 (independent) ──> F004
F005 (independent)
F006 (independent)
F007 (independent)
```

No feature blocks another except F004, which needs F003's addressed/resolved concept to define "unaddressed."
Everything else can be built and merged in any order. F002 and F007 both touch
`research_projects/review_queue.html`; building them back-to-back avoids rebase churn but neither requires
the other to land first.

## Open issues

- None outstanding — the consequential product decisions (alert mechanism, other trigger situations,
  addressed-report visibility, status granularity, Home page naming) were resolved during planning; see
  Decisions above.

## Assumptions

- `expiry_date`'s date-only display (D006) is a reasonable reading of "datetime items" even though the
  column is technically a timestamp; revisit if reviewers want the time shown.
- The results page's "Your jobs" table keeps its existing "Project" column even though the Home page's
  table doesn't have one (Results spans multiple projects in a way Home's per-user-active-projects table
  also technically does, but Home never shows it) — dropping it wasn't asked for, so F005 adds the counts
  columns to match Home's format without removing information Results already shows.
- Nav badge counts are computed via the same list endpoints the relevant pages already call (no new
  lightweight "counts-only" backend endpoint), consistent with how `nav_notifications`/`nav_expiring_soon`
  already do a small extra backend round-trip per request for logged-in users. Error-report volume is
  expected to be low (a user-feedback form), so this stays cheap.

## Limitations

- None beyond what's noted per-feature.

## Deferred decisions

- None.

## Out of scope

- Any new *kind* of admin alert beyond project-review-needed and urgent-error-reports (D002).
- Retrying/expanding notification delivery (email, etc.) for urgent reports — still just recorded, per
  `backend/src/error_reports/endpoints.py`'s existing module docstring.
- Renaming the `dashboard` route name or `jobs/dashboard.html` file (D005 — display text only).
- Pagination on the Results page's jobs table beyond the 25-row cap (F005) — matches the Home page's own
  silent-truncation convention.
