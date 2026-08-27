# PII boundary safety — testing and enforcement design

**Status:** v2 — remediation complete. Originally written ahead of the first
DMZ-facing deployment (`proxy/` in front of the backend, `ANON_DB_*`
configured) as a design and risk-register document with none of the gaps
below fixed. `docs/plans/pii-boundary-test-suite.md` (§4's testing-harness
sketch, turned into a concrete 8-step implementation plan) has since closed
every row in §2's risk register — see each row for the PR that closed it.
§1's "correcting an overstated claim" note below is now itself out of date
in the opposite direction (the claim it corrected is accurate again) and is
kept only as a historical record of what was wrong and when; a summary of
the actual final mechanism follows it.

## §0 — Scope & purpose

This document governs one specific property: **no patient-identifiable data —
real MRNs, dates, DICOM UIDs, or server filesystem paths — ever reaches a
browser through the DMZ-facing `proxy/`, including in error states.**

Out of scope, deliberately:
- **DICOM-tag-level de-identification** (stripping tags, scrubbing burned-in
  pixel data). Handled by a separate, pre-existing node downstream of central
  Orthanc — see `docs/known-issues.md`'s "Clarified, not defects" section.
  HERMES's job is access control, routing, and audit around that pipeline,
  not touching image content.
- Whether pseudonymisation (HERMES's actual model — an ID swap, reversible
  via a Trust-owned mapping table) is the right legal basis. Already settled
  in `docs/known-issues.md`; not revisited here.

In scope: every HTTP response body and SSE event the backend produces, on
both the success and the error path, for as long as it might be reachable
through the proxy.

## §1 — Current state

### Request/response chain

```
browser ⇄ proxy/ (DMZ, pure relay) ⇄ backend/ (internal network)
```

`proxy/forward.py` and `proxy/main.py` were read in full for this document.
The proxy does no body inspection, no transformation, and no logging of
request/response bodies, for either JSON or SSE responses — it forwards
4xx/5xx statuses and bodies verbatim (`forward.py`'s `proxy_request`). This
matches its own docstring and CLAUDE.md's description of it as carrying zero
business logic. **Whatever the backend puts in a response body reaches the
browser byte-for-byte.** All responsibility for this document's goal sits
with the backend (and, secondarily, with `frontend/`/`frontend_fastapi/`,
which relay the backend's own error text into user-visible messages without
further scrubbing).

### What's already correct

- `backend/src/identity/anon.py` — the real⇄anon translation boundary.
  Fails closed on an unmapped *inbound* anon ID (422). Fails safe on an
  unmapped *outbound* real ID (substitutes the literal string `"[unknown]"`,
  never the real value) — "so that a gap in the mapping never causes a real
  ID to be shown to the user" (the function's own docstring).
- `backend/src/results/endpoints.py`'s `_scrub`/`_scrub_json`
  (lines 68–98) — substring/JSON-substring replacement of a real MRN with
  its anon id. Correctly wired into this file's four consumers:
  `job_stream`'s `_observe_job`, `job_patients_summary`, `patient_plans`,
  `patient_timeline`/`patient_timeline_all`. `_observe_job` in particular is
  the **live path**: it backs `GET /results/job/{job_id}/stream`, which
  `frontend/jobs/views.py`'s `job_stream` relays to the browser for every
  real CSV-batch job today. The main user-facing progress/results view is
  correctly scrubbed for MRNs.
- Unhandled exceptions get Starlette's default generic 500 — FastAPI's
  `debug` flag is never set to `True` anywhere in this codebase, so there is
  no traceback-in-response-body leak from that path.

### Correcting an overstated claim (historical — see below for current state)

Both `docs/known-issues.md` ("Strengths worth preserving," line 92) and
`CLAUDE.md`'s "Anonymisation boundary" section described scrubbing as applied
"at every outbound API edge" / "every outbound response/SSE event." Against
the code as it stood at the time this document was first written, that was
true only for `results/endpoints.py`'s four consumers above — **not** true
for `common/sse.py`'s `run_batch_job` generator, nor for two single-item
endpoints (see the risk register below, rows 1-2). Both docs have since been
corrected (`docs/known-issues.md`, `CLAUDE.md`) to describe the mechanism
below, which now makes the "every outbound edge" claim accurate again.

### The final mechanism (current state, all risk-register rows closed)

Five layers, applied together rather than any one being a complete fix on
its own:

1. **A general pattern class, not a single known value**
   (`backend/src/common/pii_patterns.py`, row 5) — `redact()`/`redact_dict()`
   detect and redact dates, DICOM UIDs, filesystem paths, and DB
   connection-string/`host:port` shapes generically, plus every format
   variant of a known real id (zero-padded, float-cast) via
   `real_id_variants()` — not just an exact-string match against one known
   MRN. `redact_dict`'s `NON_PII_STRUCTURAL_FIELDS` exclusion (`mrn`,
   `destination`, `destination_type`, `submitted_by`) keeps this generic
   floor from mangling legitimate operational values that happen to look
   date/UID-shaped.
