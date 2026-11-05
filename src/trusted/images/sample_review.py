"""Reconstruct and compare image samples using the production image rules."""

import io

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageOps

from src.formatting.images.persistent_to_formatted import convert_to_jpeg, to_rgb
from src.pipelines.backup_sources import jsonl
from src.trusted.images.metrics import compute_metrics
from src.trusted.images.rules import apply_rules, flag_duplicates
from src.trusted.review_support import assess_samples, difference, select_samples, sha


def image_evidence(root, out):
    rows, pairs = [], {}
    for source in jsonl(root / "rawg/manifests/images.jsonl"):
        path = root / "rawg" / source["local_path"].replace("\\", "/")
        raw = path.read_bytes()
        formatted = convert_to_jpeg(raw)
        with Image.open(io.BytesIO(raw)) as img:
            original = to_rgb(img).copy()
        with Image.open(io.BytesIO(formatted)) as img:
            canonical = img.copy()
        change = difference(np.asarray(original), np.asarray(canonical))
        row = {"sample_id": str(source["source_game_id"]), "game_name": source["name"],
               "source_path": path.relative_to(root).as_posix(), "source_sha256": sha(raw),
               "formatted_sha256": sha(formatted), "key": path.name, "error": None,
               **compute_metrics(formatted), "pixel_rms_error": change["rms_error"],
               "pixel_max_error": change["max_absolute_error"],
               "psnr_db": float(20 * np.log10(255 / change["rms_error"])) if change["rms_error"] else None}
        rows.append(row)
        pairs[row["sample_id"]] = (original, canonical)
    measured = apply_rules(flag_duplicates(pd.DataFrame(rows))).replace({np.nan: None}).to_dict("records")
    for row in measured:
        row["trusted_sha256"] = row["formatted_sha256"] if row["reject_reason"] is None else None
    samples = select_samples(measured, ["brightness", "sharpness", "letterbox_ratio", "pixel_rms_error"], 8)
    assets = []
    sheet = Image.new("RGB", (1000, 210 * len(samples)), "white")
    for index, row in enumerate(samples):
        pair = Image.new("RGB", (1000, 210), "white")
        label = ImageDraw.Draw(pair)
        label.text((8, 4), f"{row['sample_id']} - source (left), formatted / accepted Trusted (right)", fill="black")
        for col, img in enumerate(pairs[row["sample_id"]]):
            thumb = ImageOps.contain(img, (490, 180))
            pair.paste(thumb, (col * 500, 25))
        path = out / f"image_{row['sample_id']}.jpg"
        pair.save(path, quality=95)
        sheet.paste(pair, (0, index * 210))
        assets.append((row, path))
    sheet.save(out / "image_comparisons.jpg", quality=95)
    return measured, assets


def assess_images(assets):
    """Assess only the selected before/after image comparisons."""
    return assess_samples("image", [(row, [path]) for row, path in assets], "image/jpeg")
