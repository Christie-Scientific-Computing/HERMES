# TODO

Superseded by `CLAUDE.md`'s "Known Gaps / TODO" section and `docs/known-issues.md` — kept here only for items not yet tracked there.

1. Replace `pinn_db` (Pinnacle's own SQLite export cache) with a Postgres `pinnacle_index` schema.
2. ~~Re-design Web~~ — in progress, see `frontend_fastapi/` and `docs/frontend-rewrite-implementation-plan.md`.
3. ~~Workers?~~ — done, see `docs/worker-queue-design.md` and `backend/worker.py`.
4. Do we need study endpoint? Not sure it's useful to know what is already on internal Orthanc?
5. Implement ukCAT PACS query. Don't import if patient already on there? How do we know that's all data that exists? (esp. with pinnacle data) — note: the PACS-comparison querying that used to exist in `gateway/` was deleted in the 2026-07-30 cleanup and would need rebuilding from scratch.