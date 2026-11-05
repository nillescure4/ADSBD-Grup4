# Offline source backup

This release snapshot contains **50 original RAWG game-detail JSON files**,
**50 original RAWG gameplay screenshots** (one associated with each game), and
**16 original OpenGameArt CC0 audio files**. Total source size is approximately 84 MB.

The images are the 50 previously collected gameplay screenshots. No image was
generated, resized or recompressed for this backup. Descriptions retain all
original RAWG fields, not just the description text. Audio is independent of
the games and must not be described as their original soundtrack.

- `rawg/images/`: original image bytes.
- `rawg/manifests/`: game IDs, image associations, source URLs and source hashes.
- `texts/raw_api/`: original RAWG detail responses, grouped by capture date.
- `texts/manifests/`: original text capture timestamps and SHA-256 hashes.
- `audio/raw/files/`: original downloaded audio, grouped by OpenGameArt node.
- `audio/manifests/`: source URLs, authors, CC0 licences and file hashes.
- `manifest.json`: release inventory with byte sizes and SHA-256 for every source
  file and provenance manifest. This README is explanatory and is not source data.

The orchestrator first attempts live collection. On a source failure it validates
the complete backup and stages the required subset in a separate temporary
directory. A RAWG fallback selects both descriptions and associated images from
this snapshot, including when live description collection fails after successful
image collection. It never mixes partial live downloads into the selected backup
catalogue. Text uploads happen only after source selection is complete.

Backup descriptions preserve their original capture dates; restoring an old
response must not make it appear to be newly fetched from RAWG. The run report
records `backup_fallbacks`, failed source exit codes and `data_source` per source
stage. The usual Landing, Formatted, Trusted and embedding stages still run.

The fallback supports up to 50 RAWG games with one image per game and up to 16
audio files. Larger requests and stratified RAWG plans fail clearly rather than
claiming that this fixed snapshot satisfies them. Use `--no-backup` to require
successful live collection, or `--backup-dir` to select another compatible copy.

Keep source attribution and licence metadata with these files. RAWG images and
descriptions retain their original rights; the audio licence evidence is in
`audio/manifests/submissions.jsonl`. The backup is intended for this academic
project and is not a statement that all source material has the same licence.

Source content remains in its original language. See the
[data dictionary](../docs/data_dictionary.md) for identifiers and formats and the
[quality assessment](../docs/quality/README.md) for the eight documented text
samples. These assessments do not certify every backup record or future capture.
