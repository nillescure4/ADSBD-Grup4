# Architecture and provenance

The management backbone uses three object-store buckets and one embedded vector
database. [storage.yml](../config/storage.yml) configures the MinIO buckets;
`ADSBD_STORAGE_CONFIG` can select an isolated YAML configuration for a run.

| Zone | Storage | Responsibility |
|---|---|---|
| Temporal Landing | `1.landing-zone/temporal/` | Original files awaiting verified promotion |
| Persistent Landing | `1.landing-zone/persistent/` | Immutable originals with source, identity and date in their names |
| Formatted | `2.formatted-zone` | RAWG Parquet, RGB JPEG images and canonical FLAC audio |
| Trusted | `3.trusted-zone` | Generic cleaning, conservative quality decisions, provenance and analyses |
| Exploitation | `data/exploitation/chroma` | Single-modality collections in aligned Jina or CLAP spaces |

Collectors fetch original material. Separate modality scripts perform each zone
transition. Image/game provenance is explicitly verified against the collector
manifest and Persistent/Formatted/Trusted bytes, not inferred from arbitrary
filenames. A separate orchestrator calls the scripts in order and stops on a
zone error. Source errors may select a validated offline snapshot before any
selected text is uploaded. This is resumable processing, not a cross-zone transaction.

Text manifests record UTC capture date, raw SHA-256 and row-to-source mapping.
Images retain source URLs, game associations and byte hashes. Audio keys preserve
OpenGameArt submission identity; manifests preserve author and licence metadata.
Embedding metadata identifies exact Trusted objects, model revisions and vector
configuration. Originals remain available for rebuilding derived outputs.

New Formatted image names append `.jpg` to the complete Persistent filename:
`example.png` and `example.jpg` become `example.png.jpg` and `example.jpg.jpg`.
Existing legacy stem-only outputs are not silently deleted; see the migration
notes in [Formatting](../src/formatting/README.md).

TASK 1 performs searches within one modality. TASK 2 uses Jina for text/image and
CLAP for text/audio, returning mixed categories for text queries. TASK 3 retrieves
eight game candidates, requests a content/visual question tree from a configured
generative model, and narrows to one recommendation through three choices,
including a final music preference. Audio is independent of actual game soundtracks.

MinIO's web console is for inspection. Bucket creation, transfers, quality
processing and database preparation are scripted. No GPU or vector-server
container is required. See [operations](../src/pipelines/README.md) for setup.
