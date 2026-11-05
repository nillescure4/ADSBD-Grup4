"""Show preparation progress until the orchestrator verifies the application data."""

import os
import time

import streamlit as st

from src.common.pipeline_progress import STATUS_ENV, read_progress


STEP_DETAILS = {
    "Collect RAWG screenshots": "Collecting game screenshots and their identifiers from RAWG. If the source fails, the verified backup will be used.",
    "Collect RAWG descriptions": "Collecting descriptions for the same games selected for screenshots, preserving their original content and provenance.",
    "Collect OpenGameArt music": "Collecting CC0 music and its provenance from OpenGameArt. These tracks are independent of the RAWG games.",
    "Upload verified RAWG descriptions": "Uploading the selected, verified descriptions to temporary Landing storage.",
    "Upload images to Temporal": "Uploading the original screenshots to temporary Landing storage.",
    "Promote images to Persistent": "Moving verified original screenshots into persistent Landing storage while preserving their provenance.",
    "Promote text to Persistent": "Moving original descriptions into persistent Landing storage, preserving the game identifier and capture date.",
    "Upload audio to Temporal": "Uploading the original music files to temporary Landing storage.",
    "Promote audio to Persistent": "Moving the original music files into persistent Landing storage.",
    "Format images": "Converting original images to a consistent JPEG representation for later processing.",
    "Format text": "Converting the original game records into structured Parquet files while preserving their information.",
    "Format audio": "Converting music to a consistent FLAC representation and preventing clipping during conversion.",
    "Build Trusted images": "Checking image quality, detecting duplicates and publishing the accepted screenshots to Trusted.",
    "Build Trusted text": "Cleaning descriptions, checking duplicates and data quality, and publishing the Trusted records and their analysis.",
    "Profile audio quality": "Measuring audio quality, including duration, silence, volume and possible clipping, before preparing Trusted audio.",
    "Build Trusted audio": "Preparing Trusted music using the quality measurements and recording the processing applied to each track.",
    "Report image quality": "Building and publishing the image quality report, including quality issues, duplicate analysis and review findings.",
    "Report audio quality": "Building and publishing the audio quality report, including measurements, processing and review findings.",
    "Publish verified screenshot/game provenance": "Verifying and publishing the associations between screenshots and games so results can show the correct game details.",
    "Index Jina descriptions": "Creating text embeddings from Trusted descriptions with Jina CLIP v2 so descriptions can be compared with other descriptions and screenshots.",
    "Index Jina images": "Creating visual embeddings from Trusted screenshots with Jina CLIP v2 so screenshots can be compared with other images and descriptions.",
    "Index CLAP audio": "Creating CLAP embeddings from Trusted music excerpts so audio can be compared with other tracks and text.",
    "Index CLAP descriptions": "Creating CLAP text embeddings from Trusted descriptions so audio queries can retrieve descriptions and text queries can retrieve music.",
    "Verify Exploitation and retrieval": "Checking that all four embedding collections match Trusted, validating their vectors and testing searches within and between modalities. The application will open automatically if all checks pass.",
}


def current_explanation(state, active):
    index = state.get("current_step")
    if index is not None:
        return STEP_DETAILS.get(state["steps"][index]["name"], f"Preparing data in {active}.")
    return {"Checking requirements": "Checking the required tools and libraries before processing any data.",
            "Preparing local storage": "Starting or connecting to MinIO and creating the data-zone buckets that are missing.",
            "Starting the application": "Starting the live progress page before collecting and processing the data."}.get(state["phase"], state["phase"])


@st.fragment(run_every="1s")
def progress_panel(path):
    try:
        state = read_progress(path)
    except (OSError, ValueError):
        st.title("Preparing your data")
        st.info("Waiting for the pipeline status…")
        return
    if state["status"] == "ready":
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun(scope="app")
    st.caption("ADSBD · GROUP 4 / DATA PREPARATION")
    st.title("Preparing your data")
    st.write("Follow collection, data-zone processing and verification. The application will open automatically after successful verification.")
    steps = state["steps"]
    zones = {}
    for step in steps:
        zones.setdefault(step.get("zone", "Data preparation"), []).append(step)
    rows = []
    sources = {"Sources": "RAWG / OpenGameArt", "Landing zone": "Sources",
               "Formatted zone": "Landing zone", "Trusted zone": "Formatted zone",
               "Exploitation zone": "Trusted zone", "Verification": "Exploitation zone"}
    for name, members in zones.items():
        statuses = {member["status"] for member in members}
        status = "Failed" if "failed" in statuses else "Complete" if statuses == {"completed"} else "Running" if statuses & {"running", "completed"} else "Waiting"
        seconds = sum(time.time() - member["started_at"] if member["status"] == "running" else member.get("seconds", 0) for member in members)
        source = sources.get(name, "")
        if name == "Sources" and any(member.get("data_source") == "backup_data" for member in members):
            source = "Backup data" if all(member.get("data_source") == "backup_data" for member in members) else "External sources / backup data"
        rows.append({"Stage": name, "Status": status, "Data source": source,
                     "Duration": f"{int(seconds) // 60} min {int(seconds) % 60:02d} s" if status != "Waiting" else "—"})
    completed = sum(row["Status"] == "Complete" for row in rows)
    elapsed = max(0, int(state.get("finished_at", time.time()) - state["started_at"]))
    left, middle = st.columns(2)
    left.metric("Stages completed", f"{completed} / {len(rows)}")
    middle.metric("Total time", f"{elapsed // 60:02d}:{elapsed % 60:02d}")
    active = next((row["Stage"] for row in rows if row["Status"] in {"Running", "Failed"}), state["phase"])
    st.subheader(active)
    st.progress(completed / max(1, len(rows)))
    st.caption("Progress counts completed stages, not time remaining. Model inference can take longer than other stages.")
    if state["status"] in {"failed", "interrupted"}:
        st.error(state.get("error") or "Preparation stopped before verification completed.")
        st.info("Review the issue and rerun the orchestrator. Search remains unavailable until preparation succeeds.")
    elif time.time() - state["updated_at"] > 20:
        st.warning("The orchestrator is not sending updates. Check that the pipeline process is still running.")
    else:
        st.info(current_explanation(state, active))
    st.dataframe(rows, hide_index=True, width="stretch")


def preparation_complete():
    path = os.getenv(STATUS_ENV)
    if not path:
        return True
    try:
        if read_progress(path)["status"] == "ready":
            return True
    except (OSError, ValueError):
        pass
    progress_panel(path)
    return False
