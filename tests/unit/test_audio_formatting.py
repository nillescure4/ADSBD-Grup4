"""Unit tests for the local audio formatting rules."""

import pytest

from src.formatting.audio.formatting_audio import AudioInfo, choose_flac_bits, destination_key_for


def make_audio_info(codec: str, bit_depth: int | None) -> AudioInfo:
    """Build a small FFprobe-like value without accessing audio files."""
    return AudioInfo(
        codec=codec,
        sample_rate=44100,
        channels=2,
        bit_depth=bit_depth,
        sample_format=None,
        duration_seconds=90.0,
    )


@pytest.mark.parametrize(
    ("codec", "source_bits", "expected_bits"),
    [
        ("mp3", None, 16),
        ("vorbis", None, 16),
        ("pcm_s16le", 16, 16),
        ("pcm_s24le", 24, 24),
        ("flac", 16, 16),
        ("flac", 24, 24),
        ("pcm_f32le", 32, 24),
        ("pcm_s32le", 32, 24),
    ],
)
def test_bit_depth_rules(codec: str, source_bits: int | None, expected_bits: int) -> None:
    assert choose_flac_bits(make_audio_info(codec, source_bits)) == expected_bits


def test_unsupported_float_precision_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported precision"):
        choose_flac_bits(make_audio_info("pcm_f64le", 64))


def test_pcm32_conversion_requires_no_peak_scan(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from src.formatting.audio import formatting_audio as audio

    def no_peak_scan(path):
        raise AssertionError("Integer PCM should not be scanned for float peaks")

    monkeypatch.setattr(audio, "inspect_float_peak_dbfs", no_peak_scan)
    bits, filters, note = audio.flac_conversion_settings(
        tmp_path / "source.wav", make_audio_info("pcm_s32le", 32)
    )
    assert bits == 24
    assert filters == []
    assert "precision loss" in note


def test_float32_peak_below_full_scale_needs_no_gain(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from src.formatting.audio import formatting_audio as audio

    monkeypatch.setattr(audio, "inspect_float_peak_dbfs", lambda _: -2.5)
    bits, filters, note = audio.flac_conversion_settings(
        tmp_path / "source.wav", make_audio_info("pcm_f32le", 32)
    )
    assert bits == 24
    assert filters == []
    assert "no gain applied" in note


def test_float32_over_full_scale_is_attenuated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from src.formatting.audio import formatting_audio as audio

    monkeypatch.setattr(audio, "inspect_float_peak_dbfs", lambda _: 3.5)
    bits, filters, note = audio.flac_conversion_settings(
        tmp_path / "source.wav", make_audio_info("pcm_f32le", 32)
    )
    assert bits == 24
    assert filters == ["-af", "volume=-3.600000000dB:precision=float"]
    assert "prevent clipping" in note


@pytest.mark.parametrize("codec", ["mp3", "vorbis", "aac", "opus"])
def test_lossy_decoder_overshoot_gets_measured_headroom(monkeypatch, tmp_path, codec):
    from src.formatting.audio import formatting_audio as audio
    monkeypatch.setattr(audio, "inspect_float_peak_dbfs", lambda _: 0.7)
    bits, filters, note = audio.flac_conversion_settings(tmp_path / "source", make_audio_info(codec, None))
    assert bits == 16
    assert filters == ["-af", "volume=-0.800000000dB:precision=float"]
    assert "source peak=0.700000" in note
    assert "prevent clipping" in note


def test_lossy_below_full_scale_keeps_gain(monkeypatch, tmp_path):
    from src.formatting.audio import formatting_audio as audio
    monkeypatch.setattr(audio, "inspect_float_peak_dbfs", lambda _: -2.)
    bits, filters, note = audio.flac_conversion_settings(tmp_path / "source", make_audio_info("mp3", None))
    assert bits == 16 and filters == []
    assert "no gain applied" in note


def test_original_extension_is_kept_in_output_name() -> None:
    """Formatted keys must be unique across codecs with identical stems."""
    mp3 = destination_key_for("persistent/audio/opengameart$music$18-10-2026$123$song.mp3", "persistent")
    wav = destination_key_for("persistent/audio/opengameart$music$18-10-2026$123$song.wav", "persistent")
    assert mp3 == "audio/opengameart$music$18-10-2026$123$song.mp3.flac"
    assert wav == "audio/opengameart$music$18-10-2026$123$song.wav.flac"
    assert mp3 != wav
