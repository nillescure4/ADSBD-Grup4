"""Shared CLAP resources, audio inputs and playable result cards."""

from pathlib import Path
import threading
from urllib.parse import unquote

import streamlit as st

from src.app.audio_search import (
    connect_audio, preview_audio, search_audio, uploaded_query, validate_audio_index,
)
from src.exploitation.audio.search_audio import ClapTextEncoder, existing_vector, indexed_songs


@st.cache_resource
def audio_collection():
    return connect_audio()


@st.cache_resource
def audio_model():
    import torch
    torch.set_num_threads(min(torch.get_num_threads(), 4))
    return ClapTextEncoder(), threading.Lock()


def audio_status():
    try:
        catalogue = audio_collection()
        validate_audio_index(catalogue)
        return catalogue.count(), None
    except Exception:
        return 0, "The audio catalogue is unavailable. Prepare the pinned CLAP audio embeddings first."


def audio_title(key):
    name = Path(unquote(key.split("$")[-1])).stem
    if Path(name).suffix.lower() in {".mp3", ".ogg", ".wav", ".flac"}:
        name = Path(name).stem
    return name.replace("_", " ")


@st.cache_data(ttl=120, show_spinner=False)
def audio_preview(metadata):
    import boto3
    from botocore.config import Config
    from src.common.config import load_minio_settings, load_storage_config
    client = boto3.client("s3", **load_minio_settings(), config=Config(connect_timeout=2, read_timeout=30, retries={"max_attempts": 0}))
    return preview_audio(client, metadata, load_storage_config()["buckets"]["trusted"])


def query_audio(key):
    method = st.radio("Audio source", ["Choose from the catalogue", "Upload an audio file"], horizontal=True, key=f"{key}_audio_source")
    if method == "Upload an audio file":
        uploaded = st.file_uploader("Reference audio", type=["wav", "mp3", "flac", "ogg"], key=f"{key}_audio_upload")
        if uploaded is None:
            return None
        body = uploaded.getvalue()
        if len(body) > 50 * 1024 * 1024:
            raise ValueError("Choose an audio file of up to 50 MB.")
        st.audio(body)
        return {"body": body, "suffix": Path(uploaded.name).suffix}
    songs = indexed_songs(audio_collection())
    choices = {song.vector_id: song for song in songs}
    item = st.selectbox("Reference track", list(choices), format_func=lambda item: audio_title(choices[item].source_key), key=f"{key}_audio_catalogue")
    if item is None:
        return None
    vector, metadata = existing_vector(audio_collection(), item)
    st.audio(audio_preview(metadata), format="audio/wav")
    st.caption(f"Reference excerpt: {metadata['start_seconds']:.1f}–{metadata['end_seconds']:.1f} s · Excluded from results")
    return {"vector": vector, "source_key": metadata["source_key"]}


def render_audio(rank, result, *, compact=False):
    metadata = result["metadata"]
    with st.container(border=True):
        st.subheader(f"{rank:02d} · {audio_title(metadata['source_key'])}")
        st.caption(f"Cosine similarity · {result['score']:.3f}")
        try:
            st.audio(audio_preview(metadata), format="audio/wav")
        except Exception:
            st.warning("The Trusted audio could not be loaded or verified.")
        st.write(f"**Track duration:** {metadata['duration_seconds']:.1f} s")
        st.write(f"**Compared excerpt:** {metadata['start_seconds']:.1f}–{metadata['end_seconds']:.1f} s")
        st.caption("OpenGameArt music · CLAP · Independent music asset; no RAWG game attribution")


def render_audio_search():
    st.info("CLAP compares one centered excerpt of up to 10 seconds per track. Shorter tracks use their entire duration. The player below each result plays the compared excerpt.")
    count, error = audio_status()
    if error:
        st.info(error)
        return
    st.caption(f"{count} indexed audio tracks available · Uploaded files are used only for this query.")
    try:
        reference = query_audio("similarity")
    except Exception:
        st.error("The reference audio could not be decoded or verified.")
        return
    limit = st.slider("Maximum number of tracks", 1, max(1, min(20, count - 1)), min(5, max(1, count - 1)))
    if st.button("Find similar audio", type="primary"):
        st.session_state.pop("audio_results", None)
        if reference is None:
            st.warning("Choose a reference audio before searching.")
        else:
            try:
                with st.spinner("Comparing audio excerpts…"):
                    if "vector" in reference:
                        vector = reference["vector"]
                    else:
                        encoder, lock = audio_model()
                        with lock:
                            vector = uploaded_query(reference["body"], reference["suffix"], encoder)
                    st.session_state["audio_results"] = search_audio(audio_collection(), vector, top_k=limit, source_key=reference.get("source_key", ""))
            except Exception:
                st.error("Audio search could not be completed. Check CLAP, FFmpeg and the Trusted source.")
    if "audio_results" in st.session_state:
        st.subheader("Similar audio tracks")
        for rank, result in enumerate(st.session_state["audio_results"], 1):
            render_audio(rank, result)
