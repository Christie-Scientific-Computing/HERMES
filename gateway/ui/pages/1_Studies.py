"""
Studies page — search for data available in the linked Orthanc instance.

Supports both a single search form and batch CSV upload.
Results include a downloadable CSV with study and series UIDs for use in the Export page.
"""
import io
import csv
import os
import sys
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

GATEWAY_URI  = os.getenv("GATEWAY_URI", "localhost")
GATEWAY_PORT = os.getenv("GATEWAY_PORT", "8001")
BASE_URL     = f"http://{GATEWAY_URI}:{GATEWAY_PORT}"

# Anonymisation module lives one directory up from ui/pages/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import anon

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


def _collect_modalities(patient_rows: list[dict]) -> list[str]:
    """Return sorted list of distinct modality strings present in the result set."""
    seen = set()
    for patient in patient_rows:
        for study in patient.get("studies", []):
            for s in study.get("series", []):
                m = s.get("modality") or ""
                if m:
                    seen.add(m)
    return sorted(seen)


def _anonymise_study_list(studies: list[dict], real_to_anon: dict[str, str]) -> list[dict]:
    """
    Replace patient_id with the anonymised ID and blank patient_name.
    real_to_anon may be a partial mapping; missing IDs fall back to "[unknown]".
    """
    result = []
    for s in studies:
        real_pid = s.get("patient_id") or ""
        result.append({**s, "patient_id": real_to_anon.get(real_pid, "[unknown]"), "patient_name": ""})
    return result


def apply_filters(
    patient_rows: list[dict],
    modalities: list[str],
    pacs_filter: str,
    date_from,
    date_to,
    description_kw: str,
    min_instances: int,
    pacs_status: dict | None,
) -> list[dict]:
    """
    Return a filtered copy of patient_rows without mutating the originals.

    Filtering is at the series level: a study is included only if at least one
    series passes all active filters, and only passing series are shown in that study.
    """
    date_from_str = date_from.strftime("%Y%m%d") if date_from else None
    date_to_str   = date_to.strftime("%Y%m%d")   if date_to   else None
    kw = (description_kw or "").strip().lower()

    def series_passes(s: dict, study_date: str | None) -> bool:
        # Modality
        if modalities and s.get("modality") not in modalities:
            return False
        # PACS status — None (unknown) is excluded from both On/Not on PACS views
        if pacs_filter != "All" and pacs_status is not None:
            uid = s.get("series_instance_uid") or ""
            val = pacs_status.get(uid)
            if val is None:
                return False
            if pacs_filter == "On PACS" and not val:
                return False
            if pacs_filter == "Not on PACS" and val:
                return False
        # Date — prefer series_date, fall back to study_date; missing date passes
        raw_date = s.get("series_date") or study_date or ""
        if date_from_str and raw_date and raw_date < date_from_str:
            return False
        if date_to_str and raw_date and raw_date > date_to_str:
            return False
        # Description keyword
        if kw and kw not in (s.get("series_description") or "").lower():
            return False
        # Min instance count
        if min_instances > 0 and (s.get("instance_count") or 0) < min_instances:
            return False
        return True

    result = []
    for patient in patient_rows:
        filtered_studies = []
        for study in patient.get("studies", []):
            study_date = study.get("study_date") or ""
            passing = [s for s in study.get("series", []) if series_passes(s, study_date)]
            if passing:
                filtered_studies.append({**study, "series": passing})
        if filtered_studies:
            result.append({**patient, "studies": filtered_studies})
    return result


def render_filter_sidebar(all_patient_rows: list[dict]) -> dict:
    """
    Render the sidebar filter panel and return the active filter values as a dict.
    Options are derived from the full (unfiltered) result set so filters don't cascade.
    """
    pacs_available   = st.session_state.get("pacs_status") is not None
    modality_options = _collect_modalities(all_patient_rows)

    # Read current values from session state to compute active state before rendering
    cur_modalities    = st.session_state.get("filter_modalities", [])
    cur_pacs          = st.session_state.get("filter_pacs", "All")
    cur_date_from     = st.session_state.get("filter_date_from")
    cur_date_to       = st.session_state.get("filter_date_to")
    cur_description   = st.session_state.get("filter_description", "")
    cur_min_instances = st.session_state.get("filter_min_instances", 0)

    any_active = bool(
        cur_modalities
        or (pacs_available and cur_pacs != "All")
        or cur_date_from
        or cur_date_to
        or (cur_description or "").strip()
        or (cur_min_instances or 0) > 0
    )

    with st.sidebar:
        with st.expander("Filter results", expanded=any_active):
            st.multiselect(
                "Modality",
                options=modality_options,
                key="filter_modalities",
                help="Show only studies that contain at least one series of the selected modalities.",
            )
            st.text_input(
                "Series description contains",
                key="filter_description",
                placeholder="e.g. Planning CT",
            )
            st.date_input("Study/series date from", value=None, key="filter_date_from")
            st.date_input("Study/series date to",   value=None, key="filter_date_to")
            st.number_input(
                "Min instance count",
                min_value=0,
                step=1,
                key="filter_min_instances",
                help="Exclude series with fewer instances (e.g. set to 10 to hide scouts/localisers).",
            )
            if pacs_available:
                st.radio(
                    "PACS presence",
                    ["All", "On PACS", "Not on PACS"],
                    key="filter_pacs",
                )
            else:
                st.caption("Run **Check PACS** to enable the PACS presence filter.")

            if any_active:
                if st.button("Clear filters", use_container_width=True):
                    for k in [
                        "filter_modalities", "filter_pacs", "filter_date_from",
                        "filter_date_to", "filter_description", "filter_min_instances",
                    ]:
                        st.session_state.pop(k, None)
                    st.rerun()

    return {
        "modalities":     st.session_state.get("filter_modalities", []),
        "pacs_filter":    st.session_state.get("filter_pacs", "All"),
        "date_from":      st.session_state.get("filter_date_from"),
        "date_to":        st.session_state.get("filter_date_to"),
        "description_kw": st.session_state.get("filter_description", ""),
        "min_instances":  st.session_state.get("filter_min_instances", 0),
    }


