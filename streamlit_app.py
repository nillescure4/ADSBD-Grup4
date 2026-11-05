"""Run from the repository root: streamlit run streamlit_app.py."""

import streamlit as st

from src.app.views import generation, home, multimodal, reports, similarity


st.set_page_config(page_title="ADSBD · Group 4", page_icon="🎮", layout="wide")
st.markdown("""
<style>
    .block-container {max-width: 1180px; padding-top: 2.6rem; padding-bottom: 4rem;}
    h1 {letter-spacing: -.045em; font-weight: 750 !important;}
    h2, h3 {letter-spacing: -.025em;}
    [data-testid="stSidebar"] {border-right: 1px solid #304049;}
    [data-testid="stMetric"] {padding: 1rem; background: #19242B; border-radius: 12px;}
    div[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 12px;}
    .atlas-kicker {color: #9EE6BB; letter-spacing: .16em; font-size: .72rem; font-weight: 700;}
    .atlas-intro {font-size: 1.15rem; color: #BBCBCB; max-width: 760px; line-height: 1.7;}
</style>
""", unsafe_allow_html=True)

pages = {
    "": [st.Page(home.render, title="Home", icon="🏠", default=True),
         st.Page(reports.render, title="Data reports", icon="📊", url_path="reports")],
    "The three tasks": [
        st.Page(similarity.render, title="TASK 1 · Similarity", icon="🔎", url_path="similarity"),
        st.Page(multimodal.render, title="TASK 2 · Multimodal", icon="🧩", url_path="multimodal"),
        st.Page(generation.render, title="TASK 3 · Game finder", icon="✨", url_path="generation"),
    ],
}
page = st.navigation(pages)
with st.sidebar:
    st.caption("ADSBD · Group 4")
page.run()
