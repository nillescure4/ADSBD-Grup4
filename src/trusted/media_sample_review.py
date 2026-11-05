"""Orchestrate the separate image and audio sample reviews.

The format-specific modules own media decoding, comparisons and quality rules.
This entrypoint validates the snapshot, dispatches those reviews and saves the
combined evidence. Optional assisted assessment never changes quality decisions.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from src.common.config import PROJECT_ROOT
from src.pipelines.backup_sources import validate_backup
from src.trusted.images.sample_review import assess_images, image_evidence
from src.trusted.audio.sample_review import assess_audio, audio_evidence
from src.trusted.media_review_registry import current_processing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, default=PROJECT_ROOT / "backup_data")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports/media_quality")
    parser.add_argument("--assess-with-gemini", action="store_true", help="Send selected media excerpts to the configured Gemini service")
    args = parser.parse_args()
    validate_backup(args.backup_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_rows, images = image_evidence(args.backup_dir, args.output_dir)
    audio_rows, audio = audio_evidence(args.backup_dir, args.output_dir)
    report = {"version": 1, "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
              "processing": current_processing(),
              "scope": "Local reconstruction using production converters/rules; not a MinIO inventory or whole-dataset human certification",
              "images": image_rows, "audio": audio_rows,
              "selected_image_ids": [r["sample_id"] for r, _ in images],
              "selected_audio_ids": [r["sample_id"] for r, _ in audio]}
    report["processing"]["ffmpeg_version"] = subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0]
    (args.output_dir / "measurements.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    if args.assess_with_gemini:
        report["assisted_reviews"] = {"image": assess_images(images), "audio": assess_audio(audio)}
        (args.output_dir / "assessed_samples.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Evidence written to {args.output_dir}")


if __name__ == "__main__":
    main()
