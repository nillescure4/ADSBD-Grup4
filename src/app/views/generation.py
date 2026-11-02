"""Multimodal retrieval followed by content, visual and real-audio choices."""

from functools import partial

import streamlit as st

from src.app.game_finder import (
    catalogue_tree, current_options, final_recommendation, generate_multimodal_tree, plain_label,
    prepare_audio_choices, question_order, tree_dot,
)
from src.app.generative_model import configuration, generate
from src.app.finder_retrieval import model_image, retrieve_multimodal_candidates
from src.app.ui import collection, dataset_status, game_details, heading, model
from src.app.image_ui import image_collection, image_status, preview
from src.app.visual_search import decode_upload
from src.app.audio_ui import audio_collection, audio_model, audio_preview, audio_status, audio_title
from src.app.audio_search import search_audio_text
from src.app.views.similarity import render_result


STATE_KEY = "game_finder"
STATE_VERSION = 2
DEFAULT_QUERY = "A chill game to play with my friends, not too difficult."
PRIORITY_LABELS = {"Content": "content", "Visual": "visual", "Balanced · no priority": "balanced"}


def retrieve_candidates(query, references, priority):
    encoder, lock = model()
    with lock:
        return retrieve_multimodal_candidates(collection(), image_collection(), encoder, query, references,
                                             priority, game_details, preview)


def prepare_run(query, references, priority, config):
    candidates = retrieve_candidates(query, references, priority)
    images = [model_image(game["screenshot"], f"Candidate game ID {game['game_id']}") for game in candidates]
    images.extend(model_image(image, f"User reference image {index}") for index, image in enumerate(references, 1))
    question_source = "ai"
    try:
        tree = generate_multimodal_tree(query, candidates, priority, partial(generate, config), images, max_attempts=1)
    except ValueError:
        tree = catalogue_tree(candidates, priority)
        question_source = "catalogue"
    encoder, lock = audio_model()
    with lock:
        audio_choices = prepare_audio_choices(tree, candidates,
            lambda description: search_audio_text(audio_collection(), encoder, description, top_k=2), audio_preview)
    return {"version": STATE_VERSION, "query": query, "references": references,
            "priority": priority, "candidates": candidates, "tree": tree, "audio_choices": audio_choices,
            "path": "", "provider": config.provider, "model": config.model, "question_source": question_source}


def render_question(saved):
    candidates, tree, path = saved["candidates"], saved["tree"], saved["path"]
    node = next(node for node in tree["nodes"] if node["path"] == path)
    modality = question_order(saved["priority"])[len(path)]
    st.subheader(f"Question {len(path) + 1} of 3 · {modality.title()}")
    st.text(node["question"])
    if modality == "visual":
        st.caption("Compare these actual screenshots. Each option keeps every game shown in that group; one screenshot does not represent every scene in a game.")
    elif modality == "audio":
        st.caption("Listen to both excerpts and choose the atmosphere you prefer. These are independent OpenGameArt music assets, not the games' original soundtracks.")
    columns = st.columns(2)
    for column, branch, label, ids in zip(columns, "LR", ("left_label", "right_label"), ("left_ids", "right_ids")):
        with column:
            if modality == "content" and saved.get("question_source") == "catalogue":
                st.markdown("**Group A**" if branch == "L" else "**Group B**")
                for game_id in node[ids]:
                    game = next(game for game in candidates if game["game_id"] == game_id)
                    st.write(game["name"])
                    description = game["description"]
                    st.text(description[:450] + ("…" if len(description) > 450 else ""))
            if modality == "visual":
                st.markdown("**Option A**" if branch == "L" else "**Option B**")
                for game_id in node[ids]:
                    game = next(game for game in candidates if game["game_id"] == game_id)
                    st.image(game["screenshot"], caption=game["name"], use_container_width=True)
            elif modality == "audio":
                choice = saved["audio_choices"][path][branch]
                st.markdown("**Audio A**" if branch == "L" else "**Audio B**")
                st.audio(choice["preview"], format="audio/wav")
                metadata = choice["result"]["metadata"]
                st.caption(f"Compared excerpt: {metadata['start_seconds']:.1f}–{metadata['end_seconds']:.1f} s")
            if st.button(plain_label(node[label]), key=f"answer_{path}_{branch}", use_container_width=True):
                saved["path"] += branch
                st.rerun()


def render_final(saved):
    candidates, tree, path = saved["candidates"], saved["tree"], saved["path"]
    game, reason = final_recommendation(tree, candidates, path)
    st.success("Your recommendation")
    render_result(1, game["result"], details=game["details"])
    st.image(game["screenshot"], caption=f"Retrieved screenshot · {game['name']}", use_container_width=True)
    choice = saved["audio_choices"][path[:2]][path[-1]]
    st.subheader("Your selected musical atmosphere")
    st.audio(choice["preview"], format="audio/wav")
    st.caption(f"{audio_title(choice['result']['source_key'])} · Independent OpenGameArt music; not an original soundtrack attribution.")
    st.subheader("Why this game?")
    st.text(reason)
    st.write("Your final choice selected the musical atmosphere associated with this candidate by CLAP. It does not establish that the game uses this music.")
    if saved.get("question_source") == "catalogue":
        st.caption("This explanation follows your choices among the retrieved catalogue items. Retrieval similarity is not a probability of enjoying the game.")
    else:
        st.caption("The AI explanation uses your request and the first two preference choices. Retrieval similarity is not a probability of enjoying the game.")