def show_results(patient_rows: list[dict], show_patient_stats: bool = False):
    """Render summary metrics, action buttons, and per-study expanders."""
    pacs_status   = st.session_state.get("pacs_status")
    filters       = render_filter_sidebar(patient_rows)
    filtered_rows = apply_filters(patient_rows, **filters, pacs_status=pacs_status)

    any_filters_active = bool(
        filters["modalities"]
        or (pacs_status is not None and filters["pacs_filter"] != "All")
        or filters["date_from"]
        or filters["date_to"]
        or (filters["description_kw"] or "").strip()
        or (filters["min_instances"] or 0) > 0
    )

    total_studies_raw = sum(len(p["studies"]) for p in patient_rows)
    total_studies     = sum(len(p["studies"]) for p in filtered_rows)
    patients_found    = sum(1 for p in filtered_rows if p["studies"])

    if show_patient_stats:
        c1, c2, c3 = st.columns(3)
        c1.metric("Patients searched",     len(patient_rows))   # always raw count
        c2.metric("Patients with studies", patients_found)
        c3.metric("Total studies",         total_studies)
    else:
        st.caption(f"{total_studies} stud{'y' if total_studies == 1 else 'ies'} found")

    if any_filters_active and total_studies < total_studies_raw:
        st.caption(f"Showing {total_studies} of {total_studies_raw} studies (filtered)")

    if total_studies == 0:
        if any_filters_active:
            st.info("No studies match the active filters. Try adjusting or clearing them in the sidebar.")
        else:
            st.info("No studies matched.")
        return

    # Action buttons — CSV download reflects the filtered view; PACS check covers all
    btn_col1, btn_col2 = st.columns([1, 1])
    with btn_col1:
        csv_data = build_download_csv(filtered_rows, pacs_status=pacs_status)
        st.download_button(
            "⬇ Download study list (CSV)",
            data=csv_data,
            file_name="studies.csv",
            mime="text/csv",
            help="One row per series. Reflects the current filter. Includes study and series UIDs for re-upload on the Export page.",
        )
    with btn_col2:
        if st.button("🔍 Check PACS", help="Query the remote PACS to see which series are already present there."):
            series_uids = _collect_series_uids(patient_rows)   # full unfiltered set
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

    for patient in filtered_rows:
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
        anon_to_real: dict[str, str] = {}

        if patient_id.strip():
            anon_pid = patient_id.strip()
            if anon.is_configured():
                try:
                    anon_to_real = anon.lookup_real_ids([anon_pid])
                    params["patient_id"] = anon_to_real[anon_pid]
                except anon.AnonLookupError as exc:
                    st.error(str(exc))
                    st.stop()
                except Exception as exc:
                    st.error(f"Anonymisation DB error: {exc}")
                    st.stop()
            else:
                params["patient_id"] = anon_pid

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

        # Anonymise: convert all real patient IDs in results back to anon IDs
        if anon.is_configured() and studies:
            real_pids = [s.get("patient_id") for s in studies if s.get("patient_id")]
            # Use the mapping we already have; look up any extras (e.g. date-only search)
            known_real = set(anon_to_real.values())
            extra_real = [p for p in real_pids if p not in known_real]
            real_to_anon = {v: k for k, v in anon_to_real.items()}
            if extra_real:
                try:
                    real_to_anon.update(anon.lookup_anon_ids(extra_real))
                except Exception as exc:
                    st.warning(f"Could not reverse-lookup some patient IDs: {exc}")
            studies = _anonymise_study_list(studies, real_to_anon)

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
        unique_anon_ids = list(dict.fromkeys(all_ids))  # preserve order, deduplicate

        # Anonymisation: convert all anon IDs → real IDs in one batch query
        if anon.is_configured():
            try:
                anon_to_real = anon.lookup_real_ids(unique_anon_ids)
            except anon.AnonLookupError as exc:
                st.error(str(exc))
                st.stop()
            except Exception as exc:
                st.error(f"Anonymisation DB error: {exc}")
                st.stop()
            real_to_anon = {v: k for k, v in anon_to_real.items()}
        else:
            anon_to_real = {pid: pid for pid in unique_anon_ids}
            real_to_anon = {}

        # Clear stale PACS results when re-searching
        st.session_state.pop("pacs_status", None)

        results = []
        prog = st.progress(0.0, text="Searching…")
        for i, anon_pid in enumerate(unique_anon_ids):
            prog.progress((i + 0.5) / len(unique_anon_ids), text=f"Searching `{anon_pid}`…")
            real_pid = anon_to_real.get(anon_pid, anon_pid)
            studies  = fetch_studies({"patient_id": real_pid})
            if anon.is_configured():
                studies = _anonymise_study_list(studies, real_to_anon)
            results.append({"patient_id": anon_pid, "studies": studies})

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
