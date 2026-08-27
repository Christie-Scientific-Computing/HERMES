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

    def test_is_not_quadratic_on_adversarial_input(self):
        # A long dot-less digit run with no valid UID shape anywhere forces
        # the engine to retry the match at every digit position; unbounded
        # segment-length/repetition-count quantifiers made this O(n^2)
        # (14.6s on 40k digits before bounding).
        import time

        start = time.monotonic()
        pii_patterns.find_uids("9" * 100_000)
        assert time.monotonic() - start < 2.0


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

    def test_finds_bare_relative_path_with_no_leading_slash_or_dot(self):
        # Path("./tmp") / f"{job_id}_{filename}" stringifies WITHOUT the
        # leading "./" (pathlib drops it) -- FileNotFoundError/pandas quote
        # exactly this bare relative form.
        assert pii_patterns.find_paths("tmp/9f1c2b3a_patients.csv") == ["tmp/9f1c2b3a_patients.csv"]

    def test_finds_quoted_path_containing_spaces(self):
        text = "No such file or directory: 'tmp/9f1c2b3a_patient list.csv'"
        found = pii_patterns.find_paths(text)
        assert "tmp/9f1c2b3a_patient list.csv" in " ".join(found)

    def test_finds_double_quoted_path(self):
        text = 'No such file or directory: "tmp/9f1c2b3a_patients.csv"'
        found = pii_patterns.find_paths(text)
        assert any("tmp/9f1c2b3a_patients.csv" in f for f in found)

    def test_two_distinct_occurrences_of_the_same_filename_are_not_collapsed(self):
        # Positional dedup must not drop a genuinely separate, later
        # occurrence just because its text is a substring of an earlier,
        # unrelated match elsewhere in the string.
        text = "export A at tmp/patients.csv done; separate retry at archive/tmp/patients.csv done"
        found = pii_patterns.find_paths(text)
        joined = " ".join(found)
        assert "tmp/patients.csv" in joined
        assert "archive/tmp/patients.csv" in joined
        # both redacted, not just the first
        redacted = pii_patterns.redact(text)
        assert "patients.csv" not in redacted

    def test_quoted_path_match_is_length_bounded(self):
        # An unrelated, distant quote character elsewhere in the same
        # string must not let the quoted-path match swallow everything
        # between it and the real path.
        far_prefix = "x" * 1000
        text = f"log: '{far_prefix}' and separately 'tmp/real_leak.csv' happened"
        found = pii_patterns.find_paths(text)
        assert any(len(f) < 400 for f in found)
        assert not any(len(f) > 900 for f in found)

    def test_bare_relative_pattern_over_redacts_ordinary_prose_by_design(self):
        # Documents an accepted tradeoff (docs/pii-boundary-safety.md SS3:
        # over-redaction is the safe failure mode) rather than a bug --
        # an unrooted word/word.ext shape can't be distinguished from a
        # real relative path by regex alone.
        assert pii_patterns.find_paths("see input/output.py for details") != []

    def test_partially_overlapping_matches_are_fully_redacted_not_dropped(self):
        # A rooted match and a bare-relative match can overlap WITHOUT
        # either fully containing the other (unlike the earlier
        # same-filename-twice case above) -- a contained-spans-only dedup
        # would drop the shorter one outright and leave its non-overlapping
        # tail un-redacted. Both repros below leaked a bare filename
        # ("patients.csv"/"output.csv") under that earlier version.
        assert "patients.csv" not in pii_patterns.redact("archive/backup.v2-1/patients.csv missing")
        assert "output.csv" not in pii_patterns.redact("wrote logs/run.log-3/output.csv")

    def test_bare_relative_pattern_is_not_quadratic_on_adversarial_input(self):
        # A long run of "/"-separated segments with no valid trailing
        # extension anywhere forces the regex engine to retry the match at
        # every word-boundary start position; unbounded segment/repetition
        # quantifiers made this O(n^2) (multi-second on ~16k chars).
        # Bounded quantifiers should keep this comfortably sub-second even
        # at 100k chars.
        import time

        text = "a/" * 100_000 + "a"
        start = time.monotonic()
        pii_patterns.find_paths(text)
        assert time.monotonic() - start < 2.0


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

    def test_hostport_is_not_quadratic_on_adversarial_input(self):
        # A long dotted run with no port anywhere forces the engine to
        # retry at every label boundary; unbounded label length/count
        # quantifiers made this O(n^2) (5.8s on 16k chars before bounding).
        import time

        start = time.monotonic()
        pii_patterns.find_secrets("a." * 100_000)
        assert time.monotonic() - start < 2.0


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
        assert "[redacted]" in result

    def test_redacts_uid_with_no_real_id_in_scope(self):
        result = pii_patterns.redact("uid 1.2.840.10008.5.1.4.1.1.481.3 failed")
        assert "1.2.840.10008.5.1.4.1.1.481.3" not in result
        assert "[redacted]" in result

    def test_redacts_path_with_no_real_id_in_scope(self):
        result = pii_patterns.redact("Could not read CSV: ./tmp/job1_patients.csv")
        assert "./tmp/job1_patients.csv" not in result
        assert "[redacted]" in result

    def test_cross_category_overlap_does_not_leak(self):
        # A UID immediately followed by an MRN inside a path used to leak
        # the MRN: redacting the UID first (via a sequential category pass)
        # left "_MRN500123_" un-redacted because the UID's placeholder broke
        # up what would otherwise have been one longer path match on the
        # original text. Spans from every category must be computed against
        # the SAME pristine text and merged together, not chained.
        result = pii_patterns.redact(
            "/tmp/scan_1.2.840.10008.5.1.4.1.1.481.3_MRN500123_results.csv"
        )
        assert "500123" not in result

    def test_bare_digits_adjacent_to_a_uid_with_no_path_or_real_id_are_not_caught(self):
        # NOT a regression -- documents a real, accepted boundary of the
        # generic floor. Without a path/date/secret shape around it and
        # with no real_id passed, a bare digit run has no structural
        # signature distinguishing it from any other number (a count, a
        # job id, ...): redact()'s generic patterns can only catch
        # dates/UIDs/paths/secrets, never an arbitrary embedded id in free
        # prose. A caller who knows the specific real id in play must pass
        # real_id=/display_id= for that precise substitution -- this is the
        # same documented blind spot the plan names for names/prose dates.
        result = pii_patterns.redact("error for 1.2.840.10008.5.1.4.1.1.481.3_MRN500123 during export")
        assert "500123" in result  # unredacted, as expected, not a leak fix target

    def test_redacts_connection_string(self):
        result = pii_patterns.redact("db error: postgres://hermes:hunter2@db.internal:5432/hermesdb")
        assert "hunter2" not in result
        assert "[redacted]" in result

    def test_redacts_bare_relative_tmp_path(self):
        result = pii_patterns.redact("Could not read CSV: [Errno 2] No such file or directory: 'tmp/9f1c2b3a_patients.csv'")
        assert "9f1c2b3a_patients.csv" not in result

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


