import streamlit as st

from src.app.ui import dataset_status, heading


def render():
    heading("ADSBD / GROUP 4 / MULTIMODAL EXPLORATION", "One game. Many ways to discover it.",
            "Game Atlas · Text, images and audio connected through data.")
    st.markdown('<p class="atlas-intro">Explore how descriptions, screenshots '
                'and music can help us discover gaming experiences. From collecting '
                'data to semantic search and generative assistance.</p>', unsafe_allow_html=True)
    st.divider()
    info, error = dataset_status()
    a,b,c = st.columns(3)
    a.metric("Games with indexed text", info["games"] if info else "—")
    b.metric("Data snapshots", info["captures"] if info else "—")
    c.metric("Indexed descriptions", info["vectors"] if info else "—")
    if error:
        st.info(error)
    elif info["dates"]:
        st.caption(f"Local catalogue · {info['dates'][0]} → {info['dates'][-1]} · Counts from the available embeddings.")
    st.header("Three ways to explore")
    columns = st.columns(3)
    cards = [
        ("01 / SIMILARITY", "Find similar experiences", "Compare items within the same modality. Description search is available now.", "🟢 Text available"),
        ("02 / MULTIMODAL", "Connect the modalities", "Describe an idea and explore text, images and audio related to the same concept.", "🟠 Coming soon"),
        ("03 / AI GAME FINDER", "Find your next game", "Start with 8 retrieved games. Answer 3 AI-generated questions to get one recommendation.", "✨ Requires 8 games and an AI connection"),
    ]
    for column, (label, title, text, status) in zip(columns, cards):
        with column, st.container(border=True):
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
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.subheader("Our sources")
        st.write("**RAWG:** video game descriptions and screenshots.\n\n**OpenGameArt:** CC0 music for exploring soundscapes.")
        st.caption("Audio tracks are independent; they are not soundtracks attributed to the RAWG games.")
    with right, st.container(border=True):
        st.subheader("What can you try today?")
        st.write("Open **TASK 1 · Similarity**, enter a description and explore the closest games.")
        st.caption("TASK 2 is a preview. TASK 3 needs an AI connection and at least eight different indexed games.")
