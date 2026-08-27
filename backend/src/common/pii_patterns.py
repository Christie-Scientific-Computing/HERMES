"""
Shared "what looks identifiable" detection/redaction, for free text where no
structured real_id/display_id pair is known ahead of time (or as a floor
applied even when one is).

This is the structural fix docs/pii-boundary-safety.md's finding #5 named as
missing: every prior scrub (results/endpoints.py's _scrub/_scrub_json) only
ever targeted one known real MRN by exact substring match. This module adds
a general pattern class -- dates, DICOM UIDs, filesystem paths, DB
connection strings -- on top of that, and a broader real-id-variant match
(zero-padded, float-cast) so a format change in how an id gets stringified
doesn't silently slip past a bare `==`/`in` check.

backend/tests/support/pii_assertions.py (test-only) is built directly on the
functions here rather than reimplementing its own detection, so the test
suite always checks against exactly what production code redacts.

Deliberately NOT date-shift-aware: this module has no access to
identity/anon.py's per-patient date_perturbation (that lookup needs a real
id and a DB round-trip, neither available to a generic redact() call at an
arbitrary free-text call site). A *shifted* date is expected to still look
date-shaped -- callers that legitimately return a shifted date (e.g.
studies/endpoints.py's study_date) do so through anon.shift_date directly,
not through redact(), and the test suite's SHIFTED_DATE_FIELDS allow-list
(pii_assertions.py) is what tells those fields apart from an accidental raw
one, by field name rather than by re-deriving the perturbation here.
"""
import re
from datetime import date as _date

# 6+ dot-separated numeric segments -- long enough that a short version
# string (e.g. plans.pinnacle_version = "16.2") never false-positives, but
# still catches any real DICOM UID shape (SOPInstanceUID, StudyInstanceUID,
# etc. are always much longer than that in practice).
UID_PATTERN = re.compile(r"\d+(?:\.\d+){5,}")

# DICOM DA (YYYYMMDD), ISO (YYYY-MM-DD), and slash-separated forms. Matched
# structurally, then validated as an actual calendar date (see _valid_ymd)
# so an 8-digit id/count/study-count doesn't false-positive -- a real date
# is always a valid one by construction, so validating loses no true
# positives, only trims noise.
_DA_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")
_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_SLASH_RE = re.compile(r"(?<!\d)(\d{1,4})/(\d{1,2})/(\d{1,4})(?!\d)")
DATE_PATTERNS = (_DA_RE, _ISO_RE, _SLASH_RE)

# Unix absolute/relative and Windows paths, matched structurally -- rooted
# at "/", "./", "../", or a drive letter -- rather than any specific known
# prefix like "./tmp/", so a path in an unanticipated location is still
# caught, not just the ones already found in the risk register.
_UNIX_PATH_RE = re.compile(r"(?:\.{1,2})?/[\w.\-]+(?:/[\w.\-]+)+")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[\w.\-]+\\)*[\w.\-]+")
# pathlib silently drops a leading "./" when a Path built from "./tmp" is
# stringified -- retrieve/export endpoints.py's own
# `Path("./tmp") / f"{job_id}_{filename}"` renders as the bare relative
# "tmp/<job_id>_<filename>", which _UNIX_PATH_RE's required "/"/"./"/"../"
# root would miss entirely, and pandas/open()'s own FileNotFoundError
# quotes that exact bare form. Extension-terminated so this doesn't swallow
# unrelated slash-separated text (a ratio, "and/or") that never ends in a
# dotted extension.
_BARE_RELATIVE_PATH_RE = re.compile(r"\b[\w\-]+(?:/[\w\-]+)*/[\w\-]+\.[A-Za-z][A-Za-z0-9]{0,7}\b")
# A user-uploaded filename can contain a space (e.g. "patient list.csv"),
# which the character-class-based patterns above can't span -- but Python's
# own OSError/FileNotFoundError repr always quotes the path
# (`'tmp/x patients.csv'`), so matching "anything single/double-quoted that
# contains a slash" catches the space-bearing case the structural patterns
# above miss, without needing to guess which characters a filename may hold.
_QUOTED_PATH_RE = re.compile(r"'[^'\n]*/[^'\n]*'|\"[^\"\n]*/[^\"\n]*\"")
PATH_PATTERNS = (_UNIX_PATH_RE, _WINDOWS_PATH_RE, _BARE_RELATIVE_PATH_RE, _QUOTED_PATH_RE)

# DB connection strings (postgres://user:pass@host/db, or any scheme://user:pass@...)
# and host:port pairs -- a bonus catch-all for a raw psycopg2 error echoing
# connection details (docs/pii-boundary-safety.md finding #3's underlying
# risk), cheap to add given the same mechanism. Host:port is deliberately
# restricted to a dotted hostname, an IPv4 address, or "localhost" -- not a
# bare `\w+:\d+`, which would false-positive on plenty of ordinary text
# (e.g. a ratio or a time-of-day-shaped string).
_CONNSTRING_RE = re.compile(r"\b\w+://[^\s@/]+:[^\s@/]+@\S+")
_HOSTPORT_RE = re.compile(
    r"\b(?:(?:\d{1,3}\.){3}\d{1,3}|(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z0-9\-]+|localhost):\d{2,5}\b"
)
SECRET_LIKE_PATTERNS = (_CONNSTRING_RE, _HOSTPORT_RE)

