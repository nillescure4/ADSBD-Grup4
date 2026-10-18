# Data Collection Scripts

This folder contains the scripts used to collect the raw data for the video-game multimodal dataset.

The final dataset uses three sources/modalities:

- **Images:** RAWG gameplay screenshots.
- **Text:** RAWG game descriptions.
- **Audio:** CC0 music from OpenGameArt.

Steam reviews are **not** part of the final dataset and are therefore not collected by the scripts in this folder.

## Files

```text
.
├── collect_rawg_images.py
├── collect_rawg_descriptions.py
├── collect_opengameart_audio.py
├── collection_plan.example.json
├── requirements.txt
└── README.md
```

`collect_rawg_images.py` should be run before `collect_rawg_descriptions.py`, because the text collector uses the `games.jsonl` manifest produced by the image collector. The OpenGameArt audio collector is independent.

## Requirements

- Python **3.9+**
- Internet connection
- A RAWG API key for the image and text collectors

Create and activate a virtual environment, then install the dependencies.

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks activation for the current session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## RAWG API key

The image and text collectors require a **RAWG API key**. Before running them, create a RAWG account and obtain an API key from the RAWG API / developer page. The key is personal and must **not** be committed to GitHub or shared in the repository.

Once you have the key, expose it to the scripts through the environment variable `RAWG_API_KEY`:

### Windows PowerShell

```powershell
$env:RAWG_API_KEY="YOUR_RAWG_API_KEY"
```

### Linux / macOS

```bash
export RAWG_API_KEY="YOUR_RAWG_API_KEY"
```

Alternatively, both RAWG scripts accept `--api-key`, although the environment variable is preferred so that credentials do not appear in commands or shell history.

---

# 1. RAWG image collection

`collect_rawg_images.py` queries RAWG, selects games, downloads the original screenshot bytes without resizing/conversion, and writes provenance manifests with SHA-256 hashes.

## Quick test

```bash
python collect_rawg_images.py \
  --output data/landing/rawg_test \
  --max-games 5 \
  --screenshots-per-game 2
```

PowerShell equivalent:

```powershell
python collect_rawg_images.py `
  --output data\landing\rawg_test `
  --max-games 5 `
  --screenshots-per-game 2
```

## Full collection used for the project

The project collection plan targets approximately 1,000 games distributed across four release-date periods:

- 1980-1999: 150 games
- 2000-2009: 200 games
- 2010-2019: 350 games
- 2020-2026: 300 games

with up to 4 screenshots per game.

```bash
python collect_rawg_images.py \
  --plan collection_plan.example.json \
  --output data/landing/rawg_full
```

PowerShell:

```powershell
python collect_rawg_images.py `
  --plan collection_plan.example.json `
  --output data\landing\rawg_full
```

Expected output:

```text
data/landing/rawg_full/
├── images/
│   └── <game_id>_<slug>/
│       └── shot_*.jpg|png|webp|...
├── manifests/
│   ├── games.jsonl
│   └── images.jsonl
└── collection_summary.json
```

Important manifest fields include `asset_id`, `source_game_id`, `source_url`, `local_path`, dimensions, byte size, SHA-256, and ingestion timestamp.

---

# 2. RAWG description collection

`collect_rawg_descriptions.py` collects the raw RAWG game-detail response for each game listed in `games.jsonl`. The raw JSON is deliberately kept unchanged in the Landing Zone; extracting and cleaning the description text belongs to later pipeline stages.

This script therefore depends on the image collection manifest.

## Quick test

First create a small RAWG image/game manifest using the image collector, then run:

```bash
python collect_rawg_descriptions.py \
  --games-manifest data/landing/rawg_test/manifests/games.jsonl \
  --output data/landing/text_descriptions_test \
  --max-games 5
```

PowerShell:

```powershell
python collect_rawg_descriptions.py `
  --games-manifest data\landing\rawg_test\manifests\games.jsonl `
  --output data\landing\text_descriptions_test `
  --max-games 5
```

## Full collection used for the project

```bash
python collect_rawg_descriptions.py \
  --games-manifest data/landing/rawg_full/manifests/games.jsonl \
  --output data/landing/text_descriptions_full
```

PowerShell:

