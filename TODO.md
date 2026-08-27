# TODO

Superseded by `CLAUDE.md`'s "Known Gaps / TODO" section and `docs/known-issues.md` — kept here only for items not yet tracked there.

1. ~~Replace `pinn_db`~~ (Pinnacle's own SQLite export cache) with a Postgres `pinnacle_index` schema.
2. ~~Re-design Web~~ — in progress, see `frontend_fastapi/` and `docs/frontend-rewrite-implementation-plan.md`.
3. ~~Workers?~~ — done, see `docs/worker-queue-design.md` and `backend/worker.py`.
4. ~~Do we need study endpoint? Not sure it's useful to know what is already on internal Orthanc?~~
5. Implement ukCAT PACS query. Don't import if patient already on there? How do we know that's all data that exists? (esp. with pinnacle data) — note: the PACS-comparison querying that used to exist in `gateway/` was deleted in the 2026-07-30 cleanup and would need rebuilding from scratch.
6. An error/issue reporting page would be useful. Users should be able to raise app issues but also anonymisation/data-leak issues. 
7. Documentation/User guide page? Maybe doesn't need to be big but reference for users to go to + people to contact if any issues. 
8. Add export destination +  patient ID list (optional) in project approval page. Export destination is mandatory and should be as fine-grained as picking ProKnow collections (if ProKnow is the destination). Patient IDs are optional but should raise a warning (prior to submission and during review).
9. Add status/overview to the project page. X patients requested, Y sent to DESTINATION.