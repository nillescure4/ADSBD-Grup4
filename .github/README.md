# Continuous integration and third-person execution

The repository must be accessible to the assessor. Private repository access must
be granted through GitHub; no production credentials are committed. Clone the
repository, check out the branch or commit being assessed, and follow the root
[installation instructions](../README.md). Python 3.12, Docker with Compose and
FFmpeg/FFprobe are the documented host prerequisites.

## Automatic checks

Both workflows run on pushes and pull requests:

- **Automated tests:** installs the project on Ubuntu, runs the default test suite
  and checks the complete orchestration plan. External-service tests are explicitly
  skipped in this suite.
- **Backup end-to-end pipeline:** installs CPU dependencies on a fresh Ubuntu
  runner, starts disposable MinIO storage and processes the real versioned
  **50-description / 50-image / 16-audio** backup. Source failures are deliberately
  simulated to exercise normal backup recovery. Models are downloaded from their
  pinned revisions; no prebuilt vectors or processed data are supplied. The test
  verifies all zones, all four Chroma collections, eight retrieval probes and the
  application's catalogue counts. Separate real-service cases check retired images
  and legacy-audio upgrades. The job allows 60 minutes and uploads its diagnostics
  as **backup-pipeline-reports**, retained for 14 days.

The end-to-end job requires outbound access to package registries, the container
registry and Hugging Face. It needs no RAWG/Gemini API key and uses disposable CI
MinIO credentials. Storage is deleted even after a test failure. Cancelling a
superseded run prevents duplicate full pipeline jobs for the same branch.

To inspect a result, open the repository's **Actions** tab, select the workflow,
and check the run for the exact commit under assessment. A configuration file or
a local passing test is not proof of hosted success. Download the job artifacts
for the run report, verified collection counts, test results and storage logs.
For a pull request, both jobs should pass before review/merge.

## Optional live-source check

**Live end-to-end pipeline** is manually dispatched from Actions after its
workflow is available on the default branch. Configure the repository secret
`RAWG_API_KEY`, choose the branch, and run it. This job disables backup recovery:
an API outage is a real failure of this live-source check, not evidence of a broken
backup pipeline. It is intentionally separate from automatic acceptance checks.

## Generative-service boundary

The automatic jobs verify preparation and retrieval without making billable
generative requests. TASK 3 additionally needs the provider/model/key described
in [the application guide](../src/app/README.md). Its optional real-provider test
is documented in [integration tests](../tests/integration/README.md). Provider
availability and question-grounding quality remain separate from successful
ingestion, indexing or a green offline test suite.
