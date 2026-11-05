"""Small real-FFmpeg checks for the agreed 32-bit audio conversion policy."""

from __future__ import annotations

import shutil
import re
import subprocess
from pathlib import Path

import pytest

from src.formatting.audio.formatting_audio import (
    convert_audio,
    inspect_float_peak_dbfs,
    probe_audio,
)


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg and FFprobe are required for media integration tests",
)


def create_wave(tmp_path: Path, codec: str, *, loud: bool = False) -> Path:
    """Generate a tiny source WAV for testing without distributing media files."""
    output = tmp_path / f"{codec}{'-loud' if loud else ''}.wav"
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2:sample_rate=48000",
    ]
    if loud:
        command += ["-af", "volume=12"]
    command += ["-c:a", codec, str(output)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return output


def measure_output_peak_dbfs(path: Path) -> float:
    """Read the FLAC peak from FFmpeg astats after integer decoding."""
    completed = subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
        "-i", str(path), "-af", "astats=metadata=0:reset=0", "-f", "null", "-",
    ], check=True, capture_output=True, text=True)
    peaks = re.findall(r"Peak level dB:\s*([^\s]+)", completed.stderr)
    assert peaks
    return float(peaks[-1])


def test_pcm32_is_converted_to_24_bit_flac(tmp_path: Path) -> None:
    """Converting PCM32 does not resample, downmix or shorten the track."""
    source = create_wave(tmp_path, "pcm_s32le")
    destination = tmp_path / "pcm32.flac"
    status, original, bits, note = convert_audio(source, destination)

    assert status == "converted"
    assert original is not None and original.codec == "pcm_s32le"
    assert bits == 24
    assert "precision loss" in note
    formatted = probe_audio(destination)
    assert formatted.codec == "flac"
    assert formatted.bit_depth == 24
    assert formatted.sample_rate == original.sample_rate
    assert formatted.channels == original.channels
    assert formatted.duration_seconds == pytest.approx(original.duration_seconds, abs=0.02)


def test_float32_in_range_is_converted_without_gain(tmp_path: Path) -> None:
    """In-range float samples must not be unnecessarily attenuated."""
    source = create_wave(tmp_path, "pcm_f32le")
    destination = tmp_path / "float32.flac"
    status, original, bits, note = convert_audio(source, destination)

    assert status == "converted"
    assert original is not None and original.codec == "pcm_f32le"
    assert bits == 24
    assert "no gain applied" in note
    assert probe_audio(destination).bit_depth == 24


def test_float32_over_range_is_attenuated_before_quantization(tmp_path: Path) -> None:
    """A source above 0 dBFS is attenuated with headroom, avoiding clipping."""
    source = create_wave(tmp_path, "pcm_f32le", loud=True)
    destination = tmp_path / "float32-over.flac"
    assert inspect_float_peak_dbfs(source) > 0.0

    status, original, bits, note = convert_audio(source, destination)

    assert status == "converted"
    assert original is not None
    assert bits == 24
    assert "applied gain" in note
    assert probe_audio(destination).bit_depth == 24
    assert measure_output_peak_dbfs(destination) == pytest.approx(-0.1, abs=0.01)


@pytest.mark.parametrize("relative", [
    "3922/cave%20themeb4.ogg", "49460/enchanted%20tiki%2086_0.mp3",
])
def test_real_lossy_overshoots_are_not_clipped(tmp_path, relative):
    import math
    import numpy as np
    from src.common.config import PROJECT_ROOT
    from src.trusted.media_sample_review import pcm, difference
    source = PROJECT_ROOT / "backup_data/audio/raw/files" / relative
    output = tmp_path / "converted.flac"
    _, info, bits, note = convert_audio(source, output)
    gain = float(re.search(r"applied gain=([-0-9.]+) dB", note)[1])
    assert gain < 0 and bits == 16
    residual = difference(pcm(source) * math.pow(10, gain / 20), pcm(output))
    assert residual["max_absolute_error"] < 2 / 65536
    assert measure_output_peak_dbfs(output) == pytest.approx(-0.1, abs=0.01)
