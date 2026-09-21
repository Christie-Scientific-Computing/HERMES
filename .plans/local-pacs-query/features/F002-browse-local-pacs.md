# Feature F002: Search and browse the local PACS

## Purpose

Lets any logged-in user check, ahead of running an import/restore job, whether a patient's data already exists on the trust's local PACS (Conquest) — the primary motivating use case for this whole plan. Replaces the existing non-functional "Coming soon: Browse local PACS" homepage tile with a real page.

## Behaviour

- A new standalone page, reachable from the homepage dashboard tile, presents a search form with these fields: anonymised patient ID, study date range, study description, and modalities-in-study. All fields are optional except that at least one must be provided (an unconstrained "browse everything" query is not the goal here and may not even be practical against a real PACS).
- Submitting the form issues a C-FIND (via F001) against Conquest and displays matching results grouped by patient, each with its studies listed underneath (study date, description, modalities, accession number if available).
- Selecting a study expands it to show its series (a second, series-level C-FIND via F001), each with its own modality, series description, and instance count if Conquest's response includes one.
- No results is a clear, explicit "nothing found" state, distinguished from a query error.
- The page requires the user to be logged in (`require_login`), but no project membership or other gate — matching the existing ungated `/studies` Orthanc-browsing precedent (CLAUDE.md's Architecture section, `backend/src/studies/endpoints.py`).
- Users only ever search/see anonymised IDs here — Conquest stores no real MRNs, so there is nothing to translate or redact on this page (see plan.md D004). This page must not attempt to resolve or display a real MRN.
- If Conquest is unreachable or misconfigured (F001's connectivity check/error), the page shows a clear error state rather than an empty or misleadingly-successful "no results" state.

## Dependencies

- **F001** — provides the actual C-FIND primitives this page calls. Cannot be built independently of F001's connection module (though the two can be developed in parallel against a shared interface, then integrated).

## Relevant code

### Existing

- `frontend_fastapi/templates/jobs/dashboard.html:21-27` — the existing non-clickable placeholder tile ("Browse local PACS" / "Search what's already on the local PACS", "Coming soon" badge) this feature replaces with a real link.
- `frontend_fastapi/routers/jobs.py:76-85` — the dashboard route this tile lives on, for the pattern of how a new top-level page route/router is registered and gets access to `get_template_context`.
- `backend/src/studies/endpoints.py` — the closest existing analogue for "read-only DICOM browse, ungated." Different backend (Orthanc, not Conquest) and different transport (Orthanc REST via `pyorthanc`, not raw `pynetdicom`), but the same shape of feature: patient → study → series drill-down over a DICOM store. Worth reviewing for UX/field conventions even though the underlying calls differ.
- `frontend_fastapi/deps.py:107-110` — `require_login`, the gate this page uses.
- `frontend_fastapi/routers/error_reports.py` and `frontend_fastapi/forms/error_reports.py` — a recent, small, single-purpose router+form+template addition to this app; a reasonable structural template for adding this new page's router/form module.

### Likely changes

- `frontend_fastapi/templates/jobs/dashboard.html` — the placeholder tile (lines ~21-27) becomes a real `<a href="/local_pacs">` link, matching the styling of the other two tiles.
- New: `frontend_fastapi/routers/local_pacs.py` — the search/browse routes.
- New: `frontend_fastapi/forms/local_pacs.py` — the search form (WTForms, matching this app's existing form conventions in `forms/jobs.py`/`forms/research_projects.py`).
- New: `frontend_fastapi/templates/local_pacs/browse.html` (and a results partial/fragment if the drill-down is implemented as an htmx-style partial reload, matching this app's stated "django-cotton + htmx" frontend approach — see project memory on frontend framework decision).
- `frontend_fastapi/main.py` — register the new router.

## Acceptance criteria

### Scenario: Searching by anonymised patient ID finds existing data

**Given** a patient's data already exists on Conquest under a given anonymised ID

**When** a logged-in user searches for that ID on the local PACS browse page

**Then** the patient's study/studies are listed, letting the user confirm the data is already there before starting an import

### Scenario: Searching for a patient not on Conquest

**Given** no data exists on Conquest for a given anonymised ID

**When** a logged-in user searches for that ID

**Then** the page shows a clear "no results" state, not an error

### Scenario: Drilling into a study shows its series

**Given** search results including a study

**When** the user expands that study

**Then** its series are listed with modality, description, and instance count where available

### Scenario: Searching by date range, study description, or modality

**Given** the search form's other fields (date range, study description, modalities-in-study)

**When** a user searches using any combination of them (with or without a patient ID)

**Then** matching studies across patients are returned, following standard DICOM C-FIND semantics for those keys

### Scenario: Conquest is unreachable

**Given** Conquest is not reachable (network issue, Conquest down, misconfiguration)

**When** a user submits a search

**Then** the page shows a distinct "local PACS unavailable" error state, not a misleading empty-results state

### Scenario: Anonymous/unauthenticated access is blocked

**Given** a visitor who is not logged in

**When** they attempt to reach the local PACS browse page

**Then** they are redirected to login, matching every other `require_login`-gated page in this app

## Edge cases

- A search with no fields filled in at all should be rejected client- and/or server-side with a clear message, not silently sent as an unbounded query against Conquest.
- Date range validation (start after end, or a clearly invalid date) should be caught before issuing the C-FIND.
- Conquest returning a very large result set (e.g. an overly broad study-description search) — the implementer should decide a sane result cap/pagination approach; not fully specified here since Conquest's actual query behaviour under load can't be verified from this repository (see plan.md's Open issues).

## Success

A logged-in user can reach this page from the homepage tile, run a search by anonymised ID (alone or combined with the other fields), see matching patients/studies, and drill into a study's series — all without the page ever displaying or requiring a real MRN.

## Failure behaviour

Every Conquest-side failure mode F001 can report (unreachable, misconfigured, association rejected, timeout) should map to a specific, user-legible message on this page rather than a generic 500.

## Testing considerations

Follow `frontend_fastapi/tests/`'s pytest conventions. Mock/stub F001's connection module for route-level tests (form validation, gating, template rendering, error-state rendering) rather than requiring a live DICOM SCP for every test — reserve an actual C-FIND round trip against a test SCP for F001's own test suite. Check `frontend_fastapi/tests/test_jobs.py` for the existing pattern of testing a gated, form-driven page in this app.

## Implementation notes

- This app's stated frontend approach is django-cotton-style components + htmx (project memory: "Django frontend rewrite uses django-cotton + htmx, not Vue/JS") — the study→series expansion should likely be an htmx partial swap rather than a full page reload or client-side JS framework, consistent with the rest of `frontend_fastapi`'s templates.
- Keep this page's route namespace distinct from `/studies` (the existing Orthanc-browsing feature) — they are unrelated systems (Conquest vs Orthanc) that happen to look superficially similar; do not merge their UI or code paths.

## Out of scope

- Moving data (F004) — this feature is read-only.
- Any anon↔real ID translation — Conquest is already anonymised; there is nothing to translate here (see plan.md D004; contrast with F005, which does translate, for the audit record only).
- Caching/persisting search results locally.
