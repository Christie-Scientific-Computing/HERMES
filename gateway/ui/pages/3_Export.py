"""
Export page — move data from Orthanc to a DICOM destination or ProKnow.

Accepts two CSV formats:
  - patient_id column  → existing patient-level C-MOVE / ProKnow upload
  - study_instance_uid / series_instance_uid columns → targeted UID-based C-MOVE
    (use the CSV downloaded from the Studies page, filter it, then upload here)
"""
import csv
import io
import os
import sys
import json
import uuid
import threading
import requests
import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import anon

load_dotenv()

GATEWAY_URI  = os.getenv("GATEWAY_URI", "localhost")
GATEWAY_PORT = os.getenv("GATEWAY_PORT", "8001")
BASE_URL     = f"http://{GATEWAY_URI}:{GATEWAY_PORT}"

st.set_page_config(page_title="Export — HERMES", page_icon="🪽", layout="wide")
st.title("Export")
st.markdown("""
Move data from Orthanc to a DICOM destination or upload to ProKnow.

Upload a **patient CSV** (`patient_id` column) to export all studies per patient,
or upload a **study/series CSV** (downloaded from the Studies page) to export specific studies.
""")


# ── PACS pre-filtering ────────────────────────────────────────────────────────

def _filter_csv_by_pacs(file_bytes: bytes, level: str) -> tuple[bytes, int]:
    """
    Query the gateway's PACS endpoints and return (filtered_csv_bytes, n_skipped).
    At study level, checks StudyInstanceUID; at series level, checks SeriesInstanceUID.
    Items whose PACS status is unknown (null) are NOT skipped — only confirmed positives.
    """
    content = file_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(content.splitlines())
    rows = list(reader)
    fieldnames = reader.fieldnames or []

    if not rows:
        return file_bytes, 0

    if level == "series":
        uid_key  = "series_instance_uid"
        endpoint = f"{BASE_URL}/pacs/query_series"
        body_key = "series_uids"
    else:
        uid_key  = "study_instance_uid"
        endpoint = f"{BASE_URL}/pacs/query_studies"
        body_key = "study_uids"

    unique_uids = list({
        (row.get(uid_key) or "").strip()
        for row in rows
        if (row.get(uid_key) or "").strip()
    })

    if not unique_uids:
        return file_bytes, 0

    try:
        res = requests.post(endpoint, json={body_key: unique_uids}, timeout=120)
        if not res.ok:
            st.warning(f"PACS check failed ({res.status_code}) — exporting all items.")
            return file_bytes, 0
        pacs_status = res.json().get("results", {})
    except Exception as exc:
        st.warning(f"Could not reach PACS — exporting all items: {exc}")
        return file_bytes, 0

    # Only skip items confirmed present (True); leave unknowns (None) through
    filtered = [
        row for row in rows
        if pacs_status.get((row.get(uid_key) or "").strip()) is not True
    ]
    n_skipped = len(rows) - len(filtered)

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(filtered)
    return out.getvalue().encode("utf-8"), n_skipped


# ── Shared SSE progress fragment ──────────────────────────────────────────────

