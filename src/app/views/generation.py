import streamlit as st

from src.app.ui import heading, pending_card


def render():
    heading("TASK 03 / GENERATIVE TASKS", "From discovery to answers",
            "An assistant combining data retrieval with a generative model.")
    st.info("Coming soon · The generative model is not connected yet. No queries are sent to external services.")
    main, context = st.columns([3,2])
    with main, st.container(border=True):
        st.subheader("What would you like to explore?")
        st.selectbox("Request type", ["Recommend and explain", "Compare experiences", "Suggest an atmosphere"], disabled=True)
        st.text_area("Your request", placeholder="I am looking for a medieval adventure. Explain which options in the catalogue fit and suggest a musical atmosphere.", height=150, disabled=True)
        st.multiselect("Context to retrieve", ["Descriptions", "Images", "Audio"], default=["Descriptions"], disabled=True)
        st.button("Generate an answer with context", disabled=True, type="primary")
    with context:
        pending_card("Retrieved sources", "Review the chunks and assets used as context, together with their provenance.")
    st.subheader("Your answer will appear here")
    with st.container(border=True):
        st.write("The generated answer will distinguish catalogue content from creative suggestions and provide access to the retrieved sources.")
        st.caption("No answer or conversation history has been generated yet.")
    st.header("The planned workflow")
    st.markdown("**Request → Context retrieval → Generative model → Answer with sources**")
    with st.expander("What is needed to enable this task?", expanded=True):
        st.markdown("""
- Define the final use case and select a suitable generative model.
- Connect context retrieval to searches from the previous tasks.
- Prepare the prompt, model invocation and source presentation.
- Verify handling of missing context and model errors.
""")
        st.caption("A text-only model will receive textual context. Direct image or audio processing will require a compatible model or an explicit transformation.")
