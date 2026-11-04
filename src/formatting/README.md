# Formatted Zone

These independent scripts read Persistent Landing and write canonical objects
to the configured Formatted bucket. They never modify original source bytes.
Install dependencies from the repository root with `python -m pip install -e ".[dev]"`.

## Text: RAWG records to Parquet

```powershell
python -m src.formatting.text_to_formatted_zone --dry-run
python -m src.formatting.text_to_formatted_zone
```

`text_input_formats.py` accepts JSON, JSONL/NDJSON, CSV/TSV, TXT, Markdown and HTML.
Each file represents one game/date and must follow the Persistent filename and
manifest identity/hash contract. JSON contains one object; JSONL contains exactly
one nonblank record. CSV/TSV contains one header and one row, uses JSON for typed
or nested values and `\N` for null. Plain text/Markdown populates description_raw;
HTML populates description. Inputs are UTF-8 with optional BOM. Duplicate fields,
inconsistent IDs and malformed values fail validation before a partition is written.

The base schema has 54 RAWG fields with stable nullable types. Unknown fields are
retained as JSON text. No provenance columns are added. HTML, whitespace, source
languages and business values are retained for Trusted to assess. Output is
`texts/rawg/collection_date=<UTC-date>/game_details.parquet`. Adjacent manifests
record all source hashes, output hash and exact row_provenance. Unchanged runs
skip writes; changed or missing processed originals do not silently replace history.

## Images: supported media to JPEG

```powershell
python -m src.formatting.images.persistent_to_formatted --dry-run
python -m src.formatting.images.persistent_to_formatted
```

JPEG, PNG, WebP, BMP, TIFF and GIF are converted to RGB JPEG, with EXIF orientation
applied, white transparency backgrounds and first-frame selection for multi-frame
sources. The output key appends `.jpg` to the full input filename: `x.png` becomes
`x.png.jpg` and `x.jpg` becomes `x.jpg.jpg`. This injective mapping prevents two
different input extensions from silently sharing one destination. The provenance
publisher uses the same key function and image metadata parsing supports both
new and legacy names. Unsupported extensions are ignored; undecodable image bytes
are copied to `quarantine/images/`. Infrastructure errors fail the stage.

Old stem-only outputs are not silently removed or renamed. When upgrading an
existing store, new canonical keys may coexist with legacy derived objects.
Rebuild/validate an isolated store before switching an existing deployment;
review any Trusted pruning explicitly. Persistent originals are always retained.

## Audio: playable formats to FLAC

```powershell
python -m src.formatting.audio.formatting_audio --report reports/audio/formatting.csv
```

The formatter probes playable inputs rather than assuming codec from extension.
Supported sources include AAC, AIFF, AU, FLAC, M4A, MP3, OGA/OGG, Opus, WAV and WMA.
FFmpeg converts them to FLAC, preserving sample rate and channel count. Output
precision is 16 or 24 bits according to the source; integer PCM32 and float32
conversion to 24 bits can lose precision and is documented in the report. Float32
peaks above 0 dBFS receive measured attenuation to avoid clipping.
Output retains the complete
original filename before `.flac`, avoiding extension collisions. Source hashes,
converter settings and verified output metadata support incremental reuse.
The report records successful, skipped and failed inputs. Tracker/MIDI source
files and archives are outside this playable-audio formatting contract.

See [the data dictionary](../../docs/data_dictionary.md) and [Trusted](../trusted/README.md)
for the next stage. Run one writer per bucket set.
