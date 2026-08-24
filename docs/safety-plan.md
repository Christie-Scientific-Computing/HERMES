# Safety plan — pseudonymised export governance

**Status:** v3, converged after two rounds of sub-agent critique against the
live codebase. See "Revision history" at the bottom for what changed and why.
Follows the architecture review (repo memory, not checked in) and the author's
scoping decisions in response to it. This plan does **not** cover DICOM-level
de-identification (handled by a separate, pre-existing node downstream of
central Orthanc) and does not treat pseudonymisation as a defect — both are
explicitly out of scope, per the author.

## What this plan is responding to

| # | Finding | Author's decision | Covered here |
|---|---|---|---|
| 1 | No way to restrict which destinations a user may export to | Wants it built | §A |
| 2 | Terminology: pseudonymisation, not anonymisation | No action — confirmed correct as-is | — |
| 3 | Backend→`anon_db` link crosses into the DMZ unhardened | Wants it addressed, but TLS/cert-pinning and a restricted DB role aren't fully within HERMES's control | §B |
| 4 | Project approval has no cohort/volume bound, and doesn't say so | Wants the proposed banner copy shipped | §C |
| 5 | A pre-flight review doesn't fit a future single-step import+export | Not solvable yet — availability isn't known until import finishes. Document as a known limitation | §F (deferred) |
| 6a | Audit trail doesn't record what left | Wants it implemented, including a post-import Orthanc query for what's actually available to export | §D |
| 6b | No patient-level import outcome reporting (found/not found, why, per source) | Wants it implemented, with a per-source reason and a top-line "N/M imported" figure | §E |
| 7 | Admin/superuser access isn't restricted in code | Accepted as an operational control (small, known-trustworthy admin group), not a code change | §F (deferred) |

---

## §A — Per-user export destination allow-list

**Goal:** an admin can restrict which Orthanc modalities / ProKnow collections a
given user is allowed to target, independent of project membership (which
governs *whether* someone may export at all, not *where*).

### Schema (HermesDB, new Alembic migration)

Both existing migrations (`12852434505f_...py`, `8aa3a51c978c_...py`) build
tables via `op.create_table()`/`sa.Column()`, never raw SQL strings — match
that shape, not a hand-written `CREATE TABLE`:

```python
def upgrade() -> None:
    op.create_table(
        "user_export_destinations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("destination_type", sa.Text(), nullable=False),
        sa.Column("destination", sa.Text(), nullable=False),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "destination_type IN ('dicom_modality', 'proknow_collection')",
            name="ck_user_export_destinations_type",
        ),
        sa.UniqueConstraint("username", "destination_type", "destination"),
    )
    op.create_index(
        "ix_user_export_destinations_username", "user_export_destinations", ["username"]
    )
```

