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
    from src.exploitation.text.build_embeddings import OpenClipEncoder
    return OpenClipEncoder("cpu"), threading.Lock()


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
def game_names():
    """Optional labels from Trusted. Local vector search works without MinIO."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    import boto3
    from botocore.config import Config
    from src.common.config import load_minio_settings, load_storage_config
    from src.ingestion.text_temporal_to_persistent import read_bytes

    names = {}
    try:
        client = boto3.client("s3", **load_minio_settings(),
                              config=Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 0}))
        records = collection().get(include=["metadatas"])["metadatas"] or []
        keys = sorted({(row["collection_date"], row["trusted_object_key"]) for row in records})
        bucket = load_storage_config()["buckets"]["trusted"]
        for _, key in keys:
            data = read_bytes(client, bucket, key)
            table = pq.ParquetFile(pa.BufferReader(data)).read(columns=["id", "name"])
            for row in table.to_pylist():
                if row["name"]:
                    names[row["id"]] = row["name"]
    except Exception:
        pass
    return names
