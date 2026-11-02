"""Cross-modal discovery with aligned Jina text and image embeddings."""

import streamlit as st

from src.app.ui import collection, dataset_status, heading, model, pending_card
from src.app.image_ui import image_collection, image_status, query_image, render_image
from src.app.visual_search import search_from_image, search_from_text
from src.app.views.similarity import render_result
from src.app.audio_ui import audio_collection, audio_model, audio_status, render_audio
from src.app.audio_search import search_audio_text


def render():
    heading("TASK 02 / CROSS-MODAL SIMILARITY SEARCH", "One idea, different perspectives",
            "Search game descriptions, screenshots and music from a text query, or descriptions and screenshots from an image.")
    st.info("Text and images share the same pinned Jina CLIP v2 model and 1024-dimensional space. "
            "CLAP connects text queries to audio in a separate embedding space. Results are ranked separately by modality; scores from different models are not directly comparable.")
    source = st.radio("How would you like to start?", ["Text", "Image", "Audio"], horizontal=True)
    if source == "Audio":
        pending_card("Audio-to-text connections", "Audio-to-description retrieval requires a separate CLAP text index. Text-to-audio search is available by starting with Text; audio-to-audio search is available in Task 1.")
        st.button("Explore connections", disabled=True)
        return
    text_info, text_error = dataset_status()
    image_count, image_error = image_status()
    audio_count, audio_error = audio_status()
    st.caption(f"{text_info['games'] if text_info else 0} games with descriptions · {image_count} screenshots · {audio_count} audio tracks")
    if text_error:
        st.info(text_error)
    if image_error:
        st.info(image_error)
    query, image, excluded = "", None, ()
    if source == "Text":
        query = st.text_area("Describe an idea", placeholder="A historical western landscape with horses and open countryside", max_chars=4000, height=120)
    else:
        try:
            image, excluded = query_image("multimodal")
        except Exception:
            st.error("The reference image could not be decoded or verified.")
            return
    options = ["Descriptions", "Images", "Audio"] if source == "Text" else ["Descriptions", "Images"]
    defaults = options if source == "Text" and not audio_error else ["Descriptions", "Images"]
    labels = st.multiselect("Modalities to retrieve", options, default=defaults, key=f"multimodal_targets_{source}")
    targets = [{"Descriptions": "text", "Images": "image", "Audio": "audio"}[label] for label in labels]
    if "audio" in targets:
        st.caption("CLAP uses at most the first 77 tokens of the text query and compares them with a centered audio excerpt of up to 10 seconds. Short English descriptions of sound or mood work best.")
        if audio_error:
            st.info(audio_error)
    limit = st.slider("Results per modality", 1, 20, 5)
    unavailable = ("text" in targets and bool(text_error)) or ("image" in targets and bool(image_error)) or ("audio" in targets and bool(audio_error))
    if st.button("Explore connections", type="primary", disabled=not targets or unavailable):
        st.session_state.pop("multimodal_results", None)
        if source == "Text" and not query.strip():
            st.warning("Describe an idea before searching.")
        elif source == "Image" and image is None:
            st.warning("Choose a reference image before searching.")
        else:
            try:
                with st.spinner("Searching the selected modalities…"):
                    results = {}
                    visual_targets = [target for target in targets if target != "audio"]
                    if visual_targets:
                        encoder, lock = model()
                        text_catalogue = collection() if "text" in targets else None
                        image_catalogue = image_collection() if "image" in targets else None
                        with lock:
                            if source == "Text":
                                results = search_from_text(text_catalogue, image_catalogue, encoder, query, targets=visual_targets, top_k=limit)
                            else:
                                results = search_from_image(text_catalogue, image_catalogue, encoder, image, targets=visual_targets, top_k=limit, exclude_ids=excluded)
                    if "audio" in targets:
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
        results = saved["results"]
        columns = st.columns(len(results))
        for column, (modality, hits) in zip(columns, results.items()):
            with column:
                st.subheader({"text": "Related descriptions", "image": "Related screenshots", "audio": "Related audio"}[modality])
                if not hits:
                    st.info("No matching items are available.")
                for rank, hit in enumerate(hits, 1):
                    {"text": render_result, "image": render_image, "audio": render_audio}[modality](rank, hit, compact=True)
    st.caption("OpenGameArt tracks are independent music assets, not soundtracks attributed to RAWG games. Uploaded images are processed locally and are not added to the catalogue.")
