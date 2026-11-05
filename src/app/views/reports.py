"""Read-only access to current quality analyses for the three data modalities."""

import io
import json
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import pandas as pd
import streamlit as st

from src.app.ui import heading
from src.common.config import load_minio_settings, load_storage_config
from src.common.quality_reports import ReportUnavailable, list_revisions, load_bundle, load_text_bundle


@st.cache_data(ttl=30, show_spinner=False)
def load_report(modality, storage_json, revision=None):
    client = boto3.client("s3", **load_minio_settings(),
                          config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 0}))
    storage = json.loads(storage_json)
    return load_text_bundle(client, storage) if modality == "text" and revision is None else load_bundle(client, storage, modality, revision)


@st.cache_data(ttl=30, show_spinner=False)
def report_history(modality, storage_json):
    client = boto3.client("s3", **load_minio_settings(),
                          config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 0}))
    return list_revisions(client, json.loads(storage_json), modality)


def publication_label(timestamp):
    return datetime.fromisoformat(timestamp).astimezone(ZoneInfo("Europe/Madrid")).strftime("%Y-%m-%d %H:%M:%S %Z")


def revision_label(row):
    return publication_label(row["generated_at_utc"]) + " · " + row["revision"][:10] + (" · Latest" if row["is_latest"] else "")


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        for name, body in files.items():
            output.writestr(name, body)
    return buffer.getvalue()


def text_analysis(files):
    """Render old structured profiles with current English explanations as well."""
    from src.trusted.text_quality_analysis import analysis_markdown, interpretations
    report = json.loads(files["overview.json"])
    report["interpretation"] = [
        f"This report covers {report['capture_count']} captures of {report['distinct_valid_game_ids']} distinct games. "
        "Repeated snapshots are not independent games; historical dates are included.",
        *interpretations(report["completeness"], report["capture_count"]),
    ]
    markdown = "# Text quality analysis\n\n" + analysis_markdown(report, global_report=True)
    result = {**files, "overview.md": markdown.encode(),
              "overview.json": json.dumps(report, ensure_ascii=False, indent=2).encode()}
    tables = {"partitions": report.get("partitions", []),
              "completeness": [{"field": name, **stats} for name, stats in report["completeness"].items()],
              "numeric_profile": [{"field": name, **{key: value for key, value in stats.items()
                                   if not isinstance(value, (dict, list))}} for name, stats in report["numeric_profile"].items()]}
    for name, rows in tables.items():
        if rows:
            result[name + ".csv"] = pd.DataFrame(rows).to_csv(index=False).encode()
    return report, markdown, result


