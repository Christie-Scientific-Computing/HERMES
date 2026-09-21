# Feature F001: Connect to and verify the local PACS (Conquest)

## Purpose

Every other feature in this plan needs a way to actually talk DICOM to Conquest. This feature provides that: a small, reusable connection layer inside `frontend_fastapi` offering C-ECHO (verify), C-FIND (query), and C-MOVE-with-explicit-destination (relay) — the three DICOM operations F002 and F004 build on. It exists specifically because HERMES's `backend` cannot reach Conquest (firewalled) while `frontend_fastapi` can (see plan.md D001), so this logic cannot live where every other DICOM integration in this codebase lives (behind Orthanc's REST API, called from `backend`).

## Behaviour

- Configuration for the Conquest connection (its host, port, AE title, and the AE title `frontend_fastapi` presents as the calling application) is read from environment variables, following this repository's existing hardcoded-destination convention (e.g. `PINNACLE_PUSH_HOST`/`PORT`/`AE_TITLE` in `.env.example`).
- A connectivity check (DICOM C-ECHO) can be performed against Conquest, returning success/failure with enough detail to distinguish "Conquest unreachable" from "Conquest reachable but rejected the association" from "not configured at all."
- A patient/study/series-level query (DICOM C-FIND) can be issued against Conquest with a combination of search keys (see F002 for which keys the UI actually exposes), returning matching records.
- A move (DICOM C-MOVE) can be issued against Conquest for a given study or series, naming an explicit destination AE title other than the requester itself, and reports the outcome (completed/failed/warning sub-operation counts, as DICOM's C-MOVE response provides).
- All DICOM operations run off the async event loop (this app is ASGI/uvicorn) so a slow or hung Conquest association cannot stall unrelated requests — mirrors the existing `asyncio.to_thread()` pattern `backend`'s own Orthanc/ProKnow/Pinnacle calls already use for the same reason.
- Missing/incomplete Conquest configuration is a clear, distinguishable startup or first-use state, not a silent no-op — this feature moves real patient data, so a misconfigured deployment should fail loudly rather than pretend to work.

## Dependencies

None.

## Relevant code

### Existing

- `requirements.txt:27-29` — `pydicom`, `pynetdicom`, `pyorthanc` are already listed; `pynetdicom` is currently unused anywhere in the codebase (confirmed via repo-wide grep), so this feature is its first real caller.
- `backend/src/retrieve/logic.py:33-35, 88, 229-244, 326-349` — the closest existing analogue: how `PULL_MODALITY_AET_ONE`/`TWO` are read from env and used for query/retrieve against an external DICOM source. Different mechanism (Orthanc REST via `pyorthanc.Modality`, not raw `pynetdicom`), but the same *shape* of problem (a hardcoded external DICOM source, queried and pulled from).
- `.env.example:25-31` — existing hardcoded-DICOM-connection env var conventions (`PINNACLE_PUSH_HOST`/`PORT`/`AE_TITLE`, `PULL_MODALITY_AET_ONE`/`TWO`) to follow for naming new variables.
- `frontend_fastapi/settings.py` — where this project's own env-var-driven settings are declared and loaded (two-stage `load_dotenv`: repo-root `.env` then an optional `frontend_fastapi/.env`).
- CLAUDE.md's "Async threading" Key Design Pattern — the existing convention (`asyncio.to_thread()`) for running blocking I/O from async FastAPI handlers, which this feature's pynetdicom calls should follow.

### Likely changes

- `frontend_fastapi/settings.py` — add the new Conquest connection env vars (suggested names: `LOCAL_PACS_HOST`, `LOCAL_PACS_PORT`, `LOCAL_PACS_AE_TITLE`, `HERMES_FRONTEND_AE_TITLE`; implementer should confirm final names against any existing convention not yet surfaced).
- `.env.example` (repo root) — document the new variables alongside the existing DICOM-connection ones.
- A new module, e.g. `frontend_fastapi/local_pacs/conquest_client.py` — the actual `pynetdicom` wrapper (association setup, C-ECHO, C-FIND, C-MOVE).

## Acceptance criteria

### Scenario: Verifying Conquest is reachable

**Given** Conquest connection settings are configured and Conquest is running and reachable

**When** the connectivity check is invoked

**Then** it reports success

### Scenario: Conquest is configured but unreachable

**Given** Conquest connection settings are configured but point to a host/port nothing is listening on

**When** the connectivity check is invoked

**Then** it reports failure with a message distinguishing "could not connect" from an association-level rejection

### Scenario: Conquest is not configured

**Given** the Conquest connection environment variables are unset

**When** anything in this module is invoked

**Then** it fails clearly and immediately, not silently as a no-op (this differs deliberately from HERMES's anonymisation-boundary convention of "unset → passthrough," since a misconfigured local-PACS feature has no safe no-op equivalent — it either can reach real infrastructure or it can't)

### Scenario: A C-FIND query returns matches

**Given** Conquest holds a study matching a given anonymised patient ID

**When** a C-FIND is issued for that ID

**Then** the matching study record(s) are returned with the queried fields populated

### Scenario: A C-MOVE relays to an explicit third-party destination

**Given** a study identified on Conquest and a destination AE title Conquest is configured to reach

**When** a C-MOVE is issued naming that destination as the Move Destination AE Title (not `frontend_fastapi`'s own AE)

**Then** Conquest performs the transfer directly to that destination and the call returns the DICOM sub-operation outcome (counts of completed/failed/warning sub-operations)

## Edge cases

- Conquest's SCP may refuse to relay to a Move Destination AE other than the requester (this is an SCP-side configuration choice, not something `frontend_fastapi` controls) — the failure must surface as a clear, attributable error, not an ambiguous timeout or a false "success."
- An association that times out mid-operation (network partition, Conquest under load) must not hang the request indefinitely — needs an explicit timeout with a clear resulting error.
- A C-FIND or C-MOVE issued while Conquest configuration is only partially set (e.g. host set, AE title missing) should fail at configuration-validation time, not produce a confusing pynetdicom-level exception.

## Success

A developer (or an automated check) can call this module's C-ECHO against a real or test DICOM SCP and get a clear success/failure result; C-FIND and C-MOVE against a real or test SCP behave per DICOM semantics, with `TargetAet` support confirmed working against at least a non-Conquest test SCP (e.g. a throwaway `pynetdicom`-based test server or DCMTK's `storescp`/`movescu` tooling), since a real Conquest instance likely isn't available in every development/test environment.

## Failure behaviour

Every failure mode (misconfiguration, unreachable host, association rejected, DIMSE-level failure/warning status, timeout) must be distinguishable in the raised exception/error so callers (F002, F004) can show the user an accurate, specific message rather than a generic "something went wrong."

## Testing considerations

`pynetdicom` ships a test-friendly SCP (`pynetdicom.sop_class` fixtures / its own test utilities, or a minimal hand-rolled test SCP using `pynetdicom.ae.AE`) — use that rather than requiring a real Conquest instance for the test suite. Follow `frontend_fastapi/tests/`'s existing conventions (`conftest.py`, pytest) for test structure. Since this is genuinely new integration surface (no existing test in this codebase exercises `pynetdicom`), prioritise: config validation, C-ECHO success/failure, a C-FIND round trip against a test SCP, and a C-MOVE round trip against a test SCP asserting the `TargetAet` (or equivalent) is actually sent as the Move Destination.

## Implementation notes

- This is the one piece of this plan operating genuinely outside every existing DICOM-transfer convention in this codebase (which otherwise always goes through Orthanc's REST API via `pyorthanc`, never raw `pynetdicom`) — implementer should not assume any existing helper covers association/timeout/error handling here; it needs to be built from `pynetdicom` primitives directly.
- Keep this module's public surface narrow (echo/find/move) and free of any UI or HermesDB concerns — F002 and F004 are where those get layered on.

## Out of scope

- Any UI. This is a backend-of-the-frontend integration module only.
- Staging or storing any Conquest data inside Orthanc or HermesDB — this feature only ever relays metadata/queries and triggers transfers; it never receives or stores DICOM instance data itself.
