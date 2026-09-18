# Feature F001: Translate patient IDs in a CSV

## Purpose

Let an authorised operator convert a CSV column of anonymised IDs to real patient IDs, or real patient IDs to anonymised IDs, without going through the HERMES API. It is a placeholder for the transition period.

## Behaviour

Invoked as `python backend/scripts/anon_lookup.py IN.csv OUT.csv (--to-real | --to-anon) [--column NAME]`.

- Exactly one of `--to-real` / `--to-anon` is required.
- `--column NAME` selects the header of the ID column. If omitted, the first column is used.
- The input has a header row. The output has the same header plus one appended column: `patient_id` for `--to-real`, `anon_id` for `--to-anon`. The original ID column is left unchanged.
- Every input row is written, in order, with all its original columns.
- IDs are stripped of surrounding whitespace before lookup. Repeated IDs are looked up once and translated consistently.
- An ID with no mapping, or one that isn't a valid integer, gets an empty cell in the new column.
- After the file is written, a summary goes to stderr: rows translated, rows unmapped, and the 1-based data-row numbers of the unmapped ones. Real or anon IDs are never printed.
- Exit codes: `0` all rows translated; `1` output written but at least one row unmapped; `2` nothing written (usage error, missing column, anon DB not configured or unreachable).

## Dependencies

- None.

## Relevant code

### Existing

- `backend/src/identity/anon.py` — the only sanctioned read path to the anon DB. `is_configured()`, `lookup_anon_ids()` (real→anon, already non-raising: unmapped → `"[unknown]"`), `lookup_real_ids()` (anon→real, but raises `AnonLookupError` on ANY unmapped ID with no partial result), `_query`, `_SQL_ANON_TO_REAL`, `_to_bigints`, and `AnonServiceError`.
- `backend/scripts/verify_audit_chain.py` — convention for standalone scripts: a `sys.path` shim so it runs as `python backend/scripts/<name>.py` from the repo root, plus a docstring stating usage and exit codes.
- `backend/main.py` / `backend/worker.py` — call `load_dotenv()`. The script needs to do the same so `.env` credentials are picked up. Load it **before** importing `anon`, because `anon.py` reads its env vars at import time.
- `backend/tests/test_anon.py` — the pattern for testing against the seeded anon test DB (env vars set at the top of the file, before the import). Fixed pairs: `1001↔500123`, `1002↔500456` (anon↔real); `9999` exists only under `key_type_id=2`, so it counts as unmapped.

### Likely changes

- `backend/scripts/anon_lookup.py` — new script.
- `backend/tests/test_anon_lookup_script.py` — new tests.
- `README.md` or the script's own docstring — usage. The docstring alone is enough.

## Acceptance criteria

### Scenario: anon → real

**Given** `in.csv` with header `anon_id,note` and rows `1001,a` / `1002,b`
**When** run with `--to-real --column anon_id`
**Then** `out.csv` has header `anon_id,note,patient_id` and rows `1001,a,500123` / `1002,b,500456`, and the exit code is 0.

### Scenario: real → anon

**Given** `in.csv` with header `mrn` and rows `500123` / `500456`
**When** run with `--to-anon` and no `--column`
**Then** `out.csv` has header `mrn,anon_id` and rows `500123,1001` / `500456,1002`, and the exit code is 0.

### Scenario: some IDs unmapped

**Given** rows with IDs `1001`, `424242`, `not-a-number` and `--to-real`
**When** the script runs
**Then** all 3 rows are written; the second and third have an empty `patient_id`; stderr reports 2 unmapped at rows 2 and 3 and does not contain `424242`; the exit code is 1.

### Scenario: wrong key type

**Given** the ID `9999`, which exists only under `key_type_id=2`
**When** translated with `--to-real`
**Then** it is treated as unmapped.

### Scenario: real → anon unmapped

**Given** the real ID `000000` with `--to-anon`
**When** the script runs
**Then** the cell is empty (never the string `[unknown]`), and the exit code is 1.

### Scenario: repeated IDs and whitespace

**Given** an ID column containing ` 1001` twice
**When** translated
**Then** both rows get `500123`.

### Scenario: missing column

**Given** `--column nope`, which isn't in the header
**When** run
**Then** exit code 2, an error message naming the available columns, and no output file written.

### Scenario: anon not configured

**Given** neither `ANON_DB_HOST` nor `ANON_CONFIG` is set
**When** run
**Then** exit code 2 with a clear message, and no output file written (no passthrough).

### Scenario: anon DB unreachable

**Given** the anon DB can't be reached (`AnonServiceError`)
**When** run
**Then** exit code 2, no output file written, and the message contains no IDs.

### Scenario: direction flag required

**Given** neither or both of `--to-real`/`--to-anon`
**When** run
**Then** argparse usage error (exit 2).

## Edge cases

- Empty ID cells: written with an empty output cell and counted as unmapped.
- Header-only file: writes a header-only output with the new column, exit 0.
- UTF-8 BOM on input (Excel exports): read with `utf-8-sig` so the first header isn't corrupted.
- Output path equals input path: refuse (exit 2), to avoid truncating the input before it is read.
- Output file already exists: overwrite is fine (single-use tool). Write only after all lookups succeed, so a hard failure never leaves a partial file.
- Use `csv.writer` with `newline=""`, so quoting and embedded commas round-trip.
- Lookup volume above `ANON_LOOKUP_WARN_THRESHOLD` (500) logs the module's existing warning; nothing is blocked.

## Success

All scenarios above pass, and a manual run against the seeded anon test DB (`backend/scripts/seed_anon_test_db.py`) gives the expected CSVs in both directions.

## Failure behaviour

See exit codes under Behaviour. Hard failures (2) write no output. A partial mapping (1) writes the output, so the operator can use the mapped rows, but is loud on stderr.

## Testing considerations

Follow `test_anon.py`: set the `ANON_DB_*` vars (localhost:55433, `anon_test`) before importing, and rely on the seeded DB. Drive the script's `main(argv)` function directly using `tmp_path` and `capsys`, with no subprocess. For "not configured" and "unreachable", monkeypatch `anon.is_configured` and `anon._query` (or `_get_pool`). One test file, roughly one test per scenario.

## Implementation notes

- Structure: `main(argv=None) -> int` plus an `if __name__ == "__main__": sys.exit(main())` guard, so tests can call it.
- Forward direction (anon→real): `lookup_real_ids` raises on the first missing ID and returns no partial mapping, so it can't drive per-row reporting. Prefer calling `anon._query(anon._SQL_ANON_TO_REAL, list(anon._to_bigints(ids).values()))` and building the mapping from its rows, the same way `lookup_real_ids` does. This reads `_`-prefixed names from the same package and adds no new SQL. The alternative is to add a small non-raising public helper to `anon.py`, which is a slightly larger diff to a shared module. Either is acceptable; the implementer should pick the smaller diff.
- Reverse direction (real→anon): call `anon.lookup_anon_ids`, and map `"[unknown]"` back to an empty cell.
- Mapping keys are strings, matching how the anon module stringifies rows. Compare after `.strip()`.
- Print no IDs on stderr or in exception text (see `_safe_db_error_text` in `anon.py`). Catch `AnonServiceError` and print only its message, which is already sanitised.
- Add a top-of-file `ponytail:`-style note that this is a transition-period script and should be deleted once HERMES is used full-time.

## Out of scope

- API/frontend exposure, audit logging, ethics-gate checks, output-path safety checks against the repo tree.
- Date perturbation or any column other than the ID column.
- Streaming or very large files.