2. **Real DICOM UIDs are stripped, not just redacted-if-recognised**
   (`backend/src/common/sse.py`'s `to_public_details()`, the export-manifest
   finding) — `study_uids`/`series_uids` are dropped entirely and
   `checksums` re-keyed from `dict[SOPInstanceUID, hash]` to a plain
   `list[str]`, applied at every outbound success-emission point while
   `events.details`/`tasks.details` keep full fidelity for audit.
3. **Clinical dates are shifted, not redacted**
   (`backend/src/identity/anon.py`'s `shift_date()`, row 4) — a per-patient
   day offset (`key_value.date_perturbation`) preserves relative clinical
   intervals while breaking the link to the real calendar date, applied to
   `study_date`/`series_date`/`plan_date`.
4. **A global, catch-all safety net** (`backend/src/common/errors.py`'s
   `register_pii_safe_exception_handlers`, row 3) — every `HTTPException`
   anywhere in the app, not just the ones someone remembered to scrub, has
   its `detail` run through `pii_patterns.redact()`'s generic floor before
   the response is sent. Deliberately generic-pattern-only, not
   real-id-aware (no request-scoped real id is available at that layer) —
   a safety net for the *unexpected* case, not a replacement for the precise
   substitution every call site that already knows its own real id still
   does.
5. **A CI-gated test suite, not manual review** (row 8/9) —
   `.github/workflows/test.yml` runs the full suite (including
   `backend/tests/support/pii_assertions.py`'s `assert_no_pii`, a strict
   superset of "does the one known real MRN appear," applied across every
   `test_*_anon_boundary.py`/`test_batch_alias_pii_boundary.py`/
   `test_single_item_pii_boundary.py`/`test_http_exception_pii_boundary.py`
   file) on every PR/push to `main`.

None of this closes the free-text blind spot named below — pattern-based
detection catches *shapes*, not arbitrary prose.

## §2 — Risk register

All 10 rows below are now closed (row 7, this document's own accuracy, by
this note itself) — most **fixed** outright, row 8 fixed with one residual
manual step for a repo admin, and row 10 addressed structurally rather than
independently "fixed" (see each row's own Resolution for the exact wording,
not flattened here). Original finding/location/severity columns are kept
verbatim for the historical record; the **Resolution** column is new.

| # | Finding | Location | Live UI path today? | Severity | Resolution |
|---|---|---|---|---|---|
| 1 | `run_batch_job`'s SSE `error` event and spread `**res` success fields are never scrubbed | `backend/src/common/sse.py:167–191` | No — the UI's actual path (CSV upload → `tasks` queue → `_observe_job`) already scrubs `error_message`/`details` and never emits `real_id` (confirmed: `TasksDB.job_progress` selects `real_id` only as the scrub key, `results/endpoints.py:300,312`). This row is about the separate, non-queue "list of MRNs" batch-alias route, which is unscrubbed and still live/reachable even though the UI doesn't call it | High (unscrubbed by construction, not just untested) | **Fixed** — PR #50 (`pii_patterns.redact()`/`redact_dict()` applied to both the error yield and the success spread), PR #53 (composed with `to_public_details()` to also strip UIDs, closing a regression the #50↔main merge briefly reopened). Covered by `test_sse.py`, `test_batch_alias_pii_boundary.py` |
| 2 | `single_import` and `proknow_upload_patient` return raw `str(e)` in a 200-status JSON body | `backend/src/retrieve/endpoints.py:172`, `backend/src/export/endpoints.py:302` | **Yes** — both are real `backend_client.py` call sites | High | **Fixed** — PR #50, PR #53. Covered by `test_single_item_pii_boundary.py` |
| 3 | ~34 `HTTPException(..., detail=str(e))` sites have no structural guard against an exception message embedding an MRN, DB connection string, or path | `retrieve/`, `export/`, `studies/`, `results/`, `projects/endpoints.py`, `identity/anon.py` | Yes — frontend clients pass `detail` through unmodified into `messages.error(...)` | Medium (mostly latent — depends what a given exception's message happens to contain) | **Fixed** — PR #51: `backend/src/common/errors.py`'s `register_pii_safe_exception_handlers`, one global handler covering every current and future site, not 34 individual edits. Covered by `test_http_exception_pii_boundary.py`. Generic-pattern-only (no real-id context at that layer) — a deliberate, documented scoping tradeoff, not a gap |
| 4 | `studies/endpoints.py` never redacts `study_date`/`series_date`/`study_instance_uid`/`series_instance_uid`/`study_description`/`series_description` | `backend/src/studies/endpoints.py` (`list_studies`/`get_study`) | Yes | Medium — may be an intentional scope decision, but nothing in code or docs says so (contrast with `patient_name`, which *is* explicitly redacted alongside these fields) | **Fixed** — PR #44 (`anon.shift_date()` mechanism), PR #50 (applied: dates shifted, UIDs/descriptions nulled the same way `patient_name` already was). Covered by `test_studies_anon_boundary.py` |
| 5 | Scrubbing only ever targets the MRN as a single known value — no code path treats dates, DICOM UIDs, or filesystem paths as a general class to redact | Repo-wide | — | Medium (structural, underlies #1–4) | **Fixed** — PR #42 (`backend/src/common/pii_patterns.py`, the general pattern-class module named here as missing), PR #50 (`redact_dict`/`NON_PII_STRUCTURAL_FIELDS` generalized across every call site). See "The final mechanism" above |
| 6 | CSV/tmp-file path disclosure: `Could not read CSV: {e}` can embed the server's `./tmp/{job_id}_{filename}` path, including a user-supplied filename | `retrieve/endpoints.py:73`, `export/endpoints.py:152,395` | Yes | Low–Medium (filesystem layout disclosure, not patient PII, but the filename itself could contain an MRN if a user named their CSV that way) | **Fixed** — PR #50 (`pii_patterns.redact()` applied at both CSV-read-error sites) |
| 7 | `known-issues.md`/`CLAUDE.md` overstate scrubbing coverage as universal | Docs only | — | Low (documentation accuracy, but risks future contributors trusting a guarantee that doesn't hold) | **Fixed** — this document, `docs/known-issues.md`, and `CLAUDE.md` updated together (step 8 of the implementation plan) to describe the actual mechanism rather than just `results/endpoints.py`'s `_scrub` |
| 8 | No CI enforcement of any of the above | `.github/workflows/` (only workflow: `docker-publish.yml`, release-triggered image build/push — no test/lint/security-scan job exists) | — | High (process gap — nothing currently prevents a regression here from merging) | **Fixed** — PR #54: `.github/workflows/test.yml` (full suite on every PR/push) + `backend/scripts/seed_anon_test_db.py` (reproducible seeding, previously hand-done). **Residual manual step**: making the new check a hard, required merge gate (as opposed to merely existing) needs a GitHub branch-protection rule configured by a repo admin — not expressible in the workflow file itself; noted in PR #54, not yet done as of this writing |
| 9 | Every existing negative/leakage test checks only for the literal real-MRN string, never a date/UID/path pattern as a class | `test_results_anon_boundary.py`, `test_export_anon_boundary.py`, `test_studies_anon_boundary.py`, `test_observer_stream.py` | — | Medium — test fixtures already contain exactly this data (e.g. `StudyDate`, `StudyInstanceUID` in `test_studies_anon_boundary.py`'s mock) unexamined by any assertion | **Fixed** — PR #42 (`backend/tests/support/pii_assertions.py`'s `assert_no_pii`), PR #53 (rolled out across every `test_*_anon_boundary.py` file, plus new `test_batch_alias_pii_boundary.py`/`test_single_item_pii_boundary.py` with induced-failure and format-variant coverage) |
| 10 | This exact failure mode has already occurred once and was caught late | `docs/plans/safety-plan.md` §E (v2 correction) — new `*_reason` fields nearly shipped into `job_patients_summary` unscrubbed | — | Informational — evidence the "remember to call `_scrub` per field" discipline is leak-prone by construction, not hypothetical | **Addressed structurally** — row 5's fix replaces the per-field-memory discipline this row is evidence against with a floor applied automatically to every string value, not something a future field addition can forget to opt into |

**Known residual gap, accepted, not fixed by any of the above** (per
`docs/plans/pii-boundary-test-suite.md` §A, stated up front rather than
discovered late): pattern-based detection catches *shapes* (a date-shaped
string, a UID-shaped string, a path-shaped string), not arbitrary
identifying prose that doesn't match one of those shapes — e.g. a clinician
typing a patient's name into a DICOM series description at scan time, or a
date written as prose ("seen 3rd Jan") in a free-text comment field rather
than a structured date field. This is a real limit of pattern-based testing
generally, not a gap in this implementation specifically, and there is no
proposed fix — a general solution would require something closer to NLP-based
PII detection, a materially different (and much higher false-positive-rate)
approach than the structural pattern-matching used throughout this document.

## §3 — Design principle: enforcement does not belong in the proxy

The open question going into this document was whether checking should be a
live feature-set inside `proxy/`, or a test-suite run on every build. The
recommendation is: **not in the proxy.** Primarily a CI-gated test suite,
backed by a structural (not per-field) fix in the backend, with an optional
backend-side live safety net as a third layer. Reasoning:

- **The proxy is deliberately, architecturally inert.** Its own docstring and
  CLAUDE.md both describe it as carrying zero business logic, specifically so
  it can be swapped, scaled, or removed without touching anything that
  matters clinically or legally. Teaching it to inspect bodies for PII adds
  exactly the kind of business logic that design choice was made to avoid.
- **The proxy structurally cannot do this correctly even if it tried.** It
  has no access to `ANON_DB_*` — that's the backend's dependency — so it has
  no ground truth for what a "real" MRN even looks like. It would be reduced
  to blind pattern-matching (regex guesses at date/UID shapes), which both
  over-redacts (corrupting legitimate non-PII numeric content) and
  under-redacts (anything that doesn't match the guessed pattern sails
  through untouched).
- **The backend is the only place that holds both values at once.** Every
  real/anon pair is resolved inside the backend, at the same call site that
  produces the response. That is the only place a *correct* check —
  "does this exact real value appear in what I'm about to send?" — can be
  made. Pushing the check downstream to the proxy throws away the one piece
  of context (the real value itself) that makes the check meaningful.

Given that, the layers below are ordered by where they should carry the most
weight.

## §4 — Testing harness design

### Primary layer: CI-gated boundary-contract tests

Generalise the existing `test_*_anon_boundary.py` pattern (currently: assert
a specific literal real-MRN string is absent from `resp.text`) into a
systematic contract applied to every endpoint, not just the four already
covered:

- A shared pytest helper, e.g. `assert_no_pii(body, *, real_ids, forbidden_patterns)`,
  checking:
  - none of the test's known real IDs appear verbatim, in any case/format;
  - no substring matches a date pattern (`\d{8}`, `\d{4}-\d{2}-\d{2}`, DICOM
    `DA` format), a DICOM UID pattern (`\d+(\.\d+)+`), or a server filesystem
    path pattern (`/[\w./-]+` rooted at a known server prefix like `./tmp/`).
- Applied to **both** the success path and an *induced-failure* path per
  endpoint — force an exception with a real MRN already in scope (e.g. mock
  Mosaiq/Pinnacle/ProKnow to raise `Exception(f"lookup failed for {real_mrn}")`)
  and assert the response/SSE stream never surfaces it. This is the shape
  that would have caught findings #1, #2, and #6 above; today's tests only
  exercise the success path plus one specific known-scrubbed error field per
  endpoint.
- Extend coverage explicitly to `common/sse.py`'s `run_batch_job` output
  (currently entirely untested for leakage — `test_sse.py`'s only error case
  uses a message with no MRN in it) and to `single_import`/`proknow_upload_patient`.

### Wiring: an actual CI workflow

No workflow currently runs tests, lint, or a scanner on PRs/pushes — the only
one (`docker-publish.yml`) builds and pushes an image on GitHub Release. Add
a new `.github/workflows/test.yml` triggered on `pull_request`/`push`,
running `pytest` (including the extended boundary suite above) as a hard
merge gate. Leaves room to bolt on a dependency/static-analysis scanner
(e.g. `pip-audit`, `bandit`) later — neither exists in `requirements*.txt`
today — without that being a prerequisite for the boundary suite itself.

**Implemented as designed** (`.github/workflows/test.yml`, §2 row 8) with one
caveat: a workflow file can make the check *exist* and run on every PR, but
making it an actually-enforced, blocking "hard merge gate" needs a GitHub
branch-protection rule (repo Settings → Branches → require this status
check) — that's a one-time manual step for a repo admin, not something
expressible in the workflow YAML itself.

### Secondary layer: a structural backend fix (named here, not designed in full)

The actual defect underlying findings #1, #2, #5, and #10 is procedural: the
codebase relies on remembering to call `_scrub`/`_scrub_json` at every new
free-text field, on an ad hoc, per-endpoint basis. `docs/plans/safety-plan.md` §E
already shows this discipline failing once during review, before it shipped.
The durable fix is to stop relying on memory — e.g. a single outbound
JSON-walking scrub step applied once per response (keyed off whatever real
IDs were resolved for the current request), rather than N manual call sites.
Scoping and implementing this is follow-up work, out of scope for this
document; it's named here so the CI suite in the previous section has a
clear target to eventually make redundant rather than permanently rely on.

### Optional tertiary layer: a backend-side outbound safety net

Belt-and-braces only, not a substitute for the two layers above: a small
piece of backend middleware that runs the same date/UID/path pattern check
(plus the current request's resolved real IDs) against every outbound
response body just before it leaves the backend process, logging (or,
if the team wants it strict, blocking with a generic 500) on a match. This
still lives in the backend — never the proxy, per §3 — and exists only to
catch what the structural fix and the CI suite both missed, not as the
primary control.

## §5 — Governance

- **Ownership.** Sign-off follows the same review process `docs/plans/safety-plan.md`
  already goes through — whoever currently reviews that document reviews
  this one and its risk register before deployment.
- **Definition of an incident.** Any real MRN, date, DICOM UID, or server
  filesystem path appearing in a response body the proxy relays to a browser.
- **Pre-deployment checklist:**
  1. The CI boundary suite (§4) is green. — **Pending #53 merging into #54**:
     #51 and #53 (still open as of this writing, alongside this doc-update
     PR) were each independently verified green on their own branch before
     being opened. #54 (the CI workflow itself) was not — its own branch's
     first real CI runs show 20 failing tests, the pre-existing regression
     #53 fixes (#54 branched from `main` before #53 existed, so it inherits
     the gap; see #54's own description for the full run history, including
     two other CI-specific issues found and fixed from real runs). This
     checklist item completes once #53 merges and #54's workflow is re-run
     against the result — not yet done as of this writing.
  2. Every row in the risk register (§2) has been explicitly marked
     **fixed**, **accepted risk** (with a one-line reason, mirroring
     `docs/known-issues.md`'s existing accepted-limitations tier), or
     **deferred** (with a reason it's safe to defer) — not left silently
     unaddressed. — **Done**, see §2's Resolution column.
  3. `docs/known-issues.md` and `CLAUDE.md`'s scrubbing-coverage claims
     (§1's "correcting an overstated claim") have been corrected to match
     whatever state is actually shipped. — **Done**, this PR.
- **Triage for future findings.** Add a new row to §2's table, classify
  severity using the same live-path/latent distinction used above, decide
  fix-now vs. accepted-risk, and update this document — the same lifecycle
  `docs/known-issues.md` already uses for its own findings.

## §6 — Cross-references

- `docs/plans/pii-boundary-test-suite.md` — the concrete implementation plan
  for §4's testing harness, worked out through interactive design review;
  closes every finding in §2 above (including one, the export manifest's
  UID exposure, found only while designing that plan).
- `docs/plans/safety-plan.md` §D/§E — the audit-manifest and import-outcome work,
  and the one prior real instance of this document's core failure mode.
- `docs/known-issues.md` — sibling risk-register document; its "Strengths
  worth preserving" section has been corrected per §1 above.
- `docs/plans/worker-queue-design.md` — the observer-stream SSE vocabulary that
  §1's "what's already correct" section relies on.
- `CLAUDE.md`'s "Anonymisation boundary" section — carries the same
  correction as `known-issues.md`, plus a summary of the final mechanism.
