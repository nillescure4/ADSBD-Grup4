"""Cross-modal discovery with aligned Jina text and image embeddings."""

import streamlit as st

from src.app.ui import collection, dataset_status, heading, model
from src.app.image_ui import image_collection, image_status, query_image, render_image
from src.app.visual_search import search_from_image, search_from_text
from src.app.views.similarity import render_result
from src.app.audio_ui import audio_collection, audio_model, audio_status, query_audio, render_audio
from src.app.audio_search import search_audio, search_audio_text, uploaded_query
from src.app.clap_text_search import connect_clap_texts, search_clap_descriptions


@st.cache_resource
def clap_collection():
    return connect_clap_texts()


def clap_text_status():
    try:
        count = clap_collection().count()
        return count, None if count else "The CLAP description catalogue is empty."
    except Exception:
        return 0, "Prepare the CLAP description embeddings to find game descriptions from audio."


def interleave_results(results):
    """Mix category rankings without comparing scores from independent models."""
    for index in range(max((len(hits) for hits in results.values()), default=0)):
        for modality, hits in results.items():
            if index < len(hits):
                yield modality, index + 1, hits[index]


def render_results(results):
    titles = {"text": "Descriptions", "image": "Images", "audio": "Audio"}
    renderers = {"text": render_result, "image": render_image, "audio": render_audio}
    tabs = st.tabs(["All modalities", *[titles[modality] for modality in results]])
    with tabs[0]:
        st.caption("Results alternate between categories by their rank within each category. Jina and CLAP scores are not a single shared ranking.")
        if not any(results.values()):
            st.info("No matching items are available.")
        for modality, rank, hit in interleave_results(results):
            st.caption(f"{titles[modality]} · Rank {rank}")
            renderers[modality](rank, hit, compact=True)
    for tab, (modality, hits) in zip(tabs[1:], results.items()):
        with tab:
            if not hits:
                st.info("No matching items are available.")
            for rank, hit in enumerate(hits, 1):
                renderers[modality](rank, hit, compact=True)


def render():
    heading("TASK 02 / CROSS-MODAL SIMILARITY SEARCH", "One idea, different perspectives",
            "Start with text, an image or audio and explore related items across the available modalities.")
    st.info("Text and images share the same pinned Jina CLIP v2 model and 1024-dimensional space. "
            "CLAP connects text and audio in both directions in a separate embedding space. Results open in a mixed view; use the tabs to focus on one category.")
    source = st.radio("How would you like to start?", ["Text", "Image", "Audio"], horizontal=True)
    text_info, text_error = dataset_status()
    image_count, image_error = image_status()
    audio_count, audio_error = audio_status()
    st.caption(f"{text_info['games'] if text_info else 0} games with descriptions · {image_count} screenshots · {audio_count} audio tracks")
    if text_error:
        st.info(text_error)
    if image_error:
        st.info(image_error)
    _, clap_error = clap_text_status() if source == "Audio" else (0, None)
    query, image, excluded, reference = "", None, (), None
    if source == "Text":
        query = st.text_area("Describe an idea", placeholder="A historical western landscape with horses and open countryside", max_chars=4000, height=120)
    elif source == "Image":
        try:
            image, excluded = query_image("multimodal")
        except Exception:
            st.error("The reference image could not be decoded or verified.")
            return
    else:
        st.info("Find descriptions and other audio tracks from an audio reference. CLAP uses one vector per game and truncates descriptions exceeding 512 tokens. Trusted descriptions stay complete; the encoded prefix is highlighted when truncation was needed. Audio-to-image retrieval is not directly supported by these models.")
        if audio_error:
            st.info(audio_error)
            return
        if clap_error:
            st.info(clap_error)
        try:
            reference = query_audio("multimodal")
        except Exception:
            st.error("The reference audio could not be decoded or verified.")
            return
    options = {"Text": ["Descriptions", "Images", "Audio"], "Image": ["Descriptions", "Images"],
               "Audio": ["Descriptions", "Audio"]}[source]
    defaults = options if not audio_error else [label for label in options if label != "Audio"]
    labels = st.multiselect("Modalities to retrieve", options, default=defaults, key=f"multimodal_targets_{source}")
    targets = [{"Descriptions": "text", "Images": "image", "Audio": "audio"}[label] for label in labels]
    if "audio" in targets and source == "Text":
        st.caption("CLAP uses at most 512 tokens of the text query and compares them with a centered audio excerpt of up to 10 seconds. English descriptions of sound or mood work best.")
        if audio_error:
            st.info(audio_error)
    limit = st.slider("Results per modality", 1, 20, 5)
    unavailable = ("text" in targets and bool(clap_error if source == "Audio" else text_error)) or ("image" in targets and bool(image_error)) or ("audio" in targets and bool(audio_error))
    if st.button("Explore connections", type="primary", disabled=not targets or unavailable):
        st.session_state.pop("multimodal_results", None)
        if source == "Text" and not query.strip():
            st.warning("Describe an idea before searching.")
        elif source == "Image" and image is None:
            st.warning("Choose a reference image before searching.")
        elif source == "Audio" and reference is None:
            st.warning("Choose a reference audio before searching.")
        else:
            try:
                with st.spinner("Searching the selected modalities…"):
                    results = {}
                    visual_targets = [target for target in targets if target != "audio"]
                    if source == "Audio":
                        if "vector" in reference:
                            vector = reference["vector"]
                        else:
                            encoder, lock = audio_model()
                            with lock:
                                vector = uploaded_query(reference["body"], reference["suffix"], encoder)
                        if "text" in targets:
                            results["text"] = search_clap_descriptions(clap_collection(), vector, top_k=limit)
                        if "audio" in targets:
                            results["audio"] = search_audio(audio_collection(), vector, top_k=limit, source_key=reference.get("source_key", ""))
                    elif visual_targets:
                        encoder, lock = model()
                        text_catalogue = collection() if "text" in targets else None
                        image_catalogue = image_collection() if "image" in targets else None
                        with lock:
                            if source == "Text":
                                results = search_from_text(text_catalogue, image_catalogue, encoder, query, targets=visual_targets, top_k=limit)
                            else:
                                results = search_from_image(text_catalogue, image_catalogue, encoder, image, targets=visual_targets, top_k=limit, exclude_ids=excluded)
                    if "audio" in targets and source == "Text":
                        encoder, lock = audio_model()
                        with lock:
                            results["audio"] = search_audio_text(audio_collection(), encoder, query, top_k=limit)
                st.session_state["multimodal_results"] = {"source": source, "query": query, "results": results}
            except Exception:
                st.error("Cross-modal search could not be completed. Check the pinned model configurations, indexes and Trusted storage.")
    saved = st.session_state.get("multimodal_results")
    if saved:
        st.caption(f"Results for the submitted {saved['source'].lower()} query")
        if saved["query"]:
            st.text(saved["query"])
        render_results(saved["results"])
    st.caption("OpenGameArt tracks are independent music assets, not soundtracks attributed to RAWG games. Uploaded images are processed locally and are not added to the catalogue.")
