# Feature F007: Projects table view

## Purpose

The projects list (`/projects`) and review queue (`/projects/review`) currently show projects as a grid of
cards (`project_card` macro) — title, ethics reference, status badge, a truncated description, and creator.
For an admin looking at dozens of projects, a card grid is harder to scan than a table, and cards don't show
`created_at` or `expiry_date` at all today. This feature replaces the card grid with a table on both pages,
using colour (via the existing status badge) to make status legible at a glance across many rows.

## Behaviour

- `/projects` (`research_projects/list.html`) and `/projects/review` (`research_projects/review_queue.html`,
  both its "awaiting review" and "pending amendment" sections) show projects as a table instead of a card
  grid.
- Table columns: **Name** (linking to the project detail page, as the card did), **Description**,
  **Created by** (username), **Created at** (formatted per F001), **Status** (using the existing
  `status_badge` macro — the full draft / pending review / approved / rejected / revoked / expired set, per
  D004 in `plan.md`), **Expiry date** (formatted per F001's date-only filter; blank/dash for a project with
  no `expiry_date`, i.e. anything not approved).
- The existing `ethics_reference` field, shown on the card today, is dropped from the table's visible
  columns to keep it scannable (it remains visible on the project detail page, unaffected) — implementer's
  call if there's room to keep it as a subtitle under Name instead, but a dedicated column isn't required.
- Status colouring is the only colour cue required — no new colour scheme beyond what `status_badge` already
  provides; this satisfies "make sure to use colours to make this easier" without inventing a second
  colour-coding system that could conflict with it.
- The existing status filter dropdown on `/projects` (staff-only, `<select name="status">`) is unaffected —
  it still filters which rows the table shows, the same way it filtered which cards showed today.
- The "New project" button, the expiring-soon banner, and the staff-vs-non-staff visibility rules (staff see
  every project, everyone else sees only their own) are all unaffected — this feature only changes how the
  already-fetched project list is *rendered*.

## Dependencies

None. (Reuses `hermes_timestamp`/the new date-only filter from F001 wherever available; if built before
F001 lands, use the raw `created_at`/`expiry_date` values as a placeholder and note the TODO — see
Implementation notes.)

## Relevant code

### Existing

- `frontend_fastapi/templates/research_projects/_macros.html` — `status_badge` (reuse as-is) and
  `project_card` (the macro this feature replaces the *usage* of — the macro itself can stay if anything
  else still references it, but neither `list.html` nor `review_queue.html` should call it after this
  feature).
- `frontend_fastapi/templates/research_projects/list.html` — the card grid to replace; the status filter
  dropdown and "New project" button to keep as-is.
- `frontend_fastapi/templates/research_projects/review_queue.html` — the two card grids (pending review,
  pending amendments) to replace with tables.
- `frontend_fastapi/templates/admin/overview.html`'s existing tables (e.g. the "Expiring soon" table) —
  the table styling convention (`min-w-full text-sm divide-y divide-gray-100`, `<thead>`/`<tbody>` shape)
  to follow for visual consistency with the rest of the app.
- `frontend_fastapi/routers/research_projects.py:104-121` (`project_list`) and `:213-224` (`review_queue`) —
  already fetch and pass through everything this table needs (`title`, `description`, `created_by`,
  `created_at`, `status`, `expiry_date` are all existing fields on the project dicts these already return,
  per the `research_projects` HermesDB schema in `CLAUDE.md`) — no backend/route changes expected, this is
  template-only.

### Likely changes

- `frontend_fastapi/templates/research_projects/list.html` — replace the `<div class="grid ...">` +
  `project_card` loop with a table.
