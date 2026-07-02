"""
Studies page — search for data available in the linked Orthanc instance.

Supports both a single search form and batch CSV upload.
Results include a downloadable CSV with study and series UIDs for use in the Export page.
"""
import io
import csv
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_date(d: str | None) -> str:
    if d and len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d or "—"


def fetch_study_detail(orthanc_id: str) -> dict | None:
    try:
        res = requests.get(f"{BASE_URL}/studies/{orthanc_id}", timeout=15)
        return res.json() if res.ok else None
    except Exception:
        return None


def fetch_studies(params: dict) -> list[dict]:
    """Fetch summary list then enrich each with full series detail."""
    try:
        res = requests.get(f"{BASE_URL}/studies", params=params, timeout=30)
        res.raise_for_status()
        summaries = res.json().get("studies", [])
    except Exception as exc:
        st.error(f"Failed to reach gateway: {exc}")
        return []

    detailed = []
    for s in summaries:
        detail = fetch_study_detail(s["orthanc_id"])
        detailed.append(detail if detail else s)
    return detailed


def build_download_csv(patient_rows: list[dict], pacs_status: dict | None = None) -> str:
    """Return a CSV string (one row per series) suitable for upload to the Export page."""
    out = io.StringIO()
    w   = csv.writer(out)
    include_pacs = pacs_status is not None
    headers = [
        "patient_id", "patient_name", "study_date", "study_description",
        "study_instance_uid", "modality", "series_description",
        "series_date", "instance_count", "series_instance_uid",
    ]
    if include_pacs:
        headers.append("on_pacs")
    w.writerow(headers)
    for patient in patient_rows:
        for study in patient.get("studies", []):
            for series in study.get("series", []):
                uid = series.get("series_instance_uid", "")
                row = [
                    study.get("patient_id", ""),
                    study.get("patient_name", ""),
                    fmt_date(study.get("study_date")),
                    study.get("study_description", ""),
                    study.get("study_instance_uid", ""),
                    series.get("modality", ""),
                    series.get("series_description", ""),
                    fmt_date(series.get("series_date")),
                    series.get("instance_count", ""),
                    uid,
                ]
                if include_pacs:
                    val = pacs_status.get(uid)
                    row.append("" if val is None else ("true" if val else "false"))
                w.writerow(row)
    return out.getvalue()


def display_study(study: dict, pacs_status: dict | None = None):
    """Render a single study as an expander with a series table."""
    pid   = study.get("patient_id") or "—"
    date  = fmt_date(study.get("study_date"))
    desc  = study.get("study_description") or "—"
    label = f"**{pid}** · {date} · {desc}"

    with st.expander(label):
        st.caption(
            f"Study UID: `{study.get('study_instance_uid') or '—'}` · "
            f"Patient name: {study.get('patient_name') or '—'}"
        )
        series_list = study.get("series", [])
        if series_list:
            rows = []
            for s in series_list:
                uid = s.get("series_instance_uid") or ""
                if pacs_status is not None:
                    val = pacs_status.get(uid)
                    pacs_col = "✅ Yes" if val is True else ("❌ No" if val is False else "❓")
                else:
                    pacs_col = None

                row = {
                    "Modality":    s.get("modality") or "—",
                    "Description": s.get("series_description") or "—",
                    "Date":        fmt_date(s.get("series_date")),
                    "Instances":   s.get("instance_count") or "—",
                    "Series UID":  uid or "—",
                }
                if pacs_col is not None:
                    row["On PACS"] = pacs_col
                rows.append(row)
            st.table(rows)
        else:
            st.write("No series detail available.")


def _collect_series_uids(patient_rows: list[dict]) -> list[str]:
    """Collect all non-empty series UIDs from the result set."""
    uids = []
    seen = set()
    for patient in patient_rows:
        for study in patient.get("studies", []):
            for s in study.get("series", []):
                uid = s.get("series_instance_uid") or ""
                if uid and uid not in seen:
                    uids.append(uid)
                    seen.add(uid)
    return uids


