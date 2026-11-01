"""Shared image inputs, verified previews and result cards."""

import streamlit as st

from src.app.visual_search import connect_images, decode_upload, load_image
from src.app.ui import collection, game_details
from src.app.trusted_details import names


@st.cache_resource
def image_collection():
    return connect_images()


def image_status():
    try:
        count = image_collection().count()
        return count, None if count else "The image catalogue is empty."
    except Exception as exc:
        return 0, str(exc)


@st.cache_data(ttl=120, show_spinner=False)
def verified_image(bucket, key, digest):
    import boto3
    from botocore.config import Config
    from src.common.config import load_minio_settings, load_storage_config
    client = boto3.client("s3", **load_minio_settings(), config=Config(connect_timeout=2, read_timeout=10, retries={"max_attempts": 0}))
    return load_image(client, {"trusted_bucket": bucket, "trusted_object_key": key, "trusted_object_sha256": digest},
                      load_storage_config()["buckets"]["trusted"])


def preview(metadata):
    return verified_image(metadata["trusted_bucket"], metadata["trusted_object_key"], metadata["trusted_object_sha256"])


def query_image(key):
    method = st.radio("Image source", ["Upload an image", "Choose from the catalogue"], horizontal=True, key=f"{key}_source")
    if method == "Upload an image":
        uploaded = st.file_uploader("Reference image", type=["png", "jpg", "jpeg", "webp"], key=f"{key}_upload")
        if uploaded is None:
            return None, ()
        image = decode_upload(uploaded.getvalue())
        st.image(image, caption="Your reference image", width=300)
        return image, ()
    records = image_collection().get(include=["metadatas"])
    choices = dict(zip(records["ids"], records["metadatas"]))
    if not choices:
        st.info("The image catalogue is empty.")
        return None, ()
    item = st.selectbox("Reference screenshot", list(choices),
                        format_func=lambda item: choices[item].get("game_name") or choices[item].get("image_id", item), key=f"{key}_catalogue")
    image = preview(choices[item])
    st.image(image, caption="Reference screenshot · excluded from image results", width=300)
    return image, (item,)


def render_image(rank, result, *, compact=False):
    metadata = result["metadata"]
    with st.container(border=True):
        title = f"{rank:02d} · {metadata.get('game_name') or metadata.get('image_id', 'Screenshot')}"
        if compact:
            st.subheader(title)
        else:
            left, right = st.columns([4, 1])
            left.subheader(title)
            right.metric("Cosine similarity", f"{result['score']:.3f}")
        try:
            st.image(preview(metadata), use_container_width=True)
        except Exception:
            st.warning("The source image could not be loaded or verified. No substitute image is shown.")
        if compact:
            st.caption(f"Cosine similarity · {result['score']:.3f}")
        st.caption(f"{metadata.get('source', 'Trusted image')} · Snapshot {metadata.get('collection_date', 'not recorded')}")
        if metadata.get("game_id") is not None:
            try:
                records = collection().get(where={"game_id": metadata["game_id"]}, include=["documents", "metadatas"])
                index = max(range(len(records["ids"])), key=lambda n: records["metadatas"][n]["collection_date"])
                details = game_details({"game_id": metadata["game_id"], "text": records["documents"][index], "metadata": records["metadatas"][index]})
                st.write(f"**RAWG rating:** {details.get('rating', 'Not available')} · **Genres:** {names(details.get('genres'))}")
                st.caption(f"Game details · Text snapshot {records['metadatas'][index]['collection_date']}")
            except Exception:
                st.caption("Related game details are unavailable.")
