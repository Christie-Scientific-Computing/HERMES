# Feature F001: Human-readable timestamps everywhere

## Purpose

Every timestamp currently rendered in `frontend_fastapi/` shows the raw value `backend_client` hands back
from the backend's JSON — an ISO 8601 string with microseconds and a UTC offset, e.g.
`2026-08-27T15:02:07.785851+00:00`. This is hard to scan at a glance. A filter that reformats this
(`YYYY-MM-DD at HH:MM:SS`) already exists (`frontend_fastapi/templating.py`'s `hermes_timestamp`, registered
as the Jinja filter `hermes_timestamp`) and is already used in exactly one place
(`jobs/patient_detail.html`'s event timeline). This feature applies it everywhere else a real
datetime is displayed.

## Behaviour

Every place in the rendered UI that currently shows a raw ISO timestamp string instead shows it formatted
as `2026-08-27 at 15:02:08` (no microseconds, no UTC offset shown, using the existing `hermes_timestamp`
filter's exact output format — do not introduce a second format).

`expiry_date` fields are the one exception: they are stored as `TIMESTAMP(timezone=True)` but are
semantically a date (set via a date-only `<input type="date">` picker, always midnight) — see D006 in
`plan.md`. These should be displayed as a plain date (`2026-08-27`), not run through `hermes_timestamp`
(which would show a meaningless "at 00:00:00" on every project). Add a small equivalent date-only
filter/helper alongside `hermes_timestamp` for this, rather than reusing `hermes_timestamp` and stripping
the time back off in the template.

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/templating.py` — the `hermes_timestamp` filter to reuse; add a sibling date-only
  filter here for `expiry_date`.
- `frontend_fastapi/templates/jobs/patient_detail.html:117,119` — the one existing correct usage
  (`{{ e.start_ts | hermes_timestamp }}`), the pattern to replicate everywhere below.

### Likely changes

Every raw timestamp render found during planning (implementer should re-grep before finishing, in case
something changed since):

- `frontend_fastapi/templates/base.html:118` — `{{ n.created_at }}` (a notification's timestamp, in the nav
  dropdown).
- `frontend_fastapi/templates/admin/overview.html:32,39` — `{{ audit_chain_check.checked_at }}` (two
  occurrences).
- `frontend_fastapi/templates/admin/overview.html:61` — `{{ project.expiry_date }}` (expiring-soon table) —
  use the new date-only filter, not `hermes_timestamp`.
- `frontend_fastapi/templates/admin/overview.html:88` — `{{ job.created_at }}` (recent jobs table).
- `frontend_fastapi/templates/admin/overview.html:116` — `{{ report.created_at }}` (error reports table) —
  note F003 also touches this section; coordinate if built close together, but this change (the filter) has
  no functional overlap with F003's changes (addressed state, reordering).
- `frontend_fastapi/templates/jobs/results_lookup.html:27` — `{{ job.created_at }}` (also touched
  structurally by F005 — same reasoning: no functional overlap, safe in either order).
- `frontend_fastapi/templates/jobs/dashboard.html:38` — `{{ job.created_at }}`.
- `frontend_fastapi/templates/research_projects/_macros.html:95` — `{{ entry.ts }}` inside the
  `audit_trail` macro.
- `frontend_fastapi/templates/research_projects/detail.html:89-93` — `{{ project.expiry_date }}` — use the
  date-only filter.
- `frontend_fastapi/templates/research_projects/detail.html:178` — `{{ job.created_at }}`.

## Acceptance criteria

### Scenario: A notification's timestamp is readable

**Given** a staff user has an unread notification created at `2026-08-27T15:02:07.785851+00:00`

**When** they open the notification dropdown in the nav

**Then** the timestamp shown reads `2026-08-27 at 15:02:07`, not the raw ISO string

### Scenario: Every remaining raw-timestamp site is covered

**Given** the list of files under "Likely changes" above

**When** each page is rendered with data containing a real (non-null) timestamp in the relevant field

**Then** none of them show a raw ISO 8601 string (`T` separator, microseconds, or a `+00:00`/`Z` offset)
anywhere in the page

### Scenario: A project's expiry date reads as a date, not a datetime

**Given** an approved project with `expiry_date = 2026-08-27T00:00:00+00:00`

**When** its expiry date is shown (on the admin "expiring soon" table, or the project detail page)

**Then** it reads `2026-08-27`, not `2026-08-27 at 00:00:00` and not the raw ISO string

### Scenario: A null/missing timestamp doesn't break the page

**Given** a field that can legitimately be empty (e.g. an in-progress event's `end_ts`, per
`hermes_timestamp`'s existing docstring)

**When** the template renders it through the filter

**Then** it renders as an empty string, matching `hermes_timestamp`'s existing behaviour — this is already
handled for fields that already use the filter; make sure any newly-filtered field that can be null/empty
gets the same treatment (it already will, by using the same filter, but verify for the new date-only filter
too).

## Edge cases

- A value that isn't a parseable ISO string at all (already handled by `hermes_timestamp`'s existing
  fallback: falls back to `str(value)` rather than raising) — the new date-only filter should have the same
  fallback behaviour, for the same reason (one malformed value must not 500 a whole page).
- `expiry_date` is `None` for a draft/submitted/rejected/revoked project — already guarded by the existing
  `{% if project.expiry_date %}` checks at the two `detail.html` call sites and the `expiring_projects` loop
  (only approved projects appear there); the date-only filter itself should still handle `None` gracefully
  for defence in depth, matching `hermes_timestamp`'s own `if not value: return ""`.

## Success

Every file listed under "Likely changes" renders formatted timestamps; no template in `frontend_fastapi/`
outputs a raw ISO 8601 datetime string to the browser.

## Failure behaviour

N/A — this is a display-only formatting change with no new failure modes; the filter's existing
fail-soft-to-raw-string behaviour is preserved (and replicated in the new date-only filter).

## Testing considerations

`frontend_fastapi/tests/test_templating.py` already tests `format_timestamp`/`hermes_timestamp` directly —
add equivalent unit tests there for the new date-only filter (valid ISO datetime → date string, empty/None
→ `""`, unparseable → raw value passthrough), following the same test shape. Router/page tests
(`test_admin.py`, `test_jobs.py`, `test_research_projects.py`) generally assert on response status and
specific substrings rather than full-page snapshots — a light grep-style assertion (formatted string appears
in the response body, e.g. `"2026-08-27 at"` for a known fixture timestamp) is enough per touched page if
not already covered; this is a low-risk mechanical change so exhaustive new page-level tests aren't needed
beyond the filter's own unit tests.

## Implementation notes

- Register the new date-only filter the same way `hermes_timestamp` is registered
  (`templates.env.filters["..."] = ...`) in `templating.py`. Suggested name: `hermes_date`.
- Do not touch `review_form.expiry_date()` (the WTForms date-input widget on `research_projects/detail.html`)
  — that renders an HTML form control, not a display value; out of scope.
- This is a broad but shallow change (many files, one-line edits) — safe to build early since it touches
  files F003, F005, and F007 also touch, but has no functional overlap with any of them (pure
  presentation), so build order relative to those doesn't matter.

## Out of scope

- Localizing the format to the viewer's timezone or locale (stays UTC, same as today — this feature is
  about readability of the existing UTC value, not timezone conversion).
- Relative/humanized timestamps ("2 hours ago") — not requested; the fixed format above is what was asked
  for.
