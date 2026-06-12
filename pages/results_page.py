"""
Streamlit Results page — alternative to pages/4_Results.py
Shows job summary and per-patient timelines using the /results API.
"""
import os
import json
import requests
import streamlit as st

BACKEND_URI = os.getenv('BACKEND_URI', 'localhost')
BACKEND_PORT = os.getenv('BACKEND_PORT', '8000')
BASE_URL = f"http://{BACKEND_URI}:{BACKEND_PORT}"

st.set_page_config(page_title="Hermes Results", page_icon="🪽", layout="wide")

st.title("Results")
st.markdown("""
Use a job id to view job-level summary and per-patient timelines. Click a patient to see the chronological events and why they failed.
""")

mode = st.radio('Mode', ['By job id', 'From CSV upload'], index=0)

patients_list = None

if mode == 'By job id':
    job_id = st.text_input("Job ID", value="")

    if st.button("Load job"):
        if not job_id:
            st.error("Please provide a job id")
        else:
            try:
                res = requests.get(f"{BASE_URL}/results/job/{job_id}")
                if res.status_code != 200:
                    st.error(f"Error fetching job summary: {res.text}")
                else:
                    st.session_state['job_summary'] = res.json()

                res = requests.get(f"{BASE_URL}/results/job/{job_id}/patients")
                if res.status_code != 200:
                    st.error(f"Error fetching patients: {res.text}")
                else:
                    st.session_state['patients'] = res.json().get('patients', [])
            except Exception as e:
                st.error(f"Failed to contact backend: {e}")

    if 'patients' in st.session_state:
        patients_list = st.session_state['patients']

else:
    uploaded_file = st.file_uploader('Upload CSV with patient IDs (header: patient_id)', type=['csv'])
    if uploaded_file is not None:
        import csv
        try:
            text = uploaded_file.getvalue().decode('utf-8')
        except Exception:
            text = uploaded_file.getvalue().decode('latin-1')
        reader = csv.DictReader(text.splitlines())
        ids = []
        for row in reader:
            if 'patient_id' in row and row['patient_id']:
                val = row['patient_id'].strip()
                if val and not val.startswith('#'):
                    ids.append(val)
        patients_list = ids
        st.session_state['patients_from_csv'] = ids

if patients_list:
    st.session_state['patients'] = patients_list
    st.success(f"Loaded {len(patients_list)} patients")


if 'job_summary' in st.session_state:
    st.subheader("Job summary")
    st.json(st.session_state['job_summary'])

if 'patients' in st.session_state:
    st.subheader("Patients")
    patients = st.session_state['patients']
    st.write(f"{len(patients)} patients found")
    patient_filter = st.checkbox("Show failed only", value=True)

    for mrn in patients:
        try:
            # If job mode and job_id present, prefer job-scoped endpoint
            if mode == 'By job id' and 'job_id' in locals() and job_id:
                res = requests.get(f"{BASE_URL}/results/patient/{job_id}/{mrn}")
            else:
                # Global patient timeline across jobs
                res = requests.get(f"{BASE_URL}/results/patient/{mrn}/all")

            if res.status_code != 200:
                st.markdown(f"**{mrn}** — Error fetching timeline: {res.text}")
                continue
            data = res.json()
            events = data.get('events', [])
            failed = any(e.get('event_type') == 'failure' for e in events)
            if patient_filter and not failed:
                continue

            with st.expander(f"{mrn} — {'FAILED' if failed else 'OK'} ({len(events)} events)"):
                if not events:
                    st.write("No events recorded")
                    continue
                for ev in events:
                    ts = ev.get('ts')
                    stage = ev.get('stage')
                    et = ev.get('event_type')
                    err = ev.get('error_message')
                    details = ev.get('details')
                    if et == 'failure':
                        st.markdown(f"- **{ts}** — {stage} — **{et.upper()}** — {err}")
                        if details:
                            try:
                                parsed = json.loads(details)
                                st.code(json.dumps(parsed, indent=2))
                            except Exception:
                                st.write(details)
                    else:
                        st.markdown(f"- {ts} — {stage} — {et}")
        except Exception as e:
            st.write(f"Failed to fetch timeline for {mrn}: {e}")


st.sidebar.header("ℹ️ Information")
st.sidebar.info("Use the job id generated during import/export runs to inspect results.")
