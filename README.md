# ADSBD · Group 4

Discover video games through descriptions, gameplay screenshots and independent
music assets. The project addresses three tasks: same-modality similarity,
cross-modal discovery, and an AI-guided game recommendation. Descriptions explain
game content, screenshots expose visual style, and music offers an additional
atmosphere preference. The intended value is helping users narrow down games
without knowing their titles in advance. OpenGameArt music is not attributed to
the RAWG games as their original soundtrack.

## Requirements and setup

Use Python 3.12 (the package requires Python >=3.11), Docker with Compose, and
FFmpeg/FFprobe. Start Docker Desktop on Windows before using `--start-minio`.
The first embedding run needs internet access and several GB for pinned model
weights; subsequent runs can reuse them.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,app]"
Copy-Item .env.example .env
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,app]"
cp .env.example .env
```

Set `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` in `.env`. `MINIO_ENDPOINT`
defaults to `http://localhost:9000`. Set `RAWG_API_KEY` for live RAWG collection;
source collection can fall back to the versioned backup if the key or service
is unavailable. For TASK 3, configure the provider/model/key described in
[the application guide](src/app/README.md). Keep credentials out of Git.

FFmpeg and FFprobe must be on PATH, supplied with `--ffmpeg-dir`, or already
installed under `data/tools/ffmpeg/bin`. They are not bundled or auto-downloaded.

## Run the complete project

Run commands from the repository root:

```powershell
python -m src.pipelines.run_pipeline --start-minio --serve
```

This prepares Landing, Formatted, Trusted and Exploitation, verifies the four
embedding collections, and then opens the application at http://127.0.0.1:8501.
Streamlit starts after preparation, not as a preparation-progress dashboard.
MinIO's optional visual console is at http://localhost:9001; scripts perform all
storage operations programmatically.

Defaults are **50 new RAWG games**, **up to one image per game**, and a target of
**16 total downloaded playable audio files**. These are source targets, not a
promise that every file passes quality rules. Text collection covers the image
collector's accumulated game manifest. Music collection uses a robots-aware
OpenGameArt HTML crawler, not a public audio API.

```powershell
python -m src.pipelines.run_pipeline --dry-run
python -m src.pipelines.run_pipeline --through trusted
python -m src.pipelines.run_pipeline --skip-collection
python -m src.pipelines.run_pipeline --embeddings-only
python -m src.pipelines.run_pipeline status
```

Omit `--serve` for batch preparation. `--skip-collection` requires existing local
image/audio collections and text already in Landing. `--embeddings-only` starts
from existing Trusted data. To open an already prepared application separately:

```powershell
python -m streamlit run streamlit_app.py
```

**Data reports**, alongside Home, presents the current description, image and audio
quality analyses with tables, charts and downloadable reports. The updated pipeline
publishes verified report artifacts to Trusted; viewing them does not load models
or process data. Missing or outdated reports are identified explicitly.
Each modality has a selector for the latest or a historical report, even when only
one revision exists. New ingestion updates the current analysis while preserving
distinct prior revisions. Home groups catalogue counts under **Data types**.

Use [the orchestrator guide](src/pipelines/README.md) for path overrides,
isolated runs, source plans, reports, failure handling and CLI options.

## Architecture

```text
RAWG API / OpenGameArt crawler / verified offline backup
  -> MinIO: 1.landing-zone (temporal -> persistent)
  -> MinIO: 2.formatted-zone (Parquet / JPEG / FLAC)
  -> MinIO: 3.trusted-zone (generic quality processing and reports)
  -> ChromaDB: data/exploitation/chroma (Jina and CLAP collections)
  -> Streamlit: similarity, multimodal retrieval and AI game finder
```

Exploitation uses embedded ChromaDB, not an object-store bucket. Older local
installations may still contain an unused `4.exploitation-zone` bucket; the
current configuration neither creates nor consumes it. Existing data is not
deleted by this configuration change.

| Area | Implementation and instructions |
|---|---|
| Sources | [Collection scripts](src/data_collection/README.md) |
| Landing | [Ingestion and safe promotion](src/ingestion/README.md) |
| Formatted | [Canonical input/output formats](src/formatting/README.md) |
| Trusted | [Cleaning, quality analysis and sample reviews](src/trusted/README.md) |
| Exploitation | [Pinned models and incremental indexing](src/exploitation/README.md) |
| Tasks | [Application and provider setup](src/app/README.md) |
| Operations | [Orchestration and CI](src/pipelines/README.md) |
| Data definitions | [Data dictionary](docs/data_dictionary.md) |

## Backup, provenance and limitations

[backup_data](backup_data/README.md) contains 50 original descriptions and 50
original gameplay screenshots for the same games, plus 16 independent CC0 audio
files. It is the deliberate versioned-data exception: runtime downloads, reports,
model caches and Chroma databases remain ignored under `data/` and `reports/`.
Snapshot hashes are preserved across Windows/Linux checkouts.

By default, a source failure selects a hash-verified backup subset and records
the failed source result and recovery. RAWG images and text switch together;
audio can switch independently. `--no-backup` requires live sources. The fixed
backup cannot fulfil larger samples, multiple screenshots per game or stratified
RAWG plans. Original description capture dates remain unchanged.

Run one writer per storage/database set. Daily RAWG text captures assume at most
one capture per game/day; conflicting originals fail rather than overwrite
history. Embedding model changes require compatible new collections. Jina and
CLAP scores belong to separate vector spaces; mixed results alternate category
ranks rather than comparing those raw scores. CLAP description inputs are capped
at 512 tokens and audio vectors represent a centered excerpt of up to ten seconds.

## Tests and delivery

```powershell
python -m pytest -q
```

The default suite uses isolated fixtures and skips opt-in service/model tests.
GitHub Actions runs it on pushes and pull requests. See [integration tests](tests/integration/README.md)
for live-source and full-backup runs. Their success must be checked from their
actual reports; source failures are not evidence of a successful live API call.
See [the quality assessment](docs/quality/README.md) for the documented sample,
its content hashes and the limits of that review. The separate submission
memo is maintained outside this repository.

The [media assessment](docs/quality/media_assessment.md) compares actual image and
audio samples before/after production conversion and cleaning. Active Trusted
images and retired image vectors synchronize automatically. Audio format policy
v2 prevents decoded overshoots from clipping; measured gain changes are recorded,
and intact tracked derivatives can be refreshed without changing originals.
