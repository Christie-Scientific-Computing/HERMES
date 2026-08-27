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
# etc. are always much longer than that in practice). Segment length and
# repetition count are bounded ({1,20} digits/segment, up to 50 segments --
# a real DICOM UID is nowhere near either limit, the DICOM standard itself
# caps a UID at 64 chars total) for the same time-complexity reason
# _BARE_RELATIVE_PATH_RE below is bounded: an unbounded `\d+(?:\.\d+){5,}`
# against a long all-digit string with no dots anywhere forces the engine
# to retry a failing match at every digit position, going O(n^2) --
# confirmed via a 40k-digit adversarial input (14.6s unbounded, <0.05s
# bounded, identical matches on every realistic UID shape).
UID_PATTERN = re.compile(r"\d{1,20}(?:\.\d{1,20}){5,50}")

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
# dotted extension -- but being unrooted, it WILL still flag ordinary prose
# that happens to look like "word/word.ext" (e.g. "input/output.py",
# "etc/config.yaml"). That's an accepted false-positive, not an oversight:
# this module's whole design (docs/pii-boundary-safety.md SS3) treats
# over-redaction as the safe failure mode and under-redaction as the unsafe
# one -- see test_bare_relative_pattern_over_redacts_ordinary_prose_by_design.
#
# Every quantifier here is bounded ({1,255} per segment -- 255 is the
# actual max filename length on most filesystems -- {1,32} segments) -- NOT
# for correctness (an unbounded `+` would match the same realistic paths)
# but for time complexity: this is meant to run on arbitrary free text,
# including (per later steps of this plan) a global exception handler fed
# attacker-influenced strings, and a string with no valid extension
# anywhere forces the engine to retry the match at every "\b" position with
# no early exit. Unbounded segment/repetition counts made that O(n^2) --
# confirmed via a 100k-char adversarial "a/a/a/.../a" input going from
# several seconds to under 0.15s once bounded. A single path segment or
# filename over 255 chars, or a path over 32 directories deep, is an
# accepted residual miss -- no realistic upload/tmp-path shape in this
# codebase gets anywhere close to either bound.
_BARE_RELATIVE_PATH_RE = re.compile(r"\b[\w\-]{1,255}(?:/[\w\-]{1,255}){1,32}\.[A-Za-z][A-Za-z0-9]{0,7}\b")
# A user-uploaded filename can contain a space (e.g. "patient list.csv"),
# which the character-class-based patterns above can't span -- but Python's
# own OSError/FileNotFoundError repr always quotes the path
# (`'tmp/x patients.csv'`), so matching "anything single/double-quoted that
# contains a slash" catches the space-bearing case the structural patterns
# above miss, without needing to guess which characters a filename may hold.
# The bound (a real path is never anywhere near this long) caps how much
# text an adversarial or coincidentally-unbalanced quote count elsewhere in
# the same string could pull into one match -- without it this is unbounded
# and could swallow everything between two distant, unrelated quote
# characters. 1024 rather than the original 300: comfortable headroom over
# any realistic `./tmp/{uuid}_{filename}` shape (a uuid is ~36 chars, most
# filesystems cap a single filename around 255) without meaningfully
# widening the "swallow a huge blob" blast radius -- this is a bounded
# quantifier on a single non-nested character class either way, so raising
# it doesn't reintroduce the quadratic-time risk above.
_QUOTED_PATH_RE = re.compile(r"'[^'\n]{0,1024}/[^'\n]{0,1024}'|\"[^\"\n]{0,1024}/[^\"\n]{0,1024}\"")
PATH_PATTERNS = (_UNIX_PATH_RE, _WINDOWS_PATH_RE, _BARE_RELATIVE_PATH_RE, _QUOTED_PATH_RE)

# DB connection strings (postgres://user:pass@host/db, or any scheme://user:pass@...)
# and host:port pairs -- a bonus catch-all for a raw psycopg2 error echoing
# connection details (docs/pii-boundary-safety.md finding #3's underlying
# risk), cheap to add given the same mechanism. Host:port is deliberately
# restricted to a dotted hostname, an IPv4 address, or "localhost" -- not a
# bare `\w+:\d+`, which would false-positive on plenty of ordinary text
# (e.g. a ratio or a time-of-day-shaped string). The dotted-hostname
# alternative's label length ({1,63}, the actual DNS label limit) and label
# count ({1,10}) are bounded for the same O(n^2)-on-a-colon-less-input
# reason UID_PATTERN above is -- confirmed via a 16k-char adversarial input
# (5.9s unbounded, <0.02s bounded).
_CONNSTRING_RE = re.compile(r"\b\w+://[^\s@/]+:[^\s@/]+@\S+")
_HOSTPORT_RE = re.compile(
    r"\b(?:(?:\d{1,3}\.){3}\d{1,3}|(?:[a-zA-Z0-9\-]{1,63}\.){1,10}[a-zA-Z0-9\-]{1,63}|localhost):\d{2,5}\b"
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


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """
    Collapse a list of (possibly overlapping, possibly duplicate, possibly
    out-of-order) match spans into maximal, non-overlapping spans covering
    the same characters.

    Shared by every find_*() below and by redact() -- multiple patterns
    (even within one category, e.g. _CONNSTRING_RE and _HOSTPORT_RE both
    matching inside "postgres://u:p@db.internal:5432/d") routinely produce
    overlapping matches. Deduplicating by comparing matched TEXT (an
    earlier version of this) can incorrectly drop a genuinely distinct
    later occurrence whose text happens to equal an earlier match; keeping
    only fully-CONTAINED spans (a later, still-earlier version) misses a
    span that partially -- not fully -- overlaps another, leaving its
    non-overlapping tail unaccounted for. Merging overlapping/touching
    spans into their union avoids both: every character any pattern matched
    ends up inside exactly one returned span, so nothing a pattern found is
    ever silently dropped.
    """
    if not spans:
        return []
    ordered = sorted(spans)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1]:  # overlapping or directly touching
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _date_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for regex in (_DA_RE, _ISO_RE):
        for m in regex.finditer(text):
            if _valid_ymd(*m.groups()):
                spans.append((m.start(), m.end()))
    for m in _SLASH_RE.finditer(text):
        a, b, c = m.groups()
        # Ambiguous group order (YYYY/MM/DD vs DD/MM/YYYY vs MM/DD/YYYY all
        # share this shape) -- accept whichever placement of the 4-digit
        # group produces a valid date.
        if len(a) == 4 and _valid_ymd(a, b, c):
            spans.append((m.start(), m.end()))
        elif len(c) == 4 and (_valid_ymd(c, b, a) or _valid_ymd(c, a, b)):
            spans.append((m.start(), m.end()))
    return spans


