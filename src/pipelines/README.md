# End-to-end orchestrator

The orchestrator addresses project constraints 15 and 16: sequential execution
of the independent zone scripts, local storage setup and reproducible operation
from a fresh checkout. It prepares the data used by all three interactive tasks.

## First run

Use Python 3.12, Docker Desktop (or Docker Engine with Compose), and FFmpeg plus
FFprobe. Create and activate a virtual environment, then install:

```powershell
python -m pip install -e ".[dev,app]"
Copy-Item .env.example .env
```

Configure `RAWG_API_KEY`, `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` in `.env`.
Choose your own MinIO password. Keep `.env` private. The optional `MINIO_ENDPOINT`
defaults to `http://localhost:9000`. For TASK 3, configure `LLM_PROVIDER`,
`LLM_MODEL` and the corresponding key; the example uses Gemini. No generative
API request is made by the batch preparation process.

```powershell
python -m src.pipelines.run_pipeline run --start-minio --serve
```

This starts MinIO, creates missing Landing/Formatted/Trusted buckets, collects
all three modalities, processes every zone, builds four embedding collections,
verifies them and starts Streamlit at http://127.0.0.1:8501. The application stays
in the foreground; use Ctrl+C to stop it. Without `--serve`, preparation finishes
and exits. MinIO data and the Chroma database remain available for future runs.

Model weights/tokenizers are downloaded from their pinned Hugging Face revisions
on first use. Allow several GB of disk space and additional time for CPU inference.
Use `HF_HUB_OFFLINE=1` only after all required model files are cached. See the
[model configuration](../exploitation/README.md) for the exact revisions and licences.

FFmpeg/FFprobe must be on PATH or supplied through `--ffmpeg-dir`. An existing
installation under `data/tools/ffmpeg/bin` is discovered automatically. The
orchestrator does not download executables. CPU workers default to four unless
`OMP_NUM_THREADS`/`MKL_NUM_THREADS` are already configured.

## Selection and execution

Defaults: **8 new RAWG games**, up to **2 screenshots per game**, and **5 total
downloaded playable music files** in the audio collection directory. These are
bounded sample targets, not a complete crawl or a guarantee that every asset
passes Trusted quality checks. TASK 3 needs eight distinct games with verified
descriptions and associated screenshots, and at least two distinct audio assets.
The final report explicitly indicates whether the data meets those requirements.

```powershell
python -m src.pipelines.run_pipeline --max-games 100 --screenshots-per-game 2 --max-audio-files 16 --audio-max-items 100
```

Text and images come from the RAWG API. Descriptions use the same game manifest
as screenshots, including previous collection entries. The OpenGameArt collector
uses the existing robots-aware HTML crawler, selects Music + CC0, and excludes
archives and tracker/MIDI source files. Music is independent of RAWG games and
must not be attributed to them as their original soundtrack.

The 23 sequential steps are:

1. Collect RAWG screenshots and the game manifest.
2. Collect RAWG descriptions and upload them to Temporal Landing.
3. Collect OpenGameArt music and provenance manifests.
4. Upload images to Temporal Landing.
5. Promote images to Persistent Landing.
6. Promote descriptions to Persistent Landing.
7. Upload playable audio to Temporal Landing.
8. Promote audio to Persistent Landing.
9. Format images as JPEG.
10. Format text as Parquet.
11. Format audio as FLAC.
12. Build Trusted images.
13. Build Trusted descriptions.
14. Profile audio quality.
15. Build Trusted audio using that exact quality report.
16. Generate the image quality analysis.
17. Generate the audio quality analysis (text analysis is produced in step 13).
18. Publish verified screenshot/game associations from collector manifests.
19. Index Jina text descriptions.
20. Index Jina images.
21. Index all Trusted audio with CLAP.
22. Index latest Trusted descriptions with CLAP.
23. Verify source coverage, provenance, vectors and retrieval.

Screenshot associations are established through the original manifest SHA-256,
Persistent bytes and reproduced Formatted/Trusted JPEG hashes. IDs are never
guessed from filenames. Existing valid associations are retained across runs.

