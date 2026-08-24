# Worker queue for batch jobs — design

**Status:** implemented, across four PRs (#23-#26) on top of `main` @ `950dba3`+.
Originally written against HEAD `2144bd9`, deferred; corresponds to
`TODO.md` item 3 ("Workers?"). The design below held up largely as
written — Postgres `SKIP LOCKED`, the `tasks` schema, the SSE vocabulary,
and the four-step sequencing all shipped close to this document's original
shape. Real deltas from implementation, for anyone reading this as a
historical record rather than current-state docs:
- `TasksDB.job_progress` initially used a `task_id` watermark (as sketched
  below); corrected before it had a real caller, since task rows are
  mutated in place, not appended per transition -- a watermark can only
  ever report a task once, at whatever state it was in on first
  appearance. It now re-reads every task per poll tick; the observer
  diffs against its own in-memory last-seen-state map instead.
- `mark_running`/`mark_succeeded`/`mark_failed`/`cancel_task` gained
  `state` **and** `claimed_by` ownership guards, not just `state` guards --
  found in review: `reap_stale_claims` can legitimately requeue (and let a
  second worker claim) a task whose original worker is merely slower than
  `stale_seconds`, not dead, and the first worker's eventual terminal
  write must not then clobber the second worker's result.
- Export handlers in `backend/worker.py` reuse `export/endpoints.py`'s own
  `_dicom_move_worker`/`_proknow_worker`/`_uid_move_worker` factories
  directly, rather than reimplementing their `Response`-building logic --
  `Exporter` needs no per-process caching (unlike `Importer`), so there
  was nothing to gain from not reusing them as-is.
- `run_batch_job` was **not** fully retired. The JSON-bodied
  `batch_import`/`dicom_move`/`proknow_upload` endpoints (server-side CSV
  path, no file upload) have no frontend caller but do have real dedicated
  test coverage (`test_retrieve_endpoints_errors.py`,
  `test_export_anon_boundary.py`, `test_export_manifest.py`'s integration
  test) exercising the synchronous path specifically -- converting them
  would mean rewriting that coverage against the queue for endpoints
  nothing currently calls. They stay on `run_batch_job`, documented inline
  at each endpoint; only the four `_file` (frontend-facing) endpoints and
  `batch_import_file` converted.

**Known limitations, stated rather than solved** (matching this codebase's
existing habit -- see `docs/known-issues.md` -- of naming a limitation
explicitly rather than silently living with it):
- **No enqueue idempotency.** `TasksDB.enqueue` is a plain `INSERT`, no
  `ON CONFLICT`. `job_id` is minted client-side (`frontend/jobs/views.py`'s
  `_enqueue_batch_job`) fresh per request, so an ordinary double-click just
  produces two distinct jobs (not a problem) -- but a transport-level retry
  that resends the *identical* already-serialized request (a proxy retrying
  a timed-out POST, for instance) would enqueue every row in the CSV twice
  under the same `job_id`, and a worker would execute each one twice. No
  unique constraint was added deliberately: there's no natural key on
  `tasks` that holds for every kind (the UID-move flow legitimately repeats
  `status_mrn`, per the schema section above). Considered low-likelihood for
  this deployment (internal NHS Trust tool, `httpx.post` has no built-in
  retry) and not fixed here; a real fix would need either an explicit
  idempotency key or a job-level "already enqueued" guard.
- **Uploaded CSV tmp files are never cleaned up.** `batch_import_file`/
  `dicom_move_file`/`proknow_upload_file`/`dicom_move_uids_file` all write
  to `./tmp/{job_id}_{filename}` and never `unlink()` it -- pre-existing
  (confirmed present before this queue work, on the old synchronous path
  too), but worth naming here since the queue's higher submission
  throughput is likely to make `./tmp` accumulate faster than before. A
  future increment could delete the tmp file once `_build_import_items`/
  `_build_export_items`/`_build_uid_items` has read it (the worker never
  needs it again -- confirmed `backend/worker.py`'s `_reconstruct_batch_item`
  only reads `real_id`/`display_id`/`status_mrn`/`extra` off the task row,
  never `input_path`), or a periodic sweep.

## Context

A batch job today **is** an HTTP request: `run_batch_job` (`backend/src/common/sse.py`)
iterates patients sequentially inside the handler, and the SSE stream is a side effect
of doing the work. Three consequences:

- **The work dies with the connection.** Close the tab mid-import and the remaining
  patients never run. `job_watch` 404s unless *this browser session* staged the job,
  so a refresh loses your view of a job that is still running.
- **No parallelism.** One patient at a time, one process.
- **A GET triggers real imports.** `job_stream` (`frontend/jobs/views.py`) is what
  actually starts the work, which is why it needs the `pending_job:` session dance
  to be safe.

## Four flows, not one

`run_batch_job` has four callers, and the unit of work is not always a patient:

| Flow | Worker factory | `real_id` is | Params |
|---|---|---|---|
| Import | `_import_worker` (`retrieve/endpoints.py:66`) | real MRN | `import_level` |
| Export C-MOVE | `_dicom_move_worker` (`export/endpoints.py:89`) | real MRN | `destination` |
| Export ProKnow | `_proknow_worker` (`export/endpoints.py:96`) | real MRN | `collection` |
| Export by UID | `_uid_move_worker` (`export/endpoints.py:349`) | **a study UID** | `destination`, `level` |

The UID flow constrains everything: its `real_id` is a DICOM study UID, its
`status_mrn` may also be a study UID (when the CSV has no `patient_id` column), and
the actual work is carried in `BatchItem.extra["study_uid"]/["series_uid"]`.
**A `tasks` table keyed on `mrn` cannot express existing behaviour** — a task row must
be a serialised `BatchItem` plus a `kind` discriminator and `params`.

## Schema

`job_id` stays the group. The `jobs` row already carries every group-level attribute
(`project_id`, `created_by`, `description`, `cancelled`, `cancelled_at`) and
`cancel_job` is already a cross-process UPDATE. Redefining a job as one patient would
break every `/results/job/*` endpoint, `jobs.project_id`, the dashboard and the results
page for no gain. The new per-item unit is a **task**.

```
tasks(task_id BIGINT Identity PK, job_id FK->jobs, kind, stage, state,
      real_id, display_id, status_mrn, input_path, extra JSONB, params JSONB,
      priority, attempts, max_attempts, claimed_by, claimed_at,
      created_at, started_at, finished_at, error_message, details JSONB)
  ix_tasks_job_id, ix_tasks_claim(state, priority, created_at)

events += task_id BIGINT NULL
```

`state` ∈ `queued`/`claimed`/`running`/`succeeded`/`failed`/`cancelled`.

- `kind`/`params` are denormalised onto the task so a claim needs no join.
- **No `UNIQUE (job_id, mrn, stage)`** — the UID flow legitimately repeats
  `status_mrn`, and `_build_uid_items` already dedups. Idempotency comes from the
  terminal-state guard instead.
- `tasks` is mutable current state, `events` stays the immutable audit log — the same
  split already chosen deliberately for `research_projects` vs `project_audit_log`.

## Queue mechanism: Postgres `SKIP LOCKED`

```sql
UPDATE tasks SET state='claimed', claimed_by=%s, claimed_at=now()
WHERE task_id = (
  SELECT task_id FROM tasks
  WHERE state='queued' AND job_id NOT IN (SELECT job_id FROM jobs WHERE cancelled)
  ORDER BY priority DESC, created_at
  FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```

No Redis, no Celery, no new service to get through NHS infrastructure approval — it
reuses the existing `psycopg2` pool. Celery/RQ buy retry policy and scheduling you'd
otherwise hand-roll, but that's a few dozen lines against a whole new dependency and
daemon. Revisit only if cross-machine fan-out with priorities and scheduled reruns is
needed.

## Worker process (`backend/worker.py`, run as `python -m backend.worker`)

Claim → cancel check → ethics re-check → `mark_running` + `add_event(start)` → call the
**existing** worker factory → terminal state + event. Five things it must get right:

1. **Don't run migrations.** `setup_status_db` does `init_pool` *and*
   `alembic upgrade head`; N workers racing Alembic on boot is a real hazard. The
   worker calls `init_pool` only — the API process stays the sole migrator.
2. **Reconstruct the `BatchItem` faithfully**, including `extra`, or the UID flow breaks.
3. **Reuse the existing worker factories unchanged** via a `kind` → factory dict.
   `Importer`/`Exporter` need no changes at all.
4. **Cache `Importer` per process, per `import_level`.** `Importer.__init__` opens
   ProKnow *and* Orthanc connections (`retrieve/logic.py:85-100`) and `_import_worker`
   currently builds one per patient — under workers that becomes a connection storm.
5. **Re-check the ethics gate at claim time.** A project can be revoked or expire
   between enqueue and execution, and `projects/enforcement.py` is explicit that
   membership is re-checked live and never cached. On denial: mark the task
   `cancelled`, don't retry.

Trap `SIGTERM`, finish the in-flight task, then exit — otherwise every deploy strands
claimed rows for the reaper.

## Enqueue / observe / cancel

**Enqueue.** The four batch endpoints insert rows and return `{job_id, total}`
immediately. CSV parsing and anon resolution stay at submit time — `_build_*_items`
already raise clean 400/422/503s, and deferring them turns an actionable synchronous
error into a silently-failed job. It also means workers never read the uploaded file.
The two single-patient endpoints stay synchronous.

**Observe.** New `GET /results/job/{job_id}/stream` polls `tasks` every ~1s and emits
the **exact** vocabulary `templates/cotton/job_progress.html:42-76` already listens for:

| Event | Emitted when | Payload |
|---|---|---|
| `start` | first tick | `{"type":"start","total":N}` |
| `progress` | task enters `running` | `{"type":"progress","current":<display_id>}` |
| `success` | task enters `succeeded` | `{"type":"success","mrn":<display_id>,**details}` |
| `error` | task enters `failed` | `{"type":"error","mrn":<display_id>,"error":...}` |
| `cancelled` | `jobs.cancelled` is true | `{"type":"cancelled"}` |
| `done` | no non-terminal tasks remain | `{"type":"done"}` |

Track a high-water `task_id` per state so each tick is a small indexed read. Emit
`display_id` from the task row, never `status_mrn` — the observer then never touches
the anon DB on a poll tick, and the real id cannot leak by construction.

**Later addition — combined import→export jobs.** `backend/worker.py`'s
`_maybe_chain_export` enqueues a follow-up export task after a successful import
(`batch_import_file`'s optional `export_kind`/`destination`/`collection`/`message_id`
fields), on the same `job_id`. This grows the task count over the job's lifetime, so
`start`/`progress`/`success`/`error` each gained a `"stage"` field (`retrieve`/`export`),
and a new `total` event (`{"type":"total","total":N,"import_total":N,"export_total":M}`)
fires whenever the count changes — both totals computed fresh from `stage` on every
tick, not inferred client-side, so a reconnect (page refresh, a colleague joining,
`EventSource`'s own auto-reconnect) reports the same split a continuous connection
would have. The enqueue happens **before** `mark_succeeded` for the import task, not
after — reversing that order reopens exactly the "poll observes zero pending tasks
too early" race this design already closed for the plain case. A second chain attempt
for the same import (e.g. a reaped, reclaimed task completing twice) is a no-op at the
database level (`tasks.chained_from_task_id` + a partial unique index), not an
application-level check — two truly concurrent workers can otherwise both pass a
read-then-write guard.

**Cancel.** `cancel_job` unchanged, plus
`UPDATE tasks SET state='cancelled' WHERE job_id=%s AND state='queued'`. Strictly
stronger than today's per-item check, and it preserves what the cancel dialog already
promises: in-flight items finish.

## Frontend

The relay and the progress component need **no changes**. What changes is staging:
`_stage_batch_job`, the `pending_job:<job_id>` session entries, `_build_stream_request`
and `_cleanup_pending_job` are all deleted; the POST handlers call the enqueue endpoint
and redirect; `job_watch` swaps its session 404 for the same `_job_is_visible_to` check
`job_detail` uses; `job_stream` relays the observer endpoint instead of POSTing a file.

Net effect: closing the tab no longer kills the job, refresh no longer loses it, a
colleague can watch it, and a GET no longer triggers imports — which is what the
current docstring says the session dance was trying to achieve.

## Retries, reaper, deployment

- `max_attempts` defaults to `1`, so **behaviour is unchanged until deliberately
  raised** — ship the plumbing, enable retries as a separate decision.
- `mark_failed` increments `attempts` and returns the row to `queued` while attempts
  remain. `events.attempt` — which exists, is displayed in the timeline, and is
  hardcoded to `1` everywhere today — finally carries a real value.
- A stale-claim reaper (`claimed_at` older than N minutes → back to `queued`) recovers
  tasks from workers that died holding a claim. Run it on a timer in the worker loop.
- **At-least-once, not exactly-once.** A worker dying after the external work but
  before the terminal write re-runs that patient; `_cleanup_orthanc` already dedups on
  the import side.

| Variable | Purpose |
|---|---|
| `HERMES_WORKER_CONCURRENCY` | tasks in flight per worker process (default 1) |
| `HERMES_WORKER_POLL_INTERVAL` | idle sleep, seconds (default 2) |
| `HERMES_TASK_STALE_SECONDS` | reaper threshold (default 1800) |

Add a `worker` service to `docker-compose.yml` reusing the `backend` image with
`command: python -m backend.worker` and the same `env_file`. Scale with
`docker compose up --scale worker=3`. **Concurrency is a real constraint, not a knob**:
N workers means N concurrent ProKnow/Orthanc/Mosaiq sessions, and nothing enforces a
ceiling today because the current design is implicitly serialised. Start at one.

## Sequencing — four shippable steps

1. Migration + `TasksDB` + tests. Nothing calls it; zero risk.
2. Worker + `batch_import_file` converted behind `HERMES_USE_QUEUE`, old path still the
   default, so both can run and be compared on the same CSV.
3. Observer stream + frontend flip.
4. Convert the three export endpoints, drop the flag, retire `run_batch_job`.

`run_batch_job` stays untouched until step 4, so `test_sse.py` keeps passing throughout
and there's a working fallback at every point.

## Tests

- `test_tasks_db.py` — enqueue count; each row claimed exactly once; **two real
  connections claiming concurrently get distinct `task_id`s** (the core `SKIP LOCKED`
  guarantee); `cancel_queued` leaves `running` rows alone; `mark_failed` returns
  `queued` then `failed`; `reap_stale_claims` requeues an old claim.
- `test_worker.py` — a fake `kind` registered in the factory dict; success, failure,
  job-cancelled-mid-flight, and project-revoked-after-enqueue paths.
- `test_job_stream.py` — exact event sequence `start … progress/success/error … done`,
  every event carries `type`, and `display_id` is emitted while `status_mrn` never
  appears (mirroring `test_results_anon_boundary.py`).

## Open decisions

1. `kind`/`params` denormalised onto each task rather than joined from `jobs`.
2. Ethics re-check at claim time — a behaviour change: a legitimately-enqueued job can
   now be cancelled mid-run if the project is revoked.
3. Retries ship disabled (`max_attempts=1`).
4. Single-patient endpoints stay synchronous rather than going through the queue.
