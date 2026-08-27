"""
Unit tests for backend/src/common/pii_patterns.py -- the shared
date/UID/path/secret detection and redaction module underpinning both
production redaction call sites and backend/tests/support/pii_assertions.py.
"""
from backend.src.common import pii_patterns


class TestFindDates:
    def test_finds_valid_da_date(self):
        assert pii_patterns.find_dates("scan on 20260115 was fine") == ["20260115"]

    def test_finds_valid_iso_date(self):
        assert pii_patterns.find_dates("scan on 2026-01-15 was fine") == ["2026-01-15"]

    def test_finds_valid_slash_date_year_first(self):
        assert pii_patterns.find_dates("scan on 2026/01/15") == ["2026/01/15"]

    def test_finds_valid_slash_date_day_first(self):
        assert pii_patterns.find_dates("scan on 15/01/2026") == ["15/01/2026"]

    def test_rejects_invalid_calendar_date(self):
        # 13th month, 32nd day -- digit-shaped but not a real date
        assert pii_patterns.find_dates("code 20261332") == []
        assert pii_patterns.find_dates("code 20260132" ) == []  # no such day

    def test_rejects_out_of_range_year(self):
        assert pii_patterns.find_dates("id 18990101") == []

    def test_does_not_false_positive_on_arbitrary_8_digit_number(self):
        # A plausible-but-coincidental 8-digit number that isn't a real date
        assert pii_patterns.find_dates("count 99999999") == []

    def test_finds_multiple_dates(self):
        assert pii_patterns.find_dates("from 20260101 to 20260201") == ["20260101", "20260201"]


class TestFindUids:
    def test_finds_dicom_style_uid(self):
        uid = "1.2.840.10008.5.1.4.1.1.481.3"
        assert pii_patterns.find_uids(f"SOPInstanceUID={uid}") == [uid]

    def test_does_not_match_short_version_string(self):
        assert pii_patterns.find_uids("pinnacle_version 16.2") == []

    def test_does_not_match_five_segment_number(self):
        assert pii_patterns.find_uids("1.2.3.4.5") == []

    def test_matches_at_six_segment_boundary(self):
        assert pii_patterns.find_uids("1.2.3.4.5.6") == ["1.2.3.4.5.6"]


class TestFindPaths:
    def test_finds_unix_absolute_path(self):
        assert pii_patterns.find_paths("wrote to /var/hermes/tmp/foo.csv") == ["/var/hermes/tmp/foo.csv"]

    def test_finds_unix_relative_path(self):
        assert pii_patterns.find_paths("Could not read ./tmp/job_1/file.csv") == ["./tmp/job_1/file.csv"]

    def test_finds_windows_path(self):
        assert pii_patterns.find_paths(r"saved to C:\Users\hermes\file.csv") == [r"C:\Users\hermes\file.csv"]

    def test_does_not_match_plain_word(self):
        assert pii_patterns.find_paths("status: success") == []

    def test_does_not_match_bare_slash_fraction(self):
        assert pii_patterns.find_paths("3/4 imported") == []


class TestFindSecrets:
    def test_finds_postgres_connection_string(self):
        text = "connection failed: postgres://hermes:hunter2@db.internal:5432/hermesdb"
        found = pii_patterns.find_secrets(text)
        assert found and found[0].startswith("postgres://hermes:hunter2@")

    def test_finds_dotted_hostport(self):
        assert pii_patterns.find_secrets("could not reach db.internal:5432") == ["db.internal:5432"]

    def test_finds_localhost_port(self):
        assert pii_patterns.find_secrets("could not reach localhost:5432") == ["localhost:5432"]

    def test_finds_ipv4_port(self):
        assert pii_patterns.find_secrets("could not reach 10.0.0.5:5432") == ["10.0.0.5:5432"]

    def test_does_not_match_time_of_day(self):
        assert pii_patterns.find_secrets("started at 12:30") == []


class TestRealIdVariants:
    def test_includes_exact_string(self):
        assert "500123" in pii_patterns.real_id_variants("500123")

    def test_includes_zero_padded_forms(self):
        variants = pii_patterns.real_id_variants("500123")
        assert "0500123" in variants
        assert "00500123" in variants

    def test_includes_float_cast_form(self):
        assert "500123.0" in pii_patterns.real_id_variants("500123")

    def test_non_numeric_id_returns_only_exact_string(self):
        assert pii_patterns.real_id_variants("not-a-number") == {"not-a-number"}

    def test_none_returns_empty_set(self):
        assert pii_patterns.real_id_variants(None) == set()


class TestRedact:
    def test_noop_on_empty_text(self):
        assert pii_patterns.redact("") == ""
        assert pii_patterns.redact(None) is None

    def test_substitutes_real_id_for_display_id(self):
        assert pii_patterns.redact("failed for 500123", real_id="500123", display_id="1001") == "failed for 1001"

    def test_substitutes_zero_padded_variant(self):
        assert pii_patterns.redact("failed for 0500123", real_id="500123", display_id="1001") == "failed for 1001"

    def test_substitutes_float_cast_variant(self):
        assert pii_patterns.redact("failed for 500123.0", real_id="500123", display_id="1001") == "failed for 1001"

    def test_noop_when_real_equals_display(self):
        # passthrough mode -- real_id == display_id, nothing to substitute
        assert pii_patterns.redact("failed for 500123", real_id="500123", display_id="500123") == "failed for 500123"

    def test_redacts_date_with_no_real_id_in_scope(self):
        result = pii_patterns.redact("scan performed 20260115")
        assert "20260115" not in result
        assert "[redacted-date]" in result

    def test_redacts_uid_with_no_real_id_in_scope(self):
        result = pii_patterns.redact("uid 1.2.840.10008.5.1.4.1.1.481.3 failed")
        assert "1.2.840.10008.5.1.4.1.1.481.3" not in result
        assert "[redacted-uid]" in result

    def test_redacts_path_with_no_real_id_in_scope(self):
        result = pii_patterns.redact("Could not read CSV: ./tmp/job1_patients.csv")
        assert "./tmp/job1_patients.csv" not in result
        assert "[redacted-path]" in result

    def test_redacts_connection_string(self):
        result = pii_patterns.redact("db error: postgres://hermes:hunter2@db.internal:5432/hermesdb")
        assert "hunter2" not in result
        assert "[redacted]" in result

    def test_redacts_everything_together(self):
        text = (
            "lookup failed for 500123 on 20260115 reading ./tmp/j1_p.csv "
            "uid 1.2.840.10008.5.1.4.1.1.481.3 via postgres://u:p@db.internal:5432/d"
        )
        result = pii_patterns.redact(text, real_id="500123", display_id="1001")
        assert "500123" not in result
        assert "20260115" not in result
        assert "./tmp/j1_p.csv" not in result
        assert "1.2.840.10008.5.1.4.1.1.481.3" not in result
        assert "u:p@" not in result
        assert "1001" in result
