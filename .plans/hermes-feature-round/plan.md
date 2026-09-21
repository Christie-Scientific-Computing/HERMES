# Implementation Plan

## Goal

Ship the seven items from `FEATURES.md`: configurable job settings, self-service
password change, a clearer patient event timeline, project-level export
governance (destinations + message ID, with an amendment workflow), an
error-reporting page, and a cleaner frontpage jobs table. All product and
behavioural decisions were resolved in a prior grilling session (full
transcript: `GRILLING.md`; confirmed-decisions summary: the published design
artifact and `docs/plans/feature-round-implementation-plan.md`) — this plan
decomposes that already-agreed behaviour into independently implementable
features with acceptance criteria.

## What we discussed

- All frontend work targets `frontend_fastapi/` — the production frontend as
  of the Phase 5 cutover. `frontend/` (Django) is burn-in-only and gets no
  new features.
- MU tolerance and message ID, originally bundled as one "advanced options"
  request, were explicitly decoupled during grilling: MU tolerance is a
  plain per-job field; message ID became project-defined and read-only at
  job time.
- Export destination and message ID share the same governance: set at
  project creation, reviewed at approval, changeable afterward only via an
  amendment that reopens approval — but the project keeps operating on its
  current values while an amendment is pending.
- Error-report email-on-urgent is explicitly deferred: `frontend_fastapi`'s
  SMTP isn't functional today despite the settings existing.

## Decisions

- **D001 — MU tolerance and message ID are decoupled:** MU tolerance is a
  plain per-job float field (env-var default, user-overridable); message ID
  is defined once per project and shown read-only on the job form, never
  client-editable. (Confirmed in grilling, reversing the original combined
  "advanced options" request.)
- **D002 — Password change requires the current password and invalidates
  every other session for that user on success.** Sessions here are
  hand-rolled/DB-backed with no automatic password-tied invalidation, so
  this must be an explicit step, not something inherited from a framework.
- **D003 — Patient timeline shows one row per attempt** (Start Time / End
  Time), replacing the old one-row-per-event table. Paired via `task_id`
  where present (queue-driven jobs — reads `tasks.started_at`/`finished_at`
  directly); falls back to matching `(job_id, mrn, stage, attempt)` for the
  still-synchronous single-item path.
- **D004 — Export destination is a two-step choice:** pick a type (DICOM SCP
  or ProKnow) first, then multi-select from that type's own list. Selections
  of both types may coexist on one project.
- **D005 — Destination(s) and message ID share one governance model:** set
  at creation, reviewed at approval, amendable only via a workflow that
  reopens approval, staying live on current values until the amendment is
  decided.
- **D006 — Amendment mechanism (this planning pass):** a `status` column
  (`active` / `proposed`) on a new `project_destinations` table, and a
  `research_projects.pending_message_id` column — not a new project
  lifecycle state. Approving an amendment promotes `proposed` rows/value and
  retires the old ones; rejecting discards the `proposed` rows/value. Chosen
  as the smallest change achieving the confirmed "stays live until approved"
  behaviour without a new status value rippling through `ProjectsDB`'s
  existing `draft`/`submitted`/`approved`/`rejected`/`revoked` lifecycle.
- **D007 — "Sent to destination" counts unique patients per destination**,
  not raw export events (a patient exported twice to the same destination
  counts once).
- **D008 — Error reports are backend-owned (HermesDB)**, mirroring the
  existing `notifications` table shape; the admin log reuses
  `require_data_custodian`.
- **D009 — Email-on-urgent is deferred**, not built this round.
- **D010 — All new UI ships in `frontend_fastapi/` only.**

## Implementation overview

