# ADSBD · Group 4

Discover video games through descriptions, gameplay screenshots and independent
music assets. The project addresses three tasks: same-modality similarity,
cross-modal discovery, and an AI-guided game recommendation. Descriptions explain
game content, screenshots expose visual style, and music offers an additional
atmosphere preference. The intended value is helping users narrow down games
without knowing their titles in advance. OpenGameArt music is not attributed to
the RAWG games as their original soundtrack.

## Requirements and setup

The project is available at
[nillescure4/ADSBD-Grup4](https://github.com/nillescure4/ADSBD-Grup4).
The commands below clone `main`; check out a different branch or commit if it is
the version being assessed. A public repository can be cloned without an
invitation; if access is restricted, the owner must grant the assessor access
through GitHub. See [continuous integration](.github/README.md) for the automatic
checks and how to inspect the result for a specific commit.

Install Git, Python 3.12, Docker Desktop with Compose (or Docker Engine with
Compose on Linux), and FFmpeg/FFprobe. Start Docker Desktop before running the
pipeline on Windows or macOS. The first embedding run requires internet access
and several GB of free disk space for pinned model weights; later runs can reuse
the downloaded files. A GPU is not required.

### Install FFmpeg and FFprobe

On Windows:

1. Open the [official FFmpeg download page](https://ffmpeg.org/download.html),
   choose Windows and follow the **Windows builds from gyan.dev** link.
2. On the [Windows builds page](https://www.gyan.dev/ffmpeg/builds/), download
   **ffmpeg-release-essentials.zip** under **release builds** and extract it to a
   permanent folder. The package contains both `ffmpeg.exe` and `ffprobe.exe`;
   no separate FFprobe download is required.
3. Open Windows **Environment Variables**, edit your user **Path**, and add the
   extracted `bin` directory containing both executables. Reopen PowerShell and
   check that both commands work:

   ```powershell
   ffmpeg -version
   ffprobe -version
   ```

Alternatively, supply `--ffmpeg-dir "C:\path\to\ffmpeg\bin"` when running the
orchestrator. An existing installation under `data/tools/ffmpeg/bin` is also
discovered automatically. The project does not bundle or download these tools.

On Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
ffmpeg -version
ffprobe -version
```

On macOS, with Homebrew installed:

```bash
brew install ffmpeg
ffmpeg -version
ffprobe -version
```

### Clone and install — Windows PowerShell

```powershell
git clone --branch main https://github.com/nillescure4/ADSBD-Grup4.git
cd ADSBD-Grup4
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,app]"
Copy-Item .env.example .env
```

Use the virtual environment's Python directly; activating it is not required.
Enable UTF-8 in the same PowerShell session before running the pipeline so model
output and subprocess logs can include Unicode characters:

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

### Clone and install — Linux/macOS

```bash
git clone --branch main https://github.com/nillescure4/ADSBD-Grup4.git
cd ADSBD-Grup4
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,app]"
cp .env.example .env
```

### Configure local storage and API keys

Edit `.env` locally: set `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` and retain
`MINIO_ENDPOINT=http://localhost:9000` for local storage. Keep credentials in this
untracked file; do not include real keys in documentation or commit them to Git.
Copy `.env.example` only for a new installation; do not overwrite an existing
`.env` containing your configuration.

**RAWG — descriptions and screenshots.** Open the
[RAWG API page](https://rawg.io/apidocs), create an account or sign in, then follow
**Get API Key** to the API-key page associated with your profile. Complete the
requested application details and copy the issued key into `RAWG_API_KEY` in
`.env`. This enables live collection for the selected games.

**RAWG availability note — validation on 5 November 2026.** Checks of both the
documentation website and the API ended in read timeouts from our validation
environment. Registration, key retrieval and live collection may fail while the
service is unavailable; the cause and recovery time were not established. This
is a dated observation, not a guarantee of the service's current status. A failed
source collection can select the verified backup of 50 descriptions, 50 associated
screenshots and 16 audio files. Recovery reprocesses that snapshot; it does not
provide newly fetched RAWG data or the larger development corpus.

**Gemini — TASK 3 generation.** Open
[Google AI Studio's API Keys page](https://aistudio.google.com/apikey) and sign in
with a Google account. Accept the terms if prompted and create an API key in an
available project; create or import a Google Cloud project if needed. Copy the
key into `GEMINI_API_KEY` in `.env`, set `LLM_PROVIDER=gemini` and retain the supplied
`LLM_MODEL` value or choose a compatible vision model available to your account.
Model access, quotas and billing requirements depend on the project and model;
a key does not guarantee unlimited or free requests. See the
[official API-key guidance](https://ai.google.dev/gemini-api/docs/api-key) and
[application guide](src/app/README.md) for provider configuration. The batch
pipeline does not make generative requests; interactive TASK 3 needs the key.

## Run the complete project

Run commands from the repository root. On Windows, use the PowerShell session
where you enabled UTF-8:

```powershell
.\.venv\Scripts\python.exe -m src.pipelines.run_pipeline --start-minio --serve
```

On Linux/macOS, with the virtual environment activated:

```bash
python -m src.pipelines.run_pipeline --start-minio --serve
```

Streamlit starts immediately at http://127.0.0.1:8501 and displays live zones,
elapsed time and backup use while Landing, Formatted, Trusted
and Exploitation are prepared. After the four embedding collections and retrieval
are verified, the same page automatically opens the application. A failed run
keeps the progress page visible with the error. Add `--port 8502` if 8501 is
occupied. Ctrl+C stops the foreground application; persisted data remains
available for the next run.
MinIO's optional visual console is at http://localhost:9001; scripts perform all
storage operations programmatically.

Defaults are **50 new RAWG games**, **up to one image per game**, and a target of
**16 total downloaded playable audio files**. These are source targets, not a
promise that every file passes quality rules. Text collection covers the image
collector's accumulated game manifest. Music collection uses a robots-aware
OpenGameArt HTML crawler, not a public audio API.

```powershell
.\.venv\Scripts\python.exe -m src.pipelines.run_pipeline --dry-run
.\.venv\Scripts\python.exe -m src.pipelines.run_pipeline --through trusted
.\.venv\Scripts\python.exe -m src.pipelines.run_pipeline --skip-collection
.\.venv\Scripts\python.exe -m src.pipelines.run_pipeline --embeddings-only
.\.venv\Scripts\python.exe -m src.pipelines.run_pipeline status
```

On Linux/macOS, use `python` in place of `.\.venv\Scripts\python.exe` after
activating the virtual environment.

Omit `--serve` for batch preparation. `--skip-collection` requires existing local
image/audio collections and text already in Landing. `--embeddings-only` starts
from existing Trusted data. To open an already prepared application separately:

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
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
.\.venv\Scripts\python.exe -m pytest -q
```

The default suite uses isolated fixtures and skips opt-in service/model tests.
GitHub Actions runs it on pushes and pull requests. A separate automatic
**Backup end-to-end pipeline** job processes the real 50/50/16 backup through
fresh storage, model indexing, retrieval verification and application checks.
It requires no source API credentials. See [CI instructions](.github/README.md)
and [integration tests](tests/integration/README.md)
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
