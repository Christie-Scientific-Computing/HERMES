# Feature F003: Manage local PACS move destinations

## Purpose

Gives data custodians a curated, safe set of destinations to choose from when relaying a study off Conquest (F004), instead of free-text AE entry. This is purely a labelling/curation layer inside HERMES — it does not itself register anything with Conquest (see plan.md's Out of scope); Conquest must already be able to reach any AE title offered here, which is an ops-managed prerequisite outside this repository, mirroring how every other DICOM modality registration already works in this codebase (`PULL_MODALITY_AET_ONE`/`TWO`, Orthanc's own modality registry).

## Behaviour

- A new admin-facing page lists the current set of local-PACS move destinations, each with an AE title, a friendly display name, and an optional description.
- Data custodians can add, edit, and remove destinations from this list.
- The list is what F004's move UI offers as destination choices (a dropdown, not free text).
- Only data custodians (`is_staff`) can view or manage this page — this is an admin tool, not something every user needs to see.

## Dependencies

None — this is a self-contained CRUD feature against `frontend_fastapi`'s own local database, independent of Conquest connectivity or the audit trail.

## Relevant code

### Existing

- `frontend_fastapi/models.py` — this project's own SQLAlchemy models (`User`, `Session`, `ProjectDocument`) and their Alembic-migrated table pattern; the new destinations table follows the same approach.
- `frontend_fastapi/alembic/versions/` — existing migrations for this project's local DB (e.g. `7d914f970eaf_initial_schema_users_sessions_project_.py`, `2d3c255c556a_widen_users_department_to_200_chars.py`) — the pattern a new migration for this table should follow.
- `frontend_fastapi/migrations.py` — runs `alembic upgrade head` against this project's own DB at startup; no separate migrate step needed once the migration file exists.
- `frontend_fastapi/deps.py:113-116` — `require_data_custodian`, the exact gate this feature's pages need; no new authorization code required.
- `frontend_fastapi/routers/admin.py` — an existing `require_data_custodian`-gated admin page, useful as a structural example (though it's read-only reporting, not CRUD).
- `frontend_fastapi/routers/accounts.py` (user CRUD: invite/create-user/user-list) — the closest existing example of a custodian-facing CRUD-style management page in this app, worth reviewing for form/template conventions.

### Likely changes

- `frontend_fastapi/models.py` — add a new model, e.g. `LocalPacsDestination` (`id`, `ae_title`, `display_name`, `description`, `created_at`, maybe `created_by`).
- New Alembic migration under `frontend_fastapi/alembic/versions/` creating that table.
- New: `frontend_fastapi/routers/local_pacs_destinations.py` (or folded into `routers/local_pacs.py` under an `/admin`-style sub-path — implementer's call, following whatever this app's existing convention favours for admin-only sub-pages of a feature).
- New: `frontend_fastapi/forms/` addition for add/edit destination forms.
- New: `frontend_fastapi/templates/` addition(s) for the list/add/edit views.

## Acceptance criteria

### Scenario: A custodian adds a new destination

**Given** a data custodian on the destination management page

**When** they add a destination with an AE title and display name

**Then** it appears in the list and becomes selectable in F004's move destination dropdown

### Scenario: A custodian edits a destination's display name

**Given** an existing destination

**When** a custodian edits its display name or description

**Then** the change is saved and reflected immediately in both the management list and F004's dropdown

### Scenario: A custodian removes a destination

**Given** an existing destination

**When** a custodian deletes it

**Then** it no longer appears in the management list or in F004's dropdown

### Scenario: A non-custodian cannot access destination management

**Given** a logged-in user who is not a data custodian

**When** they attempt to reach the destination management page

**Then** they are forbidden, matching every other `require_data_custodian`-gated page in this app

## Edge cases

- AE title validation: DICOM AE titles have length/character constraints (up to 16 characters, specific allowed character set) — the add/edit form should validate this rather than accepting anything.
- Duplicate AE titles: decide (implementer's call, no strong product requirement surfaced during planning) whether to prevent two destinations sharing an AE title or allow it with distinct display names; preventing duplicates is the simpler and safer default.
- Removing a destination that a past move's audit record references (F005's `events.details` would have recorded the AE title/display name at the time of the move, not a live foreign key to this table) — deleting a destination here must not break past audit records, since F005 should not depend on this table still containing the row at read time.

## Success

A data custodian can fully manage the destination list (add/edit/remove) through the UI, and F004's move action reflects the current list without any code change or restart.

## Failure behaviour

Invalid input (malformed AE title, empty display name) is rejected with a clear inline form error, consistent with this app's existing form-validation conventions (`forms/` modules).

## Testing considerations

Follow `frontend_fastapi/tests/`'s pytest conventions — see `test_accounts_create_user.py` or similar existing CRUD-page tests for the pattern (form validation, gating, persistence, list rendering). Uses this project's own local DB fixtures (SQLite in-memory or throwaway Postgres via `HERMES_FRONTEND_DATABASE_URL`), not HermesDB.

## Implementation notes

- This table lives in `frontend_fastapi`'s own local database (alongside `users`/`sessions`/`project_documents`), not HermesDB — it is UI configuration, not job/event/audit data.
- Keep the AE-title-to-actual-DICOM-reachability distinction visible somewhere in the UI copy (e.g. help text: "Conquest must already be configured to reach this AE title — adding it here only adds it to this list") so custodians don't mistake this for actually registering a new DICOM peer.

## Out of scope

- Registering the destination as an actual DICOM peer with Conquest (ops-managed, external).
- Any validation that a destination is actually reachable at add-time (a "test connection" feature could reuse F001's C-ECHO against the destination, but wasn't requested — flagged in plan.md's Deferred decisions if wanted later).
