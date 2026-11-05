# Qualitative assessment of images and audio

## Scope and reproducibility

This assessment uses the exact 50 RAWG gameplay screenshots and 16 OpenGameArt
music originals in the verified backup. It complements the eight text comparisons
in [the text assessment](README.md). The [machine-readable evidence](media_sample_reviews.json)
records source paths, source/Formatted/expected Trusted SHA-256, full-file
measurements, sample selection and assisted observations. It is not the separate
submission memo or a human certification of all current/future data.

`python -m src.trusted.media_sample_review` reconstructs outputs with the actual
production converters and quality rules, without changing MinIO. Its image and
audio review stages are implemented independently in `src/trusted/images/sample_review.py`
and `src/trusted/audio/sample_review.py`; the entrypoint only coordinates them.
Selected image
pairs and audio excerpts were also assessed using the configured Gemini model;
observations were checked/reworded against measured facts. A model impression is
not a measured retrieval score. This was not a blind review: filenames and
technical metadata were supplied and may bias style/instrument interpretations.
Hash-matching notes appear in subsequent normal
quality reports; changed/unmatched content and changed processing policies stay
unreviewed. Recorded policy versions/thresholds and the FFmpeg version identify
the processing context. Regeneration with
different codecs or library versions can change hashes and require renewed review.

## Images: content, style and conversion effects

The complete 50-image sample passes the current hard rules. This describes this
snapshot, not the expected acceptance rate of arbitrary new images. The source
and decoded RGB JPEG have identical dimensions; conversion applies orientation
before dropping metadata and does not resize or crop. JPEG recompression changes
pixels: PSNR ranges from **40.09 to 57.14 dB**. These numbers describe numerical
differences, not a perceptual acceptance threshold.

Eight distinct images cover brightness/sharpness/edge-darkness/conversion-error
extremes plus deterministic fill. Source/converted previews were compared side
by side; Gemini supplied additional assisted observations. No claim is made that
thumbnail inspection detects every full-resolution artifact.

| RAWG ID | Concrete interpretation | Consequence for cleaning and tasks |
|---|---|---|
| 422 | Pixel-art cave with large dark regions, a lit area and HUD. | Darkness is meaningful art direction. Keep it; a dark edge can be gameplay, not a letterbox. |
| 4252 | Bright white rooftops, skyline, sharp geometric edges. | High brightness does not by itself establish harmful overexposure. Geometry remains useful for visual retrieval. |
| 10754 | Lighthouse silhouette against a dark cloudy sky. | Sparse light and soft distant detail fit a night scene. Do not brighten or sharpen it automatically. |
| 5679 | Character and shield facing a wing in a snowy mountain scene. | Dense textures and action composition remain; small compression differences do not establish unchanged retrieval relevance. |
| 10035 | Suited character, large building and dusk shadows. | Preserve composition and lighting; hidden shadow details cannot be inferred or restored reliably. |
| 1030 | Monochrome silhouette and giant spider-like limb. | Low texture is an intentional aesthetic, not equivalent to blank content. Contrast preserves useful shapes. |
| 17822 | Rural buildings, characters and green/brown outdoor textures. | Older source texture resolution is not repaired by JPEG conversion. Preserve it rather than inventing detail. |
| 28 | Western clothing, firearms, muzzle flash and smoke. | Composition retains genre/style cues; one action frame does not represent an entire game's visual range. |

Accepted Trusted images are byte copies of Formatted outputs, so Trusted adds no
further image compression. There are no naturally rejected examples in this
fixed sample: it cannot validate the perceptual appropriateness of every hard
threshold. Tests separately exercise corrupt, small, blank and duplicate cases.
Near-duplicate pHash decisions can merge meaningfully different gameplay frames;
review candidates and their representatives before interpreting a rejection as
semantic redundancy. Resolution and contrast rules can disproportionately exclude
older games, pixel art or minimal compositions. Metric-group plots expose some
selection effects but do not establish demographic or genre fairness.

The active Trusted image set and image embeddings now remove retired decisions
after accepted replacements verify. Rejected originals remain in Persistent and
Formatted. This prevents a newly identified duplicate from remaining searchable
merely because it was accepted in an earlier run.