class TestRedactDict:
    def test_redacts_string_values_only(self):
        result = pii_patterns.redact_dict(
            {"mosaiq_reason": "failed for 500123", "in_mosaiq": False, "study_count": 2},
            real_id="500123", display_id="1001",
        )
        assert result == {"mosaiq_reason": "failed for 1001", "in_mosaiq": False, "study_count": 2}

    def test_does_not_recurse_into_nested_structures(self):
        # By design -- every direct caller passes a flat Response.model_dump(),
        # not a JSONB blob; results/endpoints.py's _scrub_json handles the
        # genuinely-nested case separately.
        result = pii_patterns.redact_dict(
            {"nested": {"id": "500123"}}, real_id="500123", display_id="1001",
        )
        assert result == {"nested": {"id": "500123"}}

    def test_none_input_returns_empty_dict(self):
        assert pii_patterns.redact_dict(None) == {}

    def test_empty_dict_returns_empty_dict(self):
        assert pii_patterns.redact_dict({}) == {}

    def test_no_real_id_still_applies_generic_floor(self):
        result = pii_patterns.redact_dict({"note": "scanned 20260115"})
        assert "20260115" not in result["note"]

    def test_does_not_mutate_input(self):
        original = {"reason": "failed for 500123"}
        pii_patterns.redact_dict(original, real_id="500123", display_id="1001")
        assert original == {"reason": "failed for 500123"}
