<img src='./static/hermes-logo.svg'>

### Handles Everything: Receive, Modify, Export Stuff

Web-app for exporting plans from all data sources (Pinnacle, Raystation, Mosaiq).

## Outline
*1. Receive*: Centralises data across data sources. Will import data (granularity specified by `import_level`) from MOSAIQ, Pinnacle (via PinnacleExport) and Raystation (*TODO*) into a single Orthanc node (specified by `ORTHANC_URL`).

*2. Modify*: Not implemented

*3. Export*: Exports data from Orthanc to other DICOM nodes (need to be registered as Orthanc Modalities) or to ProKnow.

## System Architecture
<img src='./static/diagram.png'>

One FastAPI backend (`backend/`) holds every feature. An optional thin reverse proxy (`proxy/`) can sit in front of it on a separate machine for external access. See `CLAUDE.md` for the full architecture writeup, including the anonymisation boundary and database layout.

## Getting Started

### Prerequisites

- Python 3.11+
- A reachable PostgreSQL instance (for `DATABASE_URL` — job/event tracking)
- The `PinnacleExport` git submodule, if you need Pinnacle import/export to actually work:
  ```bash
  git submodule update --init --recursive
  ```
- An Orthanc instance, and `credentials.json` for ProKnow, if you need those integrations live. The backend will still start without them — you'll just get errors from the specific endpoints that need them.

There's no pinned dependency file at the repo root yet (in progress). At minimum you'll need, per component:
- **Backend**: `fastapi`, `uvicorn[standard]`, `httpx`, `requests`, `pydantic`, `python-dotenv`, `psycopg2-binary`, `alembic`, `sqlalchemy`, `python-multipart`, `polars`, `numpy`, `pydicom`, `pynetdicom`, `pyorthanc`, `proknow`
- **Proxy**: see `proxy/pyproject.toml` (`fastapi`, `uvicorn[standard]`, `httpx`, `python-dotenv`)
- **webui**: see `webui/pyproject.toml` (`django`, `requests`, `python-dotenv`)
- **Tests**: `pytest`, `pytest-asyncio`

### 1. Configure

Copy `.env.example` to `.env` at the repo root and fill it in — at minimum `ORTHANC_URL`/`ORTHANC_USER`/`ORTHANC_PASS` and `DATABASE_URL` (a Postgres DSN). See the comments in `.env.example` for everything else (`ANON_DB_*` is optional and only needed for external/anonymised deployments).

### 2. Run the backend

```bash
fastapi run backend/main.py
# or, with hot reload:
python -m uvicorn backend.main:app --reload
```

It exits immediately if `DATABASE_URL` isn't set. On startup it runs its Alembic migrations automatically against that database, so an empty Postgres database is all you need — no manual schema setup.

### 3. Run the UI (`webui/`)

This is a minimal Django app for exercising the backend by hand — not the planned production frontend (see `CLAUDE.md`). It talks directly to the backend via `BACKEND_URI`/`BACKEND_PORT` (both already set in your `.env` from step 1).

```bash
cd webui
python manage.py migrate   # once, sets up Django's own local session/admin DB
python manage.py runserver 8080   # backend's own default port is also 8000, so pick something else here
```

Then visit `http://127.0.0.1:8080/`.

### 4. Run the tests

Run from the **repo root** (not from inside `backend/` — the tests import `backend.src...` as a package, which only resolves from the root):

```bash
pip install pytest pytest-asyncio psycopg2-binary alembic sqlalchemy fastapi httpx python-multipart
DATABASE_URL="postgresql://<user>:<pass>@<host>:<port>/<db>" python -m pytest backend/tests/
```

Tests need a real Postgres reachable via `DATABASE_URL` — there's no mocked/in-memory DB layer. A throwaway container works fine for this:

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=hermes_test postgres:16-alpine
```

`test_cleanup_orthanc.py` additionally needs the `PinnacleExport` submodule checked out (see Prerequisites above) — it skips itself gracefully if that submodule isn't present.

## TODO
- Update to use central orthanc
- Link MosaiqDataDirector with Central Orthanc
- Handle CBCT exports, export all images option
- Rebuild a pinned dependency file (`requirements.txt` or equivalent) at the repo root