def find_dates(text: str) -> list[str]:
    """Every substring of `text` that looks like a real, unshifted calendar
    date in DICOM DA, ISO, or slash-separated form."""
    return [text[start:end] for start, end in _merge_spans(_date_spans(text))]


def find_uids(text: str) -> list[str]:
    spans = [(m.start(), m.end()) for m in UID_PATTERN.finditer(text)]
    return [text[start:end] for start, end in _merge_spans(spans)]


def find_paths(text: str) -> list[str]:
    """Every substring of `text` that looks like a filesystem path, across
    all of PATH_PATTERNS (see _merge_spans for why overlapping per-pattern
    hits are merged rather than returned raw)."""
    spans: list[tuple[int, int]] = []
    for pattern in PATH_PATTERNS:
        spans.extend((m.start(), m.end()) for m in pattern.finditer(text))
    return [text[start:end] for start, end in _merge_spans(spans)]


def find_secrets(text: str) -> list[str]:
    spans: list[tuple[int, int]] = []
    for pattern in SECRET_LIKE_PATTERNS:
        spans.extend((m.start(), m.end()) for m in pattern.finditer(text))
    return [text[start:end] for start, end in _merge_spans(spans)]


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


_GENERIC_REDACTION_PLACEHOLDER = "[redacted]"


def redact(text, *, real_id=None, display_id=None) -> str:
    """
    Generalizes results/endpoints.py's _scrub: precise real-id -> display-id
    substitution when both are known (covering every variant from
    real_id_variants, not just the exact string), *plus* generic
    pattern-based redaction (dates/UIDs/paths/secrets) as a floor applied
    every time, even with no specific real id in scope -- what a
    request-agnostic call site (the global exception handler) uses.

    The generic floor collects spans from EVERY category (secrets, UIDs,
    dates, paths) over the same pristine text and merges them in one pass
    (see _merge_spans) before doing a single left-to-right rebuild -- it
    does NOT run each category sequentially via find_secrets() then
    find_uids() then ... each doing its own `str.replace` on the previous
    category's already-redacted output. That sequential approach had a real
    cross-category leak: a category's placeholder (e.g. "[redacted-uid]")
    uses bracket characters outside every other pattern's character class,
    so redacting a UID first could truncate what would otherwise have been
    a longer, fully-covering path/secret match on the ORIGINAL text,
    silently leaking the rest of it (repro this was found from:
    redact("/tmp/scan_1.2.840.10008.5.1.4.1.1.481.3_MRN500123_results.csv")
    used to leave "_MRN500123_" unredacted). Computing every category's
    spans against the same unmutated text and merging them all together
    once closes that class of bug the same way find_paths() closes it
    within the path category alone. The single merged placeholder means the
    output no longer distinguishes which category matched -- an accepted
    trade for correctness; nothing downstream parses redact()'s output by
    placeholder text.

    No-op on an empty/falsy input, matching _scrub's existing contract.
    """
    if not text:
        return text
    result = str(text)

    if real_id is not None and display_id is not None and str(real_id) != str(display_id):
        for variant in sorted(real_id_variants(real_id), key=len, reverse=True):
            if variant:
                result = result.replace(variant, str(display_id))

    spans: list[tuple[int, int]] = []
    for pattern in SECRET_LIKE_PATTERNS:
        spans.extend((m.start(), m.end()) for m in pattern.finditer(result))
    spans.extend((m.start(), m.end()) for m in UID_PATTERN.finditer(result))
    spans.extend(_date_spans(result))
    for pattern in PATH_PATTERNS:
        spans.extend((m.start(), m.end()) for m in pattern.finditer(result))

    merged = _merge_spans(spans)
    if not merged:
        return result

    pieces = []
    cursor = 0
    for start, end in merged:
        pieces.append(result[cursor:start])
        pieces.append(_GENERIC_REDACTION_PLACEHOLDER)
        cursor = end
    pieces.append(result[cursor:])
    return "".join(pieces)
