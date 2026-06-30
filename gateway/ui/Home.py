"""
HERMES Gateway — Streamlit frontend entry point.

Run from the gateway/ directory:
    streamlit run ui/Home.py
"""
import streamlit as st

st.set_page_config(page_title="HERMES Gateway", page_icon="🪽", layout="wide")

st.write("# 🪽 HERMES Gateway")
st.markdown("""
User-facing interface to the HERMES radiotherapy data pipeline.

### Pages
1. **Studies** — search what's currently available in Orthanc
2. **Import** — pull RT planning data from Mosaiq / Pinnacle into Orthanc
3. **Export** — push data from Orthanc to a DICOM destination or ProKnow
4. **Results** — inspect job summaries and per-patient event timelines
""")
