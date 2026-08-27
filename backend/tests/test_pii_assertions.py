"""
Tests for the test-only harness itself (backend/tests/support/pii_assertions.py)
-- proving assert_no_pii actually fires on a leak and stays quiet on clean
output before every other PII-boundary test in the suite starts relying on it.
"""
import json

import pytest

from backend.tests.support.pii_assertions import assert_no_pii, assert_date_shifted_correctly


class TestAssertNoPiiJson:
    def test_passes_on_clean_body(self):
        assert_no_pii(json.dumps({"mrn": "1001", "status": "success"}), real_ids=["500123"])

    def test_raises_on_real_id_leak(self):
        with pytest.raises(AssertionError):
            assert_no_pii(json.dumps({"error": "failed for 500123"}), real_ids=["500123"])

    def test_raises_on_zero_padded_real_id_variant(self):
        with pytest.raises(AssertionError):
            assert_no_pii(json.dumps({"error": "failed for 0500123"}), real_ids=["500123"])

    def test_raises_on_float_cast_real_id_variant(self):
        with pytest.raises(AssertionError):
            assert_no_pii(json.dumps({"error": "failed for 500123.0"}), real_ids=["500123"])

    def test_raises_on_raw_date_pattern(self):
        with pytest.raises(AssertionError):
            assert_no_pii(json.dumps({"note": "scanned 20260115"}))

    def test_raises_on_raw_uid_pattern(self):
        with pytest.raises(AssertionError):
            assert_no_pii(json.dumps({"note": "uid 1.2.840.10008.5.1.4.1.1.481.3"}))

    def test_raises_on_path_pattern(self):
        with pytest.raises(AssertionError):
            assert_no_pii(json.dumps({"error": "Could not read ./tmp/job1_patients.csv"}))

    def test_allowed_timestamp_field_is_exempt_from_date_pattern_ban(self):
        # created_at is an operational timestamp, not a clinical date --
        # ISO-shaped and expected, must not trip the generic date ban.
        assert_no_pii(json.dumps({"created_at": "2026-01-15T10:00:00Z"}))

    def test_shifted_date_field_is_exempt_from_date_pattern_ban(self):
        assert_no_pii(json.dumps({"study_date": "20260115"}))

    def test_nested_leak_is_found(self):
        body = json.dumps({"events": [{"details": {"nested": {"id": "500123"}}}]})
        with pytest.raises(AssertionError):
            assert_no_pii(body, real_ids=["500123"])

    def test_accepts_already_parsed_dict(self):
        with pytest.raises(AssertionError):
            assert_no_pii({"error": "failed for 500123"}, real_ids=["500123"])

    def test_real_date_check_is_independent_of_pattern_ban(self):
        # A shifted date in a SHIFTED_DATE_FIELDS field is normally exempt
        # from the generic pattern ban, but real_dates= must still catch
        # the raw unshifted value leaking through unshifted by mistake.
        with pytest.raises(AssertionError):
            assert_no_pii(json.dumps({"study_date": "20260115"}), real_dates=["20260115"])

    def test_numeric_leaf_real_id_is_caught(self):
        # Response.mrn is typed `str | int` in retrieve/export endpoints.py
        with pytest.raises(AssertionError):
            assert_no_pii(json.dumps({"mrn": 500123}), real_ids=["500123"])


class TestAssertNoPiiSse:
    def test_passes_on_clean_sse_stream(self):
        stream = 'data: {"type": "start", "total": 1}\n\ndata: {"type": "done"}\n\n'
        assert_no_pii(stream, real_ids=["500123"])

    def test_raises_on_leak_in_sse_event(self):
        stream = 'data: {"type": "error", "error": "failed for 500123"}\n\n'
        with pytest.raises(AssertionError):
            assert_no_pii(stream, real_ids=["500123"])


class TestAssertNoPiiFallback:
    def test_falls_back_to_flat_scan_on_plain_text(self):
        with pytest.raises(AssertionError):
            assert_no_pii("Internal Server Error: patient 500123 not found", real_ids=["500123"])

    def test_flat_scan_passes_on_clean_plain_text(self):
        assert_no_pii("Internal Server Error", real_ids=["500123"])


class TestAssertDateShiftedCorrectly:
    def test_positive_offset_da_format(self):
        assert_date_shifted_correctly("20260120", raw_value="20260115", perturbation_days=5, date_format="DA")

    def test_negative_offset_iso_format(self):
        assert_date_shifted_correctly(
            "2026-01-10", raw_value="2026-01-15", perturbation_days=-5, date_format="ISO"
        )

    def test_month_boundary_handled(self):
        assert_date_shifted_correctly("20260201", raw_value="20260131", perturbation_days=1, date_format="DA")

    def test_wrong_value_raises(self):
        with pytest.raises(AssertionError):
            assert_date_shifted_correctly("20260121", raw_value="20260115", perturbation_days=5, date_format="DA")

    def test_unknown_format_raises_value_error(self):
        with pytest.raises(ValueError):
            assert_date_shifted_correctly("x", raw_value="y", perturbation_days=1, date_format="MDY")
