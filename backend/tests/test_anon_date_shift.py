"""
Tests for the date-shift mechanism added to backend/src/identity/anon.py
(docs/plans/pii-boundary-test-suite.md §B): get_date_perturbation(s) and
shift_date.

This test's own fixtures own the `date_perturbation` column and per-test
seeding against the live anon_test Postgres (port 55433) -- the shared
seed script promised by plan §E (backend/scripts/seed_anon_test_db.py)
doesn't exist yet (a later step), so this file is self-contained rather
than assuming it.
"""
import os

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import psycopg2
import pytest

from backend.src.identity import anon  # noqa: E402

REAL_MRN = "500123"
ANON_MRN = "1001"
REAL_MRN_2 = "500456"


def _conn():
    return psycopg2.connect(host="localhost", port=55433, dbname="anon_test", user="postgres", password="test")


@pytest.fixture(scope="module", autouse=True)
def _ensure_date_perturbation_column():
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE key_value ADD COLUMN IF NOT EXISTS date_perturbation INT")
    finally:
        conn.close()


def _set_perturbation(real_mrn: str, value) -> None:
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE key_value SET date_perturbation = %s WHERE key_value = %s AND key_type_id = 1",
                (value, int(real_mrn)),
            )
    finally:
        conn.close()


@pytest.fixture
def perturbation():
    """Seeds a known, non-zero perturbation for REAL_MRN; resets to NULL
    (the "nothing on record" state other tests in this module rely on)
    after the test, regardless of outcome."""
    _set_perturbation(REAL_MRN, 17)
    yield 17
    _set_perturbation(REAL_MRN, None)


@pytest.fixture
def no_perturbation():
    """Explicit NULL -- documents the "row exists but perturbation isn't
    set" case as distinct from "no row at all", both of which must raise."""
    _set_perturbation(REAL_MRN, None)
    yield
    _set_perturbation(REAL_MRN, None)


class TestGetDatePerturbations:
    def test_returns_seeded_value(self, perturbation):
        assert anon.get_date_perturbation(REAL_MRN) == perturbation

    def test_batch_returns_multiple(self, perturbation):
        _set_perturbation(REAL_MRN_2, -30)
        try:
            result = anon.get_date_perturbations([REAL_MRN, REAL_MRN_2])
            assert result == {REAL_MRN: 17, REAL_MRN_2: -30}
        finally:
            _set_perturbation(REAL_MRN_2, None)

    def test_negative_offset_is_preserved(self):
        _set_perturbation(REAL_MRN, -45)
        try:
            assert anon.get_date_perturbation(REAL_MRN) == -45
        finally:
            _set_perturbation(REAL_MRN, None)

    def test_zero_offset_is_not_treated_as_missing(self):
        # 0 is a legitimate perturbation (no shift needed for this patient),
        # not the same as "nothing on record" -- must not raise.
        _set_perturbation(REAL_MRN, 0)
        try:
            assert anon.get_date_perturbation(REAL_MRN) == 0
        finally:
            _set_perturbation(REAL_MRN, None)

    def test_missing_row_raises(self):
        with pytest.raises(anon.AnonLookupError):
            anon.get_date_perturbation("424242")  # not seeded at all

    def test_null_column_raises(self, no_perturbation):
        # A row exists (key_type_id=1 mapping for REAL_MRN) but
        # date_perturbation was never set -- must raise, not silently
        # default to 0 (see shift_date's fail-safe contract).
        with pytest.raises(anon.AnonLookupError):
            anon.get_date_perturbation(REAL_MRN)

    def test_non_numeric_id_raises(self):
        with pytest.raises(anon.AnonLookupError):
            anon.get_date_perturbation("not-a-number")

    def test_empty_list_returns_empty_dict(self):
        assert anon.get_date_perturbations([]) == {}

    def test_unconfigured_returns_zero_for_every_id(self, monkeypatch):
        monkeypatch.setattr(anon, "ANON_DB_HOST", None)
        assert anon.get_date_perturbations([REAL_MRN, REAL_MRN_2]) == {REAL_MRN: 0, REAL_MRN_2: 0}

    def test_unreachable_db_raises_service_error(self, monkeypatch):
        monkeypatch.setattr(anon, "ANON_DB_PORT", 1)
        monkeypatch.setattr(anon, "_pool", None)
        try:
            with pytest.raises(anon.AnonServiceError):
                anon.get_date_perturbation(REAL_MRN)
        finally:
            monkeypatch.setattr(anon, "_pool", None)


class TestShiftDate:
    def test_shifts_da_format_forward(self, perturbation):
        assert anon.shift_date(REAL_MRN, "20260101") == "20260118"  # +17 days

    def test_shifts_iso_format_forward(self, perturbation):
        assert anon.shift_date(REAL_MRN, "2026-01-01") == "2026-01-18"

    def test_shifts_negative_offset_backward(self):
        _set_perturbation(REAL_MRN, -10)
        try:
            assert anon.shift_date(REAL_MRN, "20260115") == "20260105"
        finally:
            _set_perturbation(REAL_MRN, None)

    def test_output_format_matches_input_format(self, perturbation):
        da_result = anon.shift_date(REAL_MRN, "20260101")
        iso_result = anon.shift_date(REAL_MRN, "2026-01-01")
        assert da_result == "20260118"
        assert iso_result == "2026-01-18"

    def test_month_boundary(self, perturbation):
        assert anon.shift_date(REAL_MRN, "20260131") == "20260217"  # Jan 31 + 17d

    def test_year_boundary(self, perturbation):
        assert anon.shift_date(REAL_MRN, "20261225") == "20270111"  # Dec 25 + 17d

    def test_leap_year_boundary(self):
        _set_perturbation(REAL_MRN, 1)
        try:
            assert anon.shift_date(REAL_MRN, "20240228") == "20240229"  # 2024 is a leap year
            assert anon.shift_date(REAL_MRN, "20230228") == "20230301"  # 2023 is not
        finally:
            _set_perturbation(REAL_MRN, None)

    def test_none_for_empty_input(self, perturbation):
        assert anon.shift_date(REAL_MRN, None) is None
        assert anon.shift_date(REAL_MRN, "") is None

    def test_none_for_non_date_shaped_input(self, perturbation):
        assert anon.shift_date(REAL_MRN, "not-a-date") is None

    def test_none_for_invalid_calendar_date(self, perturbation):
        assert anon.shift_date(REAL_MRN, "20261332") is None  # month 13

    def test_none_when_perturbation_missing_not_raw_date(self, no_perturbation):
        # THE fail-safe assertion: a lookup failure must never fall through
        # to returning the raw, unshifted real date.
        result = anon.shift_date(REAL_MRN, "20260101")
        assert result is None
        assert result != "20260101"

    def test_none_on_service_error(self, monkeypatch):
        monkeypatch.setattr(anon, "ANON_DB_PORT", 1)
        monkeypatch.setattr(anon, "_pool", None)
        try:
            assert anon.shift_date(REAL_MRN, "20260101") is None
        finally:
            monkeypatch.setattr(anon, "_pool", None)

    def test_passthrough_when_anon_not_configured(self, monkeypatch):
        monkeypatch.setattr(anon, "ANON_DB_HOST", None)
        assert anon.shift_date(REAL_MRN, "20260101") == "20260101"  # 0-day shift
