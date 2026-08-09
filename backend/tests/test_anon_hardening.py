"""
Tests for docs/safety-plan.md §B1: the two backend/src/identity/anon.py
additions --

1. Standard TLS opt-in (ANON_DB_SSLMODE / ANON_DB_SSLROOTCERT), passed
   through to the connection pool only when actually set. There's no live
   TLS-enabled Postgres in this environment, so these tests verify the
   *configuration wiring* (kwargs built / forwarded correctly), not an
   actual TLS handshake -- that's out of scope for this module.
2. Application-side lookup-volume monitoring: a rolling in-process counter
   that logs a warning once lookups exceed a configurable threshold within
   a configurable window.
"""
import logging
import os

import pytest

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

from backend.src.identity import anon  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Isolate each test from the module-global pool/counter state -- both
    are process-global, like _pool already was before this change."""
    monkeypatch.setattr(anon, "_pool", None)
    monkeypatch.setattr(anon, "_lookup_window_start", None)
    monkeypatch.setattr(anon, "_lookup_window_count", 0)
    monkeypatch.setattr(anon, "_lookup_window_warned", False)
    yield
    monkeypatch.setattr(anon, "_pool", None)


# ── §B1 part 1: TLS opt-in wiring ───────────────────────────────────────────

def test_connection_kwargs_omit_ssl_when_unset(monkeypatch):
    monkeypatch.setattr(anon, "ANON_DB_SSLMODE", None)
    monkeypatch.setattr(anon, "ANON_DB_SSLROOTCERT", None)
    kwargs = anon._connection_kwargs()
    assert "sslmode" not in kwargs
    assert "sslrootcert" not in kwargs
    # unaffected fields are still present
    assert kwargs["host"] == anon.ANON_DB_HOST
    assert kwargs["dbname"] == anon.ANON_DB_NAME


def test_connection_kwargs_include_sslmode_when_set(monkeypatch):
    monkeypatch.setattr(anon, "ANON_DB_SSLMODE", "require")
    monkeypatch.setattr(anon, "ANON_DB_SSLROOTCERT", None)
    kwargs = anon._connection_kwargs()
    assert kwargs["sslmode"] == "require"
    assert "sslrootcert" not in kwargs


def test_connection_kwargs_include_sslrootcert_when_set(monkeypatch):
    monkeypatch.setattr(anon, "ANON_DB_SSLMODE", "verify-full")
    monkeypatch.setattr(anon, "ANON_DB_SSLROOTCERT", "/etc/ssl/certs/anon-ca.pem")
    kwargs = anon._connection_kwargs()
    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["sslrootcert"] == "/etc/ssl/certs/anon-ca.pem"


def test_get_pool_forwards_ssl_kwargs_to_connection_pool(monkeypatch):
    """_get_pool must pass whatever _connection_kwargs() builds straight
    through to the pool constructor, which forwards **kwargs to
    psycopg2.connect -- sslmode/sslrootcert are ordinary libpq params it
    already accepts."""
    captured = {}

    class FakePool:
        def __init__(self, minconn, maxconn, **kwargs):
            captured["minconn"] = minconn
            captured["maxconn"] = maxconn
            captured.update(kwargs)

    monkeypatch.setattr(anon, "SimpleConnectionPool", FakePool)
    monkeypatch.setattr(anon, "ANON_DB_SSLMODE", "require")
    monkeypatch.setattr(anon, "ANON_DB_SSLROOTCERT", None)

    pool = anon._get_pool()

    assert isinstance(pool, FakePool)
    assert captured["sslmode"] == "require"
    assert "sslrootcert" not in captured


def test_get_pool_omits_ssl_kwargs_when_unset(monkeypatch):
    captured = {}

    class FakePool:
        def __init__(self, minconn, maxconn, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(anon, "SimpleConnectionPool", FakePool)
    monkeypatch.setattr(anon, "ANON_DB_SSLMODE", None)
    monkeypatch.setattr(anon, "ANON_DB_SSLROOTCERT", None)

    anon._get_pool()

    assert "sslmode" not in captured
    assert "sslrootcert" not in captured


# ── §B1 part 2: lookup-volume monitoring, unit-level (deterministic clock) ─

def test_note_lookup_volume_does_not_warn_below_threshold(monkeypatch, caplog):
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_THRESHOLD", 10)
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_WINDOW_SECONDS", 3600)
    monkeypatch.setattr(anon.time, "monotonic", lambda: 100.0)

    with caplog.at_level(logging.WARNING, logger=anon.logger.name):
        anon._note_lookup_volume(4)
        anon._note_lookup_volume(4)

    assert not any("lookup volume" in r.message for r in caplog.records)


def test_note_lookup_volume_warns_once_past_threshold(monkeypatch, caplog):
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_THRESHOLD", 10)
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_WINDOW_SECONDS", 3600)
    monkeypatch.setattr(anon.time, "monotonic", lambda: 100.0)

    with caplog.at_level(logging.WARNING, logger=anon.logger.name):
        anon._note_lookup_volume(6)   # count=6, under threshold
        anon._note_lookup_volume(6)   # count=12, over threshold -> warns
        anon._note_lookup_volume(6)   # count=18, still over -> does NOT warn again

    warnings = [r for r in caplog.records if "lookup volume" in r.message]
    assert len(warnings) == 1


def test_note_lookup_volume_resets_after_window_elapses(monkeypatch, caplog):
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_THRESHOLD", 10)
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_WINDOW_SECONDS", 60)
    clock = {"t": 100.0}
    monkeypatch.setattr(anon.time, "monotonic", lambda: clock["t"])

    with caplog.at_level(logging.WARNING, logger=anon.logger.name):
        anon._note_lookup_volume(12)  # over threshold within window 1 -> warns
        clock["t"] = 200.0            # window (60s) has elapsed
        anon._note_lookup_volume(3)   # fresh window, well under threshold

    warnings = [r for r in caplog.records if "lookup volume" in r.message]
    assert len(warnings) == 1


def test_note_lookup_volume_warns_again_in_a_new_window(monkeypatch, caplog):
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_THRESHOLD", 10)
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_WINDOW_SECONDS", 60)
    clock = {"t": 100.0}
    monkeypatch.setattr(anon.time, "monotonic", lambda: clock["t"])

    with caplog.at_level(logging.WARNING, logger=anon.logger.name):
        anon._note_lookup_volume(12)  # window 1: over threshold -> warns
        clock["t"] = 200.0            # window elapses
        anon._note_lookup_volume(12)  # window 2: over threshold again -> warns

    warnings = [r for r in caplog.records if "lookup volume" in r.message]
    assert len(warnings) == 2


# ── §B1 part 2: lookup-volume monitoring, through the real lookup path ────

def test_lookup_real_ids_path_warns_past_threshold(monkeypatch, caplog):
    """Calls the actual public lookup path (not just the counter helper)
    enough times to cross the threshold, against the seeded anon-mapping
    test DB (see test_anon.py's header for the schema/data)."""
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_THRESHOLD", 3)
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_WINDOW_SECONDS", 3600)

    with caplog.at_level(logging.WARNING, logger=anon.logger.name):
        for _ in range(5):
            anon.lookup_real_ids(["1001"])

    assert any("lookup volume" in r.message for r in caplog.records)


def test_lookup_real_ids_path_silent_below_threshold(monkeypatch, caplog):
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_THRESHOLD", 1000)
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_WINDOW_SECONDS", 3600)

    with caplog.at_level(logging.WARNING, logger=anon.logger.name):
        for _ in range(5):
            anon.lookup_real_ids(["1001"])

    assert not any("lookup volume" in r.message for r in caplog.records)


def test_lookup_volume_counts_even_when_ids_are_unknown(monkeypatch, caplog):
    """The counter tracks lookup *attempts*, not just successful ones -- a
    bulk-exfiltration probe hammering unknown/invalid ids should still be
    visible, not silently excluded because every individual call raises."""
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_THRESHOLD", 3)
    monkeypatch.setattr(anon, "ANON_LOOKUP_WARN_WINDOW_SECONDS", 3600)

    with caplog.at_level(logging.WARNING, logger=anon.logger.name):
        for i in range(5):
            with pytest.raises(anon.AnonLookupError):
                anon.lookup_real_ids([f"unknown-{i}-999999"])

    assert any("lookup volume" in r.message for r in caplog.records)
