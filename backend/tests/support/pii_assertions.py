"""
Test-only PII boundary-contract assertions, built directly on
backend/src/common/pii_patterns.py rather than a parallel reimplementation --
so the suite always checks against exactly what production code redacts, not
against some independently-drifting notion of "looks like PII".

House style: call assert_no_pii(resp.text, real_ids=[...]) (or resp.json())
on both the success path and an induced-failure path per endpoint, the same
way the existing test_*_anon_boundary.py files already assert
`REAL_MRN not in resp.text` -- this is a strict superset of that check.
"""
import json
from datetime import datetime, timedelta
from typing import Any, Optional

from backend.src.common import pii_patterns

# Operational/job timestamps -- describe HERMES's own timeline, not the
# patient's clinical history, so the generic date-*pattern* ban is skipped
# for these field names (decision 5 in docs/plans/pii-boundary-test-suite.md).
ALLOWED_TIMESTAMP_FIELDS = {"created_at", "submitted_at", "approved_at", "reviewed_at", "ts", "expiry_date"}

# A date-shaped string is *expected* here (the shifted value, see
# backend/src/identity/anon.py's shift_date) -- correctness for these is
# checked by assert_date_shifted_correctly below, not by absence.
SHIFTED_DATE_FIELDS = {"study_date", "series_date", "plan_date"}

_DATE_FORMATTERS = {
    "DA": ("%Y%m%d"),
    "ISO": ("%Y-%m-%d"),
}


def _date_variants(value: str) -> set[str]:
    """Every DA/ISO/slash rendering of a given raw date string.

    A caller of assert_no_pii(..., real_dates=[...]) only knows the raw date
    in whatever format they happened to seed it in (typically DICOM DA, from
    a fixture's StudyDate) -- but the endpoint under test might echo an
    unshifted leak in a different format (e.g. ISO). Exact-string-only
    matching against a single format would silently miss that. Falls back to
    just the literal value if it doesn't parse as a recognised date shape at
    all (e.g. a non-date string passed by mistake) -- still checked as-is,
    just with no extra variants added.
    """
    variants = {value}
    parsed = None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        return variants
    variants.add(parsed.strftime("%Y%m%d"))
    variants.add(parsed.strftime("%Y-%m-%d"))
    variants.add(parsed.strftime("%Y/%m/%d"))
    variants.add(parsed.strftime("%d/%m/%Y"))
    return variants


def _parse_sse(text: str) -> Optional[list]:
    """Parse concatenated `data: {...}\\n\\n` SSE events into a list of
    dicts. Returns None (not []) if any `data:` line isn't valid JSON, so
    callers can fall back to a flat text scan instead of silently checking
    zero events."""
    events = []
    saw_any = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        saw_any = True
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            return None
    return events if saw_any else None


def _to_parsed(body) -> Any:
    """Best-effort structure a response body into JSON-walkable data:
    already-parsed dict/list, a JSON string, or concatenated SSE `data:`
    events. Returns None if none of those apply (e.g. a plain-text error
    page), so the caller knows to fall back to a flat regex scan."""
    if isinstance(body, (dict, list)):
        return body
    text = body if isinstance(body, str) else str(body)
    if "data:" in text:
        parsed = _parse_sse(text)
        if parsed is not None:
            return parsed
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _check_leaf(
    path: str, key: Optional[str], value: str, real_id_variants: set, real_date_variants: set, errors: list
) -> None:
    for variant in real_id_variants:
        if variant and variant in value:
            errors.append(f"{path}: contains real-id variant {variant!r} in {value!r}")
    for variant in real_date_variants:
        if variant and variant in value:
            errors.append(f"{path}: contains raw real date variant {variant!r} in {value!r}")
    if key not in ALLOWED_TIMESTAMP_FIELDS and key not in SHIFTED_DATE_FIELDS:
        for found in pii_patterns.find_dates(value):
            errors.append(f"{path}: looks like a raw date {found!r} in {value!r}")
    for found in pii_patterns.find_uids(value):
        errors.append(f"{path}: looks like a DICOM UID {found!r} in {value!r}")
    for found in pii_patterns.find_paths(value):
        errors.append(f"{path}: looks like a filesystem path {found!r} in {value!r}")
    for found in pii_patterns.find_secrets(value):
        errors.append(f"{path}: looks like a secret/connection string in {value!r}")


