# Feature F004: Move a study from the local PACS to a destination

## Purpose

Lets a data custodian relay a study (or series) found on Conquest directly to another registered DICOM destination, without staging it through Orthanc — the second half of this plan's motivating request, after F002's browse/check capability.

## Behaviour

- On F002's browse/search results, a data custodian (and only a data custodian — `require_data_custodian`) sees a "Move" action against each study (and/or series, if series-level move is supported — see Edge cases) result.
- Choosing "Move" presents the custodian-managed destination list (F003) to pick from — a dropdown of curated AE titles/display names, not free text.
- Confirming the move issues a direct DICOM C-MOVE (via F001) against Conquest, naming the chosen destination's AE title as the Move Destination — Conquest performs the actual transfer to that destination; the data does not pass through `frontend_fastapi`, `backend`, or Orthanc.
- This is synchronous from the user's perspective: the page waits for Conquest's C-MOVE response and then reports the outcome (success, partial success/warning, or failure) inline — no background job, no progress stream, no batch/CSV upload (v1 scope, per plan.md).
- After the move attempt (whether it succeeded or failed), F004 calls F005's backend endpoint to record it in HermesDB's audit trail, passing along the anonymised patient ID, study/series identifiers, destination, the custodian's username, and the outcome.
- If the C-MOVE itself fails, the user sees a failure result with whatever detail Conquest/`pynetdicom` provided (per F001's distinguishable failure modes).
- If the C-MOVE succeeds but the F005 audit call fails, the user still sees a success result, with an additional warning that the move's audit record could not be saved (plan.md D006).

## Dependencies

- **F001** — provides the C-MOVE primitive this feature calls.
- **F002** — provides the browse/search results this feature's "Move" action is triggered from; F004 has no standalone entry point of its own.
- **F003** — provides the destination choices; without it, there is nothing valid to move data to.
- **F005** — provides the audit-recording call this feature makes after every move attempt.

This is the integration feature of the plan — it has no independent value without the other four, and should be implemented last.

## Relevant code

### Existing

- `frontend_fastapi/deps.py:113-116` — `require_data_custodian`, the gate this feature's move action uses.
- `backend/src/export/endpoints.py` (`Exporter.dicom_c_move`, and the `Response` model at lines 77-96) — the closest existing analogue for "report the outcome of a DICOM transfer," even though the underlying mechanism (Orthanc REST C-STORE push) differs entirely from this feature's direct `pynetdicom` C-MOVE. Useful for outcome-reporting shape/vocabulary (status, destination, destination_type), not for the transfer mechanism itself.
- `frontend_fastapi/backend_client.py` — the sole module that talks to `backend`; a new function here will call F005's endpoint, following this file's existing `_post`/error-handling conventions (`BackendError`).
- CLAUDE.md's "single patient stays synchronous" convention (referenced throughout the Architecture section for both import and export) — the precedent this feature's synchronous-only v1 scope follows.

### Likely changes

- `frontend_fastapi/routers/local_pacs.py` (from F002) — add the move route/handler.
- `frontend_fastapi/templates/local_pacs/` (from F002) — add the "Move" action UI (destination picker, confirmation, result display) to the browse results template(s).
- `frontend_fastapi/backend_client.py` — add a function calling F005's new backend endpoint.

## Acceptance criteria

### Scenario: A custodian successfully moves a study

**Given** a data custodian viewing a study on the local PACS browse page, and a configured destination

**When** they choose "Move," select a destination, and confirm

**Then** the study is relayed directly from Conquest to that destination, the page reports success, and the move is recorded in HermesDB's audit trail (F005)

### Scenario: A non-custodian cannot move data

**Given** a logged-in user who is not a data custodian, viewing the local PACS browse page

**When** they view a study's results

**Then** no "Move" action is available to them (and, defensively, attempting to hit the move endpoint directly is rejected server-side, not just hidden in the UI)

### Scenario: A move fails at the DICOM level

**Given** a destination that is unreachable or rejects the transfer

**When** a custodian attempts the move

**Then** the page reports failure with the specific reason where available (per F001's distinguishable failure modes), and the failure is still recorded in the audit trail (F005)

### Scenario: A move succeeds but audit recording fails

**Given** a successful C-MOVE, but F005's backend endpoint is unreachable or errors when called afterward

**When** the custodian's move completes

**Then** the page reports success with a visible warning that the audit record failed to save (plan.md D006) — the custodian is not told the move failed when it didn't

### Scenario: No destinations configured

**Given** F003's destination list is empty

**When** a custodian attempts to move a study

**Then** they see a clear message that no destinations are configured, rather than an empty/broken dropdown

## Edge cases

- Series-level move vs study-level move: the browse page (F002) drills down to series, so decide (implementer's call, informed by whatever Conquest's C-MOVE actually supports at study vs series granularity) whether "Move" is offered at the study level only, series level only, or both. Study-level is the simpler default and covers the primary use case; series-level can be added if needed.
- A custodian double-submitting the move action (e.g. double-click) before the first request completes — since this is synchronous and DICOM C-MOVE is not inherently idempotent, the UI should guard against a duplicate submission (disable the button while in flight is likely sufficient; no stronger dedup is specified here).
- A very large study (many series/instances) making the synchronous C-MOVE take a long time — the request/response cycle needs a sensible timeout (see F001), and the UI should show that a move is in progress rather than appearing hung.

## Success

A data custodian can move a study found via F002 to a destination chosen from F003's list, see an accurate outcome, and find a corresponding record in HermesDB's audit trail (F005) afterward — all without the data ever landing in Orthanc.

## Failure behaviour

Every failure surfaced by F001 (unreachable, misconfigured, rejected, timeout) and every outcome from F005 (recorded vs failed-to-record) must produce a distinct, accurate message to the custodian — never a generic "something went wrong," given this action moves real patient data.

## Testing considerations

Follow `frontend_fastapi/tests/`'s pytest conventions. This feature is the integration point, so its tests should exercise the full chain with F001 and F005 mocked/stubbed at their boundaries (a fake Conquest response, a fake backend audit call) rather than requiring a live Conquest instance or a live backend — reserve true end-to-end verification for manual/staging testing against a real or test Conquest instance, consistent with F001's own testing notes.

## Implementation notes

- Keep the "authorize" (custodian gate) and "audit" (F005) concerns clearly separate in the code even though they're both invoked from this feature's route handler — per plan.md D005, F005's backend endpoint deliberately does not re-check authorization; `frontend_fastapi`'s `require_data_custodian` is the only gate, and it must run before the C-MOVE is even attempted, not just before the result is shown.
- Since this feature has no task queue, "single-study synchronous" is not just a scope choice but a hard constraint of the current architecture (see plan.md's Deferred decisions for what batch support would require).

## Out of scope

- Batch/multi-study move, CSV upload, background job/progress UI (plan.md Limitations/Deferred decisions).
- Any staging of the moved data through Orthanc.
- Building a new destination on the fly from this page — destinations must already exist via F003.