The shared Chroma directory is `data/exploitation/chroma`. Collections are
`texts_jina_clip_v2`, `images_jina_clip_v2` (1024 dimensions) and `texts_clap`,
`audio_clap_center10_v1` (512 dimensions). All use L2 normalization and cosine
distance. CLAP descriptions exceeding 512 tokens use one truncated source prefix;
Trusted descriptions remain complete. Existing legacy CLIP collections are preserved.

Verification checks every vector and its current Trusted source, exact coverage
of usable inputs, and eight retrieval probes: four same-collection searches and
text/image plus CLAP text/audio searches in both directions. These probes check
that the system operates; they do not measure relevance or generate recommendations.
The guided TASK 3 remains interactive in Streamlit and calls the configured AI
only when the user starts a search.

## Planning and recovery

```powershell
python -m src.pipelines.run_pipeline --dry-run
python -m src.pipelines.run_pipeline --through trusted
python -m src.pipelines.run_pipeline --skip-collection
python -m src.pipelines.run_pipeline --embeddings-only
python -m src.pipelines.run_pipeline --embeddings-only --serve --port 8502
python -m src.pipelines.run_pipeline status
```

`--dry-run` prints commands without loading credentials, contacting sources,
creating directories or changing storage. `--through trusted` retains the earlier
backbone-only execution. `--skip-collection` reuses existing local image/audio
collections (including `manifests/images.jsonl`) and text already in Landing.
`--embeddings-only` needs only existing Trusted data and resumes the four indexers
plus verification. `status` checks the three object-store buckets.

A stage failure stops later stages and returns a nonzero exit code. Recovery
reuses the independent scripts' checkpoints; this is not a transaction across
zones. Unchanged source bytes/vectors are retained. Same-day conflicting source
captures and incompatible model configurations fail rather than silently overwrite
historical content. Run one writer per database/bucket set and restart Streamlit
after updating indexes used by an existing session.

`--rawg-output`, `--text-output`, `--audio-output`, `--image-dataset`,
`--rawg-plan` and `--audio-query` control source selection/paths.
`--storage-config` supplies an alternative storage YAML through
`ADSBD_STORAGE_CONFIG` to every child process. `--db-path` selects an independent
Chroma database and is also passed to Streamlit through `ADSBD_CHROMA_PATH`.
Use both options together for complete isolation.

Each run writes `reports/pipeline/run_<UTC>.json` with stage names, exit codes,
durations, final zone and failure details. Detailed quality CSVs/plots are under
that directory; `exploitation.json` contains the verified counts, retrieval probes
and `task3_data_ready`. `--report-dir` overrides the location. Credentials are
passed through the environment and configured secrets are redacted from child logs.

## Tests and continuous integration

```powershell
python -m pytest -q
$env:ADSBD_RUN_LIVE_PIPELINE = "1"
python -m pytest tests/integration/test_global_pipeline.py -q -s
Remove-Item Env:ADSBD_RUN_LIVE_PIPELINE
```

The opt-in live test downloads two games/screenshots and two audio assets, creates
randomly named test buckets and an independent Chroma database, exercises all 23
steps, then repeats without source collection. It verifies unchanged source bytes,
metadata and vectors. Test buckets are deleted in `finally`; pytest temporary
files contain the local sample/index. Production buckets and indexes are not used.
The small sample verifies preparation/retrieval, not TASK 3's eight-game threshold.

When the earlier 50-image/100-description and 16-audio downloads exist locally,
`ADSBD_RUN_CAPTURED_PIPELINE=1` enables the companion test in the same file. It
reuses eight original game/image captures and two audio downloads, verifies their
manifest hashes, uploads them to isolated storage and tests all zones, indexes,
retrieval and incremental recovery. It also checks the TASK 3 data threshold.
This is a recovery test, not evidence of a new successful source API request.

GitHub Actions runs the automated suite on pushes and pull requests. The manual
`Live end-to-end pipeline` workflow runs the real test on an Ubuntu runner with
Docker and FFmpeg; configure the repository secret `RAWG_API_KEY` before running
it. It does not need Gemini or production MinIO credentials. Source/network
failures are reported as failures. CI execution itself must be observed on GitHub;
a local passing test is not evidence that a hosted job has run.
