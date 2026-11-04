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

Configure `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` in `.env`. Set
`RAWG_API_KEY` for live collection; a missing key can trigger the verified backup.
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

Defaults: **50 new RAWG games**, up to **1 screenshot per game**, and **16 total
downloaded playable music files** in the audio collection directory. These are
bounded sample targets, not a complete crawl or a guarantee that every asset
passes Trusted quality checks. TASK 3 needs eight distinct games with verified
descriptions and associated screenshots, and at least two distinct audio assets.
The final report explicitly indicates whether the data meets those requirements.

```powershell
python -m src.pipelines.run_pipeline --max-games 50 --screenshots-per-game 1 --max-audio-files 16 --audio-max-items 100
```

Text and images come from the RAWG API. Descriptions use the same game manifest
as screenshots, including previous collection entries. The OpenGameArt collector
uses the existing robots-aware HTML crawler, selects Music + CC0, and excludes
archives and tracker/MIDI source files. Music is independent of RAWG games and
must not be attributed to them as their original soundtrack.

The 24 sequential steps are:

1. Collect RAWG screenshots and the game manifest.
2. Collect RAWG descriptions locally.
3. Collect OpenGameArt music and provenance manifests.
4. Upload the selected, hash-verified descriptions to Temporal Landing.
5. Upload images to Temporal Landing.
6. Promote images to Persistent Landing.
7. Promote descriptions to Persistent Landing.
8. Upload playable audio to Temporal Landing.
9. Promote audio to Persistent Landing.
10. Format images as JPEG.
11. Format text as Parquet.
12. Format audio as FLAC.
13. Build Trusted images.
14. Build Trusted descriptions.
15. Profile audio quality.
16. Build Trusted audio using that exact quality report.
17. Generate the image quality analysis.
18. Generate the audio quality analysis (text analysis is produced in step 14).
19. Publish verified screenshot/game associations from collector manifests.
20. Index Jina text descriptions.
21. Index Jina images.
22. Index all Trusted audio with CLAP.
23. Index latest Trusted descriptions with CLAP.
24. Verify source coverage, provenance, vectors and retrieval.

Screenshot associations are established through the original manifest SHA-256,
Persistent bytes and reproduced Formatted/Trusted JPEG hashes. IDs are never
guessed from filenames. Existing valid associations are retained across runs.

Formatted image keys retain the complete source filename before `.jpg`, avoiding
collisions between different input extensions. Existing legacy derived images are
not automatically deleted; see [the migration notes](../formatting/README.md).
Text quality version 5 produces English reports and attaches exact-hash sample
assessments from [the review registry](../../docs/quality/README.md). Changing the
registry invalidates Trusted report checkpoints without changing source data.

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

A zone failure stops later stages and returns a nonzero exit code. Recovery
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
per-stage durations, total preparation time (`total_seconds`), final zone and
failure details. Detailed quality CSVs/plots are under
that directory; `exploitation.json` contains the verified counts, retrieval probes
and `task3_data_ready`. `--report-dir` overrides the location. Credentials are
passed through the environment and configured secrets are redacted from child logs.

## Offline source fallback

The versioned [backup_data snapshot](../../backup_data/README.md) contains 50
original RAWG descriptions, 50 associated RAWG gameplay screenshots and 16 OpenGameArt CC0
audio files. On a source collector failure (including a missing RAWG key), the
orchestrator validates every release file's SHA-256 and selects a matching subset
from this backup. RAWG text and images switch together; audio switches independently.
Partial live downloads remain separate and text is not uploaded until selection
has succeeded. Original text capture dates are retained. Temporary backup staging
is removed after the run; the release snapshot is never modified.

Every fallback is visible in the console and `backup_fallbacks` in the run report.
Per-stage `source_exit_code` retains the failed source result and `data_source`
identifies the offline replacement. This is not reported as a successful live
API request. `--no-backup` disables fallback; `--backup-dir` overrides the snapshot
location. Invalid backups, storage errors and zone-processing errors still fail.
The fixed backup cannot satisfy more than 50 games, multiple images per game,
stratified RAWG plans or more than 16 audio files, and rejects those requests.

If a source fails while obtaining the default sample, the pipeline can therefore
continue through all zones and index creation without an API. Once-only model
downloads and installed dependencies are separate prerequisites; after the pinned
models are cached, `HF_HUB_OFFLINE=1` allows model loading without network access.

## Tests and continuous integration

```powershell
python -m pytest -q
$env:ADSBD_RUN_LIVE_PIPELINE = "1"
python -m pytest tests/integration/test_global_pipeline.py -q -s
Remove-Item Env:ADSBD_RUN_LIVE_PIPELINE
```

The opt-in live test downloads two games/screenshots and two audio assets, creates
randomly named test buckets and an independent Chroma database, exercises all 24
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

`ADSBD_RUN_BACKUP_PIPELINE=1` enables `tests/integration/test_backup_pipeline.py`.
It simulates both source collectors failing, then processes the real 50/50/16
backup through fresh isolated buckets and a new vector database. Its report is
`reports/pipeline/backup_validation_50_50_16.json`. Model weights may already be
cached; no data objects or existing embeddings are reused. Test buckets are deleted.

GitHub Actions runs the automated suite on pushes and pull requests. The manual
`Live end-to-end pipeline` workflow runs the real test on an Ubuntu runner with
Docker and FFmpeg; configure the repository secret `RAWG_API_KEY` before running
it. It does not need Gemini or production MinIO credentials. Source/network
failures are reported as failures. CI execution itself must be observed on GitHub;
a local passing test is not evidence that a hosted job has run.

Readers retry only Chroma's known transient HNSW reload error up to twice, with
short delays; permanent errors and all source/vector validations still fail.
This addresses the cache-eviction race reported in
[chroma-core/chroma#7946](https://github.com/chroma-core/chroma/issues/7946).
The same bounded read behavior is used by the final verifier and application.
If this specific reload error persists, the verifier returns temporary-failure
code 75 and the orchestrator retries it once in a fresh process, without rerunning
ingestion or inference. A second failure still stops the run. Other verification
errors are not retried, and `verification_restarts` records the reader restart.
