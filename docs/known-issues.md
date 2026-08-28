# Known issues — pseudonymised research export

Quick-skim summary of everything surfaced during the export-governance review
and the follow-up planning discussion. Each item links to where it's tracked
in more detail. Status reflects the author's decisions, not just severity.

## Being actively addressed — see `docs/plans/safety-plan.md`

- **No way to restrict which destinations a user may export to.** Any active
  project member can target any registered Orthanc modality or ProKnow
  collection. → building a per-user allow-list (`docs/plans/safety-plan.md` §A).
  Still the only item in this section not yet built — no `AccessDB`/
  `user_export_destinations` exists in the codebase as of this update.

## Resolved — shipped since the original review

- **Backend→`anon_db` link crossed into the DMZ unhardened.** TLS opt-in
  (`ANON_DB_SSLMODE`/`ANON_DB_SSLROOTCERT`) and app-side lookup-volume
  monitoring are both live in `backend/src/identity/anon.py`
  (`docs/plans/safety-plan.md` §B1). A restricted DB role and CA cert for that
  table remain an external request to the table's owning team, not
  something HERMES's own code can complete (§B2) — the app-side half is
  done, the infra-side half is still pending on their end.
- **Project approval didn't say what it authorises.** The agreed banner
  copy ("Approving this project lets its members export pseudo-anonymised
  data for any patient, in any volume, for as long as the approval is
  active. There is currently no way to limit exports to a specific
  cohort — membership is the only control in place.") is live on the
  project detail page (`docs/plans/safety-plan.md` §C).
- **Audit trail didn't record what actually left, and could be edited with
  no trace.** `events` is now a hash chain (`prev_hash`/`row_hash`,
  `backend/src/status/hash_chain.py`), and exports carry a real per-export
  manifest (study/series UIDs, per-instance checksums, destination) instead
  of a bare `{'status': 'Success'}` (`docs/plans/safety-plan.md` §D0-§D2).
- **`event_type='success'` on import didn't mean the patient was actually
  found anywhere.** `Importer.verify_on_orthanc` now does a ground-truth
  post-import check, and `StatusDB.count_imported_patients` computes the
  "N/M imported" figure off `details->>'imported'`, not off `event_type`
  alone (`docs/plans/safety-plan.md` §D3/§E).
- **No per-source reason when a patient isn't found.** `job_patients_summary`
  now surfaces `mosaiq_reason`/`pinnacle_reason`/`proknow_reason` per patient
  (`backend/src/results/endpoints.py`), scrubbed of the real MRN before
  leaving the boundary (`docs/plans/safety-plan.md` §E).

## Documented limitations — accepted, no code change planned this iteration

- **No pre-flight review before a combined import+export job.** Can't
  confirm what's about to be sent until import has actually finished
  finding it, and pausing a multi-hour/day job to wait for a human is worse
  than not pausing at all. No design proposed. The precondition for
  revisiting this (§D3/§E's import-outcome data) now exists in the
  codebase — still not built, but worth reconsidering now that the
  ground-truth "was this patient actually found" data is available.
- **Admin/superuser access isn't restricted at the code level.** A Django
  superuser is auto-enrolled in a permanently-approved bypass project with no
  ethics review, and can use it for export today. Accepted as an operational
  control instead: the admin group is kept small and vetted by hand, rather
  than gated in code.
- **Approval has no cohort or volume scoping.** Once a project is approved,
  any member can export any patient, any volume — approval is the only
  control. The banner above makes this explicit to reviewers; actually
  restricting it (per-project cohort lists, per-project volume caps) is real
  future work, not committed.
- **HERMES doesn't verify a research-bound export actually reaches the
  anonymising DICOM node.** `dicom_c_move` can target any registered Orthanc
  AE title; nothing distinguishes "this destination anonymises" from "this
  destination is an ordinary clinical modality." Mitigated in part by §A's
  destination allow-list once shipped, but no dedicated "this destination is
  the anonymising gateway" marker exists yet.

## Clarified, not defects — no action needed

- **What HERMES builds is pseudonymisation, not full anonymisation.** This
  is intentional: exported data carries pseudonymised IDs, linked back to
  hospital records via the Trust-owned mapping table. Worth keeping the
  terminology precise in any DPIA/data-sharing paperwork (pseudonymised data
  stays in scope of data protection law, on a different legal basis than
  anonymised data would need) — but the design itself is correct as built.
- **DICOM-level de-identification (tag stripping, burned-in pixel scrubbing)
  is out of scope.** It's handled by a separate, pre-existing node
  downstream of central Orthanc. HERMES's responsibility is access control,
  routing, and audit around that pipeline — not touching image content.

## Separately tracked — see `docs/plans/frontend-migration.md`

- **Whether to migrate `frontend/` off Django.** Under consideration
  (Django feels heavy for what's mostly a thin client). The migration plan's
  own analysis surfaces a real tension worth noting here too: `frontend/`'s
  session handling, CSRF, auth (incl. invite/activation tokens), Django
  admin's break-glass value, ethics-document file storage with correct
  access control, forms with cross-field validation, and flash messaging are
  all things Django currently solves and a migration would re-solve by hand.
  Not a reason to block the migration, but worth weighing deliberately rather
  than assuming "Django is overkill" without pricing in what's traded away.

## Strengths worth preserving through any of the above changes

- The anon/real ID split and PII scrubbing at every outbound API edge:
  `backend/src/identity/anon.py` (real⇄anon translation, plus
  `shift_date()` for clinical dates — shifted to preserve relative
  intervals, not just redacted), `backend/src/common/pii_patterns.py`
  (general pattern-class redaction — dates/UIDs/paths/secrets/format
  variants of a known id, not a single hardcoded MRN string),
  `backend/src/common/sse.py`'s `to_public_details()` (strips real DICOM
  UIDs from the export manifest and every batch success event),
  `backend/src/common/errors.py`'s global exception handler (a pattern-based
  floor on every `HTTPException`, not per-site memory), and
  `results/endpoints.py`'s `_scrub`/`_scrub_json` (precise real-id
  substitution, now built on the pattern module above rather than a bare
  string replace). `docs/pii-boundary-safety.md` has the full design and a
  risk register recording exactly what each piece closed — the "overstates
  reality" gap that document once flagged here has been closed by the work
  it describes.
- Fail-closed authorization discipline in `backend/src/projects/enforcement.py`
  (a DB error denies, never silently allows).
- The two-phase, session-gated SSE job pattern in `frontend/jobs/views.py` —
  reused as the mechanism for the new destination-allow-list admin UI and
  should stay the model for any future review/approval UI.
