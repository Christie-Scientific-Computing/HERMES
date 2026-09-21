# Implementation Plan: Local PACS Query (Conquest browse + move)

## Goal

Let HERMES users check, ahead of an import/restore, whether a patient's data already exists on the trust's local PACS (a Conquest DICOM server) — and let data custodians relay studies straight from Conquest to another registered DICOM destination, without staging the data through Orthanc. Reachable from a "Browse local PACS" homepage tile that today is a non-clickable "Coming soon" placeholder.

## What we discussed

- "Local PACS" is a separate DICOM server (Conquest), not Orthanc — the trust's actual archive, distinct from HERMES's own Orthanc hub.
- Conquest sits on the same network segment as the frontend server and its users. HERMES's `backend` (and therefore Orthanc) cannot reach Conquest at all — a strict firewall separates them. `frontend_fastapi` ↔ `backend` stays reachable as normal through the existing proxy/firewall convention.
- Conquest stores **anonymised** patient IDs only — this is the pseudo-anonymised archive referenced elsewhere in this repo as "ukCAT PACS" / "Conquest instance." Users search and browse it by anon ID; no real MRN ever needs to reach `frontend_fastapi` for this feature.
- Browsing is open to any logged-in user (read-only, no ethics-project gate — matches the existing ungated `/studies` Orthanc-browsing precedent).
- Moving a study is restricted to data custodians (`is_staff`), with no additional ethics-project gate layered on.
- Moves are a direct one-hop DICOM relay (Conquest → destination via C-MOVE `TargetAet`), never landing in Orthanc's storage — chosen over staging through Orthanc because it's less code, avoids incidental data duplication, and is how DICOM C-MOVE is natively meant to work. Trade-off accepted: no per-instance checksummed manifest the way Orthanc-sourced exports get, since HERMES never touches the actual instance bytes.
- Single-study, synchronous moves for v1 — no batch/CSV, no task-queue (that infrastructure is backend/HermesDB-owned and isn't naturally reusable here since the DICOM call must originate from `frontend_fastapi`, which has no queue of its own).
- Move destinations are a small, custodian-editable list (not free text, not Orthanc's modality registry — Conquest itself must already be configured, ops-side, to reach any AE offered here).
- Moves are still audited centrally in HermesDB, reusing `jobs`/`patients`/`events` (hash-chained) rather than inventing a parallel audit table — tagged with a sentinel `research_projects` row (`project_id = "localPACSTransfer"`) rather than a real ethics-approved project, since this action is authorized by custodian status alone.
- If the DICOM move itself succeeds but the follow-up audit-logging call fails, report success to the user with a warning — the transfer is the thing that actually matters clinically, matching this app's existing best-effort tone for non-authorization bookkeeping (contrast with the ethics-gate's own fail-closed convention, which doesn't apply here since there is no ethics gate on this action).

## Decisions

- **D001 — DICOM logic lives in `frontend_fastapi`, not `backend`:** the backend cannot reach Conquest at all (firewalled). `pynetdicom` (already an unused dependency in `requirements.txt`) runs directly from `frontend_fastapi` request handlers. This is the one deployment-topology fact everything else in this plan follows from.
- **D002 — Direct one-hop relay for moves, not staged through Orthanc:** `pyorthanc`'s `Modality.move()` (and the Orthanc REST API it wraps) supports a `TargetAet` parameter for exactly this pattern, but it requires Orthanc as an intermediary — irrelevant here since Orthanc can't reach Conquest either. The equivalent primitive for a direct relay is a plain DICOM C-MOVE issued by `frontend_fastapi` itself (via `pynetdicom`) with an explicit Move Destination AE Title, which Conquest's own SCP performs.
- **D003 — Audit trail reuses `jobs`/`patients`/`events`, tagged with a sentinel project:** avoids inventing a parallel audit mechanism; gets the existing hash chain and job-detail UI "for free." The sentinel `research_projects` row (`project_id = "localPACSTransfer"`) exists only to satisfy the `jobs.project_id` foreign key — it carries no real membership and this flow never calls `require_project_member`.
- **D004 — No anon-translation round-trip needed for browsing:** Conquest already stores anonymised IDs, so `frontend_fastapi` never handles a real MRN for search/browse. Only the audit-recording step (backend-side) needs `identity/anon.py`'s `resolve_real_id()`, translating the anon ID it's given back to the real MRN before writing to `events`/`patients` — consistent with every other endpoint's convention that those tables store the real ID.
- **D005 — Custodian gate only, no ethics-project gate, on the move action:** `require_data_custodian` (already exists, checks `is_staff`) is the entire authorization story for moving. This is treated as an operational/admin action, not a research-data export.
- **D006 — Move failure/audit-failure semantics:** a failed C-MOVE is reported as a failure. A successful C-MOVE whose audit-logging call fails is still reported as a success, with a warning that the audit record didn't save.

## Implementation overview

1. **F001 — Connect to and verify the local PACS (Conquest)** ([`features/F001-conquest-connection.md`](features/F001-conquest-connection.md)): the `pynetdicom`-backed connection module (C-ECHO/C-FIND/C-MOVE primitives) and its configuration, shared by browsing and moving.
2. **F002 — Search and browse the local PACS** ([`features/F002-browse-local-pacs.md`](features/F002-browse-local-pacs.md)): the user-facing search page (anon ID, date range, study description, modalities), patient → study → series drill-down, and the homepage tile becoming a real link.
3. **F003 — Manage local PACS move destinations** ([`features/F003-manage-move-destinations.md`](features/F003-manage-move-destinations.md)): the custodian-editable destination list (new local table + CRUD UI).
4. **F004 — Move a study from the local PACS to a destination** ([`features/F004-move-study.md`](features/F004-move-study.md)): the custodian-only, single-study synchronous relay action, triggered from F002's browse results.
5. **F005 — Audit local PACS moves in HermesDB** ([`features/F005-audit-local-pacs-moves.md`](features/F005-audit-local-pacs-moves.md)): the new backend endpoint that records each move as a job/patient/event trio under the sentinel project, called by F004.

## Dependencies

```text
F001 ──────┬──────> F002 ──────┐
           │                   │
           └───────────────────┼──> F004
F003 ──────────────────────────┤
F005 ──────────────────────────┘
```

F001 and F003 and F005 have no dependencies on each other and can be built in any order (or in parallel). F002 depends only on F001. F004 depends on all four of the others — it's the integration point.

## Open issues

- Conquest's actual supported DICOM Query/Retrieve SOP classes (Patient Root vs Study Root, which optional query keys it honours, whether it accepts an explicit Move Destination AE Title from a requester other than the eventual receiver) can't be verified from this repository — there's no Conquest instance or its config reachable here. F001's implementer will need to confirm against a real/staging Conquest instance and adjust query construction accordingly.
- Whether Conquest's own C-STORE/C-MOVE SCP is configured to permit relaying to a *third* AE (rather than only to the requester) is an ops/Conquest-config question, not something this plan can resolve — flagged in F004 as a dependency on external configuration.

## Assumptions

- Conquest is reachable via standard DICOM networking (C-ECHO/C-FIND/C-MOVE) from wherever `frontend_fastapi` is deployed, with no additional protocol/gateway in between.
- The frontend server has a stable AE title of its own (configured, not auto-negotiated) to use as the calling AE for associations with Conquest.
- `research_projects.status`/`expiry_date` semantics (from `backend/src/projects/db_client.py`) accommodate a permanently-approved sentinel project the same way the existing superuser-bypass project already does (far-future `expiry_date`, `status='approved'`).

## Limitations

- No per-instance checksummed manifest for moved studies (unlike Orthanc-sourced exports) — only C-FIND-level metadata (study/series UIDs, patient, destination, timestamp) is available to audit, since the data's bytes never pass through HERMES.
- No batch/multi-study move in v1 — one study (or series) at a time.

## Deferred decisions

- Batch/CSV-driven multi-study moves, if custodians need them later — would need either a lightweight sequential loop inside `frontend_fastapi` or new backend queue support, neither designed here.
- A connectivity/health indicator (e.g. "Conquest reachable: yes/no") on the browse page — not requested, easy to add later using F001's C-ECHO primitive.

## Out of scope

- Any relay of moved data through Orthanc's own storage.
- Registering new DICOM peers with Conquest itself (ops-managed, external to this repository, same as every other DICOM modality registration in this codebase).
- Layering `require_project_member`/`require_any_active_project` on top of the custodian gate for the move action.
- In-browser (client-side JS) DICOM networking — confirmed infeasible; all DICOM traffic originates server-side from `frontend_fastapi`.
