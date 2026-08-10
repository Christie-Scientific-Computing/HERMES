"""
Tests for the events hash chain (docs/safety-plan.md §D1):
- StatusDB.add_event / backend/src/status/hash_chain.py
- backend/scripts/verify_audit_chain.py
- the ThreadedConnectionPool swap in backend/src/db.py, which is what makes
  it safe to run add_event's new row-locking DB work off the event loop via
  asyncio.to_thread (backend/src/common/sse.py's run_batch_job).
"""
import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import backend.src.db as db_module
from backend.src.db import get_conn
from backend.src.status.db_client import StatusDB
from backend.src.status.hash_chain import GENESIS_HASH, compute_row_hash
from backend.scripts.verify_audit_chain import fetch_chain_state, fetch_events_in_order, verify_chain


def _ensure_pool_sized_for_concurrency(maxconn: int) -> None:
    """
    The module-level pool (backend/src/db.py) lazily initializes itself with
    a small default maxconn (10) the first time anything calls get_conn() in
    this test session. That default is a legitimate resource limit, not part
    of what this section is testing -- ThreadedConnectionPool.getconn()
    raises PoolError immediately once exhausted rather than queueing, so
    firing more concurrent add_event calls than maxconn would fail with
    "connection pool exhausted" regardless of whether the pool is
    thread-safe. Rebuild the pool with headroom before the stress tests
    below so what actually gets exercised is thread-safety under real
    concurrency, not an unrelated sizing limit.
    """
    if db_module._pool is not None:
        db_module._pool.closeall()
    db_module._pool = None
    db_module.init_pool(os.environ["DATABASE_URL"], minconn=1, maxconn=maxconn)


@pytest.fixture
def db():
    return StatusDB()


@pytest.fixture
def job_id():
    return f"hashchain-test-{uuid.uuid4()}"


def _events_for_job(job_id: str) -> list[dict]:
    """All events for one job, in id order -- a slice of the global chain,
    but still contiguous, so relative prev_hash/row_hash linkage within it
    can be checked directly without needing the whole table."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, job_id, mrn, stage, event_type, ts, attempt,
                   error_message, details, prev_hash, row_hash
            FROM events WHERE job_id = %s ORDER BY id
            """,
            (job_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def test_sequential_add_event_calls_produce_a_valid_chain(db, job_id):
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="start")
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"in_mosaiq": True})
    db.add_event(job_id, mrn="MRN2", stage="retrieve", event_type="failure", error_message="boom")

    rows = _events_for_job(job_id)
    assert len(rows) == 3

    # Every row got a hash, and each row's prev_hash is exactly the previous
    # row's row_hash -- the defining property of the chain.
    for i, row in enumerate(rows):
        assert row["prev_hash"] is not None
        assert row["row_hash"] is not None
        if i > 0:
            assert row["prev_hash"] == rows[i - 1]["row_hash"]

    # Each row_hash is independently reproducible from its own fields --
    # this is exactly what verify_audit_chain.py does at scale.
    for row in rows:
        recomputed = compute_row_hash(
            row["prev_hash"], row["job_id"], row["mrn"], row["stage"],
            row["event_type"], row["ts"], row["attempt"],
            row["error_message"], row["details"],
        )
        assert recomputed == row["row_hash"]


def test_verify_chain_reports_intact_chain_ok(db, job_id):
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="start")
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"x": 1})

    all_rows = fetch_events_in_order()
    ok, bad_row, reason = verify_chain(all_rows)
    assert ok is True
    assert bad_row is None
    assert reason is None

    # Also exercise the chain_state_last_hash cross-check on the happy
    # path -- it must not false-positive when nothing's actually wrong.
    ok, bad_row, reason = verify_chain(all_rows, chain_state_last_hash=fetch_chain_state())
    assert ok is True
    assert bad_row is None
    assert reason is None


def test_verify_chain_detects_tampering(db, job_id):
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="start")
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="failure", error_message="original message")
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"in_mosaiq": True})

    rows = _events_for_job(job_id)
    tampered_id = rows[1]["id"]  # the failure event, mid-chain

    # Simulate someone with direct DB access editing a row after the fact --
    # the row_hash column is left untouched, exactly as an attacker who
    # doesn't know how row_hash is derived would leave it. verify_chain is
    # checked (and the row restored) inside try/finally: the test DB
    # persists across pytest invocations (see test_status_db.py's own
    # comment on this), so a tampered row left behind here would poison
    # every full-table chain check any later test run does, not just this one.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE events SET error_message = %s WHERE id = %s", ("tampered", tampered_id))

    try:
        all_rows = fetch_events_in_order()
        ok, bad_row, reason = verify_chain(all_rows)

        assert ok is False
        assert bad_row["id"] == tampered_id
        assert "row_hash mismatch" in reason
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE events SET error_message = %s WHERE id = %s", ("original message", tampered_id))


