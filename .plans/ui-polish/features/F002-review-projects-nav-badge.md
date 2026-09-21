# Feature F002: "Review Projects" nav badge

## Purpose

Today, an admin only discovers a project is awaiting review (or an approved project has a pending
amendment) by clicking into "Review Queue". This feature surfaces that count directly in the nav, the same
way the existing bell icon already surfaces unread notifications and expiring-soon projects — so an admin
can tell at a glance, from any page, that something needs their attention.

## Behaviour

- The nav link currently labelled "Review Queue" (`frontend_fastapi/templates/base.html`, staff-only,
  linking to `/projects/review`) is relabelled "Review Projects". The URL and route are unchanged.
- Next to that link, a small red count badge (visually reusing the existing badge style used for the
  notification bell's count in `base.html`) shows the number of projects currently awaiting review — the
  same total the review queue page itself computes: submitted projects (`status="submitted"`) plus approved
  projects with a pending amendment.
- The badge is computed fresh on every page render (no persisted row, no "mark read" affordance) — it
  simply reflects current state, the same way `nav_expiring_soon` already does.
- The badge is only ever shown to staff users (non-staff never see the "Review Projects" link at all,
  unchanged).
- When the count is zero, no badge is shown (matching the existing notification badge's
  `{% if badge_count %}` pattern).

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/templates/base.html:60` — the "Review Queue" nav link to relabel and add the badge
  next to.
- `frontend_fastapi/templates/base.html:88-90` — the existing badge markup/style
  (`rounded-full bg-red-600 text-white text-xs px-1.5 py-0.5`) to reuse for visual consistency.
- `frontend_fastapi/deps.py:149-199` (`get_template_context`) — where `nav_notifications`/
  `nav_active_projects`/`nav_expiring_soon` are already assembled per-request; the new count belongs here,
  following the same try/except-degrades-to-empty pattern already used for the other two backend calls in
  this function.
- `frontend_fastapi/routers/research_projects.py:213-224` (`review_queue`) — the existing route already
  computes exactly this pair of calls (`backend_client.list_projects(status="submitted")` +
  `backend_client.list_pending_amendments()`); the nav badge counts the same two lists.

### Likely changes

- `frontend_fastapi/deps.py` — add a `nav_review_queue_count` (or similar) key to `get_template_context`'s
  returned dict, computed only when `user is not None and user.is_staff`, via the two backend calls above,
  wrapped the same way `nav_notifications`/`nav_active_projects` already are (backend down → count is 0 /
  omitted, never crashes the page).
- `frontend_fastapi/templates/base.html` — relabel the link, add the badge span.

## Acceptance criteria

### Scenario: Admin sees the badge when something needs review

**Given** a staff user is logged in, and one project has `status="submitted"` while none have a pending
amendment

**When** they view any page in the app

**Then** the "Review Projects" nav link shows a red badge with the count `1`

### Scenario: Badge counts both submissions and amendments

**Given** two projects are `status="submitted"` and one approved project has a pending amendment

**When** a staff user views the nav

**Then** the "Review Projects" badge shows `3`

### Scenario: No badge when nothing is pending

**Given** no project is submitted and no approved project has a pending amendment

**When** a staff user views the nav

**Then** no badge appears next to "Review Projects" (same as the existing notification bell's zero-state)

### Scenario: Non-staff users never see this link or badge

**Given** a non-staff user is logged in

**When** they view any page

**Then** neither the "Review Projects" link nor its badge is present (unchanged existing behaviour for the
link itself — just confirming the badge doesn't leak into a code path that shouldn't render it)

### Scenario: Backend unreachable degrades gracefully

**Given** the backend is unreachable

**When** a staff user views any page

**Then** the page still renders (no 500), and the "Review Projects" badge is simply absent — matching how
`nav_active_projects`/`nav_notifications` already degrade in `get_template_context`

## Edge cases

- A project that is both approved-with-pending-amendment doesn't also independently appear in the
  `status="submitted"` list — these are disjoint sets already (amendments only apply to approved projects,
  submissions are pre-approval), so no double-counting risk; verify against `list_pending_amendments`'s
  actual backend query if in doubt.

## Success

A staff user can tell, from any page's nav bar, how many items are waiting in the review queue, without
navigating there.

## Failure behaviour

Same as every other nav-context backend call today: a `BackendError`/`httpx.HTTPError` while fetching either
list results in the badge being omitted (treated as zero), not a broken page.

## Testing considerations

`frontend_fastapi/tests/test_deps.py` covers `get_template_context`'s existing nav fields (per the
`ARCHITECTURE.md`/`CLAUDE.md`-referenced test conventions) — add a case asserting the new count field for a
staff user with known mocked `list_projects`/`list_pending_amendments` results, and a case for a non-staff
user (field absent or zero, whichever the implementation naturally produces — pick one and assert it
consistently). A `test_research_projects.py`-style page test (or a `test_jobs.py`/any staff-gated page test)
can assert the badge markup appears in a rendered page's HTML when the count is nonzero.

## Implementation notes

- Reuse the exact badge visual style already in `base.html` for consistency — this feature is explicitly
  about reusing that existing component, not designing a new one.
- Do not rename the `/projects/review` route or its `name="review_queue"` FastAPI route name — only the
  user-visible link text changes 	(the `<a>` tag's text content), consistent with how F006 treats the
  `dashboard` route name for the Home rename.
- F007 (projects table view) also touches `review_queue.html`'s body content, but not the nav link in
  `base.html` — no functional overlap, safe to build in either order.

## Out of scope

- Any change to the review queue page's own content/layout (that's F007).
- A persisted per-admin notification when a project is submitted (see D001 in `plan.md` — a live badge was
  chosen instead).
