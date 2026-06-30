"""
Studies page — search for data available in the linked Orthanc instance.
"""
import os
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

GATEWAY_URI  = os.getenv("GATEWAY_URI", "localhost")
GATEWAY_PORT = os.getenv("GATEWAY_PORT", "8001")
BASE_URL     = f"http://{GATEWAY_URI}:{GATEWAY_PORT}"

st.set_page_config(page_title="Studies — HERMES", page_icon="🪽", layout="wide")
st.title("Studies")
st.markdown("Search for data available in Orthanc.")

with st.form("search"):
    col1, col2, col3, col4 = st.columns([2, 1.5, 1.5, 1.5])
    patient_id   = col1.text_input("Patient ID", placeholder="e.g. 1234567")
    date_from    = col2.date_input("Study date from", value=None)
    date_to      = col3.date_input("To", value=None)
    modality     = col4.selectbox("Modality", ["", "CT", "RTPLAN", "RTSTRUCT", "RTDOSE", "MR"])
    submitted    = st.form_submit_button("🔍 Search", type="primary")

if submitted:
    params = {}
    if patient_id.strip():
        params["patient_id"] = patient_id.strip()
    if date_from or date_to:
        f = date_from.strftime("%Y%m%d") if date_from else ""
        t = date_to.strftime("%Y%m%d")   if date_to   else ""
        params["study_date"] = f"{f}-{t}" if (f and t) else f or t
    if modality:
        params["modality"] = modality

    try:
        res  = requests.get(f"{BASE_URL}/studies", params=params, timeout=30)
        res.raise_for_status()
        data = res.json()
    except Exception as exc:
        st.error(f"Failed to reach gateway: {exc}")
        st.stop()

    studies = data.get("studies", [])
    st.caption(f"{data.get('total', 0)} stud{'y' if data.get('total') == 1 else 'ies'} found")

    if not studies:
        st.info("No studies matched.")
    else:
        for s in studies:
            date_str = s.get("study_date") or ""
            if len(date_str) == 8:
                date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

            header = f"**{s.get('patient_id', '—')}** · {date_str} · {s.get('study_description') or '—'} · {s.get('series_count', '?')} series"
            with st.expander(header):
                detail_res = requests.get(f"{BASE_URL}/studies/{s['orthanc_id']}", timeout=15)
                if detail_res.ok:
                    detail = detail_res.json()
                    st.caption(f"Patient name: {detail.get('patient_name') or '—'} · UID: {detail.get('study_instance_uid') or '—'}")
                    series_rows = []
                    for sr in detail.get("series", []):
                        sd = sr.get("series_date") or ""
                        if len(sd) == 8:
                            sd = f"{sd[:4]}-{sd[4:6]}-{sd[6:]}"
                        series_rows.append({
                            "Modality":    sr.get("modality") or "—",
                            "Description": sr.get("series_description") or "—",
                            "Date":        sd or "—",
                            "Instances":   sr.get("instance_count") or "—",
                        })
                    if series_rows:
                        st.table(series_rows)
                else:
                    st.warning("Could not load series detail.")