## Audio: distinguish conversion from cleaning

All 16 originals were decoded and compared over their complete sample sequences,
not just excerpts. Rate, channel count and decoded shape are preserved. Four
PCM16 WAV sources retain identical decoded samples through formatting. The
other 12 sources are lossy MP3/Vorbis: integer FLAC preserves neither the source
container bytes nor its floating-point decoder output exactly. Existing lossy
artifacts cannot be undone by converting to FLAC.

The initial comparison found a real defect: decoded samples in `cave%20themeb4.ogg`
and `enchanted%20tiki%2086_0.mp3` exceeded integer full scale. Direct conversion
clamped peaks, with maximum sample errors of approximately **0.08115** and
**0.02220** respectively. A near-full-scale flag alone did not reveal that loss;
the full before/after comparison did.

Audio format policy v2 measures decoded float peaks before quantization. It
applies **-0.777759 dB** and **-0.290447 dB** respectively, leaving approximately
0.1 dB headroom. Other inputs receive no gain. This is constant attenuation to
avoid integer clipping, not loudness/true-peak normalization or dynamic compression.
After accounting for the recorded gain, the maximum complete-signal residual is
**0.0000152588**, consistent with half a 16-bit quantization step. Rate/channels
and duration are not changed. Absolute level changes may still matter to another
analytical task, which is why originals and conversion notes are retained.

All 16 reconstructed Trusted decisions are `copy`: no duplicate exclusion or DC
subtraction is justified by this sample's measured evidence. Formatted-to-Trusted
bytes and PCM therefore remain identical. This does not validate the perceptual
effects of DC subtraction on arbitrary future audio; separate controlled tests
verify DC improvement and essential signal properties.

Four assisted listening comparisons use opening, middle and ending excerpts of
up to ten seconds each. The model input is mono 16 kHz, while full-file numerical
comparison retains original channels/rate. Excerpt joins are artificial review
montage boundaries, not evidence of glitches in the source.

| Original | Assisted content impression | Evidence and task implications |
|---|---|---|
| `3922/cave%20themeb4.ogg` | Atmospheric pads, bass and sparse/echoing musical elements. | Peak-safe attenuation preserves measured shape and the excerpt's atmospheric cues; full stereo/high-frequency perception is not certified. |
| `49460/enchanted%20tiki%2086_0.mp3` | Upbeat plucked/percussive tropical-style instrumental texture. | Correct the decoded overshoot without flattening dynamics. The slightly lower level is documented; mood suitability remains subjective. |
| `4148/b423b42.wav` | Short pulsing low-frequency rhythmic/ambient loop. | Complete decoded PCM is identical. A short loop is valid music material; repetition is not automatically a defect or duplicate. |
| `18888/song18_0.mp3` | Synthesized retro/chiptune melody and arpeggios. | No gain change; small quantization differences are measured. Original MP3 artifacts remain, and this observation does not prove a retrieval-quality score. |

For music-atmosphere retrieval, these observations support retaining intentional
silence, repetition and timbre rather than applying speculative denoising or
volume normalization. CLAP uses a centered ten-second excerpt in Exploitation:
intro/outro and internal changes can be absent from that vector even though
Trusted retains the whole track. An excerpt may not represent a long changing
piece; this is an analytical representation limitation, not a reason to delete
parts of the original. Music is independently sourced and is never asserted to
be a RAWG game's actual soundtrack.

## Limits and ongoing review

RAWG selection by popularity and one screenshot per game is not representative
of all genres or scenes. Music is restricted to the collector's CC0 selection.
Neither sample establishes cultural coverage, legal validity beyond recorded
licence evidence, factual correctness, or human preference. Models may mislabel
instruments/styles or miss artifacts. Complete waveform comparisons constrain
claims about conversion but do not replace human perceptual evaluation.

Fresh content and changed converters/rules require new sample assessments.
Automatic reports continue to identify warnings and unknown content without
transferring a prior assessment to a different hash. Local sample reconstructions
and the isolated MinIO end-to-end test are distinct evidence: the former supports
quality interpretation, while the latter verifies publication, synchronization
and operational retrieval.
