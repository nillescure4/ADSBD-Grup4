# Game Atlas · Streamlit application

Run from the repository root:

```powershell
python -m pip install -e ".[dev,app]"
python -m streamlit run streamlit_app.py
```

Open [Game Atlas](http://localhost:8501). All interfaces and documentation are
in English. Source descriptions retain their original language.

## Pages

- **Home:** project context, architecture and real catalogue counts.
- **TASK 1 · Similarity** (`/similarity`): text-to-text search with complete
  descriptions and verified game details from Trusted. Image and audio inputs
  remain disabled.
- **TASK 2 · Multimodal** (`/multimodal`): preview; retrieval is not connected.
- **TASK 3 · Game finder** (`/generation`): text search followed by an
  AI-generated preference tree: eight games, three answers, one recommendation.

## Retrieval prerequisites

Prepare `texts_jina_clip_v2` in `data/exploitation/chroma` using the pipeline in
`src/exploitation/README.md`. The app uses the same pinned Jina CLIP v2 query
encoder, configuration fingerprint, 1024 dimensions, L2 normalization and cosine
metric. The old `texts_clip` collection is preserved and never mixed with Jina.

Queries contain up to 4,000 characters and are encoded in full. Descriptions
are indexed in full, without chunking. Consequently, results no longer claim
that a particular passage produced the score. Each game appears once. Latest
snapshots are used by default; TASK 1 can also search historical snapshots.
Exhaustive grouping currently suits the small local corpus.

MinIO must be available to show Trusted metadata. The app checks the exact
snapshot hash, row, game ID and full description hash before using its fields.
TASK 3 requires these verified records for all eight candidates; it does not
substitute invented metadata or duplicate historical captures.

The app reads sources and embeddings without changing them. Models load on
demand and share a lock across sessions. No search history is written to disk.

Restart Streamlit after another process updates the local ChromaDB database.
The in-process vector index may otherwise retain an older snapshot. Retrieval
detects an incomplete index and reports the required restart before using it.

## TASK 3 · AI configuration

The selected project setup uses Gemini with `gemini-3.5-flash-lite`. The example
environment file contains these settings; add your own `GEMINI_API_KEY` to the
untracked `.env` before using TASK 3. Never commit the key.

Set these values in the repository's untracked `.env`, or in the server
environment. Configuration is re-read on page reruns. Entries in the local
file take precedence over environment variables, including empty values.

| Provider | LLM_PROVIDER | LLM_MODEL | Other settings |
| --- | --- | --- | --- |
| Gemini | `gemini` | An available model supporting JSON Schema | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) |
| OpenAI | `openai` | A Responses API model supporting Structured Outputs | `OPENAI_API_KEY` |
| Ollama | `ollama` | The installed local model name | Optional `OLLAMA_HOST`; default `http://127.0.0.1:11434` |

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

1. Enter a text request. Retrieve the **eight highest-ranked different games**,
   using each game's latest indexed description.
2. Load and verify their corresponding Trusted records.
3. Make one AI request containing the demand, the eight complete descriptions,
   names, genres and ratings. Credentials and internal storage paths are excluded.
4. The response schema fixes a binary tree with three question levels and eight
   game leaves. The AI selects the groupings, questions and leaf explanations.
   Candidate partitions are derived from the leaves. Local validation checks
   exact IDs, unique nodes, balanced partitions and full
   coverage. Every path must reduce candidates **8 → 4 → 2 → 1**.
5. Answer three questions. Traversal, backtracking and viewing the tree make
   **no additional AI calls**.
6. Show the one remaining game, its complete description, Trusted metadata,
   retrieval similarity, generated explanation and selected preferences.

A new submitted search replaces the previous run. A temporary HTTP 5xx service
failure is retried once; answering questions never calls the model again.
Invalid, refused, incomplete
or unavailable AI responses show an error and produce no fabricated tree.
The app also checks that the catalogue contains eight distinct games before
enabling generation. Five games with ten snapshots are still only five options.
The AI explanation is generated content; the description and game details are
the verified catalogue data. Scores express retrieval similarity, not a
probability of satisfaction.

## Verification

```powershell
python -m pytest tests/unit/test_app_text_search.py tests/unit/test_app_trusted_details.py tests/unit/test_streamlit_app.py tests/unit/test_game_finder.py tests/unit/test_generative_model.py tests/unit/test_game_finder_ui.py -q
```

Tests exercise all eight paths, three-question completion, backtracking, restart,
model failures, malformed trees and provider request/response formats. UI and
provider tests use explicit fixtures; they never send paid API requests or write
test games into the production catalogue. A live provider run additionally
requires a configured model and at least eight actual indexed games.
