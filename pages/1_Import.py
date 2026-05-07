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
from pathlib import Path
import logging
import streamlit as st
import threading
import time
import logging
import requests
#from backend.src.find_plans import main as run_main
from dotenv import load_dotenv

load_dotenv()
ORTHANC_URL = os.getenv('ORTHANC_URL')
BACKEND_URI = os.getenv('BACKEND_URI')
BACKEND_PORT = os.getenv('BACKEND_PORT')


logger = logging.getLogger(__name__)

@st.fragment(run_every=0.5)  # polls every 500ms automatically
def show_progress_and_stop():
    thread = st.session_state.get("thread")
    
    if thread and thread.is_alive():
        st.spinner("🔄 Finding patients...")
        if st.button("⛔ Stop", type="secondary"):
            st.session_state["stop_event"].set()
    
    elif st.session_state.get("stop_event", threading.Event()).is_set():
        st.warning("⛔ Cancelled.")
    
    elif "result" in st.session_state:
        result = st.session_state["result"]
        if result.get("error"):
            st.error(f"**Error:** {str(result['error'])}")
            with st.expander("Show full error traceback"):
                st.exception(result["error"])
        else:
            st.success("✅ Completed successfully!")


def submit_form(uploaded_file, import_level):
    stop_event = threading.Event()
    st.session_state["stop_event"] = stop_event
    st.session_state["result"] = {}
    logger.info("Writing file to tmp")
    uploads_dir = Path("./tmp")
    uploads_dir.mkdir(exist_ok=True)
    file_path = uploads_dir / uploaded_file.name
    with open(file_path, 'wb') as f:
        f.write(uploaded_file.getvalue())
    def run():
        logger.info("Sending request")
        payload = {'path_to_csv': str(file_path), 'import_level': import_level}
        try:
            res = requests.post(f"http://{BACKEND_URI}:{BACKEND_PORT}/import/batch_import", json=payload)
        except Exception as e:
            st.session_state["result"] = {"error": e}
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    st.session_state["thread"] = thread

def main():
    """
    Streamlit frontend for interacting with app.
    Start by adding params from config.toml.

    TODO: 
        - Show progress
        - Report errors/plans that need reviewing
    """
    st.set_page_config(
        page_title="Hermes",
        page_icon="🪽",
        layout="wide"
    )
    st.title("Import")
    st.markdown(f"""
        Fetch RT planning data from archives and treatment planning systems (Raystation, Pinnacle, Elements). 
        
        Upload a CSV of patient IDs and click Run. Additional parameters can be provided for filtering plans (**see docs**).  

        Data will be sent to {ORTHANC_URL} 
         
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