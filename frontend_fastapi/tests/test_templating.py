from frontend_fastapi.templating import format_date, format_timestamp


def test_formats_an_iso_string_as_date_at_time():
    assert format_timestamp("2026-01-15T13:03:26.345775+00:00") == "2026-01-15 at 13:03:26"


def test_none_and_empty_string_pass_through_as_empty():
    assert format_timestamp(None) == ""
    assert format_timestamp("") == ""


def test_unparseable_value_falls_back_to_the_raw_value_instead_of_raising():
    # A malformed/unexpected timestamp must not 500 the whole page.
    assert format_timestamp("not-a-timestamp") == "not-a-timestamp"
    assert format_timestamp(12345) == "12345"


def test_format_date_strips_the_time_off_an_iso_datetime():
    assert format_date("2026-08-27T00:00:00+00:00") == "2026-08-27"


def test_format_date_none_and_empty_string_pass_through_as_empty():
    assert format_date(None) == ""
    assert format_date("") == ""


def test_format_date_unparseable_value_falls_back_to_the_raw_value():
    assert format_date("not-a-date") == "not-a-date"
    assert format_date(12345) == "12345"