def render_bundle(modality, bundle):
    files = bundle["files"]
    if modality == "text":
        report, markdown, files = text_analysis(files)
        metrics = [("Captures analysed", report["capture_count"]),
                   ("Distinct games", report["distinct_valid_game_ids"]),
                   ("Captures with warnings", report["warnings"])]
        st.caption("Historical snapshots are included in quality profiling; search pages use the latest description per game.")
    else:
        profile = pd.read_csv(io.BytesIO(files["profile.csv"]))
        if modality == "image":
            metrics = [("Images analysed", len(profile)), ("Accepted", int(profile["reject_reason"].isna().sum())),
                       ("Rejected", int(profile["reject_reason"].notna().sum()))]
            markdown = files["analysis/summary.md"].decode("utf-8")
        else:
            metrics = [("Audio files analysed", len(profile)), ("Flagged for review", int((profile["status"] == "review").sum())),
                       ("Exact duplicates", int(profile["duplicate_of"].notna().sum()))]
            markdown = files["analysis/quality_overview.md"].decode("utf-8")
            st.caption("Audio quality is measured before Trusted cleaning. The processing table records actual corrections and publication outcomes.")
    for column, (label, value) in zip(st.columns(3), metrics):
        column.metric(label, value)
    if bundle["generated_at_utc"]:
        st.caption(f"Report published: {publication_label(bundle['generated_at_utc'])}")
    st.markdown(markdown)
    tables = sorted(name for name in files if name.endswith(".csv"))
    if tables:
        st.subheader("Data tables")
        selected = st.selectbox("Table to inspect", tables, key=f"report_table_{modality}",
                               format_func=lambda name: name.removeprefix("analysis/").removesuffix(".csv").replace("_", " ").title())
        st.dataframe(pd.read_csv(io.BytesIO(files[selected])), hide_index=True, width="stretch")
    if modality == "text":
        distributions = {name: stats["histogram"] for name, stats in report["numeric_profile"].items() if stats.get("histogram")}
        if distributions:
            st.subheader("Numerical distributions")
            selected = st.selectbox("Field", sorted(distributions), key="text_report_histogram")
            histogram = pd.DataFrame(distributions[selected]).rename(columns={"lower": "Bin lower bound", "count": "Captures"})
            st.bar_chart(histogram.set_index("Bin lower bound")[["Captures"]])
            st.caption("Bars show captures per stored histogram interval. Numerical extremes are descriptive signals; they are not automatically removed.")
    figures = sorted(name for name in files if name.endswith(".png"))
    if figures:
        st.subheader("Charts")
        columns = st.columns(2)
        for index, name in enumerate(figures):
            columns[index % 2].image(files[name], caption=name.split("/")[-1].removesuffix(".png").replace("_", " ").title(),
                                     width="stretch")
    st.download_button("Download report and data", archive(files), file_name=f"{modality}_quality_report_{bundle.get('revision', 'latest')[:10]}.zip",
                       mime="application/zip", key=f"download_report_{modality}")


def render():
    heading("DATA / QUALITY REPORTS", "Data reports",
            "Explore the quality of the descriptions, images and audio used by the project.")
    st.caption("Reports are read from Trusted storage. Viewing this page does not collect data, change quality decisions or load embedding models.")
    if st.button("Refresh reports"):
        load_report.clear()
        report_history.clear()
    storage_json = json.dumps(load_storage_config(), sort_keys=True)
    for tab, modality in zip(st.tabs(["Descriptions", "Images", "Audio"]), ("text", "image", "audio")):
        with tab:
            try:
                with st.spinner("Loading verified quality reports…"):
                    history = report_history(modality, storage_json)
                options = [row["revision"] for row in history] or ["current"]
                selector_key = f"report_revision_{modality}"
                if selector_key in st.session_state and st.session_state[selector_key] not in options:
                    del st.session_state[selector_key]
                if history:
                    labels = {row["revision"]: revision_label(row) for row in history}
                    chosen = st.selectbox("Report snapshot", options,
                                          format_func=lambda value, names=labels: names.get(value, str(value)),
                                          key=selector_key)
                    bundle = load_report(modality, storage_json, chosen)
                    if not next(row for row in history if row["revision"] == chosen)["is_latest"]:
                        st.info("You are viewing a historical report. Its figures describe the data and processing at that time.")
                    if not bundle.get("current_matches", True):
                        st.info("This saved report does not match the current data. It remains available as a historical snapshot.")
                else:
                    st.selectbox("Report snapshot", ["current"], format_func=lambda value: "Current report",
                                 key=f"report_revision_{modality}")
                    st.caption("No archived revisions are available yet. Showing the current verified analysis where available.")
                    bundle = load_report(modality, storage_json)
                render_bundle(modality, bundle)
            except ReportUnavailable as exc:
                st.warning(str(exc))
                st.info("Run the pipeline again to refresh the reports for the current data.")
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
                    st.info("No published report is available yet. Run the updated pipeline through Trusted to generate it.")
                else:
                    st.warning("Trusted reports could not be read. Check the storage connection and permissions.")
            except Exception:
                st.warning("This report is unavailable or incomplete. Check MinIO and regenerate the quality reports with the updated pipeline.")
