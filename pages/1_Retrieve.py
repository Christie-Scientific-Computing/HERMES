"""

Streamlit page for importing patients into a centralised Orthanc.
(This is the most involved step. Will need to async call imports & track errors).
Also most time-consuming process.


Should PinnacleExport run as a service? Or call directly from here?

Author: Donal McSweeney
Date: 05/05/2025
Version: 0.01

Copyright (C) 2026 The Christie NHS
Foundation Trust
"""
import os
import sys
import json
from pathlib import Path
import logging
import streamlit as st
import threading
import time
import logging
import requests
import sseclient
import uuid

#from backend.src.find_plans import main as run_main
from dotenv import load_dotenv

load_dotenv()
ORTHANC_URL = os.getenv('ORTHANC_URL')
BACKEND_URI = os.getenv('BACKEND_URI')
BACKEND_PORT = os.getenv('BACKEND_PORT')


logger = logging.getLogger(__name__)

@st.fragment(run_every=0.5)
def show_progress(file_path: Path, import_level: str):
    if "job_id" not in st.session_state:
        st.session_state["job_id"] = str(uuid.uuid4())
        st.session_state["messages"] = []
        st.session_state["import_done"] = False

        job_id = st.session_state["job_id"]
        messages = st.session_state["messages"]

        def stream():
            url = f"http://{BACKEND_URI}:{BACKEND_PORT}/import/batch_import"
            payload = {"job_id": job_id, "path_to_csv": str(file_path), "import_level": import_level}
            with requests.post(url, json=payload, stream=True, timeout=(10, None)) as response:
                for line in response.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    messages.append(json.loads(raw))

        threading.Thread(target=stream, daemon=True).start()

    def stop_job():
        requests.post(f"http://{BACKEND_URI}:{BACKEND_PORT}/import/cancel/{st.session_state['job_id']}")

    messages = st.session_state.get("messages", [])
    terminal = next((m for m in messages if m.get("type") == "cancelled" or m.get("done")), None)

    if terminal and terminal.get("type") == "cancelled":
        initial_label, initial_state = "Cancelled by user", "error"
    elif terminal and terminal.get("done"):
        initial_label, initial_state = "All done!", "complete"
    else:
        initial_label, initial_state = "Importing patients...", "running"

    total_in_messages = next((m["total"] for m in messages if m.get("type") == "start"), 0)
    completed_in_messages = sum(1 for m in messages if m.get("type") == "success")

    if terminal and total_in_messages:
        initial_progress = 1.0 if terminal.get("done") else completed_in_messages / total_in_messages
        initial_text = "Done" if terminal.get("done") else f"Imported {completed_in_messages} / {total_in_messages}"
    elif total_in_messages:
        initial_progress = completed_in_messages / total_in_messages
        initial_text = f"Imported {completed_in_messages} / {total_in_messages}"
    else:
        initial_progress = 0
        initial_text = "Starting..."

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
            status.write(f"Importing {total} patients")

        elif message.get("type") == "progress":
            patient_progress[message['current']] = status.empty()
            patient_progress[message['current']].markdown(f"Importing `{message['current']}`")

        elif message.get("type") == "success":
            completed += 1
            patient_progress[message['mrn']].markdown(f"Finished importing `{message['mrn']}` in {message['execution_time']}s")

        elif message.get("type") == "error":
            errors.append(message)
            patient_progress[message['mrn']].markdown(f"Failed: `{message['mrn']}` — {message['error']}")

        elif message.get("type") == "cancelled":
            break

        elif message.get("done"):
            break


def submit_form(uploaded_file, import_level):
    uploads_dir = Path("./tmp")
    uploads_dir.mkdir(exist_ok=True)
    file_path = uploads_dir / uploaded_file.name
    with open(file_path, 'wb') as f:
        f.write(uploaded_file.getvalue())
    show_progress(file_path, import_level)



def main():
    """
    Streamlit frontend for interacting with app.

    TODO: 
        - Report errors/plans that need reviewing
    """
    st.set_page_config(
        page_title="Hermes",
        page_icon="🪽",
        layout="wide"
    )
    st.title("Retrieve")
    st.markdown(f"""
        Fetch RT planning data from archives and treatment planning systems (Raystation, Pinnacle, Elements). 
        
        Upload a CSV of patient IDs and click Run. Additional parameters can be provided for filtering plans (**see docs**).  

        Data will be sent to {ORTHANC_URL} 
         
        **TODO** 

            - Issue with duplicate RTSTRUCT and RTPLAN when importing from multiple sources.
            - Raystation query/retrieve
            - Proknow download

    """)
    
    if 'submitted' not in st.session_state:
        st.session_state.submitted = False

    #with st.form("Some form", enter_to_submit=False):
    
    patients_to_import = st.file_uploader(
        "Patients to import",
        type=['csv'],  
        help="Upload a file containing christie IDs to export"
    )
    can_submit = patients_to_import is not None

    st.subheader("Options")
    
    import_level = st.selectbox(
        "What do you want to import?",
        ("Everything", "Images only", "Planning data"),
        help="""
            Everything: All images and planning data.\n
            Images: CBCT & REG objects included but RTSTRUCT, RTPLAN and RTDOSE excluded.\n
            Planning data: Images that are not directly associated with an RTPLAN will not be imported.""",
        index=2
    )
    
    if not can_submit:
        st.warning("⚠️ Please upload a CSV with patient IDs before running.")

    # Submit button
    st.divider()

    if st.button("🏃 Run", disabled=not can_submit, type="primary"):
        submit_form(patients_to_import, import_level)

    #show_progress_and_stop()


    # Add sidebar with helpful info
    with st.sidebar:
        st.header("ℹ️ Information")

        st.info("""
        **How to use:**
        1. Upload a CSV with IDs (header: patient_id). 
        2. Specify what data you want (default: complete planning data only)
        3. Click 'Run'
        """)
        
        st.success("Logs will be saved to ./logs")


if __name__ == '__main__':
    main()