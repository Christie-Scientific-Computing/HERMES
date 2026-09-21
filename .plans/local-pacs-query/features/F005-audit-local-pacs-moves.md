# Feature F005: Audit local PACS moves in HermesDB

## Purpose

Gives every local-PACS move the same tamper-evident audit trail every other data-moving action in HERMES gets, even though the DICOM transfer itself happens entirely in `frontend_fastapi`, outside HermesDB's normal job/task pipeline. Without this, custodian-initiated relays off Conquest would be the one data-moving action in the whole system with no record in `events`.

## Behaviour

- A new backend endpoint accepts a report of one completed (or failed) local-PACS move: the anonymised patient ID, the study/series identifiers moved, the destination AE title, who performed it, and the outcome.
- The endpoint resolves the anonymised ID to the real MRN (via `identity/anon.py`'s existing `resolve_real_id`) before writing anything, since HermesDB's `events`/`patients` tables store real MRNs by convention (CLAUDE.md's HermesDB Schema section) — everywhere else in this codebase, that translation happens once, generically, at the API boundary, and this endpoint follows the same rule.
- The endpoint writes a `jobs` row (if one doesn't already exist for this move), a `patients` row, and an `events` row — the exact same shape every other job/patient/event trio in HermesDB takes, going through the existing hash chain (`StatusDB.add_event`).
- Every job written this way is tagged with a fixed sentinel `project_id` (`"localPACSTransfer"`) rather than a real ethics-approved project. The corresponding `research_projects` row is auto-provisioned on first use if it doesn't already exist (idempotent), permanently approved, mirroring the existing superuser-bypass-project pattern (`frontend_fastapi/backend_client.py`'s `_find_or_create_superuser_bypass_project`) but without creating any project membership — this endpoint is never reached through `require_project_member`, so no membership check ever runs against it.
- This endpoint is called by `frontend_fastapi` (F004) after attempting a move, whether the move succeeded or failed — both outcomes get recorded.
- Access control: this endpoint is gated the same way `error_reports`'s creation endpoint is — `verify_internal_key` only (the shared-secret header), no per-user role check, because HermesDB has no user/role table of its own to check against. The actual custodian-only authorization already happened in `frontend_fastapi` (F004, via `require_data_custodian`) before this endpoint is ever called; this endpoint trusts that a request bearing the correct internal key is legitimate, exactly like every other backend endpoint in this app.

## Dependencies

None — this is a self-contained backend addition (new endpoint + sentinel-project bootstrap) that can be built and tested independently of F001-F004, using synthetic inputs. F004 depends on it, not the reverse.

## Relevant code

### Existing

- `backend/src/error_reports/endpoints.py` — the closest existing structural analogue: a small router, `verify_internal_key`-only gating, no role check (module docstring explicitly explains why: "HermesDB has no user/role table to check against"). Model this endpoint's shape on it.
- `backend/src/status/db_client.py` — `StatusDB.create_job`, `add_patient`, `add_event` (lines 16-87) — the exact methods this endpoint calls to write the job/patient/event trio and extend the hash chain. `create_job` already accepts a `project_id` parameter.
- `backend/src/identity/anon.py` — `resolve_real_id(anon_id) -> str` (line 378) — the translation this endpoint must call before writing any MRN to HermesDB. In passthrough mode (no `ANON_DB_*`/`ANON_CONFIG` configured), it returns the id unchanged, so no special-casing is needed for non-anonymising deployments.
- `frontend_fastapi/backend_client.py:200-263` — the existing "superuser bypass project" auto-provisioning pattern (`_find_or_create_superuser_bypass_project`, `ensure_superuser_bypass_project`) — the closest existing precedent for "auto-provision a permanently-approved sentinel `research_projects` row on first use." That existing code creates the project via HTTP calls to `/projects` from the frontend side; this feature's sentinel-project bootstrap is simpler and can be done directly against `ProjectsDB`/HermesDB from inside the backend process itself, since the new endpoint already lives there — no need for a self-referential HTTP round trip.
- `backend/src/projects/db_client.py` (`ProjectsDB.create_project`, `is_project_active`) and `backend/alembic/versions/8aa3a51c978c_add_research_projects_and_membership.py` — `research_projects` schema and the `jobs.project_id` foreign key (nullable, but FK-constrained) this sentinel row satisfies.
- `backend/main.py:45-52` — where routers get registered (`app.include_router(...)`); the new router needs the same treatment.
- CLAUDE.md's HermesDB Schema section — confirms `mrn` columns store the real patient ID by convention, and that `jobs.project_id` "traces a job back to the ethics-approved project that authorized it" (this sentinel project is a deliberate, documented exception to that framing — it authorizes nothing; F005/F004's custodian gate is what authorizes the move).

### Likely changes

- New: `backend/src/local_pacs/endpoints.py` — the new router (structurally close to `error_reports/endpoints.py`).
- New: a small helper (in that module or `backend/src/local_pacs/db_client.py`) to idempotently ensure the `"localPACSTransfer"` sentinel project exists — one-time creation, not per-call.
- `backend/main.py` — register the new router.
- Possibly `backend/src/status/db_client.py` — only if an existing `StatusDB` method doesn't cleanly cover a "record a single already-completed action as one job+patient+event" shape (the existing methods look sufficient: `create_job` once, `add_patient` once, `add_event` once per move); implementer should confirm no new method is actually needed before adding one.

## Acceptance criteria

### Scenario: A successful move is recorded

**Given** a local-PACS move completed successfully in `frontend_fastapi`

**When** the audit endpoint is called with the move's details (anon patient ID, study/series identifiers, destination, performed-by)

**Then** a `jobs` row (tagged `project_id = "localPACSTransfer"`), a `patients` row, and a success `events` row (with the real MRN, resolved from the anon ID) are written, extending the hash chain

### Scenario: A failed move is recorded

**Given** a local-PACS move attempt failed in `frontend_fastapi`

**When** the audit endpoint is called reporting that failure

**Then** a corresponding failure `events` row is written (same job/patient bookkeeping), so failed attempts are visible in the audit trail too, not just successes

### Scenario: The sentinel project is created once, reused thereafter

**Given** no `research_projects` row with `project_id = "localPACSTransfer"` exists yet

**When** the first local-PACS move is audited

**Then** that row is created (permanently approved, no real expiry) and every subsequent move reuses it rather than creating duplicates

### Scenario: Anonymised ID is translated before storage

**Given** an anonymising deployment (`ANON_DB_*`/`ANON_CONFIG` configured) and a move reported against an anonymised patient ID

**When** the audit endpoint processes it

**Then** the `events`/`patients` rows store the real MRN, not the anonymised ID — consistent with every other write to these tables

### Scenario: A request without the internal key is rejected

**Given** `HERMES_INTERNAL_KEY` is configured on the backend

**When** a request to this endpoint arrives without a matching `X-Hermes-Internal-Key` header

**Then** it is rejected, matching every other `verify_internal_key`-gated router in this codebase

## Edge cases

- An anon ID that doesn't resolve to any known real ID (unmapped/unknown) — should fail closed with a clear error (422, matching `identity/anon.py`'s documented convention: "failing closed with a 422 on unknown IDs"), not silently write a bogus or null MRN.
- Two workers/processes racing to create the sentinel project for the very first move ever recorded — should not produce a hard failure; either an idempotent `ON CONFLICT DO NOTHING`-style creation or a tolerated harmless duplicate (mirroring the accepted race already documented in `backend_client.py`'s own sentinel-project bootstrap) is acceptable.
- This endpoint's `job_id` — decide (implementer's call) whether each move gets its own fresh `job_id`, or whether related moves could share one; a fresh `job_id` per move is the simpler default and matches "single-study synchronous" (F004) cleanly — one job, one patient, one event, one move.

