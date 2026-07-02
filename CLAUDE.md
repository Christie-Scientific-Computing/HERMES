# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

HERMES (**H**andles **E**verything: **R**etrieve, **M**odify, **E**xport **S**tuff) is a medical imaging data management app for the Christie NHS Foundation Trust. It centralises radiotherapy planning data from Mosaiq, Pinnacle, and Raystation into a single Orthanc DICOM server, then exports to DICOM modalities or ProKnow.

## Running the App

Two processes must run concurrently:

```bash
# Backend (FastAPI)
fastapi run backend/main.py
# or with hot reload:
python -m uvicorn backend.main:app --reload

# Frontend (Streamlit) — separate terminal
streamlit run Home.py
```

Backend requires `STATUS_DB` env var set before it will start — it exits immediately otherwise.

## Environment Variables

Required in `.env` (not committed):

| Variable | Purpose |
|---|---|
| `ORTHANC_URL`, `ORTHANC_USER`, `ORTHANC_PASS` | Central DICOM server |
| `BACKEND_URI`, `BACKEND_PORT` | FastAPI location (used by Streamlit pages) |
| `STATUS_DB` | Path to SQLite job-tracking database |
| `PINN_DB` | Path to Pinnacle SQLite database |
| `PULL_MODALITY_AET_ONE`, `PULL_MODALITY_AET_TWO` | DICOM AE titles to pull from |
| `PATH_TO_CERT`, `PATH_TO_KEY` | TLS certificates for Orthanc |
| `PACS_AE_TITLE`, `PACS_HOST`, `PACS_PORT` | Remote PACS for C-FIND comparison (optional) |

ProKnow credentials live in `credentials.json` (git-ignored).

## Architecture

```
Home.py + pages/          ← Streamlit multi-page frontend
    │  (HTTP + SSE)
    ▼
backend/main.py           ← FastAPI app
    ├── /import/*         ← retrieve/endpoints.py
    ├── /export/*         ← export/endpoints.py
    └── /results/*        ← results/endpoints.py
              │
              ├── StatusDB (SQLite)   ← status/db_client.py
              ├── Orthanc (DICOM hub) ← via pyorthanc
              ├── ProKnow (cloud RT)  ← via proknow SDK
              └── Pinnacle (local)    ← PinnacleExport submodule
```

**Pages** (`pages/`):
- `1_Retrieve.py` — CSV upload → batch import with live SSE progress + cancel
- `2_Modify.py` — Metadata editing (not yet implemented)
- `3_Export.py` — DICOM C-MOVE or ProKnow upload with live SSE progress + cancel
- `4_Results.py` — Inspect job/patient event timelines; supports job ID or CSV upload

**Backend modules** (`backend/src/`):
- `retrieve/logic.py` — `Importer` class: searches Mosaiq, Pinnacle, ProKnow; pulls DICOM to Orthanc; runs `_cleanup_orthanc()` to deduplicate and filter by import level
- `export/logic.py` — `Exporter` class: C-MOVE to registered modalities or ProKnow SDK upload
- `status/db_client.py` — `StatusDB`: wraps SQLite for job/patient/event tracking with WAL mode
- `database.py` — creates all SQLite tables on startup

## Key Design Patterns

**SSE streaming** — Batch operations stream `text/event-stream` from FastAPI; Streamlit polls via `threading.Thread` and uses `@st.fragment(run_every=0.5)` to refresh the UI. SSE message types: `start`, `progress`, `success`, `error`, `cancelled`, `{done: True}`.

**Cancellation** — Each batch job gets a UUID. A `cancel_flags: dict[str, bool]` (protected by `threading.Lock`) allows `POST /import/cancel/{job_id}` or `POST /export/cancel/{job_id}` to stop processing mid-batch.

**Async threading** — FastAPI endpoints are `async` but the heavy sync I/O (Orthanc, ProKnow, Pinnacle) runs via `asyncio.to_thread()` to avoid blocking the event loop.

**Import levels** — `Importer._set_import_level()` controls which DICOM modalities are accepted: `Planning data` (CT, RTSTRUCT, RTPLAN, RTDOSE), `Images only` (CT, MR, REG), `Everything` (all).

**Orthanc cleanup** — After importing a patient, `_cleanup_orthanc()` deletes studies lacking RTDOSE (in Planning mode), CBCTs from Elekta manufacturers, and duplicate RTSTRUCT/RTPLAN series that differ between Mosaiq and Pinnacle sources.

## SQLite Schema

The `events` table is the core audit log:

```
jobs(job_id, created_at, created_by, description)
patients(job_id, mrn, input_path, created_at)
events(job_id, mrn, stage, event_type, ts, attempt, error_message, details)
```

`stage` is `'retrieve'` or `'export'`; `event_type` is `'start'`, `'success'`, or `'failure'`. Legacy tables (`status`, `uploads`, `errors`, `plans`) exist for backwards compatibility.

## Git Submodule

`backend/src/retrieve/PinnacleExport/` is a git submodule for Pinnacle DICOM export. After cloning, run `git submodule update --init` to populate it.

## Known Gaps (TODOs in Code)

- Raystation import not implemented
- `2_Modify.py` page not implemented
- ProKnow RTSTRUCT UID regeneration workaround incomplete
- CBCT export and "all images" export option pending
