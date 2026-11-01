"""Cross-modal discovery with aligned Jina text and image embeddings."""

import streamlit as st

from src.app.ui import collection, dataset_status, heading, model, pending_card
from src.app.image_ui import image_collection, image_status, query_image, render_image
from src.app.visual_search import search_from_image, search_from_text
from src.app.views.similarity import render_result


def render():
    heading("TASK 02 / CROSS-MODAL SIMILARITY SEARCH", "One idea, different perspectives",
            "Search game descriptions and screenshots from a text or an image.")
    st.info("Text and images share the same pinned Jina CLIP v2 model and 1024-dimensional space. "
            "A text can find images, and an image can find descriptions. Results are ranked separately by modality.")
    source = st.radio("How would you like to start?", ["Text", "Image", "Audio"], horizontal=True)
    if source == "Audio":
        pending_card("Audio connections", "Text/audio retrieval will be available once the aligned CLAP collections are ready.")
        st.button("Explore connections", disabled=True)
        return
    text_info, text_error = dataset_status()
    image_count, image_error = image_status()
    st.caption(f"{text_info['games'] if text_info else 0} games with descriptions · {image_count} screenshots")
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
    labels = st.multiselect("Modalities to retrieve", ["Descriptions", "Images"], default=["Descriptions", "Images"])
    targets = ["text" if label == "Descriptions" else "image" for label in labels]
    limit = st.slider("Results per modality", 1, 20, 5)
    unavailable = ("text" in targets and bool(text_error)) or ("image" in targets and bool(image_error))
    if st.button("Explore connections", type="primary", disabled=not targets or unavailable):
        st.session_state.pop("multimodal_results", None)
        if source == "Text" and not query.strip():
            st.warning("Describe an idea before searching.")
        elif source == "Image" and image is None:
            st.warning("Choose a reference image before searching.")
        else:
            try:
                with st.spinner("Searching descriptions and screenshots…"):
                    encoder, lock = model()
                    text_catalogue = collection() if "text" in targets else None
                    image_catalogue = image_collection() if "image" in targets else None
                    with lock:
                        if source == "Text":
                            results = search_from_text(text_catalogue, image_catalogue, encoder, query, targets=targets, top_k=limit)
                        else:
                            results = search_from_image(text_catalogue, image_catalogue, encoder, image, targets=targets, top_k=limit, exclude_ids=excluded)
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
                st.subheader("Related descriptions" if modality == "text" else "Related screenshots")
                if not hits:
                    st.info("No matching items are available.")
                for rank, hit in enumerate(hits, 1):
                    (render_result if modality == "text" else render_image)(rank, hit, compact=True)
    st.caption("Audio is not connected yet. Uploaded images are processed locally and are not added to the catalogue.")