Column-level conventions (plain TEXT `username`, no FK — same reasoning as
`project_memberships.username`: Django is the only source of truth for user
identity, per `backend/src/projects/db_client.py`'s own docstring) match the
existing tables; only the migration file's own shape needed correcting here.

### Backend

- New module `backend/src/access/db_client.py` (`AccessDB`), mirroring
  `ProjectsDB`'s shape: `list_for_user`, `add`, `remove`, `is_allowed(username, destination_type, destination)`.
- `is_allowed` semantics — **opt-in allow-list, not fail-closed by default**:
  if a user has zero rows, treat as "no restriction configured" and allow any
  destination (today's behavior, unchanged). Once a user has ≥1 row, only
  those destinations are allowed. This matches the codebase's existing
  idiom for optional hardening (`ANON_DB_HOST` unset → passthrough,
  `HERMES_INTERNAL_KEY` unset → no-op) rather than introducing a new pattern.
  **Known limitation, stated explicitly**: this means "not yet restricted" and
  "deliberately left open" are indistinguishable at the database level. A
  later hardening pass could add an explicit per-user `restricted BOOLEAN`
  flag if that ambiguity becomes a real problem; not needed for v1.
- New dependency in `backend/src/projects/enforcement.py`:
  `require_allowed_destination(username, destination_type, destination)`,
  fail-closed on DB error (same discipline as `require_project_member`).
- Wire into `backend/src/export/endpoints.py`, immediately after the existing
  `enforcement.require_project_member(...)` call, in every handler that
  accepts a `destination`/`collection`: `dicom_move`, `dicom_move_file`,
  `dicom_move_uids_file`, `proknow_upload`, `proknow_upload_file`,
  `proknow_upload_patient`.
- New router `backend/src/access/endpoints.py` (`/access` prefix, same
  `verify_internal_key` dependency as `/export`/`/import`/`/projects`):
  `GET /access/{username}`, `POST /access/{username}`, `DELETE /access/{username}/{id}`.
  No staff/admin check in the backend itself — same posture as
  `research_projects` review endpoints, which trust Django to have already
  gated on `is_staff` before calling in.

### Frontend

- New Django view(s), staff-only (`@user_passes_test(_is_data_custodian)`,
  matching `research_projects/views.py`'s existing pattern), e.g.
  `accounts:user_access` — a single page listing a user's current allowed
  destinations with add/remove forms. Populate the "add" dropdown from the
  same live calls the export form already uses
  (`backend_client.get_orthanc_modalities`/`get_proknow_collections`) so an
  admin picks from real, currently-registered destinations rather than typing
  free text.
- Deliberately kept to a plain table + two small forms (list + add-row +
  per-row remove button) — no JS beyond what `frontend/`'s existing
  CSRF-protected POST pattern already needs. This is a small enough surface
  that it stays cheap to port under the frontend-migration plan (`docs/frontend-migration.md`)
  regardless of which stack wins.

### Open question for the reviewing sub-agent

Where should this page hang off the nav — under `accounts/` (it's fundamentally
a user-permissions concept) or under a new top-level `access/` app? Leaning
`accounts/`, since `_is_data_custodian` and user-facing identity already live
there conceptually, but there's no dedicated Django app for it yet — confirm
this doesn't collide with something already planned.

---

## §B — Hardening the backend → `anon_db` link

Revised after pushback: certificate pinning and a restricted DB role are not
straightforwardly implementable without cooperation from a team the author
doesn't administer. Split into what's fully within HERMES's own control and
what has to be filed as an external request.

### B1 — Within HERMES's control (no external dependency)

- **Standard TLS, not pinning.** Add `ANON_DB_SSLMODE` (default unset →
  today's behavior, unchanged) to `backend/src/identity/anon.py`, passed as
  `sslmode` to `psycopg2.pool.SimpleConnectionPool`. If the `anon_db` server
  already has TLS available (common for a managed Postgres instance — worth
  checking before assuming it needs new work), `sslmode=require` needs
  **no coordination at all**: it encrypts the connection using whatever
  certificate the server already presents, with no pinning and nothing to
  update if that certificate rotates. `verify-full` (stronger — also confirms
  server identity) needs a one-time ask: a copy of the CA/root certificate
  the `anon_db` operator's cert chains to, passed via a new `ANON_DB_SSLROOTCERT`
  path. Neither of these is certificate pinning — normal PKI validation only
  needs updating if the *CA itself* is ever replaced, which is rare, unlike a
  pinned leaf certificate that must be updated every renewal. This corrects
  the previous draft's recommendation, which conflated the two and
  overstated the operational burden.
- **Application-side lookup-volume monitoring.** Wrap `_query` in
  `backend/src/identity/anon.py` with a rolling counter (in-process is enough
  to start; HermesDB is HERMES-owned if a persistent counter is wanted later)
  and log a warning past a configurable threshold (e.g. N lookups/hour).
  This needs no cooperation from anyone — it's pure HERMES application code —
  and is the most useful of the three original recommendations to actually
  ship, since it's the one that would surface a bulk-exfiltration attempt in
  something someone might actually look at.

### B2 — Needs a request to the team that owns `anon_db`

Track as an open item, **not** a HERMES engineering task with a HERMES
deadline:

- A least-privilege Postgres role scoped to `SELECT` on `key_value` only
  (today's connection presumably uses a role with broader rights, since
  nothing in `backend/src/identity/anon.py` suggests otherwise).
- If `verify-full` is wanted: the CA/root certificate for that server (a
  one-time file, not an ongoing maintenance burden — see B1).

Suggested wording for that request, since it's a small, bounded ask:

> *"Could HERMES get a dedicated Postgres role on `key_value`'s database,
> scoped to `SELECT` only on that one table? Separately, if the server has
> TLS configured, could we get a copy of the CA certificate it presents, so
> our client can verify it? Neither needs ongoing maintenance from your side
> once set up."*

---

## §C — Approval-page warning

Confirmed copy (author approved as-is):

**Banner**, `frontend/research_projects/templates/research_projects/detail.html`,
inside the existing `{% if project.status == "submitted" and user.is_staff %}`
"Review decision" card (`views.py:103` `project_review`), placed above the
`<form>` so it's the last thing a reviewer reads before submitting a decision:

> Approving this project lets its members export pseudo-anonymised data for
> any patient, in any volume, for as long as the approval is active. There is
> currently no way to limit exports to a specific cohort — membership is the
> only control in place.

**Docs**, `CLAUDE.md`, under "Known Gaps in Code" (or a new short note under
"Ethics-gate enforcement" in the Key Design Patterns section):

> Project approval is a single yes/no gate on tool access, not a bound on
> which patients or how much data a member may subsequently export. Approving
> a project is equivalent to trusting every member with unrestricted
> pseudo-anonymised export for the life of the approval.

No schema/enforcement change — this is copy only. §A (destination allow-list)
doesn't change this: a destination-restricted user can still export any
cohort, any volume, to whichever destinations they *are* allowed.

---

## §D — Audit trail: hash-chained events + export manifest + post-import verification

### D0 — Prerequisite: the narrow `Response` models silently drop every new field

**Found by review, and load-bearing for D2/D3/E below — not optional.**
`backend/src/retrieve/endpoints.py:45-50` and `backend/src/export/endpoints.py:60-62`
each declare a fixed-field `Response(BaseModel)` (import's has `mrn`/`status`/
`in_mosaiq`/`in_pinnacle`/`in_proknow`; export's has only `mrn`/`status`).
Every batch worker — `_import_worker` (`retrieve/endpoints.py:66-70`),
`_dicom_move_worker`/`_proknow_worker` (`export/endpoints.py:89-100`) — routes
its raw result dict through `Response(mrn=..., **res).model_dump(exclude={"mrn"})`
*before* handing it back to `run_batch_job`. Pydantic 2 silently drops any key
not declared on the model (confirmed against this repo's pinned `pydantic==2.13.4`,
no `model_config` override in either file) — so `imported`/`study_count`/
`study_uids` (D3), the `*_reason` fields (E), and the manifest fields (D2)
would all be silently stripped before ever reaching `run_batch_job`, breaking
the `events.details` write **and** the SSE payload, for every batch endpoint
(`batch_import`, `batch_import_file`, `dicom_move`, `dicom_move_file`,
`dicom_move_uids_file`, `proknow_upload`, `proknow_upload_file`) — i.e. the
primary code paths, not an edge case. Single-item endpoints
(`single_import`, `proknow_upload_patient`) are partly spared for their
StatusDB write (they pass the raw worker dict to `add_event` directly,
bypassing `Response` for that call) but their own HTTP response, and
`find_patient`'s (`retrieve/endpoints.py:152-176`, typed `-> Response`), still
go through the same narrow model.

**Fix, required before D2/D3/E can work end-to-end:** expand both `Response`
models with the new fields as `Optional`, defaulting to `None`, so existing
callers that don't set them are unaffected:

```python
# backend/src/retrieve/endpoints.py
class Response(BaseModel):
    mrn: str | int
    status: str | None = None
    in_mosaiq: bool | None = None
    in_pinnacle: bool | None = None
    in_proknow: bool | None = None
    mosaiq_reason: str | None = None
    pinnacle_reason: str | None = None
    proknow_reason: str | None = None
    imported: bool | None = None
    study_count: int | None = None
    study_uids: list[str] | None = None
```

```python
# backend/src/export/endpoints.py
class Response(BaseModel):
    mrn: str | int
    status: str | None = None
    series_count: int | None = None
    instance_count: int | None = None
    study_uids: list[str] | None = None
    series_uids: list[str] | None = None
    checksums: dict[str, str] | None = None
    destination: str | None = None
    destination_type: str | None = None
    submitted_by: str | None = None
```

(`submitted_by` was missing from the first cut of this list — D2 asks for it
explicitly below; caught on the confirmation review pass, included here now.)

Explicit fields, not a single catch-all `extra: dict`, deliberately: it keeps
`events.details`/the SSE payload flat (no `details.extra.imported` indirection
for every future consumer — the frontend templates in E read these directly),
matching the shape every existing field on both models already uses.

### D1 — Hash-chained `events`

New Alembic migration, matching the existing `op.create_table`/`op.add_column`
style (not raw SQL):

```python
def upgrade() -> None:
    op.add_column("events", sa.Column("prev_hash", sa.Text(), nullable=True))
    op.add_column("events", sa.Column("row_hash", sa.Text(), nullable=True))
    op.create_table(
        "event_chain_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("last_hash", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_event_chain_state_singleton"),
    )
    op.execute(
        "INSERT INTO event_chain_state (id, last_hash) VALUES (1, encode(sha256(''::bytea), 'hex'))"
    )
```

(`prev_hash`/`row_hash` are `nullable=True` because existing rows predate the
chain — they stay `NULL`, and the chain simply starts fresh from the first
post-migration event. Not a gap worth solving: nothing before this plan
shipped was tamper-evident anyway, so there's nothing to retroactively chain.)

`StatusDB.add_event` (`backend/src/status/db_client.py`) changes to, within
one transaction: `SELECT last_hash FROM event_chain_state WHERE id = 1 FOR UPDATE`
(the row lock serialises concurrent writers — necessary since multiple
uvicorn workers can call `add_event` at the same time, and the chain must
have one, and only one, valid next link), compute
`row_hash = sha256(prev_hash || canonical_json(job_id, mrn, stage, event_type, ts, attempt, error_message, details))`,
insert the event with both hashes, then `UPDATE event_chain_state SET last_hash = row_hash`.

**Throughput cost, flagged by review and worth stating plainly:** unlike the
domain `worker(item)` call in `run_batch_job` (`backend/src/common/sse.py:140`,
wrapped in `asyncio.to_thread`), the `status_db.add_patient`/`add_event` calls
around it run as plain synchronous calls directly on the event loop
(`sse.py:130-135`, `143-147`, `161-165`) — CLAUDE.md's "async threading"
description only holds for the worker call, not these. Adding a `FOR UPDATE`
row lock to `add_event` means every concurrently-running job's event writes
now serialise on one global lock, held on the event-loop thread rather than a
worker thread — a real availability cost under concurrent batch jobs, not
just a correctness detail. **Recommendation:** wrap the DB calls inside
`add_event`/`add_patient`/`create_job` in `asyncio.to_thread` as part of this
same change (a small, justified fix riding along with D1, not a separate
workstream), so lock contention blocks a thread-pool worker, not the loop
that every other concurrent request also depends on.

**Correction from v2 — that recommendation, as written, introduces a real
race condition and needs a companion fix.** `backend/src/db.py:16,26` builds
HermesDB's shared pool as `psycopg2.pool.SimpleConnectionPool`, whose own
docstring says it "can't be shared across different threads" — `getconn`/
`putconn` do no locking. That's safe *today* only because every call into it
happens synchronously on the single asyncio event-loop thread (no `await`
inside `add_event`, so cooperative scheduling means one call always finishes
before another starts, even with many concurrent jobs). Moving these calls
into `asyncio.to_thread` — as just recommended, to get lock contention off
the event loop — puts genuinely concurrent OS threads through `getconn`/
`putconn` for the first time, against a pool that was never built to allow
that: unsynchronized mutation of the pool's internal connection-tracking
state, under load, is a real bug (double-checkout, connection leaks, or a
crash), not a hypothetical. **This has to ship together with the
`asyncio.to_thread` change, not as an afterthought:** swap
`SimpleConnectionPool` → `psycopg2.pool.ThreadedConnectionPool` in
`backend/src/db.py` (same constructor signature — `ThreadedConnectionPool`
only adds an internal `threading.Lock()` around `getconn`/`putconn` on top of
the same base class). Small, contained change, but load-bearing: D1 as
originally revised traded a documented, bounded problem (event-loop
contention) for an unbounded one (a pool race) — this closes that.

A hash chain is only useful if something checks it. Add a small, separate
verification entrypoint (e.g. `backend/scripts/verify_audit_chain.py`, run
periodically or on demand) that walks `events` in `id` order, recomputes each
`row_hash`, and reports the first mismatch. This is the piece that actually
answers "has this record been altered" — the columns alone don't.

**Known limitation to state, not solve, in this plan:** a hash chain proves
*the row wasn't silently altered after being written*. It does not stop
someone with direct DB access from truncating the table and re-seeding
`event_chain_state`, or from disabling the trigger/application logic that
maintains the chain in the first place. That requires a separate control
(e.g. shipping a periodic copy of `last_hash` somewhere outside the DBA's
reach) — worth a line in `docs/known-issues.md`, not a blocker for shipping
this.

### D2 — A real export manifest, not `{'status': 'Success'}`

`backend/src/export/endpoints.py`'s workers (`_dicom_move_worker`,
`_proknow_worker`, `_uid_move_worker`) return a dict merged into the
`details` written by `run_batch_job`. Extend each to compute, before
triggering the actual transfer — but **the three workers start from very
different amounts of already-enumerated data, corrected from v1's claim that
this is uniformly free**:

- **ProKnow** (`_proknow_worker` → `Exporter.upload_to_proknow` →
  `download_data`, `backend/src/export/logic.py:167-208`): genuinely close to
  free. `download_data` already calls `get_series_id_instances` and downloads
  every instance's bytes to build the ProKnow upload — series/instance counts
  and StudyInstanceUIDs/SeriesInstanceUIDs are already being enumerated, and a
  checksum is a cheap addition over bytes already in hand (see below).
- **DICOM C-MOVE** (`_dicom_move_worker` → `Exporter.dicom_c_move`,
  `logic.py:53-83`): only partially free. `dicom_c_move` calls `find_series`
  (series-level enumeration) but never `get_series_id_instances` — instance
  counts and per-instance checksums are net-new queries, not already
  happening today.
- **UID-based C-MOVE** (`_uid_move_worker` → `_c_move_by_uid`,
  `export/endpoints.py:273-287,349-353`): not free at all. This path doesn't
  use `Exporter`/`find_series` — it posts CSV-supplied study/series UIDs
  straight to `/modalities/{ae}/move`. Building a manifest here means adding
  a `find_studies`/`find_series`-by-UID lookup before the move that doesn't
  exist today.

For a checksum per instance: Orthanc already computes one — pyorthanc exposes
it via `get_instances_id_attachments_name_info`/`_md5` (confirmed present on
the pinned `pyorthanc==1.23.0` in this repo, `GET /instances/{id}/attachments/dicom/info`
under the hood) — HERMES doesn't need to re-hash bytes itself for the C-MOVE
paths; for ProKnow's path the bytes are already local anyway (either source
works there).

Also record `destination`, `destination_type`, `submitted_by` (already
available as `created_by` on the job, but worth denormalising onto the event
too so a single `events` row is self-describing).

**DICOM C-MOVE is asynchronous** (`Exporter.dicom_c_move` sends
`"Synchronous": False`) — the manifest above describes what was *asked to be
sent*, not confirmation of completion. `post_modalities_id_store`'s response
includes an Orthanc Job ID; polling `GET /jobs/{id}` for `State`/`Progress`
would confirm actual completion, at the cost of either blocking the worker
(defeating the point of an async C-MOVE) or a follow-up reconciliation step.
**Recommendation for this iteration:** record the pre-send manifest as the
audit record (answers "what did HERMES ask to leave, and when" — the
important question for most audit purposes), and flag Orthanc-job-completion
polling as a clearly-separated future increment rather than building it now.

### D3 — Post-import Orthanc verification

New method, `Importer.verify_on_orthanc(mrn) -> dict` in
`backend/src/retrieve/logic.py`, following the same `find_studies` pattern
`retrieve/logic.py` itself already uses elsewhere (`_cleanup_orthanc`'s
`find_series` call, `logic.py:163`) — not `studies/endpoints.py`'s raw
`/tools/find` REST call, which is a different (also valid, but unnecessary
here) way to reach the same Orthanc data:

```python
def verify_on_orthanc(self, mrn: int) -> dict:
    """Ground truth: what does Orthanc actually hold for this patient,
    right now, regardless of what find_patient predicted beforehand."""
    studies = find_studies(client=self.ot, query={"PatientID": str(mrn)})
    return {
        "imported": bool(studies),
        "study_count": len(studies),
        "study_uids": [s.main_dicom_tags.get("StudyInstanceUID") for s in studies],
    }
```

Called from `handle_patient`, **after** `_cleanup_orthanc` has already run, so
this reflects what actually survived cleanup, not what was found pre-cleanup.
Its result is merged into the `details` already written for the retrieve
success event, alongside `in_mosaiq`/`in_pinnacle`/`in_proknow`.

**Corrected from v1:** giving this "its own SSE progress tick" is not just a
call-site choice. `run_batch_job` (`backend/src/common/sse.py:120-174`) yields
exactly one `progress` event per item, then calls a single sync
`worker(item) -> dict` — there's no hook today for a worker to emit a second,
distinct event mid-item. Simplest correct option for this iteration: call
`verify_on_orthanc` from inside `handle_patient` itself (so it's already part
of what `_import_worker`'s single `worker()` call returns, no shared-generator
change needed) and accept that it's folded into the existing single
`progress`→`success` cycle rather than surfaced as its own tick. Giving it a
distinct SSE event is a real UX nicety (a visible "verifying..." step) but
means restructuring `run_batch_job` itself — shared by every batch
import/export endpoint — and should be scoped as its own follow-up if wanted,
not bundled into this change.

This is the piece §E's "N/M imported" figure is built on — see below for why
`event_type == 'success'` alone can't answer that question today.

---

## §E — Import outcome reporting (per-source reasons + "N/M imported")

### The core problem this fixes

`Importer.handle_patient` (`backend/src/retrieve/logic.py:103-112`) returns
`{'status': 'success', **locations}` whenever it completes without raising —
**including when `in_mosaiq`/`in_pinnacle`/`in_proknow` are all `False`**, i.e.
the patient was found nowhere. `run_batch_job` then records this as
`event_type='success'`. So a naive count of "retrieve success events" already
overstates how many patients actually got data — `frontend/jobs/views.py`'s
existing `not_found` filter pill works around this at the display layer, but
the job-level `summary` (`StatusDB.summarize_job`) doesn't. Any "N/M imported"
figure has to be built on §D3's Orthanc ground truth, not on `event_type`.

### Richer per-source reasons

`Importer.find_patient` (`backend/src/retrieve/logic.py:115-130`) currently
returns three bare booleans. Change `search_mosaiq`, `search_pinnacle_db`,
`search_proknow` to each return `(found: bool, reason: str | None)`, and
`find_patient` to return:

```python
{
    "in_mosaiq": bool, "mosaiq_reason": str | None,
    "in_pinnacle": bool, "pinnacle_reason": str | None,
    "in_proknow": bool, "proknow_reason": str | None,
}
```

**Mosaiq** (`search_mosaiq`, `logic.py:310-344`): currently returns bare
`False` whether the patient genuinely isn't in either configured source, or a
source query raised and got silently `continue`d past — both look identical
today, which is exactly the "better logging" gap flagged. Distinguish three
cases:
- No studies found in either source at all → `"Not found in Mosaiq"`.
- Studies found, but none contain an RTDOSE series (Planning-level import) →
  `"Incomplete planning data"` (the wording requested).
- A source query raised (today's `except Exception: continue`) →
  `"Could not query {src}: {exc}"`, surfaced rather than swallowed. This is a
  behavior change from today's silent `continue` — the loop should still
  continue to the next source, just also remember what happened, rather than
  losing the exception entirely.

**Correction from v1 — there are two query call sites, not one.**
`search_mosaiq` (`logic.py:310-344`) makes an outer `modality.find(study_query)`
call (line 320, already wrapped in try/except) and, for every study that call
returns, an *inner* per-study `modality.find(series_query)` call (line 335,
**not** wrapped today). If the inner call raises, it currently propagates
uncaught out of `search_mosaiq` → `find_patient` → `handle_patient`, aborting
the Pinnacle/ProKnow checks for that patient entirely (they run sequentially,
after Mosaiq) and failing the whole batch item rather than producing a clean
per-source reason. This rework needs to explicitly decide whether to guard
the inner call too (recommended — wrap it the same way as the outer call, so
a series-query failure on one study becomes a per-study reason rather than an
uncaught exception that skips Pinnacle/ProKnow checking entirely) rather than
silently inheriting today's inconsistency.

**Pinnacle** (`search_pinnacle_db`, `logic.py:346-361`): this only checks
whether an export *request* is indexed in `pinn_db.sqlite` — it says nothing
about whether the actual DICOM reconstruction PinnacleExport performs
afterward (triggered by `import_from_pinnacle` → `pinn_entry(payload)`)
succeeds. The real success/failure signal lives in PinnacleExport's own
`pinnacle_export.status`/`pinnacle_export.errors` tables (same Postgres
database as HermesDB, read-only to HERMES — see `backend/src/plans/db_client.py`'s
docstring, which already flags `errors` as "the natural next increment").
Plan:
- Not indexed at all → `"Not found in Pinnacle export index"`.
- Indexed, and a `pinnacle_export.status` row for this mrn with
  `process_datetime` after the job started shows failure → look up the
  linked `pinnacle_export.errors.error_message` (join on `status_id`) as the
  reason, e.g. `"Could not reconstruct DICOM: {error_message}"`. Extend
  `backend/src/plans/db_client.py`'s `PlansDB` (or add a sibling reader) with
  a `latest_status_for_patient(mrn, since)` query — same read-only,
  schema-presence-tolerant posture `list_plans_for_patient` already has.
- **Timing caveat, stated rather than solved**: PinnacleExport processes
  pushes out-of-band; there's no guarantee its `status`/`errors` rows exist
  yet by the time `import_patient` returns. Recommend treating "no status row
  yet" as its own reason (`"Pinnacle reconstruction pending"`) rather than
  either blocking the batch item on a poll loop (which risks one slow/stuck
  Pinnacle job stalling an entire batch) or reporting a false negative.
  `patient_detail.html`'s existing plans panel (already reads `PlansDB` live,
  independent of any specific job) will naturally show the settled status
  once it exists — this reason is about the job-time snapshot, not a
  permanent record.

**ProKnow** (`search_proknow`, `logic.py:366-372`): already catches an
exception and returns `False`; change to distinguish ProKnow's own
"not found" exception from a connectivity/auth failure (mirroring the Mosaiq
fix), returning `"Patient not found on ProKnow"` for the former.

### Surfacing it

- `results/endpoints.py`'s `job_patients_summary` already returns
  `in_mosaiq`/`in_pinnacle`/`in_proknow` per patient — add the three
  `*_reason` fields alongside them.
  **Correction from v1 — these are not scrubbed for free.** `job_patients_summary`
  (`results/endpoints.py:143-185`) reads `in_mosaiq`/`in_pinnacle`/`in_proknow`/
  `status` straight out of the unscrubbed `details_by_mrn.get(real_mrn, {})`
  dict (lines 167-170); only `error_message` goes through `_scrub()`
  (lines 174-178). The generic `_scrub_json` pass over the whole `details`
  blob only happens in `_anonymize_events`, used by the *different*
  `patient_timeline`/`patient_timeline_all` endpoints — `job_patients_summary`
  never calls it. Since these new reason strings will routinely quote real
  MRNs (Mosaiq/ProKnow exception text; Pinnacle's `error_message`, which
  CLAUDE.md already documents as "built from or quotes the MRN"), wiring them
  into `job_patients_summary` as originally described would leak real
  identifiers straight across the anonymisation boundary — precisely the
  failure mode this whole plan exists to prevent. **Fix:** explicitly
  `_scrub()` each of the three new reason fields at the same point
  `error_message` already is, e.g.:
  ```python
  "mosaiq_reason": _scrub(details_by_mrn.get(real_mrn, {}).get("mosaiq_reason"), real_mrn, display_map[real_mrn]),
  ```
  repeated for `pinnacle_reason`/`proknow_reason`. Any other endpoint this
  plan adds these fields to needs the same explicit treatment — don't assume
  scrubbing is inherited from context.
- `frontend/templates/cotton/source_badges.html` — extend `c-vars` with
  optional `mosaiq_reason`/`pinnacle_reason`/`proknow_reason`; render as a
  `title="..."` tooltip on the ✗ mark (cheapest change, no layout impact) and,
  on `patient_detail.html` specifically (more room than the dense table),
  also as visible small red text — matching the pattern already used for
  `row.error_message` under a Failed badge in `patient_table.html`.
- **"N/M imported" stat**: new `StatusDB.count_imported_patients(job_id)` —
  count of distinct `mrn` with a retrieve-stage success event whose `details->>'imported' = 'true'`
  (from §D3), against `COUNT(DISTINCT mrn)` from `patients`. Exposed as
  `imported_count`/`submitted_count` on `results/endpoints.py`'s `job_summary`.
  Rendered on `jobs/job_detail.html` as a headline stat above the existing
  summary table, e.g. "58 / 76 patients imported".
- **Deliberately not changing `event_type`**: a patient found nowhere still
  gets `event_type='success'` (the operation ran without raising) — the
  "did we actually get data" question is answered by the new `imported`
  field in `details`, not by redefining what `event_type` means. Flagged as
  a design choice for the reviewing sub-agent to sanity-check: is anything
  else in the codebase relying on `event_type='success'` implying "found
  somewhere"? A grep of `event_type.*success` against `frontend/` and
  `backend/` didn't turn up such an assumption at review time, but this
  should be re-checked once implementation starts.

---

## §F — Explicitly deferred (documented, not built)

- **Finding 5 (pre-flight review before a combined import+export job).** Not
  solvable without either knowing import's outcome in advance (impossible)
  or interrupting a potentially multi-hour/day job (rejected as cumbersome).
  No design proposed here — tracked purely as a known limitation in
  `docs/known-issues.md`. If it's revisited later, §D3/§E's new
  "what actually landed" data is a prerequisite for any future design in this
  area, so this plan isn't a dead end for it.
- **Finding 7 (admin/superuser access control).** Accepted as an operational
  control — keeping the admin group small and vetted — rather than a code
  change, for this iteration. Documented, not built.

---

## Sequencing

1. **§C** (banner copy) — no dependencies, ships independently, immediately.
2. **§B1** (TLS env var + app-side monitoring) — no dependencies on anything
   else in this plan. §B2 is a parallel-track request to file, not a blocker
   for B1.
3. **§D0** (expand the two `Response` models) — **corrected from v1: this is
   now a shared prerequisite**, not an independent item. It has no
   dependency of its own and should ship first among §D/§E, since D2, D3, and
   E all silently no-op on the primary batch endpoints without it.
4. **§D1** (hash chain, + the companion `asyncio.to_thread` fix) and **§D3**
   (Orthanc verification) — depend on §D0, not on each other; can run in
   parallel once §D0 lands.
5. **§E** depends on §D0 and §D3 (needs the `imported` field to exist before a
   "N/M" stat can be computed), and — separately — its `job_patients_summary`
   scrubbing fix has no dependency on D0/D3 and can be verified/shipped
   independently the moment the `*_reason` fields exist.
6. **§D2** (export manifest) depends on §D0; otherwise independent of D1/D3/E.
7. **§A** (destination allow-list) is fully independent of the rest of this
   plan and can be built in parallel with any of it.

**Corrected from v1:** the dependency graph is no longer "just §E → §D3" —
§D0 is now a load-bearing prerequisite for §D1/§D2/§D3/§E's batch-endpoint
behavior, found by the reviewing sub-agent, not present in the original draft.

---

## Revision history

- **v3** (this revision): second critique round confirmed 6 of v2's 8 fixes
  as correct outright. Two needed further correction: added `submitted_by`
  to §D0's export `Response` model (D2 asks for it; the first field list
  missed it), and — more substantively — §D1's `asyncio.to_thread`
  recommendation was itself found to introduce a genuine race condition
  (`backend/src/db.py`'s pool is a `SimpleConnectionPool`, explicitly not
  thread-safe; moving DB calls onto real OS threads for the first time via
  `to_thread`, without also making the pool thread-safe, trades a bounded
  problem for an unbounded one). Fixed by adding an explicit companion change
  to §D1: swap `SimpleConnectionPool` → `ThreadedConnectionPool` in
  `backend/src/db.py`. Both reviewing passes are now satisfied; this plan is
  ready to build from.
- **v2**: incorporated a full sub-agent critique against the live codebase.
  Added §D0 (the `Response`-model fix, without which D2/D3/E silently no-op
  on every batch endpoint — the most severe finding). Fixed a real
  anonymisation-boundary leak in §E's `job_patients_summary` surfacing (new
  reason fields need explicit `_scrub()`, not inherited scrubbing). Corrected
  §D2's "no new Orthanc capability needed" claim (true only for the ProKnow
  path, not DICOM C-MOVE or UID-based C-MOVE). Flagged `search_mosaiq`'s
  second, unguarded inner query call in §E. Flagged §D1's row-lock throughput
  cost against the event loop and proposed the (later corrected)
  `asyncio.to_thread` fix. Corrected migration sketches in §A/§D1 to match
  the repo's actual `op.create_table()` Alembic style rather than raw SQL.
  Corrected a file attribution in §D3. Updated Sequencing to reflect §D0 as a
  new shared prerequisite.
- **v1**: initial draft, sent for critique.
