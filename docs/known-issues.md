# Known issues — pseudonymised research export

Quick-skim summary of everything surfaced during the export-governance review
and the follow-up planning discussion. Each item links to where it's tracked
in more detail. Status reflects the author's decisions, not just severity.

## Being actively addressed — see `docs/safety-plan.md`

- **No way to restrict which destinations a user may export to.** Any active
  project member can target any registered Orthanc modality or ProKnow
  collection. → building a per-user allow-list (`docs/safety-plan.md` §A).
- **Backend→`anon_db` link crosses into the DMZ unhardened.** The mapping
  table lives on the proxy's machine; the backend reaches out to it with no
  TLS or lookup-volume monitoring today. → TLS opt-in + app-side monitoring
  within HERMES's control now; a restricted DB role and (optionally) a CA
  cert are tracked as an external request to the table's owning team, not a
  HERMES deliverable (`docs/safety-plan.md` §B).
- **Project approval doesn't say what it authorises.** Approving a project is
  an unqualified yes/no on tool access — no cohort or volume bound, and
  reviewers aren't told that plainly. → approval-page banner + `CLAUDE.md`
  note, copy already agreed (`docs/safety-plan.md` §C).
- **Audit trail doesn't record what actually left, and can be edited with no
  trace.** A successful export's own record is typically just
  `{'status': 'Success'}`; `events`/`project_audit_log` are ordinary,
  editable Postgres rows. → hash-chained `events` + a real per-export
  manifest (counts, checksums, destination) (`docs/safety-plan.md` §D).
- **`event_type='success'` on import doesn't mean the patient was actually
  found anywhere.** `Importer.handle_patient` returns success whenever it
  completes without raising, including when all three sources come back
  empty — any "how many patients actually imported" figure needs a ground-truth
  check against Orthanc, not the event count. → post-import Orthanc
  verification + a "N/M patients imported" UI stat (`docs/safety-plan.md` §D3/§E).
- **No per-source reason when a patient isn't found.** `find_patient` returns
  bare `True`/`False` per source; a source query error and a genuine "not
  found" look identical today. → per-source reason strings (Mosaiq:
  "Incomplete planning data" / not found / source-query error; Pinnacle:
  reconstruction failure via `pinnacle_export.status`/`errors`; ProKnow:
  "Patient not found on ProKnow"), surfaced on the job and patient pages
  (`docs/safety-plan.md` §E).

## Documented limitations — accepted, no code change planned this iteration

- **No pre-flight review before a combined import+export job.** Can't
  confirm what's about to be sent until import has actually finished
  finding it, and pausing a multi-hour/day job to wait for a human is worse
  than not pausing at all. No design proposed; revisit once import-outcome
  data (§D3/§E above) exists, if this becomes worth solving.
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

## Separately tracked — see `docs/frontend-migration.md`

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

- The anon/real ID split and free-text scrubbing at every outbound API edge
  (`backend/src/identity/anon.py`, `results/endpoints.py`'s `_scrub`/`_scrub_json`).
- Fail-closed authorization discipline in `backend/src/projects/enforcement.py`
  (a DB error denies, never silently allows).
- The two-phase, session-gated SSE job pattern in `frontend/jobs/views.py` —
  reused as the mechanism for the new destination-allow-list admin UI and
  should stay the model for any future review/approval UI.
