"""Publish and read verified quality reports independently of embedding models."""

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import re
from pathlib import Path

from src.common.minio_client import list_object_keys


class ReportUnavailable(ValueError):
    pass


def digest(body):
    return hashlib.sha256(body).hexdigest()


def read(client, bucket, key):
    response = client.get_object(Bucket=bucket, Key=key)
    try:
        return response["Body"].read()
    finally:
        response["Body"].close()


def inventory(client, bucket, prefix):
    return {row["Key"]: row["ETag"].strip('"')
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for row in page.get("Contents", [])}


def source_state(client, storage, modality):
    prefix = {"image": "images/", "audio": "audio/"}[modality]
    return {zone: digest(json.dumps(inventory(client, storage["buckets"][zone], prefix), sort_keys=True).encode())
            for zone in ("formatted", "trusted")}


def publish_bundle(client, storage, modality, profile, analysis_dir, processing=None):
    """Verify the profile binding, write immutable files, publish the manifest last."""
    bucket = storage["buckets"]["trusted"]
    state = source_state(client, storage, modality)
    body = Path(profile).read_bytes()
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
    if modality == "image":
        from src.trusted.images.build_trusted_images import input_signature, list_objects
        checkpoint = json.loads(read(client, bucket, "quality/images/checkpoint.json"))
        current = list_objects(client, storage["buckets"]["formatted"], "images/")
        if checkpoint["input"] != input_signature(current) or read(client, bucket, "quality/images/images_quality.csv") != body:
            raise ReportUnavailable("Image quality reports do not match the current processing checkpoint")
    else:
        current = inventory(client, storage["buckets"]["formatted"], "audio/")
        if {row["source_key"] for row in rows} != set(current) or any(
            row["source_etag"].strip('"') != current[row["source_key"]] for row in rows
        ):
            raise ReportUnavailable("Audio quality reports do not match current Formatted inputs")
    files = {"profile.csv": body}
    for path in sorted(Path(analysis_dir).rglob("*")):
        if path.is_symlink():
            raise ValueError("Report artifacts cannot be symbolic links")
        if path.is_file() and path.suffix.lower() in {".md", ".json", ".png", ".csv"}:
            files["analysis/" + path.relative_to(analysis_dir).as_posix()] = path.read_bytes()
    if processing is not None:
        outcomes = Path(processing).read_bytes()
        processed = list(csv.DictReader(io.StringIO(outcomes.decode("utf-8"))))
        profiles = {row["source_key"]: row for row in rows}
        if len(processed) != len(profiles) or {row["source_key"] for row in processed} != set(profiles) or any(
            row["source_sha256"] != profiles[row["source_key"]]["sha256"] or row["status"] == "planned"
            for row in processed
        ):
            raise ReportUnavailable("Audio processing outcomes do not match the verified profile")
        files["processing.csv"] = outcomes
    revision = digest(json.dumps({name: digest(value) for name, value in files.items()}, sort_keys=True).encode())
    prefix = f"quality/reports/{modality}/revisions/{revision}/"
    outputs = {}
    for name, value in files.items():
        key = prefix + name
        client.put_object(Bucket=bucket, Key=key, Body=value)
        if read(client, bucket, key) != value:
            raise ValueError("A quality report upload could not be verified")
        outputs[name] = {"key": key, "sha256": digest(value), "bytes": len(value)}
    if source_state(client, storage, modality) != state:
        raise ReportUnavailable("Data changed during report publication; rerun the quality analysis")
    manifest = {"version": 1, "modality": modality, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_state": state, "revision": revision, "files": outputs}
    key = f"quality/reports/{modality}/manifest.json"
    encoded = json.dumps(manifest, indent=2).encode()
    revision_manifest = prefix + "manifest.json"
    client.put_object(Bucket=bucket, Key=revision_manifest, Body=encoded, ContentType="application/json")
    if read(client, bucket, revision_manifest) != encoded:
        raise ValueError("The archived quality report manifest could not be verified")
    client.put_object(Bucket=bucket, Key=key, Body=encoded, ContentType="application/json")
    if read(client, bucket, key) != encoded:
        raise ValueError("The quality report manifest could not be verified")
    return manifest


def list_revisions(client, storage, modality):
    bucket = storage["buckets"]["trusted"]
    try:
        latest = json.loads(read(client, bucket, f"quality/reports/{modality}/manifest.json"))["revision"]
    except Exception:
        latest = None
    prefix = f"quality/reports/{modality}/revisions/"
    revisions = []
    for key in list_object_keys(client, bucket, prefix):
        relative = key[len(prefix):]
        if not re.fullmatch(r"[a-f0-9]{64}/manifest\.json", relative):
            continue
        manifest = json.loads(read(client, bucket, key))
        revision = relative.split("/")[0]
        if manifest.get("version") == 1 and manifest.get("modality") == modality and manifest.get("revision") == revision:
            revisions.append({"revision": revision, "generated_at_utc": manifest["generated_at_utc"],
                              "is_latest": revision == latest})
    return sorted(revisions, key=lambda row: row["generated_at_utc"], reverse=True)


def load_bundle(client, storage, modality, revision=None):
    bucket = storage["buckets"]["trusted"]
    if revision is not None and not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise ReportUnavailable("Invalid quality report revision")
    manifest_key = f"quality/reports/{modality}/" + (f"revisions/{revision}/manifest.json" if revision else "manifest.json")
    manifest = json.loads(read(client, bucket, manifest_key))
    if manifest.get("version") != 1 or manifest.get("modality") != modality:
        raise ReportUnavailable("Unsupported quality report manifest")
    if revision is not None and revision != manifest["revision"]:
        raise ReportUnavailable("The report revision does not match its archived manifest")
    if revision is None and manifest["source_state"] != source_state(client, storage, modality):
        raise ReportUnavailable("These reports are outdated because the underlying data changed")
    files = {}
    prefix = f"quality/reports/{modality}/revisions/{manifest['revision']}/"
    for name, entry in manifest["files"].items():
        if entry["key"] != prefix + name or "\\" in name or any(part in {"", ".", ".."} for part in name.split("/")):
            raise ReportUnavailable("Invalid quality report artifact path")
        value = read(client, bucket, entry["key"])
        if len(value) != entry["bytes"] or digest(value) != entry["sha256"]:
            raise ReportUnavailable("A report artifact changed or failed its integrity check")
        files[name] = value
    if revision is None and manifest["source_state"] != source_state(client, storage, modality):
        raise ReportUnavailable("Data changed while reading these reports; refresh after processing finishes")
    current_matches = True
    if revision is not None:
        try:
            if modality == "text":
                current = load_text_bundle(client, storage)["files"]
                current_matches = manifest["source_report_sha256"] == {name: digest(value) for name, value in current.items()}
            else:
                current_matches = manifest["source_state"] == source_state(client, storage, modality)
        except Exception:
            current_matches = False
    return {"files": files, "generated_at_utc": manifest["generated_at_utc"],
            "revision": manifest["revision"], "current_matches": current_matches}


def load_text_bundle(client, storage):
    bucket = storage["buckets"]["trusted"]
    prefix = storage.get("trusted", {}).get("texts_prefix", "texts").strip("/") + "/rawg/"
    checkpoint = json.loads(read(client, bucket, prefix + "manifests/quality_overview.json"))
    expected = checkpoint["outputs"]
    files = {}
    for key, checksum in expected.items():
        value = read(client, bucket, key)
        if digest(value) != checksum:
            raise ReportUnavailable("The text quality overview changed or failed its integrity check")
        files[Path(key).name] = value
    dates = set()
    for partition in checkpoint["input"]["partitions"]:
        raw = read(client, bucket, partition["checkpoint_key"])
        if digest(raw) != partition["checkpoint_sha256"]:
            raise ReportUnavailable("The text overview is outdated; regenerate the global quality analysis")
        manifest = json.loads(raw)
        dates.add(partition["date"])
        for key, checksum in manifest["outputs"].items():
            if digest(read(client, bucket, key)) != checksum:
                raise ReportUnavailable("A Trusted text partition or its quality report has changed")
    current_dates = {key.split("collection_date=")[1].split("/")[0]
                     for key in list_object_keys(client, bucket, prefix)
                     if "/collection_date=" in key and key.endswith("/game_details.parquet")}
    if dates != current_dates:
        raise ReportUnavailable("The text overview does not cover the current Trusted partitions")
    return {"files": files, "generated_at_utc": None}


def publish_text_revision(client, storage):
    """Archive each distinct verified global text analysis without duplicating it."""
    bundle = load_text_bundle(client, storage)
    files = dict(bundle["files"])
    source_hashes = {name: digest(value) for name, value in files.items()}
    report = json.loads(files["overview.json"])
    if report.get("quality_version", 0) < 5:
        from src.trusted.text_quality_analysis import analysis_markdown, interpretations
        report["interpretation"] = [
            f"Captures analysed: {report['capture_count']}. Distinct games: {report['distinct_valid_game_ids']}. "
            "Historical captures are included and are not independent games.",
            *interpretations(report["completeness"], report["capture_count"]),
        ]
        files["overview.json"] = json.dumps(report, ensure_ascii=False, indent=2).encode()
        files["overview.md"] = ("# Text quality analysis\n\n" + analysis_markdown(report, global_report=True)).encode()
    revision = digest(json.dumps({name: digest(value) for name, value in files.items()}, sort_keys=True).encode())
    bucket = storage["buckets"]["trusted"]
    outputs = {}
    for name, value in files.items():
        key = f"quality/reports/text/revisions/{revision}/{name}"
        client.put_object(Bucket=bucket, Key=key, Body=value)
        if read(client, bucket, key) != value:
            raise ValueError("A text report revision could not be verified")
        outputs[name] = {"key": key, "sha256": digest(value), "bytes": len(value)}
    manifest = {"version": 1, "modality": "text", "revision": revision, "files": outputs,
                "source_report_sha256": source_hashes,
                "generated_at_utc": datetime.now(timezone.utc).isoformat()}
    encoded = json.dumps(manifest).encode()
    for key in (f"quality/reports/text/revisions/{revision}/manifest.json", "quality/reports/text/manifest.json"):
        client.put_object(Bucket=bucket, Key=key, Body=encoded, ContentType="application/json")
        if read(client, bucket, key) != encoded:
            raise ValueError("The text report manifest could not be verified")
    return manifest
