import streamlit as st

from src.app.ui import dataset_status, heading
from src.app.image_ui import image_status
from src.app.audio_ui import audio_status


def render():
    heading("ADSBD / GROUP 4 / MULTIMODAL EXPLORATION", "One game. Many ways to discover it.",
            "ADSBD · Group 4 · Text, images and audio connected through data.")
    st.markdown('<p class="atlas-intro">Explore how descriptions, screenshots '
                'and music can help us discover gaming experiences. From collecting '
                'data to semantic search and generative assistance.</p>', unsafe_allow_html=True)
    st.divider()
    st.header("Data types")
    info, error = dataset_status()
    images, image_error = image_status()
    audio, audio_error = audio_status()
    a,b,c = st.columns(3)
    a.metric("Descriptions", info["games"] if info else "—")
    b.metric("Images", images if not image_error else "—")
    c.metric("Audio tracks", audio if not audio_error else "—")
    st.caption("Descriptions count only the latest snapshot per game.")
    st.markdown("Open **Data reports** in the sidebar to explore the quality reports, charts and report history for descriptions, images and audio.")
    for catalogue_error in (error, image_error, audio_error):
        if catalogue_error:
            st.info(catalogue_error)
    st.header("Three ways to explore")
    columns = st.columns(3, border=True)
    cards = [
        ("TASK 1 / SIMILARITY", "Find similar experiences", "Compare descriptions, screenshots or audio tracks within the same modality.", "🟢 Text, images and audio available"),
        ("TASK 2 / MULTIMODAL", "Connect the modalities", "Explore mixed results from text, screenshots or audio, and use category tabs to focus your search.", "🟢 Text ↔ image and text ↔ audio available"),
        ("TASK 3 / AI GAME FINDER", "Find your next game", "Start with text and/or images. Prioritize content, visuals or both, then choose between content, screenshots and music to reach one game.", "✨ Requires a vision AI connection and prepared catalogues"),
    ]
    for column, (label, title, text, status) in zip(columns, cards):
        with column:
            st.caption(label)
            st.subheader(title)
            st.write(text)
            st.markdown(status)
    st.header("From sources to discovery")
    st.markdown("**Sources → Landing → Formatted → Trusted → Embeddings → Tasks**")
    with st.expander("What happens at each stage?"):
        st.markdown("""
- **Landing:** preserves original snapshots and their provenance.
- **Formatted:** converts representations into common formats.
- **Trusted:** cleans data and analyses quality without tailoring it to a specific model.
- **Embeddings:** represents content as vectors for semantic search.
- **Tasks:** supports exploration, cross-modal retrieval and context-aware generation.
""")
    with st.container(border=True):
        st.subheader("Our sources")
        st.write("**RAWG:** video game descriptions and screenshots.\n\n**OpenGameArt:** CC0 music for exploring soundscapes.")
        st.caption("Audio tracks are independent; they are not soundtracks attributed to the RAWG games.")