1. **F001 — Set MU tolerance per job** ([`features/F001-mu-tolerance-per-job.md`](features/F001-mu-tolerance-per-job.md)): a plain, top-level job-submission field, env-var default, threaded toward PinnacleExport's payload.
2. **F002 — Display project message ID on job form** ([`features/F002-display-project-message-id.md`](features/F002-display-project-message-id.md)): read-only display of the project's governed message ID alongside the chosen destination; removes the old client-editable field.
3. **F003 — Change own password** ([`features/F003-change-own-password.md`](features/F003-change-own-password.md)): self-service password change requiring the current password, invalidating other sessions.
4. **F004 — Paired start/end patient timeline** ([`features/F004-paired-patient-timeline.md`](features/F004-paired-patient-timeline.md)): merges start/completion events into one row per attempt with Start/End times.
5. **F005 — Mandatory project description** ([`features/F005-mandatory-project-description.md`](features/F005-mandatory-project-description.md)): project creation requires a description.
6. **F006 — Approved export destinations & message ID** ([`features/F006-approved-export-destinations-and-message-id.md`](features/F006-approved-export-destinations-and-message-id.md)): two-step destination picker, project message ID, and the amendment-reopens-approval workflow.
7. **F007 — Optional patient-ID-list upload at project creation** ([`features/F007-project-patient-list-upload.md`](features/F007-project-patient-list-upload.md)): upload a list of requested MRNs when creating a project.
8. **F008 — Project overview stats** ([`features/F008-project-overview-stats.md`](features/F008-project-overview-stats.md)): Requested / Restored / Sent-to-destination counts and colour-coded expiry on the project detail page.
9. **F009 — Submit an error report** ([`features/F009-submit-error-report.md`](features/F009-submit-error-report.md)): logged-in users file a feedback/error report, optionally flagged urgent, optionally tied to a job.
10. **F010 — Admin error-report log** ([`features/F010-admin-error-report-log.md`](features/F010-admin-error-report-log.md)): staff view the submitted reports.
11. **F011 — Jobs table with counts on the frontpage** ([`features/F011-jobs-table-with-counts.md`](features/F011-jobs-table-with-counts.md)): replaces the raw job-id/description/created table with a cleaned description and import/export counts.

## Dependencies

```text
F001 ─────────────────────────(independent)
F003 ─────────────────────────(independent)
F004 ─────────────────────────(independent)
F005 ─────────────────────────(independent)
F009 ─────────────────────────(independent)
F011 ─────────────────────────(independent)

F006 ──> F002
F006 ──┐
F007 ──┼──> F008
        (F006, F007 both independent of each other)

F009 ──> F010
```

Suggested build order (matches the grilling session's confirmed sequencing):
**F003 → F004 → F005 → F006 → F007 → F008 → F009 → F010 → F001 → F002.**
F011 is fully independent — buildable at any point, including first if a
small warm-up PR is wanted.

## Open issues

- Exact schema of `frontend_fastapi`'s `sessions` table re: linking a row to
  a user (needed for F003's "invalidate all other sessions") —
  implementation agent should verify against `frontend_fastapi/models.py`
  before writing the delete query.
- Exact JSONB path recording destination on an export event today (assumed
  `events.details->>'destination'`) — verify before building F008's
  per-destination `GROUP BY`.

## Assumptions

- ProKnow "collections" are enumerable via the same source that already
  powers `ProKnowExportForm.collection` on the existing job-submission form —
  F006 should reuse that rather than adding a second lookup.
- The patient-ID list (F007) is plain text or single-column CSV, one MRN per
  line — consistent with how batch-import CSVs are already shaped elsewhere.

## Limitations

- MU tolerance (F001) is threaded through HERMES's own contract only;
  PinnacleExport's `entry()` call doesn't yet consume it. The value has no
  real effect until that submodule gets a matching change — out of scope for
  this repo.

## Deferred decisions

- Sending an email when an error report is marked urgent (D009) — deferred
  until `frontend_fastapi`'s SMTP is actually working.

## Out of scope

- Any changes to the legacy Django `frontend/` app.
- Changes to the `PinnacleExport` submodule itself.
- A broader "my account" page beyond F003's bare change-password form.
