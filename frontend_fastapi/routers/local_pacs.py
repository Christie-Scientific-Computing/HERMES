"""
Routers for the local PACS (Conquest) feature set:

- F002 -- browse/search (require_login, no project gate, matching the
  existing ungated /studies Orthanc-browsing precedent) and its series
  drill-down partial.
- F003 -- destination management CRUD (require_data_custodian).
- F004 -- the move action (require_data_custodian), triggered from F002's
  results and calling F005's backend endpoint afterward to audit it.
- Round 2 F001 -- an automatic Conquest connectivity indicator on the browse
  page (an htmx partial calling conquest_client.echo()).
- Round 2 F002 -- moving several studies at once with live SSE progress
  (move_batch / move_batch_watch / move_batch_stream below). Unlike
  jobs.py's job_stream, this stream is a genuine local producer, not a
  relay of a backend-generated stream -- there is no backend task queue
  behind it (see .plans/local-pacs-query-round2/plan.md D002): the move
  loop runs as a background asyncio task in THIS process, writing into an
  in-memory, module-level _BATCHES dict keyed by a generated batch_id, so
  it keeps running to completion (and keeps auditing every study) even if
  nobody is watching the stream. This is the only place in the app holding
  ephemeral cross-request state in-process -- kept deliberately small
  rather than built out as general infrastructure.

All DICOM calls (frontend_fastapi/local_pacs/conquest_client.py) are
synchronous pynetdicom calls -- wrapped in asyncio.to_thread() here so a
slow/hung Conquest association can't stall the event loop, per CLAUDE.md's
Async threading pattern.
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session as DBSession

from frontend_fastapi import backend_client
from frontend_fastapi.database import get_db
from frontend_fastapi.deps import get_session, get_template_context, require_data_custodian, require_login
from frontend_fastapi.flash import flash
from frontend_fastapi.forms.local_pacs import LocalPacsDestinationForm, LocalPacsSearchForm
from frontend_fastapi.local_pacs import conquest_client as cc
from frontend_fastapi.models import LocalPacsDestination, Session, User
from frontend_fastapi.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/local_pacs", tags=["local_pacs"])


# ---- F002: browse/search ----

def _search_query(form: LocalPacsSearchForm) -> dict:
    """The current search's fields as a plain dict -- carried as hidden
    fields on each result's move mini-form (F004) so the browse page can
    redirect back to the SAME search after a move, rather than losing the
    results the custodian was just looking at."""
    return {
        "patient_id": form.patient_id.data or "",
        "study_date_from": form.study_date_from.data.isoformat() if form.study_date_from.data else "",
        "study_date_to": form.study_date_to.data.isoformat() if form.study_date_to.data else "",
        "study_description": form.study_description.data or "",
        "modalities_in_study": form.modalities_in_study.data or "",
    }


@router.get("", name="local_pacs_browse")
async def local_pacs_browse(
    request: Request, user: User = Depends(require_login), ctx: dict = Depends(get_template_context),
    db: DBSession = Depends(get_db),
):
    form = LocalPacsSearchForm(formdata=request.query_params)
    results: list[dict] = []
    truncated = False
    conquest_error = None
    # "submitted" (a hidden field always present on the search form, see
    # browse.html) distinguishes an actual form submission (even one with
    # every field left blank, which form.validate() below then rejects
    # with a clear message -- see F002's edge cases) from simply landing on
    # this page with no query string at all, which shows neither an error
    # nor a query.
    searched = "submitted" in request.query_params
    if searched and form.validate():
        if not cc.is_configured():
            conquest_error = "Local PACS (Conquest) is not configured on this deployment."
        else:
            try:
                found = await asyncio.to_thread(
                    cc.find_studies,
                    patient_id=form.patient_id.data or "", study_date_from=form.study_date_from.data,
                    study_date_to=form.study_date_to.data, study_description=form.study_description.data or "",
                    modalities_in_study=form.modalities_in_study.data or "",
                )
                results, truncated = found.matches, found.truncated
            except cc.ConquestError as e:
                conquest_error = str(e)

    patients: dict[str, list[dict]] = {}
    for study in results:
        patients.setdefault(study["patient_id"] or "(unknown)", []).append(study)

    destinations = db.query(LocalPacsDestination).order_by(LocalPacsDestination.display_name).all()
    return templates.TemplateResponse(request, "local_pacs/browse.html", {
        **ctx, "form": form, "patients": patients, "conquest_error": conquest_error, "searched": searched,
        "truncated": truncated, "search_query": _search_query(form), "destinations": destinations,
        "can_move": user.is_staff,
    })


@router.get("/status", name="local_pacs_status")
async def local_pacs_status(request: Request, user: User = Depends(require_login)):
    """htmx partial (round-2 F001): an automatic C-ECHO connectivity check,
    loaded via hx-trigger="load" into the same slot browse.html's results
    table later occupies once a search actually runs -- never blocks the
    main GET /local_pacs response itself."""
    if not cc.is_configured():
        return templates.TemplateResponse(request, "local_pacs/_status.html", {
            "reachable": False, "error": "Local PACS (Conquest) is not configured on this deployment.",
        })
    try:
        await asyncio.to_thread(cc.echo)
        return templates.TemplateResponse(request, "local_pacs/_status.html", {"reachable": True, "error": None})
    except cc.ConquestError as e:
        return templates.TemplateResponse(request, "local_pacs/_status.html", {"reachable": False, "error": str(e)})


@router.get("/series/{study_instance_uid}", name="local_pacs_series")
async def local_pacs_series(study_instance_uid: str, request: Request, user: User = Depends(require_login)):
    """htmx partial: a study row's series, fetched on expand rather than
    upfront for every result (a second C-FIND per study)."""
    error = None
    series: list[dict] = []
    try:
        series = await asyncio.to_thread(cc.find_series, study_instance_uid=study_instance_uid)
    except cc.ConquestError as e:
        error = str(e)
    # can_move controls whether browse.html's row has the extra batch-move
    # checkbox column (round-2 F002) -- this partial's own colspan must
    # match that same column count or the swapped-in fragment misaligns.
    colspan = 7 if user.is_staff else 6
    return templates.TemplateResponse(
        request, "local_pacs/_series.html", {"series": series, "error": error, "colspan": colspan},
    )


# ---- F004: move (single-study, and round-2's multi-study batch) ----

async def _move_and_audit_one(
    *, study_instance_uid: str, patient_id: str, destination_ae_title: str, destination_display_name: str,
    performed_by: str,
) -> tuple[str, Optional[str], bool]:
    """
    Moves one study via Conquest and audits the attempt (F005), tolerating
    an audit-call failure -- the exact "move, interpret MoveResult, call the
    audit endpoint, tolerate an audit failure" logic local_pacs_move always
    used, now shared with the batch flow below so the two can't drift apart
    (round-2 plan.md's Implementation notes).

    Returns (outcome, detail, audit_failed): outcome is "success"/"failure";
    detail is the failure reason (None on success); audit_failed is True iff
    the move's own outcome was recorded above but the audit call itself
    raised BackendError (the move is still real regardless -- D006).
    """
    try:
        result = await asyncio.to_thread(
            cc.move_study, study_instance_uid=study_instance_uid, destination_ae=destination_ae_title,
        )
        outcome = "success" if result.success else "failure"
        detail = None if result.success else (
            f"{result.failed} sub-operation(s) failed, {result.completed} completed, {result.warning} warning "
            f"(status 0x{result.status_code:04X})" if result.status_code is not None else "Move did not complete."
        )
    except cc.ConquestError as e:
        outcome = "failure"
        detail = str(e)

    audit_failed = False
    try:
        await backend_client.audit_local_pacs_move(
            anon_patient_id=patient_id, study_instance_uid=study_instance_uid,
            destination_ae_title=destination_ae_title, destination_display_name=destination_display_name,
            performed_by=performed_by, outcome=outcome, detail=detail,
        )
    except backend_client.BackendError:
        logger.exception("Local PACS move audit call failed for study %s", study_instance_uid)
        audit_failed = True

    return outcome, detail, audit_failed


def _search_redirect_url(request: Request, form_data) -> str:
    redirect_params = {
        k: form_data.get(k, "") for k in
        ("patient_id", "study_date_from", "study_date_to", "study_description", "modalities_in_study")
        if form_data.get(k)
    }
    # "submitted" must be carried through too -- without it, local_pacs_browse
    # treats the redirect as a fresh, un-submitted page load and skips
    # re-running the search entirely (see that route's own "submitted" check),
    # silently dropping the results the custodian was just looking at.
    redirect_params["submitted"] = "1"
    return f"{request.url_for('local_pacs_browse')}?{urlencode(redirect_params)}"


@router.post("/move", name="local_pacs_move")
async def local_pacs_move(
    request: Request, user: User = Depends(require_data_custodian), session: Session = Depends(get_session),
    db: DBSession = Depends(get_db),
):
    form_data = await request.form()
    patient_id = str(form_data.get("patient_id", ""))
    study_instance_uid = str(form_data.get("study_instance_uid", ""))
    destination_id = form_data.get("destination_id")
    redirect_url = _search_redirect_url(request, form_data)

    destination = None
    if destination_id:
        try:
            destination = db.get(LocalPacsDestination, int(destination_id))
        except ValueError:
            destination = None
    if destination is None:
        flash(session, "error", "No such destination -- it may have been removed. Choose another and try again.")
        return RedirectResponse(redirect_url, status_code=303)

    outcome, detail, audit_failed = await _move_and_audit_one(
        study_instance_uid=study_instance_uid, patient_id=patient_id,
        destination_ae_title=destination.ae_title, destination_display_name=destination.display_name,
        performed_by=user.username,
    )
    audit_warning = " (warning: the audit record for this move could not be saved)" if audit_failed else ""

    if outcome == "success":
        flash(session, "success", f"Study relayed to {destination.display_name}.{audit_warning}")
    else:
        flash(session, "error", f"Move to {destination.display_name} failed: {detail}{audit_warning}")

    return RedirectResponse(redirect_url, status_code=303)


# ---- Round-2 F002: batch move ----
#
# In-memory, module-level, keyed by a generated batch_id -- see this
# module's own docstring for why there's nowhere else for this state to
# live. Each value: {"items", "destination_ae_title", "destination_display_name",
# "started_by", "created_at", "events" (ordered log), "finished", "back_url",
# "task"}. _run_batch is the sole writer of "events"/"finished"; everything
# else is set once at creation and never mutated.
_BATCHES: dict[str, dict] = {}
_BATCH_MAX_AGE_SECONDS = 3600
_BATCH_POLL_INTERVAL_SECONDS = 0.2


def _purge_stale_batches() -> None:
    """Lazy sweep, triggered by new-batch traffic rather than a background
    thread/task of its own (round-2 plan.md: keep this as small as the job
    justifies). Only removes FINISHED batches -- an unfinished one is either
    still legitimately running or will be swept on a later call once it is,
    never killed mid-flight by this."""
    cutoff = time.time() - _BATCH_MAX_AGE_SECONDS
    stale = [bid for bid, b in _BATCHES.items() if b["finished"] and b["created_at"] < cutoff]
    for bid in stale:
        _BATCHES.pop(bid, None)


async def _run_batch(batch_id: str) -> None:
    """
    The background task a POST /move_batch kicks off (asyncio.create_task)
    -- runs independently of whether/when a client ever opens the watch
    page or its stream, so an already-dispatched C-MOVE always finishes and
    gets audited regardless (round-2 plan.md's Implementation notes: "the
    stream generator should keep running to completion... regardless of
    whether a client is still listening").
    """
    batch = _BATCHES[batch_id]
    items = batch["items"]
    batch["events"].append({"type": "start", "total": len(items)})
    for item in items:
        batch["events"].append({"type": "progress", "current": item["label"]})
        outcome, detail, audit_failed = await _move_and_audit_one(
            study_instance_uid=item["study_instance_uid"], patient_id=item["patient_id"],
            destination_ae_title=batch["destination_ae_title"], destination_display_name=batch["destination_display_name"],
            performed_by=batch["started_by"],
        )
        if outcome == "success":
            status_text = "moved" + (" (audit not recorded)" if audit_failed else "")
            batch["events"].append({"type": "success", "study": item["label"], "status": status_text})
        else:
            warning = " (audit not recorded either)" if audit_failed else ""
            batch["events"].append({"type": "error", "study": item["label"], "error": f"{detail}{warning}"})
    batch["events"].append({"type": "done"})
    batch["finished"] = True


async def _stream_batch_events(batch: dict):
    """
    Index-based polling over batch["events"] (not a queue) -- a shared
    single-consumer queue can't correctly support a client reconnecting
    mid-batch and seeing the full history so far, which polling a shared
    list trivially does. Cheap at this scale: real DICOM C-MOVEs take
    seconds, so a sub-second poll granularity is imperceptible.
    """
    idx = 0
    while True:
        events = batch["events"]
        while idx < len(events):
            event = events[idx]
            idx += 1
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
            if event["type"] == "done":
                return
        if batch["finished"]:
            return
        await asyncio.sleep(_BATCH_POLL_INTERVAL_SECONDS)


def _parse_selected_items(raw_items: list[str]) -> list[dict]:
    items = []
    for raw in raw_items:
        try:
            parsed = json.loads(raw)
            study_instance_uid = str(parsed["study_instance_uid"])
            patient_id = str(parsed["patient_id"])
        except (ValueError, KeyError, TypeError):
            continue  # a malformed/tampered row is skipped, not a 500
        description = parsed.get("study_description") or study_instance_uid
        items.append({
            "study_instance_uid": study_instance_uid, "patient_id": patient_id,
            "label": f"{patient_id} — {description}",
        })
    return items


@router.post("/move_batch", name="local_pacs_move_batch")
async def local_pacs_move_batch(
    request: Request, user: User = Depends(require_data_custodian), session: Session = Depends(get_session),
    db: DBSession = Depends(get_db),
):
    form_data = await request.form()
    redirect_url = _search_redirect_url(request, form_data)

    items = _parse_selected_items(form_data.getlist("selected"))
    if not items:
        flash(session, "error", "Select at least one study to move.")
        return RedirectResponse(redirect_url, status_code=303)

    destination_id = form_data.get("destination_id")
    destination = None
    if destination_id:
        try:
            destination = db.get(LocalPacsDestination, int(destination_id))
        except ValueError:
            destination = None
    if destination is None:
        flash(session, "error", "Choose a destination for the selected studies.")
        return RedirectResponse(redirect_url, status_code=303)

    _purge_stale_batches()
    batch_id = str(uuid.uuid4())
    _BATCHES[batch_id] = {
        "items": items, "destination_ae_title": destination.ae_title,
        "destination_display_name": destination.display_name, "started_by": user.username,
        "created_at": time.time(), "events": [], "finished": False, "back_url": redirect_url,
    }
    # Referenced from the batch dict so it isn't garbage-collected before
    # completion -- asyncio only holds a weak reference to a bare
    # create_task() result otherwise.
    _BATCHES[batch_id]["task"] = asyncio.create_task(_run_batch(batch_id))

    return RedirectResponse(
        request.url_for("local_pacs_move_batch_watch", batch_id=batch_id), status_code=303,
    )


def _get_owned_batch(batch_id: str, user: User) -> dict:
    batch = _BATCHES.get(batch_id)
    if batch is None or batch["started_by"] != user.username:
        raise HTTPException(status_code=404, detail="Unknown batch, or you don't have access to it")
    return batch


@router.get("/move_batch/{batch_id}/watch", name="local_pacs_move_batch_watch")
async def local_pacs_move_batch_watch(
    batch_id: str, user: User = Depends(require_data_custodian), ctx: dict = Depends(get_template_context),
):
    batch = _get_owned_batch(batch_id, user)
    return templates.TemplateResponse(ctx["request"], "local_pacs/move_batch_watch.html", {
        **ctx, "batch_id": batch_id, "total": len(batch["items"]), "back_url": batch["back_url"],
    })


@router.get("/move_batch/{batch_id}/stream", name="local_pacs_move_batch_stream")
async def local_pacs_move_batch_stream(batch_id: str, user: User = Depends(require_data_custodian)):
    batch = _get_owned_batch(batch_id, user)
    return StreamingResponse(
        _stream_batch_events(batch), media_type="text/event-stream", headers={"Cache-Control": "no-cache"},
    )


# ---- F003: destination management ----

@router.get("/destinations", name="local_pacs_destinations")
async def destination_list(
    user: User = Depends(require_data_custodian), ctx: dict = Depends(get_template_context),
    db: DBSession = Depends(get_db),
):
    destinations = db.query(LocalPacsDestination).order_by(LocalPacsDestination.display_name).all()
    return templates.TemplateResponse(ctx["request"], "local_pacs/destinations.html", {**ctx, "destinations": destinations})


@router.api_route("/destinations/add", methods=["GET", "POST"], name="local_pacs_destination_add")
async def destination_add(
    request: Request, user: User = Depends(require_data_custodian), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context), db: DBSession = Depends(get_db),
):
    form = LocalPacsDestinationForm(formdata=await request.form() if request.method == "POST" else None)
    if request.method == "POST":
        is_valid = form.validate()
        if is_valid and db.query(LocalPacsDestination).filter_by(ae_title=form.ae_title.data).one_or_none() is not None:
            form.ae_title.errors.append("A destination with that AE title already exists.")
            is_valid = False
        if is_valid:
            db.add(LocalPacsDestination(
                ae_title=form.ae_title.data, display_name=form.display_name.data,
                description=form.description.data or "", created_by=user.username,
            ))
            db.commit()
            flash(session, "success", f"Added destination {form.display_name.data}.")
            return RedirectResponse(request.url_for("local_pacs_destinations"), status_code=303)
    return templates.TemplateResponse(request, "local_pacs/destination_form.html", {**ctx, "form": form, "editing": False})


@router.api_route("/destinations/{destination_id}/edit", methods=["GET", "POST"], name="local_pacs_destination_edit")
async def destination_edit(
    destination_id: int, request: Request, user: User = Depends(require_data_custodian),
    session: Session = Depends(get_session), ctx: dict = Depends(get_template_context), db: DBSession = Depends(get_db),
):
    destination = db.get(LocalPacsDestination, destination_id)
    if destination is None:
        flash(session, "error", "No such destination.")
        return RedirectResponse(request.url_for("local_pacs_destinations"), status_code=303)

    if request.method == "POST":
        form = LocalPacsDestinationForm(formdata=await request.form())
        is_valid = form.validate()
        duplicate = db.query(LocalPacsDestination).filter(
            LocalPacsDestination.ae_title == form.ae_title.data, LocalPacsDestination.id != destination_id,
        ).one_or_none()
        if is_valid and duplicate is not None:
            form.ae_title.errors.append("A destination with that AE title already exists.")
            is_valid = False
        if is_valid:
            destination.ae_title = form.ae_title.data
            destination.display_name = form.display_name.data
            destination.description = form.description.data or ""
            db.commit()
            flash(session, "success", f"Updated destination {destination.display_name}.")
            return RedirectResponse(request.url_for("local_pacs_destinations"), status_code=303)
    else:
        form = LocalPacsDestinationForm(data={
            "ae_title": destination.ae_title, "display_name": destination.display_name,
            "description": destination.description,
        })
    return templates.TemplateResponse(request, "local_pacs/destination_form.html", {
        **ctx, "form": form, "editing": True, "destination": destination,
    })


@router.post("/destinations/{destination_id}/delete", name="local_pacs_destination_delete")
async def destination_delete(
    destination_id: int, request: Request, user: User = Depends(require_data_custodian),
    session: Session = Depends(get_session), db: DBSession = Depends(get_db),
):
    destination = db.get(LocalPacsDestination, destination_id)
    if destination is not None:
        db.delete(destination)
        db.commit()
        flash(session, "success", f"Removed destination {destination.display_name}.")
    return RedirectResponse(request.url_for("local_pacs_destinations"), status_code=303)
