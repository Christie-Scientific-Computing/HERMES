# Feature F002: Move multiple local PACS studies at once with live progress

## Purpose

Today a data custodian moves one study at a time (`POST /local_pacs/move`), each a separate blocking request-redirect-flash cycle. When a custodian needs to relay several studies from a search's results to the same destination, this saves them repeating that cycle per study and gives live feedback while the (potentially slow) C-MOVE operations run, instead of either one blank page per study or one very long blocking request.

## Behaviour

- Every study row in the browse results (`browse.html`'s per-study `<tr>`) that a custodian can currently move gains a checkbox, in addition to (not instead of) today's existing per-row "pick one destination, Move" mini-form. Checkboxes work across all patients shown on the page, not just within one patient's studies.
- A batch action bar appears (visible only to custodians, i.e. gated the same way `can_move` already gates the per-row move form) with: a single destination dropdown (same destination list as the per-row form) and a "Move selected" button. It is meaningfully usable once at least one study is checked; its exact enabled/disabled styling is an implementation detail, but submitting with zero studies checked or no destination chosen must be rejected with a clear message rather than silently doing nothing.
- Submitting the batch action navigates the custodian to a live progress view (in the same spirit as `jobs/job_watch.html` + `job_stream`, but scoped to this batch): as each selected study's move is attempted, the view updates to show that study's outcome (moved / failed, with the failure detail) as it happens, not only once the whole batch finishes. A running "total" gives the custodian a sense of overall progress.
- Studies are processed one at a time, in order (matching the plan's D002 — Conquest is not asked to run concurrent C-MOVEs from this app). Each study's move is followed immediately by its own audit call, exactly as today's single move does (`POST /local_pacs/audit_move`, once per study) — a study's outcome is not reported as final in the progress view until its move attempt is complete, but the view proceeds to the next study whether or not that individual audit call succeeded (matching today's single-move tolerance for a failed audit call: the move itself is what matters, with a warning noted).
- Every selected study is attempted; there is no cap on how many can be selected and no way to stop remaining studies once the batch has started (per this round's D004) — leaving is the only way to stop watching, and does not stop moves still in flight.
- Once every selected study has been attempted, the progress view shows a final summary (counts of succeeded/failed) and the custodian can navigate back to browse (their prior search should still be reachable, e.g. via a link back, consistent with how the single-move flow already redirects back to the same search).

## Dependencies

None. Independent of F001 — they touch non-overlapping regions of `browse.html` and add separate new routes.

## Relevant code

### Existing

- `frontend_fastapi/local_pacs/conquest_client.py:294` (`move_study`) and its `MoveResult` dataclass — the same primitive `local_pacs_move` already calls once per request; this feature calls it once per selected study, in the same way.
- `frontend_fastapi/routers/local_pacs.py:111-173` (`local_pacs_move`) — the existing single-study move-then-audit-then-flash-then-redirect flow. This feature's per-study logic (move, interpret `MoveResult`, call the audit endpoint, tolerate an audit failure) should mirror this function's outcome/detail/audit-warning handling rather than reinvent it — consider factoring the shared "move one study and audit it" logic into a helper both routes call, rather than duplicating it.
- `frontend_fastapi/backend_client.py`'s `audit_local_pacs_move` — called once per moved study, unchanged (round-2 D005).
- `frontend_fastapi/routers/jobs.py:247-295` (`job_watch` / `job_stream`) — the closest existing precedent for a "watch page + SSE stream" pair, including the re-framing convention (`event: <type>\ndata: {...}\n\n`) so a plain `EventSource.addEventListener('progress', ...)` works browser-side. This feature's stream is **not** a relay of a backend stream (there is no backend job/queue behind it — see Implementation notes) but should still emit the same `start` / `progress` / `success` / `error` / `done` vocabulary (CLAUDE.md's SSE streaming convention) for a consistent feel.
- `frontend_fastapi/templates/jobs/job_watch.html` (and its progress component(s)) — reference for how an `EventSource`-driven progress page is structured today; the new batch-move watch template should follow the same client-side dispatch pattern, not invent a new one.
- `frontend_fastapi/forms/local_pacs.py` — `LocalPacsDestinationForm`/`LocalPacsSearchForm` conventions (WTForms, CSRF handled globally) — the batch action bar's destination dropdown and "Move selected" submission should follow the same CSRF convention every other POST in this app uses (`deps.csrf_protect`).
- `frontend_fastapi/tests/test_local_pacs_move.py` — existing conventions for testing the move flow (monkeypatching `conquest_client.move_study` and `backend_client.audit_local_pacs_move` at their boundaries).
- `frontend_fastapi/deps.py` — `require_data_custodian`, `require_login`, `get_current_user` — the same auth dependencies `local_pacs_move`/`job_stream` already use; this feature's POST and stream routes use the same pattern (`require_data_custodian` for the POST that starts a batch, since only a custodian may move; the stream route itself only needs to verify the requester started or may view that specific batch).

### Likely changes

- `frontend_fastapi/routers/local_pacs.py` — new routes: `POST /local_pacs/move_batch` (validates the batch — non-empty selection, valid destination — and hands off to a watch view) and `GET /local_pacs/move_batch/{batch_id}/watch` + `GET /local_pacs/move_batch/{batch_id}/stream` (the SSE generator). Extract the shared "move one study, then audit it, tolerating an audit failure" logic out of `local_pacs_move` into a helper both the single-move and batch-move routes call.
- `frontend_fastapi/templates/local_pacs/browse.html` — add checkboxes to each moveable study row and the batch action bar, gated by `can_move` exactly like the existing per-row move form.
- New templates: a batch-move watch page and its progress partial/JS, modeled on `jobs/job_watch.html`.
- `frontend_fastapi/tests/test_local_pacs_move.py` (or a new `test_local_pacs_batch_move.py`) — tests for: starting a batch, the stream's per-study events, an empty-selection rejection, a missing-destination rejection, and that a non-custodian cannot reach any of the new routes.

## Acceptance criteria

### Scenario: Starting a batch move

**Given** a logged-in custodian viewing search results with several studies shown

**When** they check three studies, choose a destination, and submit "Move selected"

**Then** they land on a progress view that begins reporting each study's move outcome, one at a time, without waiting for all three to finish before showing the first

### Scenario: Mixed outcomes are all reported

**Given** a batch of studies where `conquest_client.move_study()` succeeds for some and returns a failed/incomplete `MoveResult` (or raises `ConquestError`) for others

**When** the batch runs to completion

**Then** the progress view shows each study's individual outcome (success or failure with detail) and a final summary reflecting the true mix, not an all-or-nothing result

### Scenario: Auditing happens per study, same as today

**Given** a batch of studies that all move successfully

**When** each study's move completes

**Then** `backend_client.audit_local_pacs_move` is called once per study (not once for the whole batch), matching round-2's D005

### Scenario: A failed audit call doesn't stop the batch or hide the move's own success

**Given** a study's move succeeds but its subsequent `audit_local_pacs_move` call raises `BackendError`

**When** that study's outcome is reported

**Then** it is still shown as a successful move (with a warning that the audit record didn't save, matching `local_pacs_move`'s existing tolerance), and the batch proceeds to the next study

### Scenario: Empty selection is rejected

**Given** a custodian submits the batch action with no studies checked

**When** the request is handled

**Then** it is rejected with a clear message and no move is attempted, rather than silently starting an empty batch

### Scenario: No destination chosen is rejected

**Given** at least one study is checked but no destination is selected

**When** the request is handled

**Then** it is rejected with a clear message and no move is attempted

### Scenario: Non-custodian cannot start or view a batch

**Given** a logged-in user who is not a data custodian

**When** they attempt to submit the batch-move action, or to load a batch's watch/stream URL directly

**Then** they are denied exactly as `local_pacs_move` already denies a non-custodian today

### Scenario: An unknown or already-finished batch ID

**Given** a `batch_id` that was never started by this custodian, or one whose stream has already fully completed and been cleaned up

**When** its watch or stream URL is requested

**Then** the request fails clearly (e.g. 404) rather than hanging or erroring unhandled

## Edge cases

- A destination that's deleted between page load and batch submission (same edge case `local_pacs_move` already handles for a single move — `destination is None` — must also be handled here).
- The browser closing or navigating away mid-batch: moves already dispatched to Conquest continue server-side (a C-MOVE, once sent, can't be un-sent); the stream generator should keep running to completion and finish auditing every study regardless of whether a client is still listening, then discard its state.
- Selecting the same study twice is not possible via checkboxes in one page's markup, but guard against a manipulated request re-listing the same study UID twice in the POST body — treat it as two separate move attempts (simplest, consistent with "no dedup logic" anywhere else in this feature) rather than silently dropping the duplicate.

## Success

A custodian can select several studies from one or more patients in a single search's results, submit them to one destination, and watch each study's move-and-audit outcome appear live as it happens, ending in a summary that accounts for every selected study.

## Failure behaviour

- A `ConquestError` from an individual `move_study()` call is caught per study (not allowed to abort the whole batch) and reported as that study's failure, exactly as `local_pacs_move` already does for a single move.
- A `BackendError` from an individual `audit_local_pacs_move()` call is caught per study and reported as a warning alongside that study's otherwise-successful move, exactly as `local_pacs_move` already does.
- If the batch itself can't start at all (empty selection, no destination, missing/invalid destination ID), fail before attempting any move — no partial batch should ever begin from invalid input.

## Testing considerations

Mirror `test_local_pacs_move.py`'s approach: monkeypatch `conquest_client.move_study` and `backend_client.audit_local_pacs_move` at their module boundaries (a `Mock`/`AsyncMock` per test), never exercising real pynetdicom or the real backend. For the SSE stream itself, follow whatever pattern this repo already uses for testing an async-generator `StreamingResponse` (check `backend/tests/test_observer_stream.py` or `frontend_fastapi/tests/` for an existing SSE-consuming test helper before inventing a new one) — assert the sequence of event types/payloads for a mixed-outcome batch, not just the final state. Cover: multi-study success, mixed success/failure, an audit failure that doesn't stop the batch, empty selection, missing destination, and non-custodian access to each new route.

## Implementation notes

- **No backend/task-queue involvement.** Unlike `jobs.py`'s `job_stream` (which relays a backend-generated observer stream backed by HermesDB's `tasks` table), this feature's progress events are generated entirely inside `frontend_fastapi` itself — there is nothing in HermesDB tracking "a batch," only the individual per-study audit rows each move already writes via the unchanged `audit_local_pacs_move` endpoint. This is a deliberate consequence of the original plan's D001 (Conquest is unreachable from the backend at all).
- Because the batch's in-flight state (which studies, what destination, current progress) has nowhere else to live, it must be held in `frontend_fastapi`'s own process for the life of the batch — an in-memory structure keyed by a generated `batch_id`, cleaned up once the stream finishes and is no longer being watched (or after a reasonable timeout for an abandoned one). This is a new pattern for this app (no other feature holds ephemeral cross-request state in-process); keep it as small and self-contained as the job justifies rather than building general infrastructure for it.
- A `POST` starts the batch (CSRF-protected, `require_data_custodian`) and should redirect (303) to a `GET` watch page — `EventSource` can only issue `GET` requests, so the POST cannot itself be the stream, mirroring why `jobs.py` splits `job_watch`/`job_stream` into two routes already.
- Reuse the existing SSE event-type vocabulary (`start`, `progress`, `success`, `error`, `done`) and the `event: <type>\ndata: {...}\n\n` framing `job_stream` already produces, so the browser-side `EventSource` handling can follow the same dispatch pattern — but this generator produces those events directly (it is the producer, not a relay).
- Extract the "move one study, interpret the `MoveResult`, call the audit endpoint, tolerate an audit failure, decide the flash-worthy outcome/detail/warning strings" logic out of `local_pacs_move` into a shared helper, so this feature's per-study loop and the existing single-move route call the same code rather than two copies drifting apart.

## Out of scope

- Any backend/HermesDB schema change — auditing stays exactly as it is today, per study.
- A batch-size cap or a mid-batch cancel/stop control (explicitly deferred this round, see plan.md).
- Horizontal-scaling-safe batch state (e.g. a shared store reachable from any `frontend_fastapi` process) — flagged as a limitation in plan.md, not solved here.
- Per-study destination overrides within one batch — one destination applies to the whole batch.
- Changing the existing single-study move UI/flow in any way.
