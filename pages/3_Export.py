"""
Streamlit page for exporting to different data sources.
If DICOM node, should just use C-MOVE
If ProKnow use API.

Author: Donal McSweeney
Date: 05/05/2025
Version: 0.01

Copyright (C) 2026 The Christie NHS
Foundation Trust
"""
import os
import json
import logging
from pathlib import Path
import streamlit as st
import requests
import threading
import uuid
from dotenv import load_dotenv

load_dotenv()
BACKEND_URI = os.getenv('BACKEND_URI')
BACKEND_PORT = os.getenv('BACKEND_PORT')
ORTHANC_URL = os.getenv('ORTHANC_URL')

logger = logging.getLogger(__name__)



@st.fragment(run_every=0.5)
def export_to_dicom_scp(path_to_csv, destination):
    if "job_id" not in st.session_state:
        st.session_state["job_id"] = str(uuid.uuid4())
        st.session_state["messages"] = []
        st.session_state["import_done"] = False

        job_id = st.session_state["job_id"]
        messages = st.session_state["messages"]

        def stream():
            url = f"http://{BACKEND_URI}:{BACKEND_PORT}/export/dicom_move"
            payload = {"job_id": job_id, "path_to_csv": str(path_to_csv), "destination": destination}
            with requests.post(url, json=payload, stream=True, timeout=(10, None)) as response:
                for line in response.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    messages.append(json.loads(raw))

        threading.Thread(target=stream, daemon=True).start()

    def stop_job():
        requests.post(f"http://{BACKEND_URI}:{BACKEND_PORT}/export/cancel/{st.session_state['job_id']}")

    # determine terminal state before creating any UI elements
    messages = st.session_state.get("messages", [])
    terminal = next((m for m in messages if m.get("type") == "cancelled" or m.get("done")), None)

    if terminal and terminal.get("type") == "cancelled":
        initial_label, initial_state = "Cancelled by user", "error"
    elif terminal and terminal.get("done"):
        initial_label, initial_state = "All done!", "complete"
    else:
        initial_label, initial_state = "Exporting patients...", "running"

    total_in_messages = next((m["total"] for m in messages if m.get("type") == "start"), 0)
    completed_in_messages = sum(1 for m in messages if m.get("type") == "success")

    if terminal and total_in_messages:
        initial_progress = 1.0 if terminal.get("done") else completed_in_messages / total_in_messages
        initial_text = "Done" if terminal.get("done") else f"Exported {completed_in_messages} / {total_in_messages}"
    elif total_in_messages:
        initial_progress = completed_in_messages / total_in_messages
        initial_text = f"Exported {completed_in_messages} / {total_in_messages}"
    else:
        initial_progress = 0
        initial_text = f"Sending C-MOVE to {destination}"

    progress_bar = st.progress(initial_progress, text=initial_text)
    status = st.status(initial_label, expanded=not terminal, state=initial_state)
    patient_progress = {}
    total = 0
    completed = 0
    errors = []

    st.button("Stop", type="primary", on_click=stop_job)

    for message in messages:
        if message.get("type") == "start":
            total = message["total"]
            status.write(f"Exporting {total} patients")

        elif message.get("type") == "progress":
            patient_progress[message['current']] = status.empty()
            patient_progress[message['current']].markdown(f"Exporting `{message['current']}`")

        elif message.get("type") == "success":
            completed += 1
            patient_progress[message['mrn']].markdown(f"Finished exporting `{message['mrn']}` in {message['execution_time']}s")

        elif message.get("type") == "error":
            errors.append(message)
            patient_progress[message['mrn']].markdown(f"Failed: `{message['mrn']}` — {message['error']}")

        elif message.get("type") == "cancelled":
            break

        elif message.get("done"):
            break

