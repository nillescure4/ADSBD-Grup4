import streamlit as st

from src.app.text_search import search
from src.app.trusted_details import highlight_description, names
from src.app.ui import collection, dataset_status, game_details, heading, model, pending_card


def render_result(rank, result):
    try:
        details = game_details(result)
    except Exception:
        details = None
    with st.container(border=True):
        left, right = st.columns([4,1])
        title = (details or {}).get("name") or f"Game #{result['game_id']}"
        left.subheader(f"{rank:02d} · {title}")
        right.metric("Cosine similarity", f"{result['score']:.3f}")
        if details is not None:
            metadata = result["metadata"]
            st.markdown(highlight_description(details["description_clean"], metadata["char_start"],
                                              metadata["char_end"]), unsafe_allow_html=True)
            st.caption(f"RAWG ID {result['game_id']} · Snapshot {result['date']}")
            a,b,c = st.columns(3)
            rating = details.get("rating")
            metacritic = details.get("metacritic")
            playtime = details.get("playtime")
            a.metric("RAWG rating", f"{rating:g} / 5" if rating is not None else "Not available")
            b.metric("Metacritic", f"{metacritic} / 100" if metacritic is not None else "Not available")
            c.metric("Playtime", f"{playtime} h" if playtime is not None else "Not available")
            st.write(f"**Release date:** {details.get('released') or 'Not available'}")
            st.write(f"**Genres:** {names(details.get('genres'))}")
            st.write(f"**Platforms:** {names(details.get('platforms'), nested='platform')}")
            st.write(f"**Developers:** {names(details.get('developers'))}")
            st.write(f"**Publishers:** {names(details.get('publishers'))}")
        else:
            st.warning("Full description and game details are unavailable: the Trusted snapshot could not be loaded or verified. Only the indexed matching passage is shown below.")
            st.subheader("Best-matching passage")
            st.text(result["text"])


def render():
    heading("TASK 01 / SAME-MODALITY SIMILARITY SEARCH", "Find games with similar ideas",
            "Start with a text and discover descriptions related by meaning.")
    modality = st.radio("Modality", ["Text", "Image", "Audio"], horizontal=True)
    if modality != "Text":
        pending_card(f"Search by {modality.lower()}",
                     "You will be able to provide an item and retrieve similar items of the same modality.")
        st.file_uploader("Reference item", type=["png", "jpg", "jpeg"] if modality == "Image" else ["wav", "mp3", "flac"], disabled=True)
        st.button("Find similar items", disabled=True)
        return
    st.info("Descriptions can be too long for the model, so we split them into shorter passages. "
            "Each game is ranked by its best-matching passage. The full description is shown in the results, "
            "with that passage in bold: it is the part that produced the similarity score, rather than a keyword highlight.")
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
        st.write("The query and descriptions are compared using the same CLIP model. Long queries are also split into chunks. Each game appears once, ranked by its highest-scoring chunk. The full description and game details are read from the same Trusted snapshot as that chunk. When history is included, the best match across all snapshots is used.")
        st.caption("Similarity is a relative score, not a probability or a guarantee of relevance. The current catalogue is small, and CLIP may have limitations with some languages and long descriptions.")
