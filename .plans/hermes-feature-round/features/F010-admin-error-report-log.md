# Feature F010: Admin error-report log

## Purpose

Reports submitted via F009 need somewhere staff can actually see them.
Without a log view, submissions would accumulate in the database with no
way to act on them.

## Behaviour

A staff-only page lists submitted error reports — username, category,
urgent flag, message, related job ID (if any), and submission time — most
recent first. Access is restricted the same way every other admin-only view
in this app already is.

## Dependencies

- **F009 — Submit an error report**: this feature has nothing to display
  until reports exist. Build F009 first (or at least its schema).

## Relevant code

### Existing

- `frontend_fastapi/deps.py` (`require_data_custodian`, ~lines 113-116) —
  raises `Forbidden()` unless `user.is_staff`; the exact gate to reuse,
  matching the grilling session's explicit decision to reuse this rather
  than invent a new privilege tier.
- `frontend_fastapi/routers/admin.py` (`admin_overview`, ~lines 28-47) — the
  whole pattern to follow: staff-gated route → `backend_client` call →
  render template.
- `frontend_fastapi/templates/admin/overview.html` — reusable "table card"
  idiom (e.g. Recent jobs table ~line 70+, Expiring-projects table ~lines
  47-68): a `div.bg-white border rounded-lg p-6` wrapping a `<table>` with
  an `{% if %}...{% else %}` empty state. Copy this block directly for the
  new error-reports table.
- `backend/src/error_reports/db_client.py` / `endpoints.py` (from F009) —
  the `list()` method/endpoint this view calls.

### Likely changes

- New route, either a standalone `GET /admin/error-reports` in
  `frontend_fastapi/routers/error_reports.py` or a new section folded into
  the existing `/admin` overview page — either is acceptable; a standalone
  page is simpler if the list is expected to grow long enough to want its
  own pagination later.
- New template (or template section) rendering the reports table using the
  existing table-card idiom.
- `frontend_fastapi/backend_client.py` — new `list_error_reports()` call
  alongside the existing `list_notifications`/`mark_notification_read`
  functions.

## Acceptance criteria

### Scenario: Staff can view the log

**Given** a user with `is_staff=True`

**When** they visit the error-report log page

**Then** they see every submitted report, most recent first, with
username/category/urgent/message/job_id/created_at visible

### Scenario: Non-staff users are blocked

**Given** a logged-in user with `is_staff=False`

**When** they attempt to visit the error-report log page

**Then** they receive the same `Forbidden` response every other
`require_data_custodian`-gated page returns

### Scenario: Empty state

**Given** no error reports have been submitted yet

**When** a staff user visits the log page

**Then** they see the existing empty-state treatment (matching the "no
expiring projects"/"no recent jobs" pattern already used on the admin
overview page), not a blank or broken table

## Edge cases

- A very long list of reports — no pagination is specified as a requirement;
  a simple `LIMIT`-based recent-N list (matching `list_recent_jobs_with_counts`'s
  `limit` parameter pattern) is sufficient for this round.
- An urgent report should be visually distinguishable at a glance (e.g. a
  badge/highlight), since surfacing urgency is the entire point of the flag
  given email is deferred — this is the only way an urgent report gets
  noticed this round.

## Success

A staff user can load the log page and see every report F009 has recorded,
with urgent reports visually distinct from non-urgent ones; a non-staff user
is blocked from the same URL.

## Failure behaviour

N/A beyond the standard `Forbidden` access-control failure already covered
above.

## Testing considerations

- `frontend_fastapi/tests/test_admin.py` (or a new
  `test_error_reports.py`) — assert staff access succeeds and renders
  seeded reports, and non-staff access is forbidden, mirroring
  `test_admin.py`'s existing structure for the overview page.

## Implementation notes

This is a thin read-only view — no new access-control concept, no new UI
pattern beyond copying the existing table-card idiom. Keep it that way;
resist adding filtering/search/status-tracking (e.g. "resolved" flags) since
none of that was requested this round.

## Out of scope

- Marking a report as resolved/actioned, or any workflow state beyond "it
  exists in the log."
- Filtering/searching the report list.
- Any notification when a new report is submitted (deferred, see F009).