# Zero-padded width guesses for real_id_variants -- covers the id lengths
# already seen in this codebase's own fixtures/dev-seed data (6-digit MRNs)
# with headroom either side; harmless if a given width doesn't apply to a
# particular id, since results collapse into a set.
_ZERO_PAD_WIDTHS = (5, 6, 7, 8, 9, 10)


def _valid_ymd(y: str, m: str, d: str) -> bool:
    try:
        yi, mi, di = int(y), int(m), int(d)
    except ValueError:
        return False
    if not (1900 <= yi <= 2100):
        return False
    try:
        _date(yi, mi, di)
        return True
    except ValueError:
        return False


def find_dates(text: str) -> list[str]:
    """Every substring of `text` that looks like a real, unshifted calendar
    date in DICOM DA, ISO, or slash-separated form."""
    found = []
    for regex in (_DA_RE, _ISO_RE):
        for m in regex.finditer(text):
            if _valid_ymd(*m.groups()):
                found.append(m.group(0))
    for m in _SLASH_RE.finditer(text):
        a, b, c = m.groups()
        # Ambiguous group order (YYYY/MM/DD vs DD/MM/YYYY vs MM/DD/YYYY all
        # share this shape) -- accept whichever placement of the 4-digit
        # group produces a valid date.
        if len(a) == 4 and _valid_ymd(a, b, c):
            found.append(m.group(0))
        elif len(c) == 4 and (_valid_ymd(c, b, a) or _valid_ymd(c, a, b)):
            found.append(m.group(0))
    return found


def find_uids(text: str) -> list[str]:
    return [m.group(0) for m in UID_PATTERN.finditer(text)]


def find_paths(text: str) -> list[str]:
    """
    Every substring of `text` that looks like a filesystem path, across all
    of PATH_PATTERNS.

    A rooted match (e.g. "/var/hermes/tmp/foo.csv") and the bare-relative
    pattern both fire on overlapping text (the bare pattern has no way to
    know a "/"-rooted match already covers it), and a quoted match wholly
    contains whatever structural pattern matched inside the quotes -- so
    results are deduplicated down to maximal, non-overlapping matches
    (longest first, dropping anything that's a substring of an already-kept
    match) rather than returned as raw per-pattern hits.
    """
    found = []
    for pattern in PATH_PATTERNS:
        found.extend(m.group(0) for m in pattern.finditer(text))

    deduped: list[str] = []
    for candidate in sorted(set(found), key=len, reverse=True):
        if not any(candidate in kept for kept in deduped):
            deduped.append(candidate)
    return deduped


def find_secrets(text: str) -> list[str]:
    found = []
    for pattern in SECRET_LIKE_PATTERNS:
        found.extend(m.group(0) for m in pattern.finditer(text))
    return found


def real_id_variants(real_id) -> set[str]:
    """The format variants a real MRN might appear as in free text: exact
    string, int-cast, zero-padded (a few common widths), float-cast
    ("1234.0", the specific coercion bug this was written to catch -- a
    value that round-tripped through e.g. a pandas/polars numeric column
    somewhere upstream). A substring check against this whole set catches
    more than an exact-match `==`/`in` against the id as originally typed.
    """
    if real_id is None:
        return set()
    s = str(real_id)
    variants = {s}
    try:
        as_int = int(s)
    except (TypeError, ValueError):
        return variants
    variants.add(str(as_int))
    for width in _ZERO_PAD_WIDTHS:
        variants.add(str(as_int).zfill(width))
    variants.add(f"{as_int}.0")
    return variants


def redact(text, *, real_id=None, display_id=None) -> str:
    """
    Generalizes results/endpoints.py's _scrub: precise real-id -> display-id
    substitution when both are known (covering every variant from
    real_id_variants, not just the exact string), *plus* generic
    pattern-based redaction (dates/UIDs/paths/secrets) as a floor applied
    every time, even with no specific real id in scope -- what a
    request-agnostic call site (the global exception handler) uses.

    No-op on an empty/falsy input, matching _scrub's existing contract.
    """
    if not text:
        return text
    result = str(text)

    if real_id is not None and display_id is not None and str(real_id) != str(display_id):
        for variant in sorted(real_id_variants(real_id), key=len, reverse=True):
            if variant:
                result = result.replace(variant, str(display_id))

    # Order matters: secrets/UIDs are redacted before dates so an embedded
    # connection string or long numeric UID segment isn't left with stray
    # 8-digit runs that then false-positive as a date once its surrounding
    # structure (the "://", the extra "."-segments) has already been
    # replaced by a placeholder.
    for secret in find_secrets(result):
        result = result.replace(secret, "[redacted]")
    for uid in find_uids(result):
        result = result.replace(uid, "[redacted-uid]")
    for date_str in find_dates(result):
        result = result.replace(date_str, "[redacted-date]")
    for path in find_paths(result):
        result = result.replace(path, "[redacted-path]")
    return result
