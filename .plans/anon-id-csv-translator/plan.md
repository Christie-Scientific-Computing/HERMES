# Implementation Plan

## Goal

Provide a rarely-used, standalone command-line script that translates a CSV's patient-ID column between anonymised and real IDs, in either direction. It is a stop-gap for the transition period until users work fully within HERMES.

## What we discussed

- Single-use / rarely-run admin script, not a product feature. No UI, no API endpoint.
- Two directions: anon → real (`--to-real`) and real → anon (`--to-anon`).
- Output is the input CSV plus one new column holding the translated ID.
- Unmapped IDs must not abort the run: blank cell, reported, non-zero exit.
- Governance: none beyond what the anon module already does (existing >500-lookup log warning). No ethics-gate check, no HermesDB audit logging.

## Decisions

- **D001 — Input + appended column output:** all input columns and row order are preserved; the translated ID is appended, so the file can still be joined to whatever else it carries.
- **D002 — Unmapped IDs → blank cell, stderr summary, exit code 1:** the run completes so the operator gets the mapped rows, but cannot mistake it for a clean run. The summary reports row numbers and counts, not the IDs themselves.
- **D003 — Explicit direction flags, no auto-detect:** anon IDs and MRNs are both numeric, so direction cannot be inferred.
- **D004 — No governance guardrails:** accepted by the user; the script relies on the operator already holding the `ANON_*` credentials. Recorded here so it isn't mistaken for an oversight.
- **D005 — Refuse to run in passthrough mode:** if `anon.is_configured()` is False, exit with an error. The anon module's helpers would otherwise silently return every ID unchanged, producing an output that looks valid but translated nothing.
- **D006 — Reuse `backend/src/identity/anon.py`:** no new SQL and no separate DB connection code. The forward direction needs a non-raising variant (see F001 implementation notes).

## Implementation overview

1. **F001 — Translate patient IDs in a CSV** ([`features/F001-translate-csv-ids.md`](features/F001-translate-csv-ids.md)): the whole script, its tests, and a short usage note.

## Dependencies

```text
F001 (independent)
```

## Open issues

- None.

## Assumptions

- The script is run by an authorised person on a host that can reach the anon-mapping DB, with `ANON_DB_*` or `ANON_CONFIG` in the environment or `.env`.
- Input files are small enough to load fully in memory (hundreds to low thousands of rows).
- Output CSVs containing real IDs are handled by the operator under normal information-governance practice.

## Limitations

- No audit trail beyond the anon module's log lines.
- Not covered by the ethics/project gate.

## Deferred decisions

- Retiring the script once HERMES is used full-time. It should be deleted then.

## Out of scope

- Any HTTP endpoint or frontend page.
- Date shifting (`shift_date`) or translating any column other than the ID column.
- Multiple ID columns per run, or auto-detecting the direction.
