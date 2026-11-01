import streamlit as st

from src.app.ui import heading, pending_card


def render():
    heading("TASK 02 / MULTI-MODALITY SIMILARITY SEARCH", "One idea, different perspectives",
            "Where descriptions, screenshots and soundscapes meet.")
    st.info("Coming soon · This page previews the interface. It does not run multimodal searches yet.")
    with st.container(border=True):
        st.subheader("Define what you want to discover")
        source = st.radio("How would you like to start?", ["Text", "Image", "Audio"], horizontal=True)
        if source == "Text":
            st.text_area("Describe an idea", placeholder="A dark fantasy world with a calm, mysterious atmosphere", disabled=True)
        else:
            st.file_uploader("Reference item", disabled=True,
                             type=["png", "jpg", "jpeg"] if source == "Image" else ["mp3", "wav", "flac"])
        st.multiselect("Modalities to retrieve", ["Texts", "Images", "Audio"], default=["Texts", "Images", "Audio"], disabled=True)
        st.slider("Results per modality", 1, 10, 3, disabled=True)
        st.button("Explore connections", type="primary", disabled=True)
    st.header("Your matches will appear here")
    a,b,c = st.columns(3)
    with a:
        pending_card("Related descriptions", "Text chunks that share the meaning of your query.")
    with b:
        pending_card("Screenshots", "Visual results for discovering related scenes and aesthetics.")
    with c:
        pending_card("Soundscapes", "Tracks with an audio player for exploring music related to your idea.")
    st.caption("No simulated results are shown. These areas will display real data once search is connected.")
    with st.expander("What is needed to enable this task?", expanded=True):
        st.markdown("""
- Complete and verify image embeddings compatible with the CLIP text embeddings.
- Agree on a CLAP checkpoint and generate the corresponding text and audio vectors.
- Prepare multimodal collections and demonstrate results containing multiple formats.
- Connect queries and original assets to this interface.
""")
        st.write("CLIP will connect text and images; CLAP will connect text and audio. Results will be ranked separately by modality, without directly comparing scores across models.")
        st.caption("These two models do not directly support image-to-audio search. Any additional connection will require an intermediate process.")
