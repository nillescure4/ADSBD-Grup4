import streamlit as st

from src.app.text_search import search
from src.app.trusted_details import highlight_description
from src.app.game_fields import render_game_fields
from src.app.ui import collection, dataset_status, game_details, heading, model, pending_card
from src.app.image_ui import image_collection, image_status, query_image, render_image
from src.app.visual_search import search_images
from src.app.audio_ui import render_audio_search


_UNSET = object()


def render_result(rank, result, *, details=_UNSET, compact=False):
    if details is _UNSET:
        try:
            details = game_details(result)
        except Exception:
            details = None
    with st.container(border=True):
        title = (details or {}).get("name") or f"Game #{result['game_id']}"
        if compact:
            st.subheader(f"{rank:02d} · {title}")
        else:
            left, right = st.columns([4,1])
            left.subheader(f"{rank:02d} · {title}")
            right.metric("Cosine similarity", f"{result['score']:.3f}")
        if details is not None:
            metadata = result["metadata"]
            if metadata.get("representation") == "full_description":
                st.text(details["description_clean"])
            else:
                st.markdown(highlight_description(details["description_clean"], metadata["char_start"],
                                                  metadata["char_end"]), unsafe_allow_html=True)
            st.caption(f"RAWG ID {result['game_id']} · Snapshot {result['date']}")
            if compact:
                st.caption(f"Cosine similarity · {result['score']:.3f}")
            render_game_fields(details, compact=compact)
        else:
            st.warning("Full description and game details are unavailable: the Trusted snapshot could not be loaded or verified. Only the indexed description is shown below.")
            st.subheader("Indexed description")
            st.text(result["text"])
            if compact:
                st.caption(f"Cosine similarity · {result['score']:.3f}")


def render():
    heading("TASK 01 / SAME-MODALITY SIMILARITY SEARCH", "Find games with similar ideas",
            "Compare descriptions, screenshots or audio tracks within the same modality.")
    modality = st.radio("Modality", ["Text", "Image", "Audio"], horizontal=True)
    if modality == "Image":
        render_image_search()
        return
    if modality == "Audio":
        render_audio_search()
        return
    st.info("Jina CLIP v2 compares your query with complete game descriptions. "
            "Each game appears once, ranked by semantic similarity to what you are looking for.")
    info, error = dataset_status()
    if error or not info or not info["games"]:
        st.info(error or "The text catalogue is empty.")
        return
    st.caption(f"{info['games']} games available · English queries are recommended for the current model.")
    with st.form("text_search"):
        query = st.text_area("What experience are you looking for?", placeholder="A puzzle game with portals and a mysterious laboratory",
                             max_chars=4000, height=120)
        left, right = st.columns(2)
        top_k = left.slider("Maximum number of games", 1, max(2, info["games"]), min(3, info["games"]))
        latest = right.checkbox("Use only the latest snapshot per game", value=True)
        submitted = st.form_submit_button("Find games", type="primary")
    if submitted:
        if not query.strip():
            st.warning("Enter a description before searching.")
        else:
            try:
                with st.spinner("Comparing your description with the catalogue… Loading the model may take a moment on first use."):
                    encoder, lock = model()
                    with lock:
                        results = search(collection(), encoder, query, top_k=top_k, latest_only=latest)
                st.session_state["text_results"] = {"query": query, "results": results}
            except Exception as exc:
                st.session_state.pop("text_results", None)
                st.error("The search could not be completed. Check that the model and embeddings are available.")
                with st.expander("Error details"):
                    st.text(str(exc))
    saved = st.session_state.get("text_results")
    if saved:
        st.subheader("Search results")
        st.caption(f"Submitted query: {saved['query']}")
        if not saved["results"]:
            st.info("No results were found in the selected catalogue.")
        else:
            for rank, result in enumerate(saved["results"], 1):
                render_result(rank, result)
    else:
        st.subheader("Start with an idea")
        st.write("Try concepts such as **medieval adventure**, **cooperative puzzles** or **competitive team shooter**.")
    with st.expander("How are results ranked?"):
        st.write("Queries and complete descriptions use the same pinned Jina CLIP v2 model. The full description and game details belong to the matching Trusted snapshot. When history is included, the best match across all snapshots is used.")
        st.caption("Similarity is a relative score, not a probability or a guarantee of relevance. The current catalogue is small, and CLIP may have limitations with some languages and long descriptions.")


def render_image_search():
    st.info("Find screenshots with similar visual content using Jina CLIP v2. Upload an image or choose a catalogue screenshot.")
    count, error = image_status()
    if error:
        st.info(error)
        return
    st.caption(f"{count} indexed images available")
    try:
        image, excluded = query_image("similarity")
    except Exception:
        st.error("The reference image could not be decoded or verified.")
        return
    top_k = st.slider("Maximum number of images", 1, min(20, max(1, count)), min(5, count), key="similarity_image_limit")
    if st.button("Find similar images", type="primary"):
        st.session_state.pop("image_results", None)
        if image is None:
            st.warning("Choose a reference image before searching.")
        else:
            try:
                with st.spinner("Comparing images…"):
                    encoder, lock = model()
                    with lock:
                        results = search_images(image_collection(), encoder, image=image, top_k=top_k, exclude_ids=excluded)
                st.session_state["image_results"] = results
            except Exception:
                st.error("Image search could not be completed. Check the model, image index and Trusted storage.")
    if "image_results" in st.session_state:
        st.subheader("Similar screenshots")
        for rank, result in enumerate(st.session_state["image_results"], 1):
            render_image(rank, result)