- `frontend_fastapi/templates/research_projects/review_queue.html` — same, for both sections.
- `frontend_fastapi/templates/research_projects/_macros.html` — optionally add a small `project_row` macro
  (mirroring `project_card`'s existing shape) if `list.html` and `review_queue.html` would otherwise
  duplicate the same `<tr>` markup three times (list, review-pending, review-amendments) — implementer's
  call, but avoiding triplication here fits this codebase's existing macro-reuse convention.

## Acceptance criteria

### Scenario: Projects list shows a table with the required columns

**Given** a staff user views `/projects` with several projects in different statuses

**When** the page renders

**Then** a table is shown (not a card grid) with columns for Name, Description, Created by, Created at,
Status, and Expiry date

### Scenario: Status colouring is preserved

**Given** projects in `draft`, `submitted`, `approved`, `rejected`, `revoked`, and expired-approved states

**When** the table renders

**Then** each row's Status column shows the same colour-coded badge `status_badge` already produces for
that state today (e.g. amber "Pending review" for `submitted`, green "Approved", red "Rejected"/"Revoked",
grey "Expired")

### Scenario: Expiry date only shows for projects that have one

**Given** a draft project (no `expiry_date`) and an approved project (`expiry_date` set)

**When** both appear in the table

**Then** the draft project's Expiry date cell is blank/dash, and the approved project's shows its formatted
date

### Scenario: Name still links to the project

**Given** any project row

**When** a user clicks its Name cell

**Then** they're taken to `/projects/{project_id}`, same as clicking a card was today

### Scenario: Review queue shows both sections as tables

**Given** one project is awaiting review and one approved project has a pending amendment

**When** a staff user visits `/projects/review`

**Then** both "Projects awaiting review" and "Approved projects with a pending amendment" render as separate
tables (not card grids), each with the same column set

### Scenario: Status filter still works

**Given** a staff user selects "Approved" from the existing status filter dropdown on `/projects`

**When** the page reloads

**Then** the table shows only approved projects, same filtering behaviour as before this feature

### Scenario: Empty state is preserved

**Given** no projects match the current view/filter

**When** the page renders

**Then** the existing empty-state message shows (e.g. "No projects match this filter." /
"Nothing waiting for review.") — just no longer wrapped in card-grid markup

## Edge cases

- A project with a very long description: today's card used `line-clamp-2`; the table should similarly
  truncate (e.g. `truncate`/`line-clamp` on the Description cell) rather than blowing out row height — this
  codebase already uses `line-clamp-2` and `break-words max-w-md` elsewhere (`admin/overview.html`'s error
  reports message column) as precedent for either approach.
- Small screens: existing tables elsewhere in this app (e.g. `admin/overview.html`'s "Recent jobs") wrap
  table content in `<div class="overflow-x-auto">` rather than trying to force it to reflow — follow that
  same convention here instead of inventing responsive-card-to-table logic.

## Success

Both `/projects` and `/projects/review` show projects as a table with the six specified columns, coloured
by status via the existing `status_badge` macro, with existing filtering/empty-state/link behaviour intact.

## Failure behaviour

Unaffected — this is a rendering-only change; existing `backend_error` handling on both routes is untouched.

## Testing considerations

`frontend_fastapi/tests/test_research_projects.py` already has coverage for `project_list`/`review_queue`
(status filtering, staff-vs-non-staff visibility, pending-amendments section — see the existing
`test_review_queue_*` tests found during planning). These likely assert on response content rather than
exact markup shape; verify they don't assert card-specific markup that would need updating, and add a case
asserting a known project's `created_at`/`expiry_date`/`description` appear in the rendered table (they
weren't shown on cards before, so this is new surface to cover).

## Implementation notes

- This feature and F002 both touch `review_queue.html`, but in different regions (F002: `base.html`'s nav
  only; this feature: the page body) — no actual file conflict, just worth sequencing back-to-back if
  convenient (per `plan.md`).
- Prefer introducing one shared row-rendering macro over duplicating table-row markup three times (list
  page, review-pending section, review-amendments section) — consistent with this codebase's existing
  macro-based reuse (`_macros.html` already centralises `status_badge`/`project_card`/`timeline_stepper`/
  `audit_trail` for exactly this reason).

## Out of scope

- Sorting/pagination controls on the table beyond what already exists (the status filter dropdown).
- Changing what data staff vs non-staff users can see (unchanged access rules, per `project_list`'s existing
  docstring).
- The project detail page (`detail.html`) — unaffected, still its own layout.