def test_first_chained_event_anchors_to_genesis_hash(db, job_id):
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="start")

    rows = _events_for_job(job_id)
    # This job's first event may not be the very first row in the whole
    # table (other tests/jobs run before it), so its prev_hash need not be
    # GENESIS_HASH itself -- but the *global* first chained row must be.
    all_rows = fetch_events_in_order()
    first_chained = next(r for r in all_rows if r["prev_hash"] is not None)
    assert first_chained["prev_hash"] == GENESIS_HASH


def test_verify_chain_detects_truncated_tail_without_reseeded_state(db, job_id):
    """
    Reproduces a real gap found in review: verify_chain's per-row walk alone
    cannot see a truncated tail. Delete the most recent event(s) from
    `events` *without* touching event_chain_state, and every row that's
    still present remains perfectly internally consistent (each one's
    prev_hash still matches the previous row's row_hash) -- a bare
    fetch_events_in_order() + verify_chain(rows) walk would report "intact"
    even though event_chain_state.last_hash now points at a hash that
    belongs to no row at all. Passing chain_state_last_hash is what makes
    verify_chain (and therefore `python backend/scripts/verify_audit_chain.py`
    itself) catch this.
    """
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="start")
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"x": 1})

    rows = _events_for_job(job_id)
    last_id = rows[-1]["id"]

    # Confirm this job's last row really is the current global tail --
    # otherwise deleting it wouldn't disturb event_chain_state's pointer at
    # all, and the test below would be vacuous. Safe to assume under
    # pytest's default single-threaded, sequential test execution: nothing
    # else writes to `events` between the two add_event calls above and
    # this check.
    global_rows = fetch_events_in_order()
    assert global_rows[-1]["id"] == last_id

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM events WHERE id = %s", (last_id,))

    try:
        remaining_rows = fetch_events_in_order()
        chain_state_last_hash = fetch_chain_state()

        # The per-row walk alone: still reports "intact" -- this is the gap.
        ok_rows_only, _, _ = verify_chain(remaining_rows)
        assert ok_rows_only is True

        # With the live state cross-checked: now correctly reports broken.
        ok, bad_row, reason = verify_chain(remaining_rows, chain_state_last_hash=chain_state_last_hash)
        assert ok is False
        assert bad_row is None  # no single row is at fault -- the state pointer is stale
        assert "event_chain_state" in reason
        assert "truncated" in reason
    finally:
        # Repair: point event_chain_state back at whatever is now genuinely
        # the last row (or GENESIS_HASH if none remain), so later tests --
        # and later full-suite runs against this persistent DB -- see a
        # consistent chain again rather than inheriting this test's mess.
        fresh_rows = fetch_events_in_order()
        correct_hash = fresh_rows[-1]["row_hash"] if fresh_rows else GENESIS_HASH
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE event_chain_state SET last_hash = %s WHERE id = 1", (correct_hash,))


def test_row_hash_recomputation_is_timezone_independent(db, job_id):
    """
    Reproduces the reviewer-demonstrated timezone bug directly. Before the
    fix, hash_chain.py's canonical_event_json relied on bare
    json.dumps(..., default=str) for `ts`; psycopg2 renders a TIMESTAMPTZ
    using the *reading connection's* session TimeZone GUC, not necessarily
    UTC, so str(ts) for the exact same instant differed depending on what
    timezone the connection that read it back happened to be set to --
    e.g. '...+00:00' under a UTC session vs. '...+01:00' under
    Europe/London in summer -- even with zero tampering. That would make
    verify_audit_chain.py report every row as corrupted the moment it ran
    against a connection/server with a non-UTC session timezone.
    """
    db.create_job(job_id)
    db.add_event(job_id, mrn="MRN1", stage="retrieve", event_type="success", details={"in_mosaiq": True})

    row = _events_for_job(job_id)[0]

    # Baseline: recomputing from the row as read back over this test suite's
    # normal (UTC) session reproduces the stored hash.
    recomputed_utc = compute_row_hash(
        row["prev_hash"], row["job_id"], row["mrn"], row["stage"],
        row["event_type"], row["ts"], row["attempt"], row["error_message"], row["details"],
    )
    assert recomputed_utc == row["row_hash"]

    # Now read the *same* row back over a connection whose session timezone
    # is deliberately not UTC -- SET LOCAL scopes it to this transaction
    # only, so it can't leak into the shared pool for some other borrower.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL TIME ZONE 'Europe/London'")
        cur.execute(
            """
            SELECT id, job_id, mrn, stage, event_type, ts, attempt,
                   error_message, details, prev_hash, row_hash
            FROM events WHERE id = %s
            """,
            (row["id"],),
        )
        cols = [d[0] for d in cur.description]
        london_row = dict(zip(cols, cur.fetchone()))

    # Sanity check the reproduction is actually exercising the bug: the raw
    # datetime read back under Europe/London must carry a different UTC
    # offset than the one read back under UTC (August = BST = UTC+1) --
    # otherwise this test would be vacuously passing.
    assert london_row["ts"].utcoffset() != row["ts"].utcoffset()
    assert str(london_row["ts"]) != str(row["ts"])

    recomputed_london = compute_row_hash(
        london_row["prev_hash"], london_row["job_id"], london_row["mrn"], london_row["stage"],
        london_row["event_type"], london_row["ts"], london_row["attempt"],
        london_row["error_message"], london_row["details"],
    )
    assert recomputed_london == row["row_hash"]