@st.fragment(run_every=0.5)
def show_progress(file_bytes: bytes, file_name: str, endpoint: str, form_fields: dict):
    if "job_id" not in st.session_state:
        job_id   = str(uuid.uuid4())
        messages = []
        st.session_state["job_id"]   = job_id
        st.session_state["messages"] = messages

        def stream():
            try:
                with requests.post(
                    f"{BASE_URL}/{endpoint}",
                    files={"file": (file_name, file_bytes, "text/csv")},
                    data={"job_id": job_id, **form_fields},
                    stream=True,
                    timeout=(10, None),
                ) as resp:
                    for line in resp.iter_lines(decode_unicode=True):
                        if line and line.startswith("data:"):
                            try:
                                messages.append(json.loads(line[len("data:"):].strip()))
                            except Exception:
                                pass
            except Exception as exc:
                messages.append({"type": "error", "mrn": "connection", "error": str(exc), "execution_time": 0})
                messages.append({"done": True})

        threading.Thread(target=stream, daemon=True).start()

    def stop_job():
        requests.post(f"{BASE_URL}/export/cancel/{st.session_state['job_id']}", timeout=5)

    messages     = st.session_state.get("messages", [])
    real_to_anon = st.session_state.get("real_to_anon", {})
    terminal     = next((m for m in messages if m.get("type") == "cancelled" or m.get("done")), None)

    if terminal and terminal.get("type") == "cancelled":
        label, state = "Cancelled", "error"
    elif terminal and terminal.get("done"):
        label, state = "Complete", "complete"
    else:
        label, state = "Exporting…", "running"

    total     = next((m["total"] for m in messages if m.get("type") == "start"), 0)
    completed = sum(1 for m in messages if m.get("type") in ("success", "error"))
    progress  = (completed / total) if total else 0

    # Show PACS skip summary if applicable (stored before this fragment started)
    pacs_skipped = st.session_state.get("pacs_skipped", 0)
    if pacs_skipped:
        st.caption(f"⏭️ {pacs_skipped} item{'s' if pacs_skipped != 1 else ''} skipped (already on PACS)")

    st.progress(progress, text=f"{completed} / {total}" if total else "Starting…")
    status = st.status(label, expanded=not terminal, state=state)

    patient_slots: dict = {}
    errors = []

    st.button("Stop", type="primary", on_click=stop_job, disabled=bool(terminal))

    for msg in messages:
        t = msg.get("type")
        if t == "start":
            status.write(f"Exporting {msg['total']} item{'s' if msg['total'] != 1 else ''}")
        elif t == "progress":
            patient_slots[msg["current"]] = status.empty()
            display = real_to_anon.get(msg["current"], msg["current"])
            patient_slots[msg["current"]].markdown(f"⏳ `{display}`")
        elif t == "success":
            if msg["mrn"] in patient_slots:
                display = real_to_anon.get(msg["mrn"], msg["mrn"])
                patient_slots[msg["mrn"]].markdown(f"✅ `{display}` — {msg['execution_time']}s")
        elif t == "error":
            errors.append(msg)
            if msg["mrn"] in patient_slots:
                display = real_to_anon.get(msg["mrn"], msg["mrn"])
                patient_slots[msg["mrn"]].markdown(f"❌ `{display}` — {msg['error']}")
        elif t == "cancelled" or msg.get("done"):
            break

    if errors:
        with st.expander(f"{len(errors)} error{'s' if len(errors) > 1 else ''}"):
            for e in errors:
                display = real_to_anon.get(e["mrn"], e["mrn"])
                st.code(f"{display}: {e['error']}")


# ── CSV upload and type detection ─────────────────────────────────────────────

for key in ("job_id", "messages", "pacs_skipped", "real_to_anon"):
    if key in st.session_state:
        del st.session_state[key]

uploaded = st.file_uploader(
    "CSV file",
    type=["csv"],
    help="Patient CSV (`patient_id` column) or study/series CSV from the Studies page.",
)

csv_type = None
header   = []

if uploaded:
    content = uploaded.getvalue().decode("utf-8", errors="replace")
    reader  = csv.DictReader(content.splitlines())
    header  = [h.strip().lower() for h in (reader.fieldnames or [])]

    if "study_instance_uid" in header or "series_instance_uid" in header:
        csv_type = "uid"
        st.info("📋 Study/series UID format detected — will export specific studies.")
    elif "patient_id" in header:
        csv_type = "patient"
        st.info("📋 Patient ID format detected — will export all studies per patient.")
    else:
        st.error("CSV must have a `patient_id` or `study_instance_uid` column.")


# ── Destination selection ─────────────────────────────────────────────────────

skip_on_pacs = False

