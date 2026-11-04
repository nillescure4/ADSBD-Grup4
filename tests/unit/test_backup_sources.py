"""Exercise source failures, catalogue consistency and backup integrity."""

import hashlib
import json

import pytest

from src.pipelines import backup_sources as backup
from src.pipelines import run_pipeline as pipeline
from src.ingestion import text_captures_to_temporal as uploader


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "backup"
    images, texts, games, audio, submissions = [], [], [], [], []
    def put(relative, body):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return hashlib.sha256(body).hexdigest()
    for game in range(1, 51):
        image = f"images/game_{game}.jpg"
        digest = put("rawg/" + image, f"image{game}".encode())
        images.append({"source_game_id": game, "local_path": image, "sha256": digest, "source_url": "https://example.com/image"})
        text = f"raw_api/{game}.json"
        digest = put("texts/" + text, json.dumps({"id": game, "description_raw": "Description"}).encode())
        texts.append({"source_game_id": game, "source": "rawg", "asset_id": f"rawg:{game}:game_details",
                      "raw_object_path": text, "sha256": digest, "ingested_at_utc": "2026-11-01T00:00:00+00:00"})
        games.append({"source_game_id": game})
    for index in range(16):
        path = f"raw/files/{index}/song.mp3"
        digest = put("audio/" + path, f"audio{index}".encode())
        url = f"https://example.com/audio/{index}"
        audio.append({"local_path": path, "sha256": digest, "source_url": url})
        submissions.append({"source_url": url, "licenses": ["CC0"]})
    for path, rows in (("rawg/manifests/images.jsonl", images), ("rawg/manifests/games.jsonl", games),
                       ("texts/manifests/text_sources.jsonl", texts), ("audio/manifests/files.jsonl", audio),
                       ("audio/manifests/submissions.jsonl", submissions)):
        backup.write_jsonl(root / path, rows)
    files = [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size,
              "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(root.rglob("*")) if p.is_file()]
    (root / "manifest.json").write_text(json.dumps({"version": 1,
        "counts": {"descriptions": 50, "images": 50, "audio": 16}, "files": files}))
    return root


@pytest.fixture
def recovery(snapshot, tmp_path):
    args = pipeline.parse_args(["--backup-dir", str(snapshot), "--max-games", "2", "--max-audio-files", "2",
                                "--rawg-output", str(tmp_path / "api_images"), "--text-output", str(tmp_path / "api_texts")])
    result = backup.BackupSources(args)
    yield result
    result.cleanup()


def test_backup_release_is_complete_and_corrupt_content_is_rejected(snapshot):
    assert backup.validate_backup(snapshot)["counts"]["images"] == 50
    (snapshot / "rawg/images/game_1.jpg").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        backup.validate_backup(snapshot)


def test_traversal_in_release_manifest_is_rejected_without_reading_external_files(snapshot):
    path = snapshot / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][0]["path"] = "../outside.txt"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unsafe"):
        backup.validate_backup(snapshot)


def test_image_source_failure_skips_text_api_and_keeps_matching_backup_catalogue(recovery):
    args = recovery.args
    plan = pipeline.build_plan(args)[:4]
    commands = []
    def executor(command, env):
        commands.append(command)
        return 1 if command[2] == "src.data_collection.collect_rawg_images" else 0
    # This test isolates the RAWG branch; audio collection is checked separately.
    plan = [s for s in plan if s.module != "src.data_collection.collect_opengameart_audio"]
    code, rows = pipeline.run_steps(plan, {}, executor=executor, recovery=recovery)
    assert code == 0 and rows[0]["source_exit_code"] == 1
    assert rows[0]["data_source"] == rows[1]["data_source"] == "backup_data"
    assert not any(command[2] == "src.ingestion.text_to_temporal_landing" for command in commands)
    assert str(recovery.text) in commands[-1]
    captures = uploader.selected_captures(recovery.text, recovery.rawg / "manifests/games.jsonl")
    assert [game for game, *_ in captures] == [1, 2]
    assert all(row["ingested_at_utc"] == "2026-11-01T00:00:00+00:00" for _, row, _, _ in captures)


def test_description_failure_replaces_image_selection_and_preserves_partial_live_files(recovery, tmp_path):
    original = recovery.args.rawg_output / "partial.jpg"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"live image")
    step = pipeline.Step("texts", "src.ingestion.text_to_temporal_landing")
    assert recovery.recover(step)
    resolved = recovery.resolve(pipeline.Step("upload", "images", ("--input-dir", str(recovery.args.rawg_output))))
    assert resolved.arguments[-1] == str(recovery.rawg)
    assert original.read_bytes() == b"live image"


def test_audio_fallback_uses_requested_subset(recovery):
    assert recovery.recover(pipeline.Step("audio", "src.data_collection.collect_opengameart_audio"))
    assert len(list((recovery.audio / "raw/files").rglob("*.mp3"))) == 2
    assert len(backup.jsonl(recovery.audio / "manifests/files.jsonl")) == 2


def test_zone_failure_is_not_hidden_by_backup(recovery):
    code, rows = pipeline.run_steps([pipeline.Step("upload", "src.ingestion.audio.audio_landing")], {},
                                     executor=lambda *args: 7, recovery=recovery)
    assert code == 1 and rows[0]["exit_code"] == 7 and not recovery.events


def test_no_backup_preserves_fail_fast_behavior(recovery):
    recovery.args.no_backup = True
    assert not recovery.recover(pipeline.Step("images", "src.data_collection.collect_rawg_images"))


def test_insufficient_backup_capacity_is_not_reported_as_success(recovery):
    recovery.args.max_games = 51
    with pytest.raises(ValueError, match="up to 50"):
        recovery.recover(pipeline.Step("images", "src.data_collection.collect_rawg_images"))


def test_uploader_preserves_source_bytes_and_refuses_conflicting_temporal_captures(recovery, s3, monkeypatch):
    recovery.restore_rawg()
    monkeypatch.setattr(uploader, "upload_file", lambda client, path, bucket, key:
                        client.put_object(Bucket=bucket, Key=key, Body=path.read_bytes()))
    storage = {"buckets": {"landing": "landing"}, "landing": {"temporal_prefix": "temporal", "texts_prefix": "texts"}}
    assert uploader.upload_captures(s3, storage, recovery.text, recovery.rawg / "manifests/games.jsonl") == 2
    assert s3.data("landing", "temporal/texts/1.json") == (recovery.text / "raw_api/1.json").read_bytes()
    s3.put("landing", "temporal/texts/1.json", b"different")
    with pytest.raises(ValueError, match="different Temporal"):
        uploader.upload_captures(s3, storage, recovery.text, recovery.rawg / "manifests/games.jsonl")
