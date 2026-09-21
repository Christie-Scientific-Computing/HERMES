# Feature F004: Admin nav alert badge

## Purpose

F003 gives the admin page a way to see and clear outstanding error reports, but an admin still only
discovers an urgent one exists by visiting `/admin`. This feature surfaces that specific situation (an
unaddressed *urgent* report) as a badge on the "Admin" nav link itself, so it's visible from anywhere in
the app — this is the "any other situations?" alert this planning round decided on (D002 in `plan.md`),
alongside F002's project-review badge.

## Behaviour

- A red count badge (same visual style as F002's and the existing notification badge) appears next to the
  "Admin" nav link when one or more unaddressed error reports are `urgent=True`.
- The badge shows the count of unaddressed urgent reports specifically — not all unaddressed reports (those
  stay amber-only, visible on the admin page itself per F003; this nav badge is reserved for the
  red/urgent case, matching item 2's own amber-vs-red distinction).
- Computed fresh per request, staff-only, same as F002 — no persisted row, no "mark read"; it reflects
  F003's `resolved_at`/`urgent` state directly and clears itself once every urgent report is addressed.

## Dependencies

- **F003 — Mark error reports as addressed**: this feature's badge count is "unaddressed AND urgent" — it
  needs F003's `resolved_at` column and the corresponding `backend_client`/endpoint filtering to exist
  first. Cannot be meaningfully implemented before F003 lands.

## Relevant code

### Existing

- `frontend_fastapi/templates/base.html` — the "Admin" nav link (staff-only) to add the badge next to; same
  badge markup this plan already reuses for F002.
- `frontend_fastapi/deps.py`'s `get_template_context` — where F002 will have already added
  `nav_review_queue_count` following the same staff-only, try/except-degrades pattern; this feature adds a
  sibling field the same way.
- (After F003 lands) `frontend_fastapi/backend_client.py`'s extended `list_error_reports`/equivalent
  filtered call — reuse the same "unaddressed" query F003's admin page toggle uses, further filtered to
  `urgent=True` (either via a query param if F003's endpoint supports filtering by urgent too, or filtered
  client-side in `deps.py` the same way `admin.py`'s own indicator logic does per F003's implementation
  notes).

### Likely changes

- `frontend_fastapi/deps.py` — add a `nav_urgent_reports_count` (or similar) field, staff-only, computed
  from the same backend call F003 introduced.
- `frontend_fastapi/templates/base.html` — badge markup next to "Admin".

## Acceptance criteria

### Scenario: Urgent unaddressed report shows a red badge on Admin

**Given** one error report is unaddressed and `urgent=True`

**When** a staff user views any page

**Then** the "Admin" nav link shows a red badge with count `1`

### Scenario: Non-urgent unaddressed reports don't trigger this badge

**Given** three unaddressed error reports exist, none `urgent=True`

**When** a staff user views any page

**Then** no badge appears on "Admin" (the amber indicator on the admin page itself, from F003, still
applies — just not this nav badge)

### Scenario: Addressing the last urgent report clears the badge

**Given** the "Admin" badge currently shows `1` for one urgent unaddressed report

**When** a staff user marks that report addressed (via F003's action)

**Then** the badge disappears on the next page render

### Scenario: Non-staff users never see this badge

**Given** a non-staff user is logged in

**When** they view any page

**Then** no "Admin" link or badge is present (unchanged — non-staff never see the Admin link at all)

## Edge cases

- None beyond what F003 and F002 already establish (backend-unreachable degrade-to-absent, staff-only
  scoping) — this feature is a straightforward extension of that same pattern to a new data source.

## Success

An admin can tell, from any page, whether an urgent issue is outstanding, without needing to check the
admin page proactively.

## Failure behaviour

Same as F002: a failed backend call for this count results in the badge being omitted, never a broken page.

## Testing considerations

Extend the same `test_deps.py` case(s) F002 adds, for this second badge field — staff user with a known
mocked unaddressed-urgent count, non-staff user (absent), backend-error case (absent/zero). A page-level
test (any staff-gated page) can assert the badge markup appears when the count is nonzero.

## Implementation notes

- Keep this as a small, separate addition to `get_template_context` and `base.html` rather than merging its
  logic into F002's — they're visually similar (same badge style) but semantically distinct counts on
  different nav links, and F002 has no dependency on F003 while this feature does.

## Out of scope

- Any change to the admin page itself (that's entirely F003 — this feature only adds the nav badge).