def test_concurrent_add_event_calls_produce_no_errors_and_a_valid_chain(db, job_id):
    """
    The test that exercises the bug this section fixes: db.py's pool used to
    be a SimpleConnectionPool, explicitly not thread-safe (its own docstring
    says getconn/putconn do no locking). add_event's new `SELECT ... FOR
    UPDATE` means every call now does real DB work under a row lock; firing
    many of these concurrently, across real OS threads (matching how
    backend/src/common/sse.py's run_batch_job calls add_event via
    asyncio.to_thread), is exactly the scenario that would corrupt a
    SimpleConnectionPool's internal connection bookkeeping under load
    (double-checkout / connection leak / crash) if the ThreadedConnectionPool
    swap in backend/src/db.py hadn't also shipped.

    Best-effort note: this is a data race, not a deterministic failure --
    whether it manifests depends on GIL/thread scheduling and how much real
    OS-level parallelism the machine running this test actually has. When
    manually reverted to SimpleConnectionPool and stress-tested by hand
    with heavier, tightly-synchronized concurrent load (a threading.Barrier
    forcing many threads to hit the pool at the same instant), this did
    reproduce genuine corruption -- event_chain_state.last_hash left
    pointing at a hash matching no row in the table at all. It did *not*
    reliably reproduce as a plain `pytest -k concurrent` run at this scale
    on a 2-core sandbox, where GIL scheduling and asyncio's default
    thread-pool-executor cap on real parallelism narrow the race window.
    So: treat "this test passes" as confirming the fix doesn't break
    anything, not as proof the race is impossible to hit -- the
    correctness argument for the fix stands on its own (see
    backend/src/db.py's docstring), independent of whether this specific
    test can force the bug on any given machine.
    """
    db.create_job(job_id)
    N = 60
    _ensure_pool_sized_for_concurrency(N + 10)

    async def fire_all():
        await asyncio.gather(*[
            asyncio.to_thread(
                db.add_event, job_id, mrn=f"MRN{i}", stage="retrieve", event_type="success", details={"i": i}
            )
            for i in range(N)
        ])

    # No exception propagating out of gather() means no pool/connection
    # errors occurred under concurrent load.
    asyncio.run(fire_all())

    rows = _events_for_job(job_id)
    assert len(rows) == N
    assert all(r["row_hash"] is not None for r in rows)

    # End-to-end chain validity across the whole table (not just this job's
    # rows), exactly what an operator running verify_audit_chain.py would
    # check after a burst of concurrent batch-job activity. Passing
    # chain_state_last_hash too so this also catches the state-pointer
    # divergence a bare per-row walk can miss (see
    # test_verify_chain_detects_truncated_tail_without_reseeded_state).
    all_rows = fetch_events_in_order()
    ok, bad_row, reason = verify_chain(all_rows, chain_state_last_hash=fetch_chain_state())
    assert ok is True, f"chain broken at {bad_row}: {reason}"


def test_concurrent_add_event_via_thread_pool_executor(db, job_id):
    """
    Same stress scenario, but via a raw ThreadPoolExecutor rather than
    asyncio.to_thread, to confirm the pool itself (not just the asyncio
    scheduling wrapper) is safe under genuinely concurrent OS threads.

    Best-effort note (see the longer explanation on
    test_concurrent_add_event_calls_produce_no_errors_and_a_valid_chain):
    this is a data race, so whether reverting the ThreadedConnectionPool
    swap makes *this specific test* fail depends on the host's core count
    and scheduling -- it reliably reproduced under a manual, more
    tightly-synchronized stress script, but isn't guaranteed to on every
    machine at this test's scale. A pass here doesn't by itself prove the
    race can't happen; the fix's correctness argument is independent of it.
    """
    db.create_job(job_id)
    N = 50
    _ensure_pool_sized_for_concurrency(N + 10)

    def add(i):
        db.add_event(job_id, mrn=f"TP{i}", stage="export", event_type="success", details={"i": i})

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(add, i) for i in range(N)]
        for f in futures:
            f.result()  # re-raises if any thread hit an exception

    rows = _events_for_job(job_id)
    assert len(rows) == N

    all_rows = fetch_events_in_order()
    ok, bad_row, reason = verify_chain(all_rows, chain_state_last_hash=fetch_chain_state())
    assert ok is True, f"chain broken at {bad_row}: {reason}"
