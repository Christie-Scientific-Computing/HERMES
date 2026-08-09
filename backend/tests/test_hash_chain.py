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
from backend.scripts.verify_audit_chain import fetch_events_in_order, verify_chain


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


def _chain_state_matches_last_row() -> tuple[bool, str]:
    """
    Cross-check event_chain_state.last_hash (the "next writer, read this"
    pointer add_event's FOR UPDATE guards) against the row_hash of the
    actual highest-id row in events (the true last write). verify_chain
    alone can't catch a divergence here -- it only checks relative linking
    *within* the rows that exist, so if event_chain_state.last_hash ends up
    pointing at a hash that belongs to no row at all (which is exactly what
    a lost/interleaved update under an unsafe pool can produce -- see this
    file's module docstring), every row already in the table can still look
    perfectly self-consistent while the chain's live continuation point has
    silently gone stale. This is the sharper of the two checks for a
    concurrency bug: it catches corruption immediately, in the state that
    exists right after the race, rather than waiting for a future insert to
    surface it as a prev_hash mismatch.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_hash FROM event_chain_state WHERE id = 1")
        (last_hash,) = cur.fetchone()
        cur.execute("SELECT row_hash FROM events ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        last_row_hash = row[0] if row else None
    if last_hash != last_row_hash:
        return False, f"event_chain_state.last_hash={last_hash!r} does not match the last row's row_hash={last_row_hash!r}"
    return True, ""


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
    # check after a burst of concurrent batch-job activity.
    all_rows = fetch_events_in_order()
    ok, bad_row, reason = verify_chain(all_rows)
    assert ok is True, f"chain broken at {bad_row}: {reason}"

    # Sharper still: the live event_chain_state pointer must match the
    # actual last-written row, not just "the rows that exist are internally
    # consistent" -- see _chain_state_matches_last_row's docstring for why
    # this catches corruption a bare verify_chain pass can miss.
    state_ok, state_reason = _chain_state_matches_last_row()
    assert state_ok, state_reason


def test_concurrent_add_event_via_thread_pool_executor(db, job_id):
    """Same stress scenario, but via a raw ThreadPoolExecutor rather than
    asyncio.to_thread, to confirm the pool itself (not just the asyncio
    scheduling wrapper) is safe under genuinely concurrent OS threads."""
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
    ok, bad_row, reason = verify_chain(all_rows)
    assert ok is True, f"chain broken at {bad_row}: {reason}"

    state_ok, state_reason = _chain_state_matches_last_row()
    assert state_ok, state_reason