def _walk(value, path: str, key: Optional[str], real_id_variants: set, real_date_variants: set, errors: list) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _walk(v, f"{path}.{k}", k, real_id_variants, real_date_variants, errors)
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _walk(v, f"{path}[{i}]", key, real_id_variants, real_date_variants, errors)
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (str, int, float)):
        # int/float leaves included -- Response.mrn is typed `str | int`
        # (retrieve/export endpoints.py), and a float-cast real id
        # ("500123.0") is exactly the coercion bug real_id_variants exists
        # to catch, so a numeric leaf gets the same string-substring checks
        # a string leaf would.
        s = str(value)
        if s:
            _check_leaf(path, key, s, real_id_variants, real_date_variants, errors)


def assert_no_pii(body, *, real_ids=(), real_dates=(), context: str = "") -> None:
    """
    Parse `body` (a raw response/SSE-stream string, or an already-parsed
    dict/list) and recursively assert no string/numeric leaf anywhere in it:
      - contains any format variant of a real id in `real_ids` (see
        pii_patterns.real_id_variants -- catches int/zero-padded/float-cast
        forms, not just the exact string the test happens to pass);
      - contains any raw (unshifted) date in `real_dates` verbatim;
      - matches a generic date/UID/path/secret pattern (pii_patterns.py),
        unless the field name is in ALLOWED_TIMESTAMP_FIELDS or
        SHIFTED_DATE_FIELDS.

    Falls back to a flat scan over the raw text when `body` isn't valid
    JSON or an SSE event stream (e.g. a plain-text error page) -- real-id
    and real-date checks still apply; the date-pattern check applies as if
    under no field name (nothing is exempt in unstructured text).
    """
    real_id_variants: set = set()
    for rid in real_ids:
        real_id_variants |= pii_patterns.real_id_variants(rid)
    real_date_variants: set = set()
    for rd in real_dates:
        real_date_variants |= _date_variants(str(rd))

    errors: list = []
    parsed = _to_parsed(body)
    if parsed is not None:
        _walk(parsed, "$", None, real_id_variants, real_date_variants, errors)
    else:
        text = body if isinstance(body, str) else str(body)
        _check_leaf("$", None, text, real_id_variants, real_date_variants, errors)

    if errors:
        prefix = f"[{context}] " if context else ""
        raise AssertionError(prefix + "PII leak(s) detected:\n" + "\n".join(f"  - {e}" for e in errors))


def assert_date_shifted_correctly(
    returned_value: str, *, raw_value: str, perturbation_days: int, date_format: str = "DA"
) -> None:
    """
    Computes the expected shifted date independently of
    backend/src/identity/anon.py's own shift_date (deliberately -- asserting
    against the same function under test would prove nothing) from a known
    raw value + known perturbation, and asserts exact equality.

    `date_format` is "DA" (DICOM, YYYYMMDD) or "ISO" (YYYY-MM-DD) -- must
    match the format of both `raw_value` and `returned_value`.
    """
    if date_format not in _DATE_FORMATTERS:
        raise ValueError(f"Unknown date_format: {date_format!r}")
    fmt = _DATE_FORMATTERS[date_format]
    expected = (datetime.strptime(raw_value, fmt).date() + timedelta(days=perturbation_days)).strftime(fmt)
    assert returned_value == expected, (
        f"expected date shifted by {perturbation_days}d from {raw_value!r} to be {expected!r}, "
        f"got {returned_value!r}"
    )