def upload_to_proknow(path_to_csv, collection):
    if "job_id" not in st.session_state:
        st.session_state["job_id"] = str(uuid.uuid4())
        st.session_state["messages"] = []
        st.session_state["import_done"] = False

        job_id = st.session_state["job_id"]
        messages = st.session_state["messages"]

        def stream():
            url = f"http://{BACKEND_URI}:{BACKEND_PORT}/export/proknow_upload"
            payload = {"job_id": job_id, "path_to_csv": str(path_to_csv), "collection": collection}
            with requests.post(url, json=payload, stream=True, timeout=(10, None)) as response:
                for line in response.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    messages.append(json.loads(raw))

        threading.Thread(target=stream, daemon=True).start()

    def stop_job():
        requests.post(f"http://{BACKEND_URI}:{BACKEND_PORT}/export/cancel/{st.session_state['job_id']}")

    # determine terminal state before creating any UI elements
    messages = st.session_state.get("messages", [])
    terminal = next((m for m in messages if m.get("type") == "cancelled" or m.get("done")), None)

    if terminal and terminal.get("type") == "cancelled":
        initial_label, initial_state = "Cancelled by user", "error"
    elif terminal and terminal.get("done"):
        initial_label, initial_state = "All done!", "complete"
    else:
        initial_label, initial_state = "Exporting patients...", "running"

    total_in_messages = next((m["total"] for m in messages if m.get("type") == "start"), 0)
    completed_in_messages = sum(1 for m in messages if m.get("type") == "success")

    if terminal and total_in_messages:
        initial_progress = 1.0 if terminal.get("done") else completed_in_messages / total_in_messages
        initial_text = "Done" if terminal.get("done") else f"Exported {completed_in_messages} / {total_in_messages}"
    elif total_in_messages:
        initial_progress = completed_in_messages / total_in_messages
        initial_text = f"Exported {completed_in_messages} / {total_in_messages}"
    else:
        initial_progress = 0
        initial_text = f"Uploading to {collection}"

    progress_bar = st.progress(initial_progress, text=initial_text)
    status = st.status(initial_label, expanded=not terminal, state=initial_state)
    patient_progress = {}
    total = 0
    completed = 0
    errors = []

    st.button("Stop", type="primary", on_click=stop_job)

    for message in messages:
        if message.get("type") == "start":
            total = message["total"]
            status.write(f"Exporting {total} patients")

        elif message.get("type") == "progress":
            patient_progress[message['current']] = status.empty()
            patient_progress[message['current']].markdown(f"Exporting `{message['current']}`")

        elif message.get("type") == "success":
            completed += 1
            patient_progress[message['mrn']].markdown(f"Finished exporting `{message['mrn']}` in {message['execution_time']}s")

        elif message.get("type") == "error":
            errors.append(message)
            patient_progress[message['mrn']].markdown(f"Failed: `{message['mrn']}` — {message['error']}")

        elif message.get("type") == "cancelled":
            break

        elif message.get("done"):
            break


def submit_form(data):
    uploads_dir = Path("./tmp")
    uploads_dir.mkdir(exist_ok=True)
    file_path = uploads_dir / data['patients_to_export'].name
    with open(file_path, 'wb') as f:
        f.write(data['patients_to_export'].getvalue())


    if data['dicom_export']:
        for dest in data['dicom_destinations']:
            export_to_dicom_scp(file_path, dest)
            # dicom_export


    if data['proknow_upload']:
        upload_to_proknow(file_path, data['proknow_collection'])



def main():
    # clear session state on page load
    for key in ["job_id", "messages", "import_done"]:
        if key in st.session_state:
            del st.session_state[key]

    st.set_page_config(
        page_title="Hermes",
        page_icon="🪽",
        layout="wide"
    )
    st.title("Export")
    st.markdown(f"""
        Export data to remote SCP or upload to ProKnow.

        Data will be sent from {ORTHANC_URL} to the specified AE title (Orthanc modality).

        If you can't see the intended destination AE title, contact admins.
         
    """)
    patients_to_export = st.file_uploader(
        "Patients to export",
        type=['csv'],  
        help="Upload a file containing christie IDs to export"
    )

    st.divider()
    dicom_export = st.checkbox("Send to DICOM node(s)", key='dicom_export')
    url = f"http://{BACKEND_URI}:{BACKEND_PORT}/export/get_orthanc_modalities"
    res = requests.get(url)
    if res.status_code == requests.codes.ok:
        res = res.json()
    else: 
        st.error("Could not get destinations:", res)
    
    
    dicom_destinations = st.multiselect(
            "Export destination(s)",
            res,
            disabled=not dicom_export,
            help="""
                Available export destinations.""",
        )
    
    st.divider()

    proknow_upload = st.checkbox("Upload to Proknow", key='proknow_upload')
    url = f'http://{BACKEND_URI}:{BACKEND_PORT}/export/get_proknow_collections'
    res = requests.get(url)
    if res.status_code == requests.codes.ok:
        res = res.json()
    else: 
        st.error("Could not get ProKnow collections:", res)
    # Perform an action based on the checkbox state
    
    proknow_collection = st.selectbox("ProKnow collection", 
        res,
        accept_new_options=False,
        disabled = not proknow_upload,
        index=None,
        help="Available collections on ProKnow")

    can_submit = True if any([patients_to_export is not None, dicom_export, proknow_upload]) else False


    if not can_submit:
        st.warning("⚠️ Please upload a CSV with patient IDS and select a destination")

    # Submit button
    st.divider()

    if st.button("🏃 Send", disabled=not can_submit, type="primary"):
        data = {
            'patients_to_export': patients_to_export,
            'dicom_export': dicom_export, 'dicom_destinations': dicom_destinations,
            'proknow_upload': proknow_upload, 'proknow_collection': proknow_collection}

        submit_form(data)

    # Add sidebar with helpful info
    with st.sidebar:
        st.header("ℹ️ Information")

        st.info("""
        **How to use:**

        """)
        
        st.success("Logs will be saved to ./logs")


if __name__ == '__main__':
    main()