if csv_type == "uid":
    st.subheader("Options")
    dest_type    = "DICOM (C-MOVE)"    # UID-based export only supports DICOM
    export_level = st.radio(
        "Export level",
        ["Study", "Series"],
        horizontal=True,
        help="**Study**: move the entire study for each unique study UID. **Series**: move individual series (one C-MOVE per row, after deduplication).",
    )
    skip_on_pacs = st.checkbox(
        "Skip series/studies already on PACS",
        value=False,
        help="Before exporting, the gateway queries the PACS directly and removes items already present. "
             "Requires PACS_HOST and PACS_AE_TITLE in the gateway .env.",
    )
    st.caption("UID-based export uses DICOM C-MOVE only. For ProKnow, use the patient ID format.")

elif csv_type == "patient":
    st.subheader("Options")
    dest_type = st.radio("Destination type", ["DICOM (C-MOVE)", "ProKnow"], horizontal=True)

else:
    dest_type = None


# Destination picker (DICOM AE or ProKnow collection)
@st.cache_data(ttl=60)
def get_destinations():
    try:
        r1 = requests.get(f"{BASE_URL}/export/get_orthanc_modalities", timeout=10)
        modalities = r1.json() if r1.ok else []
    except Exception:
        modalities = []
    try:
        r2 = requests.get(f"{BASE_URL}/export/get_proknow_collections", timeout=10)
        collections = r2.json() if r2.ok else []
    except Exception:
        collections = []
    return modalities, collections


destination = None
if dest_type:
    modalities, collections = get_destinations()
    is_dicom = dest_type.startswith("DICOM")
    options  = modalities if is_dicom else collections

    if options:
        destination = st.selectbox("Destination", options)
    else:
        st.warning("No destinations available — check gateway/Hermes connectivity.")


# ── Run ───────────────────────────────────────────────────────────────────────

st.divider()
can_run = bool(uploaded and csv_type and destination)
if not can_run and uploaded:
    if not destination:
        st.warning("⚠️ Select a destination before running.")

if st.button("▶ Run", disabled=not can_run, type="primary"):
    file_bytes = uploaded.getvalue()
    file_name  = uploaded.name
    is_dicom   = dest_type.startswith("DICOM")

    if csv_type == "uid":
        if skip_on_pacs:
            with st.spinner("Querying PACS before export…"):
                file_bytes, n_skipped = _filter_csv_by_pacs(file_bytes, export_level.lower())
                if n_skipped:
                    st.session_state["pacs_skipped"] = n_skipped

        show_progress(
            file_bytes, file_name,
            endpoint    = "export/dicom_move_uids_file",
            form_fields = {"destination": destination, "level": export_level.lower()},
        )
    elif csv_type == "patient":
        if anon.is_configured():
            content  = file_bytes.decode("utf-8", errors="replace")
            reader   = csv.DictReader(content.splitlines())
            anon_ids = list(dict.fromkeys(
                row["patient_id"].strip()
                for row in reader
                if row.get("patient_id") and not row["patient_id"].strip().startswith("#")
            ))
            try:
                anon_to_real = anon.lookup_real_ids(anon_ids)
            except anon.AnonLookupError as exc:
                st.error(str(exc))
                st.stop()
            except Exception as exc:
                st.error(f"Anonymisation DB error: {exc}")
                st.stop()
            st.session_state["real_to_anon"] = {v: k for k, v in anon_to_real.items()}
            file_bytes = anon.rewrite_csv_patient_ids(file_bytes, anon_to_real)

        if is_dicom:
            show_progress(
                file_bytes, file_name,
                endpoint    = "export/dicom_move_file",
                form_fields = {"destination": destination},
            )
        else:
            show_progress(
                file_bytes, file_name,
                endpoint    = "export/proknow_upload_file",
                form_fields = {"collection": destination},
            )

with st.sidebar:
    st.header("ℹ️ Info")
    st.info(
        "**Patient CSV** — exports all studies currently in Orthanc for each patient.\n\n"
        "**Study/series CSV** — exports only the specific studies or series listed. "
        "Download this from the Studies page, filter the rows you want, then upload here.\n\n"
        "**Skip if on PACS** — the gateway queries the PACS directly before exporting "
        "and removes any items already present. Only available in UID export mode."
    )
