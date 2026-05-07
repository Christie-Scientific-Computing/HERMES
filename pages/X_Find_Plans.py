"""
Streamlit page for finding patients. Will search MOSAIQ, Pinnacle, Raystation and ProKnow.

Author: Donal McSweeney
Date: 05/05/2025
Version: 0.01

Copyright (C) 2026 The Christie NHS
Foundation Trust
"""

import sys
from pathlib import Path
import logging
import streamlit as st
import threading
import time
import logging
from src.find_plans import main as run_main

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


def submit_form(uploaded_file):
    stop_event = threading.Event()
    st.session_state["stop_event"] = stop_event
    st.session_state["result"] = {}
    
    uploads_dir = Path("./tmp")
    uploads_dir.mkdir(exist_ok=True)
    file_path = uploads_dir / uploaded_file.name
    with open(file_path, 'wb') as f:
        f.write(uploaded_file.getvalue())

    def run():
        try:
            run_main({"patients-file": file_path}, stop_event=stop_event)
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
        page_title="Find Plans",
        page_icon="🔍",
        layout="wide"
    )
    st.title("Find Plans")
    st.markdown("""
        Query archives and treatment planning systems to locate patient plans.
        
        Upload a CSV of patient IDs and click Run. Once complete, head to the Import page to retrieve them.
    """)
    
    if 'submitted' not in st.session_state:
        st.session_state.submitted = False

    #with st.form("Some form", enter_to_submit=False):
    st.subheader("Search options")
    patients_to_find = st.file_uploader(
        "Patients to find",
        type=['csv'],  
        help="Upload a file containing christie IDs to export"
    )

    can_submit = patients_to_find is not None

    if not can_submit:
        st.warning("⚠️ Please upload a file with patient IDs before running.")

    # Submit button
    st.divider()

    if st.button("🏃 Run", disabled=not can_submit, type="primary"):
        submit_form(patients_to_find)

    show_progress_and_stop()

    

    # Add footer with helpful info
    with st.sidebar:
        st.header("ℹ️ Information")

        st.info("""
        **How to use:**
        1. Upload a CSV with IDs (header: patient_id). 
        2. Click 'Run'
        """)
        
        st.success("Logs will be saved to ./logs")


if __name__ == '__main__':
    main()