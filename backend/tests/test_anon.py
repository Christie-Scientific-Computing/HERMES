import os

import pytest

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

from backend.src.identity import anon  # noqa: E402


def test_is_configured():
    assert anon.is_configured() is True


def test_lookup_real_ids_success():
    result = anon.lookup_real_ids(["1001", "1002"])
    assert result == {"1001": "500123", "1002": "500456"}


def test_lookup_real_ids_unknown_raises():
    with pytest.raises(anon.AnonLookupError):
        anon.lookup_real_ids(["1001", "424242"])


def test_lookup_real_ids_ignores_wrong_key_type():
    # 9999/999999 exists but under key_type_id=2, not 1 -- must not match
    with pytest.raises(anon.AnonLookupError):
        anon.lookup_real_ids(["9999"])


def test_lookup_anon_ids_success():
    result = anon.lookup_anon_ids(["500123", "500456"])
    assert result == {"500123": "1001", "500456": "1002"}


def test_lookup_anon_ids_unknown_returns_placeholder():
    result = anon.lookup_anon_ids(["500123", "000000"])
    assert result["500123"] == "1001"
    assert result["000000"] == "[unknown]"


def test_non_numeric_id_treated_as_unmapped():
    with pytest.raises(anon.AnonLookupError):
        anon.lookup_real_ids(["not-a-number"])


def test_resolve_and_display_roundtrip():
    real = anon.resolve_real_id("1001")
    assert real == "500123"
    back = anon.to_display_id(real)
    assert back == "1001"