## Success

Every local-PACS move (success or failure) produces a corresponding, hash-chained `events` row visible through the existing job-detail/results-lookup UI, under the `"localPACSTransfer"` project, without requiring any ethics-project membership to have existed.

## Failure behaviour

If this endpoint itself fails (HermesDB unreachable, write error), it returns a clear error response to its caller (`frontend_fastapi`/F004) — per plan.md D006, F004 is responsible for turning that into a "move succeeded, but the audit record failed to save" warning to the user, not this endpoint's concern.

## Testing considerations

Follow `backend/tests/`'s pytest conventions (needs a real Postgres per `conftest.py`'s guard rails — see CLAUDE.md's Testing section). Model tests on `backend/tests/test_error_reports_endpoints.py` for the endpoint shape, and on whatever `ProjectsDB`/sentinel-project tests exist (or the superuser-bypass logic, if it has frontend-side tests worth mirroring) for the idempotent-creation behaviour. Verify hash-chain continuity the same way `test_hash_chain.py` does for other event-writing paths.

## Implementation notes

- This endpoint is the only place in this plan that touches HermesDB directly — keep it that way; F001-F004 should have no direct HermesDB access, only this endpoint (called over HTTP from `frontend_fastapi`, via a new `backend_client.py` function).
- Do not route this action through `require_project_member`/`require_any_active_project` (`backend/src/projects/enforcement.py`) — per D005, custodian status (already checked in `frontend_fastapi`) is the entire authorization story; this endpoint only records, it does not authorize.

## Out of scope

- Any authorization decision — this endpoint trusts its caller (via `verify_internal_key`) and records what it's told; F004 (calling from `frontend_fastapi`, after `require_data_custodian`) is where authorization actually happens.
- Per-instance checksums/manifests (plan.md's documented Limitations) — this endpoint records whatever metadata F004 can actually obtain from a C-FIND/C-MOVE response, not instance-level data HERMES never touches.
