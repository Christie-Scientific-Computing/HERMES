"""
Export page — move data from Orthanc to a DICOM destination or ProKnow.
"""
import os
import json
import uuid
import threading
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

GATEWAY_URI  = os.getenv("GATEWAY_URI", "localhost")
GATEWAY_PORT = os.getenv("GATEWAY_PORT", "8001")
BASE_URL     = f"http://{GATEWAY_URI}:{GATEWAY_PORT}"

st.set_page_config(page_title="Export — HERMES", page_icon="🪽", layout="wide")
st.title("Export")
st.markdown("""
Move data from Orthanc to a DICOM destination (C-MOVE) or upload to a ProKnow collection.

Upload a CSV of patient IDs (header: `patient_id`) and click **Run**.
""")


@st.fragment(run_every=0.5)
def show_progress(file_bytes: bytes, file_name: str, endpoint: str, dest_key: str, dest_value: str):
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
                    data={"job_id": job_id, dest_key: dest_value},
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

    messages = st.session_state.get("messages", [])
    terminal = next((m for m in messages if m.get("type") == "cancelled" or m.get("done")), None)

    if terminal and terminal.get("type") == "cancelled":
        label, state = "Cancelled", "error"
    elif terminal and terminal.get("done"):
        label, state = "Complete", "complete"
    else:
        label, state = "Exporting…", "running"

    total     = next((m["total"] for m in messages if m.get("type") == "start"), 0)
    completed = sum(1 for m in messages if m.get("type") in ("success", "error"))

    progress  = (completed / total) if total else 0
    prog_text = f"{completed} / {total}" if total else "Starting…"

    st.progress(progress, text=prog_text)
    status = st.status(label, expanded=not terminal, state=state)

    patient_slots = {}
    errors = []

    st.button("Stop", type="primary", on_click=stop_job, disabled=bool(terminal))

    for msg in messages:
        t = msg.get("type")
        if t == "start":
            status.write(f"Exporting {msg['total']} patients")
        elif t == "progress":
            patient_slots[msg["current"]] = status.empty()
            patient_slots[msg["current"]].markdown(f"⏳ `{msg['current']}`")
        elif t == "success":
            if msg["mrn"] in patient_slots:
                patient_slots[msg["mrn"]].markdown(f"✅ `{msg['mrn']}` — {msg['execution_time']}s")
        elif t == "error":
            errors.append(msg)
            if msg["mrn"] in patient_slots:
                patient_slots[msg["mrn"]].markdown(f"❌ `{msg['mrn']}` — {msg['error']}")
        elif t == "cancelled" or msg.get("done"):
            break

    if errors:
        with st.expander(f"{len(errors)} error{'s' if len(errors) > 1 else ''}"):
            for e in errors:
                st.code(f"{e['mrn']}: {e['error']}")


# ── Main ──────────────────────────────────────────────────────────────────────

for key in ("job_id", "messages"):
    if key in st.session_state:
        del st.session_state[key]

uploaded = st.file_uploader("Patient CSV", type=["csv"], help="Must have a patient_id column")

dest_type = st.radio("Destination type", ["DICOM (C-MOVE)", "ProKnow"], horizontal=True)
is_dicom  = dest_type.startswith("DICOM")

# Populate destination options from gateway API
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

modalities, collections = get_destinations()
options = modalities if is_dicom else collections

if options:
    destination = st.selectbox("Destination", options)
else:
    st.warning("No destinations available. Check gateway/Hermes connectivity.")
    destination = None

st.divider()
can_run = bool(uploaded and destination)
if not uploaded:
    st.warning("⚠️ Upload a CSV before running.")

if st.button("▶ Run", disabled=not can_run, type="primary"):
    endpoint = "export/dicom_move_file" if is_dicom else "export/proknow_upload_file"
    dest_key = "destination"            if is_dicom else "collection"
    show_progress(uploaded.getvalue(), uploaded.name, endpoint, dest_key, destination)

with st.sidebar:
    st.header("ℹ️ Info")
    st.info("**DICOM C-MOVE**: pushes to a registered Orthanc modality.\n\n**ProKnow**: uploads to the configured workspace collection.")
