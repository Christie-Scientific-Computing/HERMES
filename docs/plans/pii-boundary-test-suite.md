# Plan — CI-gated PII boundary-contract test suite

**Status:** v1, written ahead of the first DMZ-facing deployment. Converts
`docs/pii-boundary-safety.md`'s §4 sketch of a testing harness into a
concrete, buildable plan, arrived at through interactive design review
against the live codebase (not assumed from the earlier document alone).
Companion to `docs/pii-boundary-safety.md` (the risk register this plan
closes) and `docs/plans/safety-plan.md` (the export-governance plan this one
extends — the export manifest change below builds directly on that
document's §D2).

## What this plan is responding to

`docs/pii-boundary-safety.md` named a CI-gated test suite as the primary
enforcement layer for keeping patient-identifiable data (MRNs, dates, DICOM
UIDs, filesystem paths) from crossing the DMZ proxy, but only sketched it.
Working through the suite's actual design surfaced a chain of consequences
worth recording, not just the final answer:

1. Testing DICOM UIDs properly required understanding the export manifest's
   actual behavior — which revealed it already returns real UIDs to the
   browser by design (`docs/plans/safety-plan.md` §D2), not just to the
   audit DB as first assumed.
2. Once that was clear, the decision was to change it, not just test around
   it — UIDs should be forbidden everywhere, including the manifest.
3. That in turn raised the same question for every other known gap in
   `docs/pii-boundary-safety.md` §2: a comprehensive suite fails immediately
   against current code unless those gaps are fixed too.
4. The decision: fix everything now, ahead of deployment, rather than ship a
   partially-red suite with follow-up tickets.
5. Separately, a domain-knowledge correction changed the date-handling
   design mid-plan: the external `key_value` table has a `date_perturbation`
   column (a per-patient day offset) that this repo's own dev/test fixtures
   don't reflect but the real Trust-owned database does — not discoverable
   from the codebase itself. Dates should be **shifted**, not redacted.

So this plan covers two things together: the generalized test suite itself,
and the remediation work it requires to be meaningfully green from day one.

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Patient IDs across systems | The input real MRN (resolved via `anon.py`) is the anchor; test against it and its format variants (int/zero-padded/float-cast/substring-embedded) | Covers the "same id, different string shape" bug class the original audit flagged in `_scrub`'s plain substring match |
| 2 | Foreign-system IDs (Pinnacle `plan_id`/`primary_image_set`) | Treated as non-PII, excluded from the forbidden-pattern set | Small Pinnacle-internal sequential ints, not MRN-derived, only ever returned from an endpoint already scoped to one patient by their anon MRN in the URL — add no new identifying information |
| 3 | ProKnow/Mosaiq internal IDs | No action needed | Confirmed: ProKnow's own `patient.id`/`entity.id` are never serialized into any `Response`; Mosaiq has no internal key distinct from the MRN |
| 4 | Clinical dates (study/series/plan dates) | **Shifted**, via the `key_value.date_perturbation` column, not redacted | Preserves relative time intervals between a patient's scans while breaking the link to real calendar dates — standard practice, and available now that the mechanism is confirmed to exist |
| 5 | Operational timestamps (job `created_at`, event `ts`, etc.) | Allowed, explicit allow-list by field name | Describe the HERMES job's own timeline, not the patient's clinical history |
| 6 | DICOM UIDs | Forbidden everywhere, including the export-success manifest | The manifest's UIDs reach the browser, not just the DB — accepted design needed to change once that was clear |
| 7 | Export manifest replacement | Counts + checksums only; `checksums` re-keyed from `dict[SOPInstanceUID, hash]` to a plain `list[str]` of hash values; real UIDs stay in `events.details`/`tasks.details` (DB-internal, unchanged) | No frontend template consumes these fields today (confirmed by grep), so reshaping is safe; full fidelity kept internally for audit |
| 8 | Remaining risk-register findings (#1–#4, #6) | Fix all of them now, not xfail-and-defer | Full remediation ahead of deployment, not a partially-red gate |

## §A — Shared redaction/detection infrastructure

Two new modules, kept separate because one is production code and one is
test-only:

### `backend/src/common/pii_patterns.py` (new, production)

The single source of truth for what "looks identifiable":

- `DATE_PATTERNS` — DICOM `DA` (`YYYYMMDD`), ISO (`YYYY-MM-DD`),
  slash-separated. Used to catch a *raw, unshifted* date turning up
  somewhere it shouldn't (free text, an unexpected field) — not to police
  the known clinical-date fields themselves, which are handled by actual
  shifting (§B), not pattern-banning.
- `UID_PATTERN` — `\d+(?:\.\d+){5,}` (6+ dot-separated numeric segments,
  long enough to avoid false-positiving on short version strings like
  `pinnacle_version`, a real field on the plans model).
- `PATH_PATTERNS` — unix absolute/relative and Windows paths, matched
  structurally (rooted at `/`, `./`, `../`, or a drive letter) rather than
  any specific known prefix like `./tmp/` — catches a path in an
  *unanticipated* location, not just the ones already found.
- `SECRET_LIKE_PATTERNS` — DB connection strings (`postgres://...`),
  `host:port` pairs. A bonus catch-all for finding #3's underlying risk (a
  raw psycopg2 error echoing connection details), cheap to add given the
  same mechanism.
- `real_id_variants(real_id) -> set[str]` — the format variants a real MRN
  might appear as: exact string, int-cast, zero-padded (a few common
  widths), float-cast (`"1234.0"`, the specific coercion bug the original
  audit flagged), so a substring check catches more than today's
  exact-match `_scrub`.
- `redact(text, *, real_id=None, display_id=None) -> str` — generalizes the
  existing `_scrub`: precise real-id→display-id substitution when both are
  known, *plus* generic pattern-based redaction (dates/UIDs/paths/secrets)
  as a floor applied everywhere, even with no specific real id in scope
  (what the global exception handler in §B uses).

### `backend/tests/support/pii_assertions.py` (new, test-only)

Built on the module above, not a parallel reimplementation, so the suite
tests against exactly what production code redacts:

- `assert_no_pii(body, *, real_ids=(), real_dates=(), context="")` — parses
  `body` as JSON (or concatenated SSE `data:` events) and walks it
  recursively, tracking each string leaf's key path:
  - real-id-variant substring check — always;
  - raw-real-date check — the caller passes the actual unshifted date(s) in
    scope (e.g. the fixture's `StudyDate`), asserted absent everywhere,
    regardless of field;
  - generic date-*pattern* check — unless the key is in
    `ALLOWED_TIMESTAMP_FIELDS` or `SHIFTED_DATE_FIELDS`;
  - UID-pattern check — always, no exceptions, per decision 6 above;
  - path-pattern check — always.
  - Falls back to a flat regex scan over raw text when the body isn't valid
    JSON (e.g. a plain-text error page).
- `ALLOWED_TIMESTAMP_FIELDS` — `created_at`, `submitted_at`, `approved_at`,
  `reviewed_at`, `ts`, `expiry_date` — operational/job timestamps returned
  as-is.
- `SHIFTED_DATE_FIELDS` — `study_date`, `series_date`, `plan_date` — a
  date-shaped string is *expected* here (the shifted value), so the generic
  pattern-ban is skipped for these field names; correctness is checked by a
  dedicated assertion instead of by absence.
- `assert_date_shifted_correctly(returned_value, *, raw_value,
  perturbation_days, date_format="DA")` — computes the expected shifted
  date from a known raw value + known perturbation and asserts exact
  equality.

**Explicitly out of scope / known blind spot, stated up front:** none of
this can catch identifying information embedded in arbitrary free text that
doesn't match a structural pattern — a clinician who typed a patient's name
into a DICOM series description at scan time, or a calendar date written in
prose ("seen 3rd Jan") inside a comment field rather than a structured date
field. Pattern-based testing catches *shapes*, not arbitrary prose. This is
a real limit of the approach, not a gap in this implementation;
`docs/pii-boundary-safety.md` should note it as an accepted residual risk.
This is unrelated to the date-*shifting* mechanism below, which handles the
structured, known clinical-date fields correctly.

## §B — The date-shift mechanism

New, on top of `backend/src/identity/anon.py` (the sole boundary to the
external `key_value` table):

- **Schema doc correction** — `anon.py`'s module docstring currently
  describes `key_value` as `patient_id, key_value, key_type_id` only; add
  `date_perturbation INT` (per-patient day offset, positive = shift into
  the future, negative = into the past, one value per `key_type_id=1` row
  alongside the existing id mapping).
- **`get_date_perturbation(real_id) -> int`** (and a batched
  `get_date_perturbations(real_ids) -> dict[str, int]`, mirroring the
  existing `lookup_real_ids` shape) — queries `key_value` by the
  `key_value` column (= real id) for `key_type_id = 1`, returns the
  integer offset.
  - **Passthrough** (`ANON_DB_HOST` unset): returns `0` — no shift,
    consistent with dates not being touched at all in an internal-only
    deployment, same as the existing ID passthrough.
  - **Missing row / `NULL` column / DB error**: does **not** default to
    `0` — a silent "no shift" default would mean the *raw real date* gets
    returned unshifted, exactly the leak this prevents. Raises the same way
    `lookup_real_ids` does for an unmapped id; callers catch it and redact
    the date to `None` (fail-safe, mirroring the existing "never show the
    real value" philosophy) rather than ever falling through to the
    unshifted value.
- **`shift_date(real_id, date_str) -> str | None`** — the convenience
  function actual endpoints call: looks up the perturbation, detects DICOM
  `DA` (`YYYYMMDD`) vs ISO (`YYYY-MM-DD`) format, applies the day offset via
  `datetime.date` arithmetic (month/year/leap-year boundaries handled for
  free), re-formats in the *same* format it was given. Returns `None` for
  an empty/absent input date or on a perturbation-lookup failure.
- **`backend/scripts/dev_seed.py`** — add the `date_perturbation` column to
  the local anon-db DDL (confirmed missing today) and seed a fake offset
  per dev-seeded patient, so local dev/testing can exercise shifted dates,
  not just the id mapping.

## §C — Closing every risk-register finding

| # | Finding | Fix | File(s) |
|---|---|---|---|
| — | Export manifest returns raw UIDs | `Response.checksums` re-keyed to `list[str]`; `study_uids`/`series_uids` dropped from the HTTP-facing shape via a new `_to_public()` helper applied at every emission point (`run_batch_job`'s success yield, `_observe_job`'s read of `tasks.details` for export-stage tasks, `proknow_upload_patient`'s return) — not at the `Response` model itself, so `events.details`/`tasks.details` keep full real-UID fidelity for audit | `backend/src/export/endpoints.py`, `backend/src/common/sse.py`, `backend/src/results/endpoints.py` |
| 1 | `run_batch_job`'s SSE `error`/success fields never scrubbed | Apply `pii_patterns.redact()` to the `error` yield; apply `_to_public()` (above) to the success `**res` spread | `backend/src/common/sse.py:167-191` |
| 2 | `single_import`/`proknow_upload_patient` raw `str(e)` | Apply `pii_patterns.redact()` (real-id-aware, since both know the real MRN in scope) before embedding in the error dict | `backend/src/retrieve/endpoints.py:172`, `backend/src/export/endpoints.py:302` |
| 3 | ~34 `HTTPException(..., detail=str(e))` sites, no structural guard | **One global fix, not 34 edits**: a new `register_pii_safe_exception_handlers(app)` in `backend/src/common/errors.py`, registered once in `backend/main.py`, intercepts every `HTTPException` and runs `detail` through `pii_patterns.redact()` (generic pattern-only — no real-id context available at this layer, an accepted scoping tradeoff, see §E) before the response is sent. Closes all current sites *and* any future one | `backend/src/common/errors.py` (new), `backend/main.py` |
| 4 | `studies/endpoints.py` never redacts dates/UIDs/descriptions | **Dates**: replace `study_date`/`series_date` with `anon.shift_date(real_id, raw_value)` (§B) rather than nulling them. **UIDs/descriptions**: no shift mechanism applies (there's no legitimate "shifted UID"), so `study_instance_uid`/`series_instance_uid`/`study_description`/`series_description` are still set to `None` when `anon.is_configured()`, same pattern as `patient_name` | `backend/src/studies/endpoints.py` |
| — | `results/endpoints.py`'s `patient_plans` returns Pinnacle's `plan_date` (a real `DATE` column) unshifted — found while designing §B, not in the original risk register | Apply `anon.shift_date(real_mrn, plan_date)` the same way | `backend/src/results/endpoints.py` |
| 6 | CSV/tmp-path disclosure in `Could not read CSV: {e}` | Run `str(e)` through `pii_patterns.redact()` before embedding | `backend/src/retrieve/endpoints.py:73`, `backend/src/export/endpoints.py:152,395` |
| 5, 7 | Structural root cause; doc-accuracy | Resolved as a side effect of the above — `pii_patterns.py` is the "class, not single-value" redaction finding #5 said was missing; finding #7's overstated coverage claim becomes true once this ships | `docs/pii-boundary-safety.md`, `docs/known-issues.md`, `CLAUDE.md` |

Also extend `results/endpoints.py`'s existing `_scrub`/`_scrub_json` to call
`pii_patterns.redact()` internally, alongside the precise MRN substitution
they already do, so the one path that was already "correct" gets the same
broadened protection as everywhere else, for free.

## §D — The generalized test suite

Follows the existing house style (`backend/tests/test_*_anon_boundary.py`'s
per-router `FastAPI()` + `TestClient`, direct `StatusDB`/`TasksDB` seeding
to inject a controlled real MRN, `monkeypatch` for
`Exporter`/`_orthanc`/worker factories) — extended, not replaced:

- **Rewrite** `test_results_anon_boundary.py`, `test_export_anon_boundary.py`,
  `test_studies_anon_boundary.py` to call `assert_no_pii(...)` instead of
  the current `assert REAL_MRN not in resp.text`, broadening coverage to
  dates/UIDs/paths for free.
- **New** `test_batch_alias_pii_boundary.py` — covers `run_batch_job`'s
  three consumers (`/import/batch_import`, `/export/dicom_move`,
  `/export/proknow_upload`), success and induced-failure (mock the worker
  to raise with a real-MRN-and-fake-path-bearing message), asserting via
  `assert_no_pii` on the parsed SSE stream. This is the test that would
  have caught finding #1.
- **New** `test_single_item_pii_boundary.py` — same for
  `single_import`/`proknow_upload_patient`'s error branches (finding #2).
- **New** `test_http_exception_pii_boundary.py` — a handful of
  representative cases (one each from `retrieve/`, `export/`, `studies/`,
  `results/`, `projects/endpoints.py`, `anon.py`) forcing a
  real-MRN/path-bearing exception through the new global handler, not all
  ~34 sites individually — the handler is one mechanism, so a handful of
  tests proving it works is sufficient.
- **New** `test_export_manifest_shape.py` — asserts the new `Response`
  shape has no `study_uids`/`series_uids` and `checksums` is UID-free on
  the HTTP/SSE path, while confirming `events.details`/`tasks.details`
  (DB) still has the full real data (queried directly, not over HTTP) —
  proving the audit trail didn't lose fidelity.
- Every new/rewritten test also gets **format-variant** coverage: at least
  one case per endpoint family feeds a zero-padded or float-cast real MRN
  through and confirms `assert_no_pii` still catches it.
- **Rewrite** `test_studies_anon_boundary.py`'s date assertions: instead of
  leaving `StudyDate`/`SeriesDate` unexamined (today's gap), assert the
  response's `study_date`/`series_date` equals the seeded fixture date
  shifted by the test MRN's known `date_perturbation`
  (`assert_date_shifted_correctly`), *and* that the raw unshifted fixture
  date never appears anywhere (`assert_no_pii`'s `real_dates=`). Same
  treatment added to `patient_plans`'s `plan_date` in the results-boundary
  tests.

## §E — CI + reproducible anon-DB seeding

- **New** `backend/scripts/seed_anon_test_db.py` — creates the `key_value`
  table (`id, patient_id, key_value, key_type_id, date_perturbation`) if
  absent and seeds the fixed real↔anon MRN pairs the existing
  anon-boundary tests already hardcode (`REAL_MRN = "500123"` /
  `ANON_MRN = "1001"`, per `test_anon.py`), each with a known non-zero
  `date_perturbation` so the new shift-correctness tests have something
  deterministic to check against, plus a couple of additional pairs for
  the format-variant tests. Closes a real gap: today's `anon_test`
  Postgres (port 55433) has no in-repo seed script at all — it's
  hand-seeded, which a CI runner can't reproduce.
- **New** `.github/workflows/test.yml` — triggered on `pull_request`/`push`:
  two ephemeral Postgres service containers (HermesDB + anon-test DB),
  checkout with `submodules: recursive` (see open question below),
  `alembic upgrade head` against the HermesDB container (tests don't run
  this themselves today — only `backend/main.py`'s startup does), run the
  new seed script against the anon-test container, then `pytest`. A
  required check — a hard merge gate, not advisory.
- **Pin** `pytest`, `pytest-asyncio`, `httpx` in `requirements-dev.txt` —
  currently installed in the dev environment but not declared anywhere, so
  CI would otherwise install unpinned/absent versions.
- **Open question, flagged rather than assumed**: `backend/src/retrieve/PinnacleExport/`
  is a git submodule; `test_worker.py`/`test_cleanup_orthanc.py` need it
  checked out or they skip via `pytest.importorskip`. Whether the CI
  runner has credentials to check it out (if it's a private repo) isn't
  determinable from inside this repo — the workflow attempts
  `submodules: recursive` and documents that these two test files skip
  gracefully if it's unavailable, rather than blocking on an assumption.

## Doc updates

- `docs/pii-boundary-safety.md` — mark findings #1–#6 (and the
  export-manifest issue, added as a new row) **fixed** in the risk
  register, with a one-line pointer to the PR; add the free-text blind spot
  (§A above) as an explicit accepted-residual-risk note.
- `docs/known-issues.md` / `CLAUDE.md` — the "scrubbing at every outbound
  edge" claim (previously flagged as overstated) becomes accurate; update
  the wording to describe the actual mechanism (`pii_patterns.py` + the
  global exception handler) rather than just `results/endpoints.py`'s
  `_scrub`.

## §F — Explicitly out of scope for this task

- **Real-id-aware redaction inside the global exception handler.** It uses
  generic pattern matching only (dates/UIDs/paths/secrets), not precise
  real-id substitution, because the handler has no access to which real
  ids were in scope for the failed request without a request-scoped
  contextvar threaded through `anon.py`'s resolve functions — a genuinely
  separate feature. This task's handler is a real, working safety net for
  the *unexpected* case; the request-scoped precise version remains future
  work.
- **Pinnacle/ProKnow foreign-ID derivation research.** Treated as an
  accepted opaque gap rather than something this suite asserts about,
  since nobody has confirmed how those systems generate their own internal
  IDs.
- **Staff/user identifiers** (`created_by`, `username`) — a different
  category (accountability, not patient confidentiality); not addressed by
  this suite.

## Verification

- `pytest backend/tests/` locally against a throwaway Postgres pair (as
  today), plus the new `seed_anon_test_db.py` run first.
- Confirm the rewritten anon-boundary tests still pass with the broadened
  `assert_no_pii` (regression check that the *existing* correct paths —
  `_observe_job`'s four consumers — aren't accidentally broken by the
  stricter checks).
- Confirm the new tests fail against a pre-fix version of the touched files
  (i.e. actually exercise the bug before the fix lands) — standard "test
  would have caught it" sanity check for #1/#2/#4/#6 and the manifest
  change.
- Push a throwaway branch and confirm `.github/workflows/test.yml` actually
  runs and gates a PR, rather than just trusting the YAML is well-formed.
- Manually re-check the two frontend templates/views once more after the
  manifest reshape (`frontend/jobs/templates/`, `frontend/jobs/views.py`)
  to confirm nothing broke — the earlier grep found zero consumers, but
  worth a final look since this is the one change with a live UI in front
  of it.
