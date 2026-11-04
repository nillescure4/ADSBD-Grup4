# Source collection

Run from the repository root after `python -m pip install -e ".[dev]"`. Credentials
belong in `.env`. Collectors preserve original bytes and JSONL provenance; they
do not clean descriptions, convert media or create embeddings. The global
orchestrator adds validation, zone processing and optional offline recovery.

## RAWG screenshots and descriptions

```powershell
python -m src.data_collection.collect_rawg_images --output data/landing/rawg_full --max-games 50 --screenshots-per-game 1
python -m src.data_collection.collect_rawg_descriptions --games-manifest data/landing/rawg_full/manifests/games.jsonl --output data/landing/text_descriptions
```

Set RAWG_API_KEY for live requests. The image collector queries games and their
screenshots, downloads original media without resizing, and records game IDs,
source URLs, original dimensions, byte sizes and SHA-256 in `manifests/images.jsonl`.
`manifests/games.jsonl` selects the associated game catalogue. Local images are
stored under game-specific directories. Existing IDs/URLs are reused for source
collection recovery; subsequent selection can add new games.

The standalone image CLI defaults to 100 new games and four screenshots per game;
the orchestrator overrides this with 50 and one. `--plan` accepts a JSON list of
strata with `name`, `target_games`, `filters` and optional `screenshots_per_game`.
Supported single-query filters include dates, genres, platforms, ordering and
Metacritic range. Such selection is a source sampling plan, not a claim of a
representative sample of the entire RAWG catalogue.

The original description collector reads that manifest and saves full game-detail
responses plus `manifests/text_sources.jsonl`. For direct Temporal upload or daily
capture caching, use `src.ingestion.text_to_temporal_landing` instead. The global
orchestrator first collects locally, then uploads the selected, verified captures,
allowing both RAWG modalities to fall back together on a source failure.

## OpenGameArt music

```powershell
python -m src.data_collection.collect_opengameart_audio --output data/landing/opengameart_audio --max-audio-files 16 --max-items 30 --no-include-archives --no-include-music-source
```

There is no equivalent public audio API in this implementation. The collector
uses paced HTML crawling, honours robots.txt and verifies Music + CC0 on each
submission before downloads. It caches source HTML and writes submission/file
manifests containing author, licence, source URL, local path, sizes and hashes.
Music assets are independent of RAWG games.

The standalone CLI targets 2000 total playable files unless overridden; the
orchestrator explicitly requests 16 and inspects at most 30 eligible submissions.
`--max-audio-files 0` disables that target; `--max-items 0` disables the submission
limit. `--query` filters the search. `--no-download` catalogs metadata only.
Archives and tracker/MIDI source files are optional standalone downloads and
never count toward the playable target; the orchestrator excludes them.

Summaries remain local. Empty collections, failed source requests and invalid
collection metadata must not be presented as successful end-to-end ingestion.
The source sites can change independently; see [offline recovery](../pipelines/README.md)
and [the fixed backup](../../backup_data/README.md). Runtime datasets remain ignored
by Git; the documented backup is a deliberate versioned exception.
