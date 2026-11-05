"""Reconstruct and compare audio samples using the production audio rules."""

import re
import subprocess

import numpy as np

from src.formatting.audio.formatting_audio import convert_audio
from src.pipelines.backup_sources import jsonl
from src.trusted.audio.analyze_audio_quality import classify, inspect_signal, probe_flac
from src.trusted.audio.trusted_audio import correct_dc, select_method
from src.trusted.review_support import assess_samples, difference, select_samples, sha


def pcm(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                             "-map", "0:a:0", "-f", "f32le", "-c:a", "pcm_f32le", "-"],
                            capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype="<f4")


def clip_montage(source, duration, output):
    """Three labelled-in-report ten-second excerpts: opening, middle and ending."""
    starts = sorted({0.0, max(0.0, (duration - 10) / 2), max(0.0, duration - 10)})
    chunks = []
    for start in starts:
        result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(source),
                                 "-t", "10", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                                capture_output=True, check=True)
        chunks.append(result.stdout)
    import wave
    with wave.open(str(output), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"".join(chunks))
    return starts


def audio_evidence(root, out):
    rows, paths = [], {}
    for index, source in enumerate(jsonl(root / "audio/manifests/files.jsonl")):
        path = root / "audio" / source["local_path"].replace("\\", "/")
        formatted = out / f"audio_{index}.flac"
        status, info, bits, note = convert_audio(path, formatted, overwrite=True)
        if status != "converted":
            raise ValueError("Expected a newly verified local FLAC conversion")
        properties = probe_flac(formatted)
        metrics = inspect_signal(formatted, properties["duration_seconds"])
        row = {"sample_id": str(index), "source_path": path.relative_to(root).as_posix(),
               "source_sha256": sha(path.read_bytes()), "formatted_sha256": sha(formatted.read_bytes()),
               "duration_seconds": properties["duration_seconds"], "source_codec": info.codec,
               "sample_rate_hz": properties["sample_rate_hz"], "channels": properties["channels"],
               "source_sample_rate_hz": info.sample_rate, "source_channels": info.channels,
               "output_bits": bits, "conversion_note": note, "status": "ok", "duplicate_of": "",
               **metrics}
        classify(row)
        method, reason = select_method({key: str(value) for key, value in row.items()})
        trusted = formatted
        if method == "dcshift":
            trusted = out / f"audio_{index}_trusted.flac"
            correct_dc(formatted, trusted, {key: str(value) for key, value in row.items()})
        row.update(method=method, reason=reason, trusted_sha256=sha(trusted.read_bytes()))
        original_pcm, formatted_pcm = pcm(path), pcm(formatted)
        gain_match = re.search(r"applied gain=([-0-9.]+) dB", note)
        row["conversion_gain_db"] = float(gain_match.group(1)) if gain_match else 0.0
        row["source_peak_linear"] = float(np.abs(original_pcm).max(initial=0))
        row["formatted_pcm_difference"] = difference(original_pcm, formatted_pcm)
        row["gain_aligned_pcm_difference"] = difference(original_pcm * 10 ** (row["conversion_gain_db"] / 20), formatted_pcm)
        row["trusted_pcm_difference"] = difference(pcm(formatted), pcm(trusted))
        rows.append(row)
        paths[row["sample_id"]] = (path, trusted)
        print(f"Reviewed audio {index + 1}/16", flush=True)
    priorities = [row for row in rows if row["conversion_gain_db"] != 0]
    priorities += [row for row in rows if row["formatted_pcm_difference"]["identical"]][:1]
    priorities += [row for row in rows if not row["formatted_pcm_difference"]["identical"] and row["conversion_gain_db"] == 0][:1]
    samples = list({row["sample_id"]: row for row in priorities + select_samples(rows, ["duration_seconds", "dc_offset"], 4)}.values())[:4]
    assets = []
    for row in samples:
        pair = []
        for name, path in zip(("source", "trusted"), paths[row["sample_id"]]):
            clip = out / f"audio_{row['sample_id']}_{name}_excerpts.wav"
            row["excerpt_starts_seconds"] = clip_montage(path, row["duration_seconds"], clip)
            pair.append(clip)
        assets.append((row, pair))
    return rows, assets


def assess_audio(assets):
    """Assess only the selected source/Trusted audio excerpts."""
    return assess_samples("audio", assets, "audio/wav")
