"""Shared presentation helpers; expensive resources are loaded on demand."""

import threading

import streamlit as st

from src.app.text_search import connect, inventory


def heading(number: str, title: str, subtitle: str) -> None:
    st.markdown(f'<div class="atlas-kicker">{number}</div>', unsafe_allow_html=True)
    st.title(title)
    st.write(subtitle)


@st.cache_resource
def collection():
    return connect()


@st.cache_resource
def model():
    import torch
    # CPU inference is serialized; avoid oversubscribing the local machine.
    torch.set_num_threads(min(torch.get_num_threads(), 4))
    from src.exploitation.jina_clip import JinaEncoder
    return JinaEncoder("cpu"), threading.Lock()


def dataset_status():
    try:
        return inventory(collection()), None
    except Exception:
        return None, "The text catalogue is unavailable. Check that the embeddings have been prepared."


def pending_card(title: str, description: str) -> None:
    with st.container(border=True):
        st.caption("COMING SOON")
        st.subheader(title)
        st.write(description)


@st.cache_data(ttl=120, show_spinner=False)
def trusted_snapshot(bucket: str, key: str, digest: str):
    """Cache by snapshot identity, never just by game ID."""
    import boto3
    from botocore.config import Config
    from src.common.config import load_minio_settings
    from src.app.trusted_details import load_snapshot

    client = boto3.client("s3", **load_minio_settings(),
                          config=Config(connect_timeout=1, read_timeout=3, retries={"max_attempts": 0}))
    return load_snapshot(client, bucket, key, digest)


def game_details(result):
    from src.app.trusted_details import select_game
    metadata = result["metadata"]
    return select_game(trusted_snapshot(metadata["trusted_bucket"], metadata["trusted_object_key"],
                                       metadata["trusted_object_sha256"]), result)
