# Feature F001: Show local PACS connectivity status on the browse page

## Purpose

Today a user only learns Conquest is unreachable after submitting a search (`conquest_error` in `routers/local_pacs.py`'s `local_pacs_browse`). Surfacing reachability up front — before anyone has typed a search — saves a wasted search attempt and gives a data custodian a quick "is it worth trying right now" signal, using the C-ECHO primitive (`conquest_client.echo()`) that F001 of round 1 already built but nothing calls yet.

## Behaviour

- On landing on `GET /local_pacs` with no search submitted, the area that would otherwise hold search results shows a small status indicator instead: "Checking local PACS connectivity…" that resolves, without blocking the initial page render, into either a reachable state (e.g. "Local PACS reachable") or an unreachable state showing the failure reason (same wording style as today's `conquest_error` banner: "Local PACS unavailable: <reason>").
- The check runs automatically — the user does nothing to trigger it.
- Once a search is actually submitted (the existing `searched` flow), the connectivity indicator's slot is replaced by the normal results table / "no results" / `conquest_error` states exactly as they behave today. The indicator does not persist alongside results, and does not run again on a page that already has search results.
- Landing on the page with results already present (e.g. returning via back-button, or reloading a URL with `?submitted=1&...`) shows the search results/error, not the connectivity indicator — same rule as above, the two states are mutually exclusive on this page.

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/local_pacs/conquest_client.py:134` — `echo()`: the C-ECHO primitive this feature calls. Raises a `ConquestError` subclass on any failure, returns `None` on success. Already covered by `test_conquest_client.py`.
- `frontend_fastapi/local_pacs/conquest_client.py:105` — `is_configured()`: must be checked before calling `echo()`, same guard `local_pacs_browse` already applies before `find_studies()`.
- `frontend_fastapi/routers/local_pacs.py:53-93` — `local_pacs_browse`: the existing `searched` flag and `conquest_error` handling this feature's new partial sits alongside.
- `frontend_fastapi/routers/local_pacs.py:96-106` — `local_pacs_series`: the existing pattern for an htmx-loaded partial (`hx-get`, `hx-target`, `hx-swap="innerHTML"`) reached via `asyncio.to_thread` for the blocking pynetdicom call — this feature's new endpoint follows the same shape.
- `frontend_fastapi/templates/local_pacs/browse.html` — where the new slot is inserted; the `{% if conquest_error %}...{% elif searched and ... %}` block (lines 26-37) is the boundary this feature's slot sits above/beside.
- `frontend_fastapi/tests/test_local_pacs_browse.py` — existing conventions for testing this router (`mock_cc` fixture monkeypatching `conquest_client` at its module boundary).

### Likely changes

- `frontend_fastapi/routers/local_pacs.py` — add a new `GET /local_pacs/status` route returning an htmx partial, calling `cc.echo()` via `asyncio.to_thread` (matching `local_pacs_series`'s pattern).
- `frontend_fastapi/templates/local_pacs/browse.html` — add the htmx-loaded slot (`hx-trigger="load"`), shown only when `not searched`.
- New template: `frontend_fastapi/templates/local_pacs/_status.html` (or similar) — the partial rendered by the new route, mirroring `_series.html`'s shape (a small fragment, not a full page).
- `frontend_fastapi/tests/test_local_pacs_browse.py` — new tests for the status route and for the browse page emitting the htmx slot only when appropriate.

## Acceptance criteria

### Scenario: Landing on the page shows the connectivity slot, not results

**Given** a logged-in user with no query string

**When** they load `GET /local_pacs`

**Then** the page renders immediately with an htmx-loaded slot in place of the results area, and no search results, `conquest_error`, or "No results found" messaging is shown

### Scenario: Conquest is reachable

**Given** `conquest_client.echo()` succeeds

**When** the browser's htmx request to the new status endpoint completes

**Then** the slot shows a clear "reachable" state (no error styling)

### Scenario: Conquest is unreachable

**Given** `conquest_client.echo()` raises a `ConquestError` (e.g. `ConquestTimeout`, `ConquestUnreachable`)

**When** the browser's htmx request to the new status endpoint completes

**Then** the slot shows an "unavailable" state including the error's message, styled consistently with the existing `conquest_error` banner (red/warning styling)

### Scenario: Local PACS not configured on this deployment

**Given** `conquest_client.is_configured()` returns `False`

**When** the status endpoint is requested

**Then** the slot shows the same "Local PACS (Conquest) is not configured on this deployment." message `local_pacs_browse` already shows for an unconfigured deployment on search — without attempting `echo()`

### Scenario: A search replaces the connectivity slot

**Given** a logged-in user on the browse page

**When** they submit a search (any outcome — results, no results, or a search-time `conquest_error`)

**Then** the page shows the existing results/error/no-results UI, and the connectivity-check slot/request is not present on that response

### Scenario: Unauthenticated request

**Given** no logged-in session

**When** a request is made to the new status endpoint directly

**Then** it behaves like every other `require_login`-gated route in this router (redirect to login)

## Edge cases

- The status endpoint must not be reachable/meaningful without `require_login`, matching every other route in this router.
- A slow Conquest (near pynetdicom's association timeout) must not block the initial `GET /local_pacs` response — this is the entire reason for the htmx/auto-load approach; verify the main route itself never calls `echo()`.
- If `is_configured()` is `False`, avoid attempting a doomed `echo()` call — check it first, same guard order `local_pacs_browse` already uses for `find_studies()`.

## Success

`GET /local_pacs` with no query renders instantly and its results slot is populated moments later by a separate request reporting Conquest's reachability; submitting a search always shows the normal results/error UI in that same slot, never the connectivity indicator.

## Failure behaviour

Any `ConquestError` from `echo()` is caught and rendered as an "unavailable" state with the exception's message — never raised as an unhandled 500. Matches `local_pacs_series`'s existing `except cc.ConquestError as e: error = str(e)` handling shape.

## Testing considerations

Follow `test_local_pacs_browse.py`'s existing pattern: monkeypatch `conquest_client.echo` (and `is_configured`) at the module boundary rather than exercising real pynetdicom — a live Conquest round trip is `test_conquest_client.py`'s job, not this router's. Add cases for: reachable, each relevant `ConquestError` subclass, not-configured, and that the browse page's initial HTML (no query string) contains the htmx slot markup but not a rendered results table.

## Implementation notes

- Reuse `local_pacs_series`'s `asyncio.to_thread(...)` wrapping — `echo()` is a synchronous pynetdicom call and must not run directly on the event loop (per CLAUDE.md's Async threading pattern).
- This is a read-only, unauthenticated-from-Conquest's-perspective operation (a C-ECHO carries no patient data) — no audit trail is needed for this feature, unlike moves.
- Keep the partial visually lightweight (a single line/badge, not a full table) — "placeholder table" in the original request meant "the same slot the results table renders into," not a persistent table widget of its own.

## Out of scope

- Any change to the existing `conquest_error` banner shown on a failed *search* (not a connectivity check) — that behaviour is unchanged.
- Polling/refreshing the connectivity status automatically thereafter — a single check on page load is the entire scope.
- Showing connectivity status anywhere other than the browse page (e.g. destinations page, homepage tile).
