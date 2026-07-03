"""
Import page — fetch RT planning data into Orthanc from Mosaiq / Pinnacle.
"""
import csv
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

st.set_page_config(page_title="Import — HERMES", page_icon="🪽", layout="wide")
st.title("Import")
st.markdown("""
Fetch RT planning data from archives and treatment planning systems into Orthanc.

Upload a CSV of patient IDs (header: `patient_id`) and click **Run**.
Lines starting with `#` are skipped.
""")


@st.fragment(run_every=0.5)
def show_progress(file_bytes: bytes, file_name: str, import_level: str):
    if "job_id" not in st.session_state:
        job_id   = str(uuid.uuid4())
        messages = []
        st.session_state["job_id"]   = job_id
        st.session_state["messages"] = messages

        def stream():
            try:
                with requests.post(
                    f"{BASE_URL}/import/batch_import_file",
                    files={"file": (file_name, file_bytes, "text/csv")},
                    data={"job_id": job_id, "import_level": import_level},
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
        requests.post(f"{BASE_URL}/import/cancel/{st.session_state['job_id']}", timeout=5)

    messages     = st.session_state.get("messages", [])
    real_to_anon = st.session_state.get("real_to_anon", {})
    terminal     = next((m for m in messages if m.get("type") == "cancelled" or m.get("done")), None)

    if terminal and terminal.get("type") == "cancelled":
        label, state = "Cancelled", "error"
    elif terminal and terminal.get("done"):
        label, state = "Complete", "complete"
    else:
        label, state = "Importing…", "running"

    total     = next((m["total"] for m in messages if m.get("type") == "start"), 0)
    completed = sum(1 for m in messages if m.get("type") in ("success", "error"))

    progress = (completed / total) if total else 0
    prog_text = f"{completed} / {total}" if total else "Starting…"

    st.progress(progress, text=prog_text)
    status = st.status(label, expanded=not terminal, state=state)

    patient_slots = {}
    errors = []

    st.button("Stop", type="primary", on_click=stop_job, disabled=bool(terminal))

    for msg in messages:
        t = msg.get("type")
        if t == "start":
            status.write(f"Importing {msg['total']} patients")
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


# ── Main ──────────────────────────────────────────────────────────────────────

for key in ("job_id", "messages", "real_to_anon"):
    if key in st.session_state:
        del st.session_state[key]

uploaded = st.file_uploader("Patient CSV", type=["csv"], help="Must have a patient_id column")
import_level = st.selectbox(
    "Import level",
    ("Planning data", "Images only", "Everything"),
    help="Planning data: CT + RTSTRUCT + RTPLAN + RTDOSE\nImages only: CT + MR + REG\nEverything: all of the above",
)

st.divider()
if not uploaded:
    st.warning("⚠️ Upload a CSV before running.")

if st.button("▶ Run", disabled=not uploaded, type="primary"):
    file_bytes = uploaded.getvalue()
    file_name  = uploaded.name

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
        real_to_anon = {v: k for k, v in anon_to_real.items()}
        st.session_state["real_to_anon"] = real_to_anon
        file_bytes = anon.rewrite_csv_patient_ids(file_bytes, anon_to_real)

    show_progress(file_bytes, file_name, import_level)

with st.sidebar:
    st.header("ℹ️ Info")
    st.info("**CSV format**\n\nOne column, header `patient_id`. Comment lines with `#`.")
