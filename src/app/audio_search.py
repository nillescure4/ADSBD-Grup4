"""Verified CLAP audio retrieval and disposable uploaded queries."""

from io import BytesIO
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import wave

import numpy as np

from src.exploitation.audio.embedding_audio import (
    DEFAULT_CHROMA_PATH, DIMENSION, MODEL_ID, MODEL_REVISION, SAMPLE_RATE,
    decode_excerpt, probe_duration, revision_signature, select_center_excerpt,
)
from src.exploitation.audio.search_audio import (
    existing_vector, indexed_songs, nearest_songs, open_existing_collection,
)


def require_audio_tools():
    """Use installed FFmpeg or the project's optional local tools directory."""
    local = Path(__file__).resolve().parents[2] / "data/tools/ffmpeg/bin"
    if not shutil.which("ffmpeg") and (local / "ffmpeg.exe").is_file():
        os.environ["PATH"] = str(local) + os.pathsep + os.environ.get("PATH", "")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("Install FFmpeg and FFprobe to prepare audio queries and previews.")


def connect_audio(path=DEFAULT_CHROMA_PATH):
    return open_existing_collection(path)


def validate_audio_vector(vector):
    values = np.asarray(vector, dtype=np.float32)
    if (values.shape != (DIMENSION,) or not np.isfinite(values).all()
            or not np.isclose(np.linalg.norm(values), 1., atol=1e-3)):
        raise ValueError("CLAP requires a finite, L2-normalized 512-dimensional vector.")
    return values.tolist()


def validate_audio_index(collection):
    """Reject stale model revisions before using stored embeddings."""
    for song in indexed_songs(collection):
        record = existing_vector(collection, song.vector_id)
        if record is None or record[1].get("model_revision") != MODEL_REVISION:
            raise ValueError("Rebuild the audio index with the pinned CLAP revision.")
        validate_audio_vector(record[0])


def checked_source(client, metadata, trusted_bucket):
    bucket, key = metadata["source_bucket"], metadata["source_key"]
    if bucket != trusted_bucket or not key.startswith("audio/") or not key.lower().endswith(".flac"):
        raise ValueError("Audio must reference a FLAC in the configured Trusted zone.")
    head = client.head_object(Bucket=bucket, Key=key)
    if revision_signature(bucket, key, head) != metadata["source_signature"]:
        raise ValueError("The Trusted audio has changed since its embedding was generated.")
    return head


def preview_audio(client, metadata, trusted_bucket):
    """Verify the exact indexed source and return its selected excerpt as WAV."""
    require_audio_tools()
    head = checked_source(client, metadata, trusted_bucket)
    with tempfile.TemporaryDirectory(prefix="adsbd_audio_preview_") as folder:
        path = Path(folder) / "source.flac"
        client.download_file(trusted_bucket, metadata["source_key"], str(path))
        if path.stat().st_size != head["ContentLength"]:
            raise ValueError("The audio download is incomplete.")
        provenance = head.get("Metadata", {})
        expected_hash = provenance.get("trusted-sha256")
        if provenance.get("trusted-method") == "copy":
            expected_hash = provenance.get("source-sha256")
        if expected_hash and hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError("The Trusted audio failed its content hash check.")
        excerpt = select_center_excerpt(probe_duration(path))
        if (not np.isclose(excerpt.start, metadata["start_seconds"], atol=1e-3)
                or not np.isclose(excerpt.end, metadata["end_seconds"], atol=1e-3)):
            raise ValueError("The indexed excerpt does not match the Trusted audio.")
        samples = decode_excerpt(path, excerpt)
        checked_source(client, metadata, trusted_bucket)
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


def uploaded_query(body, suffix, encoder):
    """Decode a bounded local upload; never store it in MinIO or ChromaDB."""
    if not body or len(body) > 50 * 1024 * 1024:
        raise ValueError("Choose an audio file of up to 50 MB.")
    if suffix.lower() not in {".flac", ".wav", ".mp3", ".ogg"}:
        raise ValueError("Choose a FLAC, WAV, MP3 or OGG file.")
    require_audio_tools()
    with tempfile.TemporaryDirectory(prefix="adsbd_audio_upload_") as folder:
        path = Path(folder) / ("query" + suffix.lower())
        path.write_bytes(body)
        duration = probe_duration(path)
        excerpt = select_center_excerpt(duration)
        vector = encoder.embed_audio(decode_excerpt(path, excerpt))
    return validate_audio_vector(vector)


def search_audio(collection, vector, *, top_k=5, source_key=""):
    validate_audio_index(collection)
    hits = nearest_songs(collection, validate_audio_vector(vector), source_key, top_k)
    for hit in hits:
        hit["score"] = max(-1., min(1., 1. - hit["cosine_distance"]))
        record = existing_vector(collection, hit["vector_id"])
        if record is None:
            raise ValueError("The audio index changed during retrieval.")
        hit["metadata"] = record[1]
    return hits


def search_audio_text(collection, encoder, text, *, top_k=5):
    if not text.strip() or len(text) > 4000:
        raise ValueError("Enter a text query of between 1 and 4,000 characters.")
    if encoder.model_revision != MODEL_REVISION:
        raise ValueError("The CLAP query model does not match the pinned revision.")
    return search_audio(collection, encoder.embed_text(text), top_k=top_k)
