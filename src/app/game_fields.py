"""Consistent Trusted game metadata for description and screenshot cards."""

import streamlit as st

from src.app.trusted_details import names


def render_game_fields(details, *, compact=False):
    rating = details.get("rating")
    metacritic = details.get("metacritic")
    playtime = details.get("playtime")
    values = [("RAWG rating", f"{rating:g} / 5" if rating is not None else "Not available"),
              ("Metacritic", f"{metacritic} / 100" if metacritic is not None else "Not available"),
              ("Playtime", f"{playtime} h" if playtime is not None else "Not available")]
    if compact:
        for label, value in values:
            st.write(f"**{label}:** {value}")
    else:
        for column, (label, value) in zip(st.columns(3), values):
            column.metric(label, value)
    st.write(f"**Release date:** {details.get('released') or 'Not available'}")
    st.write(f"**Genres:** {names(details.get('genres'))}")
    st.write(f"**Platforms:** {names(details.get('platforms'), nested='platform')}")
    st.write(f"**Developers:** {names(details.get('developers'))}")
    st.write(f"**Publishers:** {names(details.get('publishers'))}")
