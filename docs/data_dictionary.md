# Data dictionary

## Sources

| Dataset | Source and collection | Original representation | Identity |
|---|---|---|---|
| RAWG descriptions | Game-detail API, using the image collector's game manifest | UTF-8 JSON responses | RAWG integer game ID and UTC capture date |
| RAWG screenshots | Game screenshots API and original media downloads | Original JPEG/PNG/WebP bytes | Collector asset ID, URL and SHA-256 |
| OpenGameArt music | Robots-aware Music + CC0 HTML crawler | Original playable audio (for example MP3, OGG or WAV) | Submission ID and original filename |

The fixed offline snapshot contains 50 matched game descriptions/screenshots and
16 music files. It preserves the actual source data and capture dates. Its full
inventory and hashes are in [the backup manifest](../backup_data/manifest.json).

## RAWG table fields

Formatted's base schema contains 54 RAWG fields; future unknown fields are retained
as JSON text, so the number of columns is not a permanent hard limit. Integer
fields such as `id`, `metacritic`, `playtime` and counts use nullable Arrow int64;
`rating` uses float64 and `tba` uses boolean. Descriptive fields such as `name`,
`slug`, `released`, `updated`, `description` and `description_raw` remain strings.
Nested values such as `genres`, `platforms`, `developers`, `publishers` and `stores`
are reversible JSON strings, identified by Parquet metadata `json_encoded_fields`.
The authoritative field sets are in `src/formatting/text_to_formatted_zone.py`.

| Trusted field or group | Meaning and processing |
|---|---|
| `id` | RAWG game identifier; invalid IDs are flagged and cannot be embedded |
| `name`, `slug` | Original business identifiers/names |
| `description_clean` | Cleaned description_raw, falling back to cleaned HTML description |
| `rating`, `metacritic`, `playtime` | Source measures retained without speculative imputation or normalisation |
| `released`, `updated` | Source date/timestamp strings, checked for parseability |
| Nested business fields | Reversible JSON data, not exploded into task-specific tables in Trusted |

Trusted replaces `description` and `description_raw` with `description_clean`,
giving 53 fields for the base schema. It adds no diagnostic columns. Flags,
selected source field, row indices, capture dates, input keys and hashes are in
the manifests and quality reports. Full original descriptions remain in Landing
and Formatted. Exact full-row duplicates are removed within a capture date;
historical captures from different dates remain independent snapshots.

## Naming and canonical media

| Modality | Persistent name | Formatted / Trusted |
|---|---|---|
| Text | `rawg$game_details_<id>$<dd-mm-yyyy>.<ext>` under `persistent/texts/rawg/` | `texts/rawg/collection_date=<yyyy-mm-dd>/game_details.parquet` |
| Image | `rawg$<dataset>$<original_stem>$<dd-mm-yyyy>.<ext>` under `persistent/images/` | `images/<complete_persistent_filename>.jpg` |
| Audio | `opengameart$music$<dd-mm-yyyy>$<submission>$<filename>` under `persistent/audio/` | `audio/<complete_persistent_filename>.flac` |

Images use RGB JPEG with EXIF orientation applied and transparent pixels on white;
multi-frame sources use the first frame. Audio uses FLAC, preserving source sample
rate and channel count, with 16- or 24-bit precision selected from the source.
See [Formatting](../src/formatting/README.md) for accepted inputs and conversion limits.
Decoded floating-point peaks from lossy codecs or float PCM above full scale are
attenuated with measured 0.1 dB headroom before integer FLAC conversion. Other
files retain their gain. The conversion note records the measured peak and gain.

Image hard-rejection reasons include corrupt, too-small, blank and duplicate
content; blur/darkness/brightness/letterbox flags are advisory. Text flags cover
identity, provenance, missing/short descriptions, date/JSON validity and nonfinite
numbers. Audio flags cover technical amplitude/silence/DC indicators; duplicate
and invalid files are excluded, and DC correction requires verified headroom.
Thresholds are heuristics; metric values alone do not certify semantic quality.
Active Trusted images and their image embeddings retire obsolete quality decisions
by default. Full originals remain available in Landing and Formatted. Sample-scoped
[qualitative evidence](quality/media_assessment.md) accompanies the numerical reports.

## Embedding collections

| Collection | Content | Dimensions / limit |
|---|---|---|
| `texts_jina_clip_v2` | Full Trusted description per game/capture | 1024; at most 8192 tokens |
| `images_jina_clip_v2` | One vector per Trusted image | 1024; model RGB preprocessing |
| `texts_clap` | Latest description per game | 512; one prefix of at most 512 tokens |
| `audio_clap_center10_v1` | One centered audio excerpt per Trusted file | 512; up to ten seconds |

All use cosine distance and L2 normalisation. Model IDs, revisions, dimensions,
configuration fingerprints and source object identities are stored as metadata.
Jina and CLAP vectors cannot be compared directly. Source data stays unchanged
when an embedding representation truncates or selects an excerpt.
