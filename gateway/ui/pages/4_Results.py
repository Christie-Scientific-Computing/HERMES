"""
Results page — inspect job summaries and per-patient event timelines.
"""
import os
import csv
import json
import requests
import streamlit as st
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

GATEWAY_URI  = os.getenv("GATEWAY_URI", "localhost")
GATEWAY_PORT = os.getenv("GATEWAY_PORT", "8001")
BASE_URL     = f"http://{GATEWAY_URI}:{GATEWAY_PORT}"

st.set_page_config(page_title="Results — HERMES", page_icon="🪽", layout="wide")
st.title("Results")
st.markdown("Use a job ID to inspect a specific run, or upload a CSV to view timelines for a list of patients.")

mode = st.radio("Mode", ["By job ID", "By patient CSV"], horizontal=True)

patient_ids = None

if mode == "By job ID":
    job_id = st.text_input("Job ID")

    if st.button("Load", type="primary"):
        if not job_id.strip():
            st.error("Enter a job ID.")
        else:
            job_id = job_id.strip()
            try:
                r_sum = requests.get(f"{BASE_URL}/results/job/{job_id}", timeout=10)
                r_pts = requests.get(f"{BASE_URL}/results/job/{job_id}/patients", timeout=10)
                if not r_sum.ok:
                    st.error(f"Job not found ({r_sum.status_code}).")
                    st.stop()
                st.session_state["job_summary"] = r_sum.json()
                st.session_state["patient_ids"] = r_pts.json().get("patients", []) if r_pts.ok else []
                st.session_state["mode"]        = "job"
                st.session_state["job_id"]      = job_id
            except Exception as exc:
                st.error(f"Failed to reach gateway: {exc}")

else:
    uploaded = st.file_uploader("Patient CSV", type=["csv"], help="Must have a patient_id column")
    if uploaded:
        text   = uploaded.getvalue().decode("utf-8", errors="replace")
        reader = csv.DictReader(text.splitlines())
        ids    = [
            row["patient_id"].strip()
            for row in reader
            if row.get("patient_id") and not row["patient_id"].strip().startswith("#")
        ]
        if ids:
            st.session_state["patient_ids"] = ids
            st.session_state["mode"]        = "csv"
            st.session_state.pop("job_summary", None)
            st.session_state.pop("job_id", None)


# ── Render ────────────────────────────────────────────────────────────────────

if "job_summary" in st.session_state:
    st.subheader("Job summary")
    summary = st.session_state["job_summary"].get("summary", [])
    success_total = sum(r["cnt"] for r in summary if r.get("event_type") == "success")
    failure_total = sum(r["cnt"] for r in summary if r.get("event_type") == "failure")
    c1, c2 = st.columns(2)
    c1.metric("Succeeded", success_total)
    c2.metric("Failed",    failure_total)

if "patient_ids" in st.session_state:
    ids      = st.session_state["patient_ids"]
    job_id   = st.session_state.get("job_id")
    use_job  = st.session_state.get("mode") == "job"

    st.subheader(f"{len(ids)} patient{'s' if len(ids) != 1 else ''}")
    show_failed_only = st.checkbox("Show failed only", value=False)

    failure_counts: dict[str, int] = defaultdict(int)

    for mrn in ids:
        try:
            url = (
                f"{BASE_URL}/results/patient/{job_id}/{mrn}"
                if use_job and job_id
                else f"{BASE_URL}/results/patient/timeline/{mrn}/all"
            )
            res    = requests.get(url, timeout=10)
            events = res.json().get("events", []) if res.ok else []
        except Exception:
            events = []

        failed = any(e.get("event_type") == "failure" for e in events)
        if show_failed_only and not failed:
            continue

        for e in events:
            if e.get("event_type") == "failure":
                failure_counts[e.get("stage") or "unknown"] += 1
                break

        label = f"{mrn} — {'❌ FAILED' if failed else '✅ OK'} ({len(events)} events)"
        with st.expander(label):
            if not events:
                st.write("No events recorded.")
                continue
            for ev in events:
                ts      = (ev.get("ts") or "").replace("T", " ")[:19]
                stage   = ev.get("stage")   or "—"
                et      = ev.get("event_type") or "—"
                err     = ev.get("error_message")
                details = ev.get("details")

                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except Exception:
                        pass

                if et == "failure":
                    st.markdown(f"- **{ts}** · {stage} · **{et.upper()}** · {err or ''}")
                else:
                    st.markdown(f"- {ts} · {stage} · {et}")

                if details:
                    st.json(details)

    if failure_counts:
        st.subheader("Failures by stage")
        import pandas as pd
        df = (
            pd.DataFrame(list(failure_counts.items()), columns=["stage", "count"])
            .set_index("stage")
        )
        st.bar_chart(df)
