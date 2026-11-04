# Integration tests

The default `python -m pytest -q` skips external-service/model tests. These tests
are explicitly opt-in because they require MinIO, FFmpeg, model weights, source
credentials or a generative provider. Use the installed project environment and
read each test's prerequisite corpus; not every test creates its own dataset.

| Environment variable set to `1` | Test file | Scope |
|---|---|---|
| `ADSBD_RUN_LIVE_PIPELINE` | `test_global_pipeline.py` | Live sources through all zones; isolated storage, then incremental recovery; backup disabled |
| `ADSBD_RUN_CAPTURED_PIPELINE` | `test_global_pipeline.py` | Earlier local 50-image/100-description captures; eight games and two audio files through fresh storage |
| `ADSBD_RUN_BACKUP_PIPELINE` | `test_backup_pipeline.py` | Simulated source failures using real 50/50/16 backup; fresh buckets/index, final retrieval checks |
| `ADSBD_RUN_JINA_MODEL_TESTS` | `test_jina_text_embeddings.py` | Actual pinned Jina model and Trusted text in a temporary index |
| `ADSBD_RUN_VISUAL_SEARCH_TESTS` | `test_visual_search_50_100.py` | Existing 50-image/100-latest-description corpus, source verification and cross-search |
| `ADSBD_RUN_AUDIO_SEARCH_TESTS` | `test_audio_search_16.py` | Existing 16-track corpus, uploads/previews and retrieval |
| `ADSBD_RUN_CLAP_TEXT_TESTS` | `test_clap_text_search.py` | Existing 100-game CLAP description index and 16 audio references |
| `ADSBD_RUN_MULTIMODAL_FINDER_TESTS` | `test_multimodal_game_finder.py` | Three real generative searches, all eight routes per tree, playable audio choices |

Example (PowerShell):

```powershell
$env:ADSBD_RUN_BACKUP_PIPELINE = "1"
python -m pytest tests/integration/test_backup_pipeline.py -q -s
Remove-Item Env:ADSBD_RUN_BACKUP_PIPELINE
```

Cold-data tests create unique MinIO buckets and Chroma directories, deleting test
buckets in `finally`. Service tests that explicitly target an existing corpus
verify that corpus; consult their source before running against a production
index. Reports are under ignored `reports/` or `data/exploitation/` paths. Local
success does not demonstrate that GitHub-hosted jobs ran. API/model failures must
remain visible and must not be confused with successful offline source recovery.