```powershell
python collect_rawg_descriptions.py `
  --games-manifest data\landing\rawg_full\manifests\games.jsonl `
  --output data\landing\text_descriptions_full
```

Expected output:

```text
data/landing/text_descriptions_full/
├── raw_api/
│   └── rawg/
│       └── game_details/
│           └── <rawg_game_id>.json
├── manifests/
│   └── text_sources.jsonl
└── collection_summary.json
```

The `source_game_id` is preserved so that descriptions can later be related to the RAWG screenshots of the same game.

---

# 3. OpenGameArt audio collection

`collect_opengameart_audio.py` crawls OpenGameArt for **Music** licensed as **CC0**, verifies the metadata on each submission, downloads directly playable audio files, computes SHA-256 hashes, and writes JSONL manifests.

The crawler is intentionally polite: it checks `robots.txt`, uses retries, and by default waits 2 seconds between HTTP requests. Because it crawls HTML rather than a stable public API, collection can take several hours and may be affected by future changes to the OpenGameArt website.

The project collection excludes archives and MIDI/tracker/VGM source files from the main audio corpus. Direct playable audio formats include WAV, FLAC, MP3, OGG/OGA, OPUS, M4A, AAC, AIFF/AIF, AU, and WMA.

## Quick test

```bash
python collect_opengameart_audio.py \
  --output data/landing/opengameart_audio_test \
  --max-audio-files 5 \
  --max-items 30 \
  --no-include-archives
```

PowerShell:

```powershell
python collect_opengameart_audio.py `
  --output data\landing\opengameart_audio_test `
  --max-audio-files 5 `
  --max-items 30 `
  --no-include-archives
```

`--max-items` is only a safety limit on accepted submissions for a test run. If the test does not find 5 direct audio files within 30 accepted submissions, increase the value or use `0` for no submission limit.

## Full collection used for the project

```bash
python collect_opengameart_audio.py \
  --output data/landing/opengameart_audio_full \
  --max-audio-files 2000 \
  --max-items 0 \
  --no-include-archives
```

PowerShell:

```powershell
python collect_opengameart_audio.py `
  --output data\landing\opengameart_audio_full `
  --max-audio-files 2000 `
  --max-items 0 `
  --no-include-archives
```

Expected output:

```text
data/landing/opengameart_audio_full/
├── raw/
│   ├── files/
│   └── pages/
│       ├── search/
│       └── submissions/
├── manifests/
│   ├── submissions.jsonl
│   ├── files.jsonl
│   └── errors.jsonl
└── collection_summary.json
```

`submissions.jsonl` records submission-level metadata such as title, author, tags, licence, description and source URL. `files.jsonl` records file-level metadata such as `asset_id`, `parent_asset_id`, download URL, local path, file type, bytes, SHA-256 and download status.

## Resuming the audio collection

The audio collector is designed to be restarted safely. If it is interrupted, run the **same command with the same `--output` directory**. Previously completed submissions/files are read from the manifests and skipped; partially completed submissions can continue.

For example:

```powershell
python collect_opengameart_audio.py `
  --output data\landing\opengameart_audio_full `
  --max-audio-files 2000 `
  --max-items 0 `
  --no-include-archives
```

Do not delete the manifests when resuming, because they are what allow the collector to identify previously processed assets.

---

# Collection order

For a clean reconstruction of the dataset:

```text
1. collect_rawg_images.py
          ↓ produces games.jsonl
2. collect_rawg_descriptions.py

3. collect_opengameart_audio.py   (independent of RAWG)
```

The three collectors write raw source material and provenance metadata only. They do not perform the cleaning, normalization, deduplication or embedding steps belonging to later pipeline zones.

# Reproducibility notes

- Re-run a collector with the same output directory to benefit from its resume/skip behavior.
- RAWG and OpenGameArt are external live sources. Their catalogues, metadata, rankings or website structure may change, so a future run is not guaranteed to reproduce the exact same bytes/assets collected originally.
- The manifests are the record of what was actually collected and should be preserved with the dataset.
- API keys, virtual environments, caches and downloaded datasets should not be committed to Git.

A typical `.gitignore` should include at least:

```gitignore
.venv/
__pycache__/
*.pyc
.env
data/
```

If the repository intentionally versions selected manifests under `data/`, adjust the final rule accordingly rather than committing the full raw datasets.
