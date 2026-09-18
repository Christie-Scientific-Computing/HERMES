"""
Translate a CSV's patient-ID column between anonymised and real IDs.

A stop-gap for the transition period until users work fully within HERMES --
delete this script once that's done. It talks to the anon-mapping DB
directly (via backend/src/identity/anon.py, read-only), so it bypasses the
ethics-project gate and leaves no HermesDB audit trail; it relies on the
operator already holding the ANON_* credentials (accepted, see
.plans/anon-id-csv-translator/plan.md D004).

Run from the repo root, with ANON_DB_* / ANON_CONFIG in the environment or .env:

    python backend/scripts/anon_lookup.py IN.csv OUT.csv --to-real [--column NAME]
    python backend/scripts/anon_lookup.py IN.csv OUT.csv --to-anon [--column NAME]

The input needs a header row. --column names the ID column (default: the
first). Every input row is written in order, with one appended column:
`patient_id` for --to-real, `anon_id` for --to-anon. An ID with no mapping
gets an empty cell; unmapped row numbers (never the IDs) are listed on stderr.

Exit codes: 0 all rows translated; 1 output written but some rows unmapped;
2 nothing written (bad usage/column/file, anon DB unconfigured or unreachable).
"""
import argparse
import csv
import logging
import sys
from pathlib import Path

# Allow `python backend/scripts/anon_lookup.py` to run directly, matching
# verify_audit_chain.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

# anon.py reads its env vars at import time, so .env must be loaded first.
load_dotenv()

from backend.src.identity import anon  # noqa: E402


def _translate(ids: list[str], to_real: bool) -> dict[str, str]:
    """{input_id: translated_id} for the IDs that have a mapping."""
    unique = list(dict.fromkeys(i for i in ids if i))
    if to_real:
        # lookup_real_ids raises on the first unmapped ID with no partial
        # result, so go through its query helper to get per-row answers.
        # _to_bigints keeps out-of-range ints, which would make Postgres reject the whole query.
        as_ints = {i: n for i, n in anon._to_bigints(unique).items() if abs(n) < 2**63}
        by_int = {int(a): str(r) for a, r in anon._query(anon._SQL_ANON_TO_REAL, list(as_ints.values()))}
        return {i: by_int[n] for i, n in as_ints.items() if n in by_int}
    return {k: v for k, v in anon.lookup_anon_ids(unique).items() if v != "[unknown]"}


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    direction = ap.add_mutually_exclusive_group(required=True)
    direction.add_argument("--to-real", action="store_true", help="anonymised -> real IDs")
    direction.add_argument("--to-anon", action="store_true", help="real -> anonymised IDs")
    ap.add_argument("--column", help="header of the ID column (default: first column)")
    args = ap.parse_args(argv)

    if args.input.resolve() == args.output.resolve():
        return _fail("output path must differ from input path")
    if not anon.is_configured():
        # Without this the anon helpers pass IDs through unchanged.
        return _fail("anonymisation DB not configured (set ANON_DB_HOST or ANON_CONFIG)")

    try:
        with open(args.input, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            rows = list(reader)
    except OSError as exc:
        return _fail(f"cannot read input: {exc.strerror}")
    if header is None:
        return _fail("input CSV is empty")

    col = 0
    if args.column is not None:
        if args.column not in header:
            return _fail(f"column {args.column!r} not in header; available: {', '.join(header)}")
        col = header.index(args.column)

    ids = [row[col].strip() if col < len(row) else "" for row in rows]
    try:
        mapping = _translate(ids, args.to_real)
    except anon.AnonServiceError as exc:
        return _fail(str(exc))

    new_col = "patient_id" if args.to_real else "anon_id"
    try:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header + [new_col])
            for row, i in zip(rows, ids):
                w.writerow(row + [""] * (len(header) - len(row)) + [mapping.get(i, "")])
    except OSError as exc:
        return _fail(f"cannot write output: {exc.strerror}")

    unmapped = [n for n, i in enumerate(ids, 1) if i not in mapping]
    print(f"{len(ids) - len(unmapped)} translated, {len(unmapped)} unmapped", file=sys.stderr)
    if unmapped:
        print("unmapped data rows: " + ", ".join(map(str, unmapped)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # anon._query logs failures with a traceback whose text may echo bound IDs.
    logging.disable(logging.ERROR)
    sys.exit(main())
