# Feature F006: Rename Dashboard to Home

## Purpose

"Dashboard" is the current label for the app's landing page (recent jobs across your projects + submit/new
project shortcuts). "Home" is a clearer, more conventional label for a page that's really just the app's
front door (and already sits behind the `/` root link) — see D005 in `plan.md`.

## Behaviour

Every user-visible occurrence of "Dashboard" for this page becomes "Home": the nav link text, the browser
tab `<title>`, and the page's `<h1>`.

Nothing else changes — not the URL (`/`), not the FastAPI route name (`dashboard`), not the template file
path (`jobs/dashboard.html`). These are internal implementation details with no user-facing consequence, and
renaming them would only add risk (broken `url_for("dashboard")` references) for zero user-facing benefit.

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/templates/base.html` — the nav link currently reading "Dashboard", linking to `/`.
- `frontend_fastapi/templates/jobs/dashboard.html` — `{% block title %}Dashboard — HERMES{% endblock %}` and
  `<h1 class="text-2xl font-semibold text-gray-900 mb-1">Dashboard</h1>`.

### Likely changes

- `frontend_fastapi/templates/base.html` — nav link text.
- `frontend_fastapi/templates/jobs/dashboard.html` — title block and `<h1>` text.

## Acceptance criteria

### Scenario: Nav link reads "Home"

**Given** any logged-in user

**When** they view the nav bar

**Then** the link to `/` reads "Home", not "Dashboard"

### Scenario: Page title and heading read "Home"

**Given** a user visits `/`

**When** the page renders

**Then** the browser tab title is "Home — HERMES" and the page's `<h1>` reads "Home"

### Scenario: Route and URL are unaffected

**Given** any existing link or redirect that points at the `dashboard` route (e.g. via `url_for("dashboard")`
or a hardcoded `/`)

**When** the app runs

**Then** it continues to resolve correctly — no route/URL renamed

## Edge cases

None — this is a pure text-label change with no behavioural surface.

## Success

The app's landing page is labelled "Home" everywhere a user sees it, with no functional change.

## Failure behaviour

N/A.

## Testing considerations

If any existing test in `frontend_fastapi/tests/test_jobs.py` (or elsewhere) asserts the literal string
"Dashboard" in a response body, update it to "Home" — grep for it before finishing (`grep -rn "Dashboard"
frontend_fastapi/tests/`, none were found during planning, but re-check at implementation time since other
features in this round land around the same time).

## Implementation notes

None beyond the two-file text change above.

## Out of scope

- Renaming `jobs/dashboard.html`, the `dashboard` route name, or any internal identifier.
