# Trusted Zone

Trusted applies generic data-quality processing rather than tailoring data to a
specific embedding model. Each modality has independent processing and analysis
scripts. Install base dependencies, start MinIO and prepare Formatted data first.

## Text

```powershell
python -m src.trusted.text_to_trusted_zone --dry-run
python -m src.trusted.text_to_trusted_zone
```

The process verifies Formatted Parquet and row provenance, removes exact full-row
duplicates within a capture date, and creates description_clean. It prefers
description_raw, falling back to HTML description when needed. HTML and script/style
content, repeated spacing and control characters are cleaned conservatively.
Source language, casing, accents, punctuation and paragraph boundaries are retained.
No speculative spelling repair, numerical imputation or model-specific normalisation
is applied. Other business fields and captures from other dates are retained.

Trusted replaces description/description_raw with description_clean: 53 fields
for the base schema, with additional unknown source fields retained. Diagnostics
are report-only. Missing/short text, invalid identity/date/provenance/JSON and
nonfinite numbers are flagged. Warnings do not silently remove source rows.

Each date writes verified Parquet, quality JSON/Markdown, and a checkpoint under
`texts/rawg/`. The global overview aggregates only verified partitions. It includes
completeness, numerical distributions, descriptive IQR extremes, recurring issues,
before/after text indicators and selected distinct-game review samples. Numerical
extremes are not automatically treated as errors. Counts describe captures rather
than independent games. Errors prevent publication of a new partial global overview.

Quality version 5 regenerates older reports in English and includes a review-registry
hash in checkpoints. [The documented qualitative assessment](../../docs/quality/README.md)
contains eight exact source/cleaned comparisons from the 50-game backup. A matching
source hash, game ID and clean-text hash are required before its assessment can be
attached to a report. This is an assisted sample review, not human certification
of the whole dataset. New or changed content remains pending. Source data is
preserved in its original language. Changing the registry invalidates report reuse.

## Images

```powershell
python -m src.trusted.images.build_trusted_images --csv_output reports/images/quality.csv
python -m src.trusted.images.quality_report --csv_input reports/images/quality.csv --output_dir reports/images/analysis
```

Metrics cover dimensions, brightness, contrast, sharpness, entropy, letterboxing
and exact/perceptual duplicates. Corrupt, too-small, blank and duplicate images
are rejected; other flags are advisory. Accepted bytes are copied without
model-specific resizing. Rule thresholds are heuristics, not certified perceptual
judgments. Analysis produces distributions, group-specific rejection rates,
summary tables and review candidates. A checkpoint restores a missing local CSV
on unchanged runs. Active Trusted images synchronize with the current accepted
set by default (`--prune`); originals remain in Persistent and Formatted. Obsolete
copies are removed only after accepted replacements verify successfully, and the
final active listing must match the accepted set. `--no-prune` is a diagnostic
opt-out that can retain rejected files and should not be used for delivery.
The image indexer and orchestrator also prune retired image vectors.

`publish_provenance` verifies the source image manifest against original Persistent
bytes and canonical Formatted/Trusted JPEGs. It writes `provenance/images.jsonl`
outside the image prefix, associating exact keys/ETags with RAWG IDs. The global
orchestrator invokes it after quality processing; arbitrary filenames never
supply guessed game IDs.

## Audio

```powershell
python -m src.trusted.audio.analyze_audio_quality --report reports/audio/quality.csv
python -m src.trusted.audio.trusted_audio --quality-report reports/audio/quality.csv --report reports/audio/processing.csv
python -m src.trusted.audio.audio_quality_report --report reports/audio/quality.csv --output-dir reports/audio/analysis
```

Full audio is decoded for amplitude, silence, DC offset and technical validity.
Invalid and exact byte-duplicate files are excluded. Advisory flags such as
near-full-scale are retained for review; they do not prove clipping or justify
deletion. DC subtraction is applied only when whole-file peak/headroom checks
support it, followed by independent output verification. Other accepted files are
copied. Metadata preserves source revisions, hashes and processing decisions.
The analysis report describes quality before cleaning; the processing CSV and
output metadata record actual corrections and verification outcomes.

Tracked Trusted derivatives can be refreshed after verified Formatted content
changes. Before replacement the old derivative's stored hash is checked; damaged
or untracked objects still stop automatic refresh. Originals remain immutable.

## Qualitative media evidence

[The media assessment](../../docs/quality/media_assessment.md) records eight image
comparisons and four audio comparisons, full-file measurements, model-assisted
observations and their limitations. Reports attach the documented sample notes
only when the current Formatted SHA-256 matches the reviewed content. Unknown
content remains unreviewed; numerical thresholds do not certify semantic quality.

Reproduce the measurements from the verified offline snapshot with FFmpeg on PATH:

```powershell
python -m src.trusted.media_sample_review
```

Outputs are local reconstructions under `reports/media_quality/`, not changes to
MinIO. `--assess-with-gemini` additionally sends selected image pairs and bounded
audio excerpts to the configured Gemini service. It is optional, uses the server
credentials and may incur provider usage; no AI request is added to normal batch
processing. Existing reviewed records are preserved in `docs/quality/`.

No Trusted process generates embeddings. Rebuild/revalidate the corresponding
indexes after changed Trusted bytes; see [Exploitation](../exploitation/README.md).
