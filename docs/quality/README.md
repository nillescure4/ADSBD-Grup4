# Documented text quality assessment

This is technical review evidence for the repository, not the separate submission
memo. Eight distinct games were selected from the current 50-game offline sample
using the quality reporter's selection order (issues/changes first, then distinct
games). All selected source text was compared with its complete cleaned output,
including content beyond the 2000-character report excerpts.

The review is Codex-assisted, not a claim of human certification. The machine-
readable [registry](text_sample_reviews.json) records the game ID, raw response
SHA-256, cleaned UTF-8 SHA-256, review date, reviewer and source-specific notes.
Reports attach those notes only if both hashes and the ID match. New or changed
content remains pending. A documented sample does not approve every row or every
future capture. The registry digest participates in Trusted checkpoints.

| Game ID | Assessment |
|---|---|
| 3498 | English and Spanish sections and their statement order remain. Only whitespace changes. The full text was checked despite excerpt truncation. |
| 4200 | Five paragraphs retain plot, portal/gel mechanics, co-op and editor information. |
| 4291 | Team objectives, equipment and cosmetics statements retain wording; CRLF becomes LF without losing the source's single line boundaries. |
| 10035 | Both paragraphs retain the Providence story and player-choice mechanics; source grammar is not rewritten. |
| 10754 | Rapture setting, plasmid/economy mechanics and remaster statement remain across two paragraphs. |
| 11936 | Multiplayer context, game modes and their rules retain wording and line boundary. |
| 11973 | Plot and Nemesis-system mechanics remain; spelling inconsistencies in the source are preserved. |
| 12020 | Campaign, AI Director, mods and competitive-mode statements retain ordering and line boundaries. |

For these eight exact pairs, collapsing whitespace yields identical complete
source/output token sequences. This supports preservation of their wording,
punctuation, casing and languages, but does not establish that publisher statements
are true, up to date or representative of all games. No selected backup sample
requires the HTML fallback path; separate synthetic tests cover HTML/script/style
removal and empty/malformed content. Numerical profiles, image quality charts and
audio flags complement this text review; they are not semantic truth detectors.

Thresholds, missing metadata, duplicate handling, source selection bias and the
small sample remain limitations. Re-review changed content instead of copying
approval status by game ID alone. Source descriptions stay in their original
language even though interfaces, reports, comments and documentation are English.
