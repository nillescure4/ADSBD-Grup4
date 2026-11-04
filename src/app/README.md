# ADSBD · Group 4 · Streamlit application

Run from the repository root:

```powershell
python -m pip install -e ".[dev,app]"
python -m streamlit run streamlit_app.py
```

Open [ADSBD · Group 4](http://localhost:8501). All interfaces and documentation are
in English. Source descriptions retain their original language.

## Pages

- **Home:** project context, architecture and real catalogue counts.
- **TASK 1 · Similarity** (`/similarity`): text-to-text search with complete
  descriptions and verified game details, plus image-to-image screenshot search.
  Images can be uploaded or selected from the catalogue. A selected catalogue
  reference is excluded from its image results. Audio-to-audio search supports
  catalogue references and local uploads, with playable compared excerpts.
- **TASK 2 · Multimodal** (`/multimodal`): text/image and text/audio search in
  both directions. Text queries can return descriptions, screenshots and audio;
  image queries return descriptions and screenshots; audio queries return
  descriptions and audio. Results open in **All modalities**, interleaved by
  rank within each category. **Descriptions**, **Images** and **Audio** tabs
  show only that category without rerunning the models.
- **TASK 3 · Game finder** (`/generation`): text and/or image search with content,
  visual or balanced priority, followed by content/visual preferences and a
  final real-audio choice: eight games, three answers, one recommendation.

## Retrieval prerequisites

Prepare `texts_jina_clip_v2` in `data/exploitation/chroma` using the pipeline in
`src/exploitation/README.md`. The app uses the same pinned Jina CLIP v2 query
encoder, configuration fingerprint, 1024 dimensions, L2 normalization and cosine
metric. The old `texts_clip` collection is preserved and never mixed with Jina.

Jina queries contain up to 4,000 characters and are encoded in full. Descriptions
are indexed in full with Jina, without chunking. Consequently, results no longer claim
that a particular passage produced the score. Each game appears once. Latest
snapshots are used by default; TASK 1 can also search historical snapshots.
Exhaustive grouping currently suits the small local corpus.

MinIO must be available to show Trusted metadata. The app checks the exact
snapshot hash, row, game ID and full description hash before using its fields.
TASK 3 requires these verified records for all eight candidates; it does not
substitute invented metadata or duplicate historical captures.

The app reads sources and embeddings without changing them. Models load on
demand and share a lock across sessions. No search history is written to disk.

## Image and cross-modal search

Prepare `images_jina_clip_v2` in the same database with
`python -m src.exploitation.images.build_embeddings`. Both collections must use
the shared checkpoint, code revisions, 1024 dimensions, L2 normalization and
cosine distance. Separate modality fingerprints are checked before retrieval;
older CLIP collections cannot be mixed into these searches.

The [Jina reference implementation](https://huggingface.co/jinaai/jina-clip-v2)
uses `retrieval.query` for text queries to images and descriptions. Image queries
use the same model's RGB image tower and pinned preprocessing. Uploaded files
are decoded locally, limited to 20 MB and 40 million pixels, and never ingested.

Results reference Trusted object keys and SHA-256 hashes. Source images are
verified before display; changed or inaccessible sources are reported. Optional
`provenance/images.jsonl` in Trusted associates exact image object keys and ETags
with game IDs and names. These IDs are never inferred from arbitrary filenames.
When this provenance exists, image cards join the game's latest verified
Trusted text record to show the same fields as description cards: RAWG rating,
Metacritic, playtime, release date, genres, platforms, developers and publishers.
TASK 1 uses metric tiles; TASK 2 uses a compact layout. The RAWG ID and text
snapshot date identify the record used. Missing provenance remains
explicitly unavailable. Keep this manifest outside the `images/` prefix so it
is not treated as image input by the image quality pipeline.

The current validation corpus contains **50 real RAWG screenshots and 100
different game descriptions**. Historical text captures are preserved; the
cross-modal validation selects exactly the latest 100 descriptions through a
read-only Chroma filter.

Run the real corpus check after preparing both collections:

```powershell
$env:ADSBD_RUN_VISUAL_SEARCH_TESTS = "1"
python -m pytest tests/integration/test_visual_search_50_100.py -v -s
```

This verifies every source, all 50 image and 100 description self-queries,
cross-modal cosine rankings against direct vector products, real text and image
encoding, and an incremental image rerun with zero changed vectors. The report
is written to `data/exploitation/text_image_validation_50_100.json`. Source-game
recall is a diagnostic of the screenshot/description pairing, not a guarantee
of semantic relevance.

## Audio retrieval

CLAP uses `laion/clap-htsat-unfused`, pinned to revision
`8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a` for both processor and model.
Its separate `audio_clap_center10_v1` collection lives in
`data/exploitation/chroma`, with 512-dimensional L2-normalized vectors and
cosine distance. Transformers 4.48.3 is verified with both Jina and CLAP.
FFmpeg and FFprobe must be on PATH; the app also recognizes the optional
project-local `data/tools/ffmpeg/bin` directory on Windows.

Index Trusted FLACs with:

```powershell
python -m src.exploitation.audio.embedding_audio --all
```

Each track has one vector from its centered excerpt of up to ten seconds.
Shorter tracks use their full duration. Result players play that excerpt,
verified against the indexed Trusted source signature and available content
hash. Cards show the full track duration, compared interval and similarity.
OpenGameArt music is independent of RAWG games; no soundtrack attribution or
game metadata is inferred for music assets.

TASK 1 can reuse a catalogue vector, excluding the reference track, or encode
the centered excerpt of an uploaded WAV/MP3/OGG/FLAC (maximum 50 MB). Uploads
use disposable local files and are never ingested. TASK 2 encodes a text query
with CLAP on demand; that temporary query vector is not stored. CLAP uses
up to 512 tokens. English descriptions of mood or sound are
recommended. Audio-only queries do not load Jina.

Jina and CLAP scores are ranked separately and must not be interpreted as a
single shared scale. Audio-to-description retrieval uses the separate CLAP
description collection below. Direct image/audio retrieval remains unavailable
with these two independently aligned models.

### CLAP description embeddings

```powershell
python -m src.exploitation.text.build_clap_embeddings
```

The `texts_clap` collection uses the same pinned checkpoint and revision as
audio, 512-dimensional L2-normalized vectors and cosine distance. It contains
one vector per game's latest Trusted description, selected across all capture
partitions and verified against their manifests. Jina collections and existing
audio vectors are preserved.

The pinned RoBERTa text configuration and tokenizer support 512 tokens. The
previous audio-query script imposed 77 tokens; this was a preprocessing choice,
not the model's architectural context limit. Text queries now use 512 tokens.
Stored descriptions exceeding the limit are truncated to an exact source
prefix fitting 512 tokens including special tokens, as requested. Shorter
descriptions are encoded in full. Trusted data remains unchanged.

The current corpus has **100 descriptions: 99 fit in full and one is truncated**.
Each record includes game ID, date, full-source and encoded-prefix hashes,
Trusted object hash/row, offsets, token count, truncation flag and configuration
fingerprint. Unchanged records skip inference; changed captures reuse identical
text vectors while updating provenance. Replaced records are removed only after
their new vectors have been verified. Incompatible configurations require a
separate collection instead of mixing vectors.

Audio queries use catalogue vectors or a freshly encoded upload. Description
results resolve to verified Trusted records and show full game metadata. When
truncation was needed, the encoded prefix is highlighted within the complete
original description. Each game is returned once.

```powershell
$env:ADSBD_RUN_CLAP_TEXT_TESTS = "1"
python -m pytest tests/integration/test_clap_text_search.py -v -s
```

The real validation checks all 100 source records and 16 audio queries, compares
retrieval with direct vector products, verifies model dimensions and unit norms,
and repeats indexing with zero recomputed vectors. Its report is saved to
`data/exploitation/clap_text_validation.json`.

The real validation corpus includes 16 CC0 tracks downloaded through the
existing OpenGameArt web collector (this source is not a RAWG-style API).
The complete source → Landing → Formatted → quality → Trusted → CLAP route
was exercised. Re-run the opt-in validation after preparing the corpus:

```powershell
$env:ADSBD_RUN_AUDIO_SEARCH_TESTS = "1"
python -m pytest tests/integration/test_audio_search_16.py -v -s
```

The test uses `data/audio/search_16` and `reports/audio_16_quality.csv`.
It verifies every source and preview, all 16 self-queries against direct
cosine products, real uploaded and text queries, and incremental reruns of
all processing stages. Its summary is saved to
`data/exploitation/audio_validation_16.json`.

Restart Streamlit after another process updates the local ChromaDB database.
The in-process vector index may otherwise retain an older snapshot. Retrieval
detects an incomplete index and reports the required restart before using it.

## TASK 3 · AI configuration

The selected project setup uses Gemini with `gemini-3.5-flash-lite`. The example
environment file contains these settings; add your own `GEMINI_API_KEY` to the
untracked `.env` before using TASK 3. Never commit the key.
For this model, image-bearing generation, audits and wording corrections use
medium thinking with a 20,000-token output budget, balancing grouping quality
and preparation time. The model
name stays explicitly configured; other models retain their default reasoning
settings. Multimodal HTTP requests have a 120-second read timeout.

Set these values in the repository's untracked `.env`, or in the server
environment. Configuration is re-read on page reruns. Entries in the local
file take precedence over environment variables, including empty values.

| Provider | LLM_PROVIDER | LLM_MODEL | Other settings |
| --- | --- | --- | --- |
| Gemini | `gemini` | A vision model supporting JSON Schema | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) |
| OpenAI | `openai` | A vision Responses API model supporting Structured Outputs | `OPENAI_API_KEY` |
| Ollama | `ollama` | An installed vision model supporting structured output | Optional `OLLAMA_HOST`; default `http://127.0.0.1:11434` |

No provider or model is silently selected. Keep credentials server-side; never
put them in a request, commit or screenshot. Ollama must already be running
with a capable model and sufficient context for all eight descriptions.
TASK 1 does not need a generative API key.

Provider implementations use their official structured-output APIs:
[Gemini](https://ai.google.dev/gemini-api/docs/generate-content/structured-output),
[OpenAI](https://developers.openai.com/api/docs/guides/structured-outputs),
and [Ollama](https://docs.ollama.com/api/chat).
Model names, availability and access depend on the configured provider.

## TASK 3 · Workflow

1. Provide text, up to three reference images, or both. Choose **Content**,
   **Visual**, or **Balanced · no priority**. Reference images use the same
   20 MB/40-million-pixel limits as image search and are never ingested.
   The editable default request is "A chill game to play with my friends, not too difficult."
2. Encode each input with pinned Jina. Search complete descriptions and
   screenshots. Within each modality, combine the input rankings with equal
   weights using reciprocal rank fusion, `sum(1 / (60 + rank))`.
   A game's multiple screenshots count once per image ranking. Only games with
   a latest indexed description and an explicit image/game association are eligible.
3. Content priority selects the first eight games from the content ranking;
   Visual selects the first eight from the visual ranking. Balanced applies
   equal-weight reciprocal rank fusion to the two game-level rankings. Cosine
   values from descriptions and images are never directly sorted together.
   Each selected game keeps its own description and most relevant screenshot.
   Trusted text rows and image bytes are verified before proceeding.
4. Send the request, reference images, eight complete descriptions, names,
   genres, ratings and eight labelled actual screenshots to the configured
   vision-capable AI. Images are JPEG-encoded with a maximum edge of 768 pixels.
   Credentials and internal storage paths are excluded.
5. Generate two balanced question levels, **8 → 4 → 2**, with fixed modality
   order. Content and Balanced ask content first, then visual. Visual priority
   reverses them. The schema and local validation enforce that order, eight
   unique IDs and all partitions. Visual questions display every real screenshot
   in both groups: four per group when first, two per group when second.
   No synthetic images are generated. Every branch must include per-game
   evidence: content evidence IDs resolve to actual sentences in the matching
   description (an exact matching source quote is also accepted); visual
   evidence describes the corresponding actual screenshot.
   A second vision request audits every question and branch against all its
   members. If generation, validation or auditing fails, the interface tries
   one automatic correction or regeneration without another user click.
   If the review still fails, it keeps the latest structurally usable AI tree
   and labels its content review as unconfirmed. Structural checks still enforce
   eight actual games, balanced partitions and the selected question order.
   Only when no usable AI tree exists does it build balanced catalogue questions.
   These compare actual descriptions and screenshots in retrieval order;
   content choices show source excerpts and visual choices show the real images.
6. For each remaining pair, temporarily encode its descriptions with pinned
   CLAP, using at most 512 tokens. Retrieve the best two tracks per
   description and select the different-source pair maximizing their combined
   cosine similarity. This resolves collisions by considering alternatives for
   either game. Prepare verified playable excerpts before showing the tree.
7. The final question compares those two audio excerpts, **2 → 1**. Tracks are
   independent music assets, not claimed soundtracks. The final recommendation
   includes the game, complete description, Trusted details, its screenshot,
   selected music, AI explanation and an audit of the answers. The AI explains
   the first two choices; the application explains the final music choice.
8. Answering, backtracking and viewing the tree make **no additional AI calls**
   or audio-ranking requests. All branches and audio choices are prepared once.

A new submitted search replaces the previous run. A temporary HTTP 5xx service
failure or connection interruption is retried once; read timeouts are not
automatically retried. Answering questions never calls the model again.
Invalid, refused, incomplete or unavailable AI responses receive one automatic
preparation retry. A usable AI question is retained even when its content audit
remains unconfirmed; the interface identifies that status. Catalogue questions
are the last resort when neither attempt produced a usable structure, and are
not presented as AI-generated. The final two distinct audio choices
are still retrieved with CLAP. Missing or changed source data can still stop a
search: the application does not substitute invented games, images or audio.
The app checks basic catalogue counts before enabling generation and verifies
at retrieval time that eight different games actually have associated images.
Five games with ten snapshots are still only five options.
The AI explanation is generated content; the description and game details are
the verified catalogue data. Scores express retrieval similarity, not a
probability of satisfaction.

## Verification

```powershell
python -m pytest tests/unit/test_app_text_search.py tests/unit/test_app_trusted_details.py tests/unit/test_streamlit_app.py tests/unit/test_game_finder.py tests/unit/test_multimodal_finder.py tests/unit/test_generative_model.py tests/unit/test_game_finder_ui.py -q
```

Tests exercise all eight paths, three-question completion, backtracking, restart,
model failures, malformed trees and provider request/response formats. UI and
provider tests use explicit fixtures; they never send paid API requests or write
test games into the production catalogue. A live provider run additionally
requires a configured vision model, MinIO, eight games with associated images
and at least two indexed audio tracks. The real validation exercises Content
with text, Visual with an image, and Balanced with text plus an image; each
tree is traversed through all eight routes and all audio pairs are verified:

```powershell
$env:ADSBD_RUN_MULTIMODAL_FINDER_TESTS = "1"
python -m pytest tests/integration/test_multimodal_game_finder.py -v -s
```

This opt-in test starts three real searches with the configured AI provider.
Each normally needs generation plus an audit; correction can add another pair
of requests. Answering questions never sends additional requests.
The public-catalogue validation summary is written to
`data/exploitation/multimodal_game_finder_validation.json`.
