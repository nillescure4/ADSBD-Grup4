# Landing Zone

Landing stores original bytes, divided into Temporal and Persistent prefixes in
`1.landing-zone`. Storage names come from `config/storage.yml`. All transfers
are scripted; no placeholder objects are needed for folders. Persistent copies
are verified before Temporal objects are removed. A different existing destination
is a conflict, not an invitation to overwrite history.

## Descriptions

```powershell
python -m src.ingestion.text_to_temporal_landing --game-ids 3498 --upload-minio
python -m src.ingestion.text_temporal_to_persistent --dry-run
python -m src.ingestion.text_temporal_to_persistent
```

The collector also accepts `--games-manifest`, `--output`, and `--max-games`.
Without `--upload-minio`, it only saves local captures. The orchestrator uses
this local mode and then `text_captures_to_temporal` to upload the final verified
source selection. This prevents partially fetched live descriptions from mixing
with an offline replacement. The original `src.data_collection.collect_rawg_descriptions`
collector remains a local-only alternative.

Local captures live under `raw_api/rawg/game_details/<UTC-date>/`, with records
in `manifests/text_sources.jsonl`. Temporal uses `temporal/texts/<id>.json` and
the text-source manifest. Persistent names are
`persistent/texts/rawg/rawg$game_details_<id>$<dd-mm-yyyy>.json`, using the actual
source capture date. Promotion verifies JSON bytes and a per-game/date provenance
record before deleting Temporal. One capture per game/day is assumed. Promote
pending captures before replacing them with another day's response.

## Images

```powershell
python -m src.ingestion.images.images_to_landing --dataset rawg_images --input-dir data/landing/rawg_full
python -m src.ingestion.images.images_temporal_to_persistent
```

Configured source roots can be overridden explicitly. Upload flattens filenames
and rejects duplicates within a selected dataset rather than silently overwriting.
Persistent names contain source, dataset, original stem and initial Temporal UTC
upload date. Multipart copies with different ETags are verified by SHA-256 before
removing Temporal. Collection manifests remain available for explicit image/game
provenance publication after Trusted processing.

## Audio

```powershell
python -m src.ingestion.audio.audio_landing --input-dir data/landing/opengameart_audio/raw/files --all
python -m src.ingestion.audio.audio_persistent --all
```

Playable original files preserve `<submission>/<filename>` identity in Temporal.
Persistent uses `opengameart$music$<dd-mm-yyyy>$<submission>$<filename>`, without
changing bytes. Hashes, sizes and existing Persistent identities support retries
and avoid duplicate ingestion. Archives and tracker/MIDI files are not playable
inputs to this path. No audio collection manifests are needed by the transfer
scripts themselves; original source/licence manifests remain in the collection.

## Compatibility promotion helper

`python -m src.ingestion.temporal_to_persistent <source-key> <destination-key>`
is a low-level compatibility command. It now uses the same conflict checks,
content verification and copy-before-delete behaviour as image promotion.
It does not discover files or construct modality naming conventions; prefer the
specific scripts or [global orchestrator](../pipelines/README.md).

Collection summaries stay local. The [versioned backup](../../backup_data/README.md)
is an explicit offline source snapshot; runtime collection roots stay ignored.