def render():
    heading("TASK 03 / AI GAME FINDER", "Find your next game",
            "Start with text, images or both. Choose your priorities. Answer three questions.")
    st.info("We shortlist 8 different games with verified descriptions and screenshots. Content priority asks about content first; visual priority asks about screenshots first. Balanced gives content and visuals equal importance and starts with content. The last question always compares two different audio excerpts: 8 → 4 → 2 → 1.")
    config, setup_error = configuration()
    info, catalogue_error = dataset_status()
    image_count, image_error = image_status()
    audio_count, audio_error = audio_status()
    enough = bool(info and info["games"] >= 8 and image_count >= 8 and audio_count >= 2)
    for error in (setup_error, catalogue_error, image_error, audio_error):
        if error:
            st.info(error)
    if not enough:
        st.info(f"The catalogue currently contains {info['games'] if info else 0} different games, {image_count} screenshots and {audio_count} audio tracks. At least 8 games with associated screenshots and 2 audio tracks are required.")
    with st.form("game_finder_search"):
        query = st.text_area("What kind of game are you looking for?", max_chars=4000, height=120,
                             value=DEFAULT_QUERY,
                             placeholder="An immersive adventure with a strong story and colourful environments.")
        uploads = st.file_uploader("Reference images (optional, up to 3)", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
        st.caption("Provide text, up to three reference images, or both. Images are limited to 20 MB and 40 million pixels each. Each supplied text/image query contributes equally within the content and visual rankings.")
        priority_label = st.radio("What matters most to you?", list(PRIORITY_LABELS), index=2, horizontal=True)
        submitted = st.form_submit_button("Find my game", type="primary", disabled=bool(setup_error or not enough or catalogue_error or image_error or audio_error))
        st.caption("Starting sends your request, reference images and the eight candidates' descriptions and real screenshots to the configured vision-capable AI model to generate and review the questions. Answering or going back makes no further AI calls. Reference images are not ingested.")
    if submitted and config:
        st.session_state.pop(STATE_KEY, None)
        if not query.strip() and not uploads:
            st.warning("Provide a description or a reference image before starting.")
        elif len(uploads) > 3:
            st.warning("Choose at most three reference images.")
        else:
            try:
                with st.spinner("Finding eight games, inspecting their screenshots and preparing your audio choices…"):
                    references = [decode_upload(upload.getvalue()) for upload in uploads]
                    saved = prepare_run(query.strip(), references, PRIORITY_LABELS[priority_label], config)
                st.session_state[STATE_KEY] = saved
            except Exception as exc:
                st.error("The game finder could not prepare consistent questions. Please try again. If this continues, check the data services and the AI connection.")
                if isinstance(exc, ValueError):
                    with st.expander("Search error details"):
                        st.text(str(exc))
    saved = st.session_state.get(STATE_KEY)
    if saved and saved.get("version") != STATE_VERSION:
        st.session_state.pop(STATE_KEY, None)
        saved = None
    if not saved:
        return
    candidates, tree, path = saved["candidates"], saved["tree"], saved["path"]
    st.divider()
    st.caption("YOUR SEARCH")
    if saved["query"]:
        st.text(saved["query"])
    if saved["references"]:
        with st.expander("Your reference images"):
            st.image(saved["references"], width=200)
    if saved.get("question_source") == "catalogue":
        st.caption(f"Priority: {saved['priority']} · Questions prepared from the retrieved descriptions and screenshots")
    else:
        st.caption(f"Priority: {saved['priority']} · Questions generated by {saved['provider']} · {saved['model']}")
    count = len(current_options(tree, candidates, path))
    st.progress(len(path) / 3, text=f"{len(path)} of 3 questions answered · {count} {'game' if count == 1 else 'games'} remaining")
    if len(path) < 3:
        render_question(saved)
    else:
        render_final(saved)
    if path:
        st.caption("YOUR CHOICES")
        for depth, branch in enumerate(path):
            node = next(node for node in tree["nodes"] if node["path"] == path[:depth])
            st.text(f"{depth + 1}. {node['question']} → {node['left_label' if branch == 'L' else 'right_label']}")
    back, reset = st.columns(2)
    if back.button("Previous question", disabled=not path):
        saved["path"] = path[:-1]
        st.rerun()
    if reset.button("Start a new search"):
        st.session_state.pop(STATE_KEY, None)
        st.rerun()
    with st.expander("View the decision tree"):
        st.graphviz_chart(tree_dot(tree, candidates, path))
    with st.expander("The eight retrieved games"):
        remaining = set(current_options(tree, candidates, path))
        st.caption("Rank fusion combines positions, not cosine values. Only games with associated descriptions and screenshots are eligible. Balanced gives content and visual rankings equal weight.")
        for rank, game in enumerate(candidates, 1):
            st.text(f"{rank:02d} · {game['name']} · Ranking score {game['retrieval_score']:.5f} · {'Still in consideration' if game['game_id'] in remaining else 'Ruled out'}")
