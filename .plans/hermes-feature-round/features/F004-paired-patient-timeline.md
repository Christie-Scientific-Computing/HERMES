# Feature F004: Paired start/end patient timeline

## Purpose

The patient event timeline currently shows one row per raw `events` row —
every attempt produces two rows (a `start` and a later `success`/`failure`),
and "When" is a full ISO timestamp. This is hard to scan: a reader has to
mentally pair rows to see how long anything took, or whether it's even
finished. This feature merges each attempt into one row with explicit start
and end times.

## Behaviour

The patient detail page's timeline table shows one row per attempt, with
**Start Time** and **End Time** columns replacing the old single "When"
column (and dropping the old separate Event-per-row shape). End Time reads
"In progress" if the attempt hasn't finished, or an actual timestamp once it
has. Stage, Attempt, and Error columns remain, now describing the attempt as
a whole rather than a single event.

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/templates/jobs/patient_detail.html` (timeline table,
  ~lines 104-131; current columns: When | Stage | Event | Attempt | Error) —
  page wired via `frontend_fastapi/routers/jobs.py`'s `patient_detail`
  (~line 386).
- `frontend_fastapi/routers/jobs.py` (~line 413) — calls
  `backend_client.patient_timeline(job_id, mrn)`.
- `backend/src/results/endpoints.py` (`patient_timeline`, ~line 577) — calls
  `status_db.get_patient_history(job_id, real_mrn)`.
- `backend/src/status/db_client.py` (`StatusDB.get_patient_history`,
  ~lines 89-92) — `SELECT * FROM events WHERE job_id=%s AND mrn=%s ORDER BY
  ts`; the raw source of the current flat list.
- `events` schema (`CLAUDE.md`'s HermesDB Schema section): `id, job_id, mrn,
  stage, event_type, ts, attempt, error_message, details, task_id,
  prev_hash, row_hash`. `event_type` is `start`/`success`/`failure`.
  `task_id` links a queue-driven event back to its `tasks` row; `NULL` for
  the still-synchronous single-item path.
- `tasks` schema: has `started_at`/`finished_at` columns directly — for any
  event with a non-null `task_id`, these are already the exact start/end
  timestamps, no pairing needed.
- `add_event`'s signature (`status/db_client.py:44`) confirms every event
  row carries `(job_id, mrn, stage, attempt)` consistently, which backs the
  fallback pairing key for the `task_id IS NULL` case.

### Likely changes

- `backend/src/results/endpoints.py`'s `patient_timeline` (or a new method
  on `StatusDB`) — reshape the response: group raw events into one record
  per attempt: `{stage, attempt, start_ts, end_ts, outcome, error_message}`.
  - Where `task_id` is present: join to `tasks` for `started_at`/
    `finished_at` (or just use the paired events' own timestamps — same
    value either way; joining `tasks` avoids re-deriving pairing logic for
    the queue-driven majority case).
  - Where `task_id IS NULL`: pair the `start` row with its `success`/
    `failure` row by `(job_id, mrn, stage, attempt)`.
  - `outcome` is `success` / `failure` / `cancelled` / `in_progress`
    (no terminal row found yet).
- `frontend_fastapi/templates/jobs/patient_detail.html` — new table
  columns: Start Time | End Time | Stage | Event | Attempt | Error.
- A Jinja timestamp filter/helper for formatting (no Django `date` filter
  available here) — e.g. render via `start_ts.strftime(...)` in the view or
  a small custom filter.

## Acceptance criteria

### Scenario: Completed attempt shows both times

**Given** an attempt that started and later succeeded

**When** viewing the patient detail page's timeline

**Then** one row shows both a Start Time and an End Time for that attempt,
not two separate rows

### Scenario: In-progress attempt shows "In progress"

**Given** an attempt that has started but has no terminal event yet

**When** viewing the timeline

**Then** the row's End Time column reads "In progress"

### Scenario: Cancelled attempt

**Given** an attempt whose job was cancelled before it produced a terminal
event

**When** viewing the timeline

**Then** the row's Event column reflects cancellation and End Time shows
"—" (no natural end time exists)

### Scenario: Queue-driven and legacy synchronous attempts render consistently

**Given** a patient's timeline includes both a queue-driven attempt (has
`task_id`) and a legacy single-item attempt (no `task_id`)

**When** viewing the timeline

**Then** both render in the same Start Time / End Time shape, regardless of
which pairing mechanism produced the times

## Edge cases

- An attempt's `start` event exists but its terminal event was somehow lost
  (should not happen given `add_event`'s invariants, but pairing logic
  should not crash if it does) — treat as `in_progress` rather than
  erroring.
- Multiple attempts for the same `(job_id, mrn, stage)` — `attempt` already
  distinguishes them; confirm the pairing key doesn't collapse distinct
  attempts together.

## Success

The patient detail page renders one row per attempt with correct Start/End
times for a real job that has both completed and in-progress patients,
verified against the existing `events`/`tasks` data without requiring any
new column on `events` itself.

## Failure behaviour

If pairing produces an attempt with no start time at all (a terminal event
with no matching start — should be structurally impossible per
`add_event`'s call sites, but worth a defensive check), skip or flag the row
rather than raising a 500 on the patient detail page.

## Testing considerations

- Backend: extend `backend/tests/` coverage near
  `test_observer_stream.py`/`StatusDB`-adjacent tests — assert the paired
  output shape for: a queue-driven completed attempt, a queue-driven
  in-progress attempt, and a legacy (`task_id IS NULL`) completed attempt.
- Frontend: extend `frontend_fastapi/tests/test_jobs.py` to assert the
  rendered table shows the new columns and correct "In progress"/timestamp
  rendering for a fixture patient timeline.

## Implementation notes

This is a read/presentation change over existing data — no new column on
`events`, no migration required. The `tasks.started_at`/`finished_at` path
exists specifically to avoid re-implementing pairing logic for the common
(queue-driven) case; only the legacy synchronous path needs the
`(job_id, mrn, stage, attempt)` fallback.

## Out of scope

- Any change to how `events` or `tasks` are written — this is purely a read
  path change.
- The elapsed-duration ("00:00:04") column originally proposed in
  `FEATURES.md` — explicitly dropped in favour of absolute Start/End
  timestamps during grilling.
