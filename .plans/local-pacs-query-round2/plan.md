# Implementation Plan: Local PACS Query — Round 2

## Goal

Two enhancements to the local PACS (Conquest) browse/move webpage shipped in `.plans/local-pacs-query/` (F001–F005, already merged): show whether Conquest is actually reachable before a user searches, and let a data custodian move several studies to one destination in a single action instead of one at a time.

## What we discussed

- The original webpage already ships: search/browse (`GET /local_pacs`), a per-study "Show series" htmx expand, single-study synchronous move (`POST /local_pacs/move`), and destination CRUD (`/local_pacs/destinations`). Nothing here changes that baseline.
- F001's `conquest_client.echo()` (C-ECHO connectivity check) already exists but nothing calls it today — this round wires it up.
- Connectivity status should appear automatically, without the user doing anything, but must not block the page from rendering (Conquest could be slow or hung). It occupies the same results-table slot that search results later replace once a search actually runs.
- Batch move: checkboxes per study row across the current search results (any/all patients on the page), one destination dropdown applies to every checked study — the batch is "the same per-row move, applied to several rows with one destination."
- Each `move_study()` call blocks until Conquest's C-MOVE fully completes, so a batch of several studies needs live per-study feedback rather than a single blocking page — reuses the app's existing SSE vocabulary (`start`/`progress`/`success`/`error`/`done`) for consistency with every other progress view in this app, but the stream is generated inside `frontend_fastapi` itself: there is still no backend task queue involved here (original plan's D001 still holds — the DICOM call must originate from `frontend_fastapi`, which has no queue of its own).
- No cap on batch size and no mid-batch cancel control in this round — kept minimal; already-started moves can't be aborted either way (real DICOM operations in flight).
- Batch auditing reuses the existing `POST /local_pacs/audit_move` backend endpoint unchanged, called once per moved study — one HermesDB job per study, exactly like today's single move. No backend change.

## Decisions

- **D001 — Connectivity check is an htmx auto-load partial, not a blocking page load:** `hx-trigger="load"` against a new `GET /local_pacs/status` partial, landing in the same DOM slot the results table renders into. Keeps the page itself instant even if Conquest is slow/unreachable.
- **D002 — Batch move progress is a `frontend_fastapi`-local SSE generator, not a backend relay:** every other SSE view in this app (`jobs.py`'s `job_stream`) relays a backend-generated stream; this one can't, because the DICOM calls themselves happen in `frontend_fastapi`. The generator lives in `routers/local_pacs.py`, looping `move_study()` + `audit_local_pacs_move()` per selected study and yielding the same `start`/`progress`/`success`/`error`/`done` event vocabulary the rest of the app already uses, for a consistent-feeling progress UI even though nothing here touches the task queue.
- **D003 — One destination for the whole selected batch:** a single dropdown, not a per-row choice, matching how the user actually wants to use it (pick several studies, send them all to the same place).
- **D004 — No batch-size cap, no cancel control in v1 of this round:** whatever is checked runs to completion; kept minimal per product decision.
- **D005 — Batch auditing reuses the existing per-study audit endpoint unchanged:** `POST /local_pacs/audit_move` is called once per moved study exactly as today's single move already does — no backend change, and the jobs list shows N rows for an N-study batch (consistent with how audit worked before this round).

## Implementation overview

1. **F001 — Show local PACS connectivity status on the browse page** ([`features/F001-connectivity-indicator.md`](features/F001-connectivity-indicator.md)): an htmx-loaded partial reporting whether Conquest answered a C-ECHO, shown before any search and replaced by results once one runs.
2. **F002 — Move multiple local PACS studies at once with live progress** ([`features/F002-batch-move-with-progress.md`](features/F002-batch-move-with-progress.md)): per-row checkboxes + one destination + a live SSE progress view covering every selected study's move-and-audit outcome.

## Dependencies

```text
F001  (independent)
F002  (independent)
```

Neither feature depends on the other or shares new code — they touch the same template (`browse.html`) in different, non-overlapping regions and can be built and reviewed in either order or in parallel.

## Open issues

- None — both features build entirely on primitives (`conquest_client.echo()`, `conquest_client.move_study()`, `backend_client.audit_local_pacs_move()`) that already exist and are already exercised by the round-1 test suite.

## Assumptions

- `frontend_fastapi` runs as effectively one logical service for the lifetime of a batch-move stream (see F002's Implementation notes for what that constrains) — the same assumption the original plan already made implicitly by keeping all local-PACS state either in Conquest itself or in this app's own small local DB, never in a queue.
- Conquest's C-ECHO SCP is enabled (a standard, near-universal DICOM conformance requirement) wherever its C-FIND/C-MOVE SCPs already are, since F001 (round 1) already built on that same assumption for other primitives.

## Limitations

- A batch move's live progress page is tied to the `frontend_fastapi` process instance that started it (see F002) — if this app is later scaled to multiple processes behind a load balancer without sticky routing, a client reconnecting to the stream mid-batch could hit a different process than the one running the moves. Not a concern for today's single-instance deployment; flagged for whoever scales this app horizontally later.
- Batch move still performs one real DICOM C-MOVE per study — the app cannot cancel a C-MOVE already dispatched to Conquest.

## Deferred decisions

- A batch-size cap and a "stop remaining" control — explicitly deferred this round; revisit if custodians select unreasonably large batches in practice.
- Grouping a batch's audit trail under one HermesDB job instead of N — explicitly deferred this round in favour of reusing the existing endpoint unchanged.

## Out of scope

- Any change to single-study move (`POST /local_pacs/move`) — it keeps its current synchronous redirect-and-flash behaviour.
- Any change to destination management (F003) or the audit endpoint's request/response shape (F005).
- Retrying a failed move from within the batch UI.