def show_results(patient_rows: list[dict], show_patient_stats: bool = False):
    """Render summary metrics, action buttons, and per-study expanders."""
    total_studies  = sum(len(p["studies"]) for p in patient_rows)
    patients_found = sum(1 for p in patient_rows if p["studies"])

    if show_patient_stats:
        c1, c2, c3 = st.columns(3)
        c1.metric("Patients searched",      len(patient_rows))
        c2.metric("Patients with studies",  patients_found)
        c3.metric("Total studies",          total_studies)
    else:
        st.caption(f"{total_studies} stud{'y' if total_studies == 1 else 'ies'} found")

    if total_studies == 0:
        st.info("No studies matched.")
        return

    pacs_status = st.session_state.get("pacs_status")

    # Action buttons row
    btn_col1, btn_col2 = st.columns([1, 1])
    with btn_col1:
        csv_data = build_download_csv(patient_rows, pacs_status=pacs_status)
        st.download_button(
            "⬇ Download study list (CSV)",
            data=csv_data,
            file_name="studies.csv",
            mime="text/csv",
            help="One row per series. Includes study and series UIDs. Can be filtered and re-uploaded on the Export page.",
        )
    with btn_col2:
        if st.button("🔍 Check PACS", help="Query the remote PACS to see which series are already present there."):
            series_uids = _collect_series_uids(patient_rows)
            if not series_uids:
                st.warning("No series UIDs available to check.")
            else:
                with st.spinner(f"Querying PACS for {len(series_uids)} series…"):
                    try:
                        res = requests.post(
                            f"{BASE_URL}/pacs/query_series",
                            json={"series_uids": series_uids},
                            timeout=120,
                        )
                        if res.ok:
                            data = res.json()
                            st.session_state["pacs_status"] = data.get("results", {})
                            pacs_info = data.get("pacs", {})
                            on_pacs = sum(1 for v in data["results"].values() if v is True)
                            st.success(
                                f"PACS check complete ({pacs_info.get('ae_title', '?')} @ "
                                f"{pacs_info.get('host', '?')}:{pacs_info.get('port', '?')}). "
                                f"{on_pacs} / {len(series_uids)} series found on PACS."
                            )
                        elif res.status_code == 503:
                            st.error("PACS not configured on the Hermes server. Set PACS_AE_TITLE and PACS_HOST in Hermes .env.")
                        else:
                            st.error(f"PACS query failed ({res.status_code}): {res.text}")
                    except Exception as exc:
                        st.error(f"Could not reach gateway: {exc}")

    for patient in patient_rows:
        if not patient["studies"]:
            if show_patient_stats:
                st.markdown(f"*{patient['patient_id']} — no studies found*")
            continue
        for study in patient["studies"]:
            display_study(study, pacs_status=pacs_status)


# ── Mode selection ────────────────────────────────────────────────────────────

mode = st.radio("Mode", ["Search form", "Batch CSV"], horizontal=True,
                help="Search form: filter by patient ID / date / modality. Batch CSV: search a list of patient IDs at once.")

# ── Search form ───────────────────────────────────────────────────────────────

if mode == "Search form":
    with st.form("search"):
        col1, col2, col3, col4 = st.columns([2, 1.5, 1.5, 1.5])
        patient_id = col1.text_input("Patient ID", placeholder="e.g. 1234567")
        date_from  = col2.date_input("Study date from", value=None)
        date_to    = col3.date_input("To",              value=None)
        modality   = col4.selectbox("Modality", ["", "CT", "RTPLAN", "RTSTRUCT", "RTDOSE", "MR"])
        submitted  = st.form_submit_button("🔍 Search", type="primary")

    if submitted:
        params: dict = {}
        if patient_id.strip():
            params["patient_id"] = patient_id.strip()
        if date_from or date_to:
            f = date_from.strftime("%Y%m%d") if date_from else ""
            t = date_to.strftime("%Y%m%d")   if date_to   else ""
            params["study_date"] = f"{f}-{t}" if (f and t) else f or t
        if modality:
            params["modality"] = modality

        # Clear stale PACS results when re-searching
        st.session_state.pop("pacs_status", None)

        with st.spinner("Searching…"):
            studies = fetch_studies(params)

        # Group by patient_id for uniform display
        by_patient: dict[str, list] = {}
        for s in studies:
            pid = s.get("patient_id") or "unknown"
            by_patient.setdefault(pid, []).append(s)

        st.session_state["results"]      = [{"patient_id": pid, "studies": ss} for pid, ss in by_patient.items()]
        st.session_state["results_mode"] = "search"

# ── Batch CSV ─────────────────────────────────────────────────────────────────

elif mode == "Batch CSV":
    uploaded = st.file_uploader(
        "Patient CSV",
        type=["csv"],
        help="One column, header `patient_id`. Lines starting with `#` are skipped.",
    )

    if st.button("🔍 Search all", type="primary", disabled=not uploaded):
        text    = uploaded.getvalue().decode("utf-8", errors="replace")
        reader  = csv.DictReader(text.splitlines())
        all_ids = [
            row["patient_id"].strip()
            for row in reader
            if row.get("patient_id") and not row["patient_id"].strip().startswith("#")
        ]
        unique_ids = list(dict.fromkeys(all_ids))  # preserve order, deduplicate

        # Clear stale PACS results when re-searching
        st.session_state.pop("pacs_status", None)

        results = []
        prog     = st.progress(0.0, text="Searching…")
        for i, pid in enumerate(unique_ids):
            prog.progress((i + 0.5) / len(unique_ids), text=f"Searching `{pid}`…")
            studies = fetch_studies({"patient_id": pid})
            results.append({"patient_id": pid, "studies": studies})

        prog.empty()
        st.session_state["results"]      = results
        st.session_state["results_mode"] = "batch"

# ── Results ───────────────────────────────────────────────────────────────────

if "results" in st.session_state:
    st.divider()
    show_results(
        st.session_state["results"],
        show_patient_stats=(st.session_state.get("results_mode") == "batch"),
    )
