"""Restore validated offline sources when a source collector fails."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from src.common.config import PROJECT_ROOT
from src.ingestion.text_captures_to_temporal import selected_captures


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def validate_backup(root):
    """Validate the complete release snapshot, including traversal and symlink checks."""
    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or manifest.get("counts") != {"descriptions": 50, "images": 50, "audio": 16}:
        raise ValueError("The backup must contain 50 descriptions, 50 images and 16 audio files")
    verified = set()
    for row in manifest["files"]:
        relative = Path(row["path"])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or any(p.is_symlink() for p in [root / relative, *(root / relative).parents] if p.is_relative_to(root)):
            raise ValueError("An unsafe path was found in the backup manifest")
        body = path.read_bytes()
        if len(body) != row["bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise ValueError(f"Backup integrity check failed: {row['path']}")
        if row["path"] in verified:
            raise ValueError("Duplicate backup manifest path")
        verified.add(row["path"])
    image_rows = jsonl(root / "rawg/manifests/images.jsonl")
    text_rows = selected_captures(root / "texts", root / "rawg/manifests/games.jsonl")
    audio_rows = jsonl(root / "audio/manifests/files.jsonl")
    if len(image_rows) != 50 or len(text_rows) != 50 or len(audio_rows) != 16:
        raise ValueError("Backup file counts do not match the release manifest")
    games = {game for game, *_ in text_rows}
    if {row["source_game_id"] for row in image_rows} != games:
        raise ValueError("The backup screenshots and descriptions must cover the same 50 games")
    expected = {"rawg/manifests/images.jsonl", "rawg/manifests/games.jsonl", "texts/manifests/text_sources.jsonl",
                "audio/manifests/files.jsonl", "audio/manifests/submissions.jsonl"}
    for row in image_rows:
        relative = "rawg/" + row["local_path"].replace("\\", "/")
        expected.add(relative)
        if relative not in verified or hashlib.sha256((root / relative).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("An image is missing from the verified backup")
    for _, row, _, _ in text_rows:
        expected.add("texts/" + row["raw_object_path"].replace("\\", "/"))
    submissions = {row["source_url"]: row for row in jsonl(root / "audio/manifests/submissions.jsonl")}
    for row in audio_rows:
        relative = "audio/" + row["local_path"].replace("\\", "/")
        expected.add(relative)
        if (relative not in verified or hashlib.sha256((root / relative).read_bytes()).hexdigest() != row["sha256"]
                or "CC0" not in submissions.get(row["source_url"], {}).get("licenses", [])):
            raise ValueError("An audio backup file lacks verified content or CC0 provenance")
    if not expected <= verified:
        raise ValueError("The backup release manifest omits required source files")
    return manifest


class BackupSources:
    """Keep partial live downloads separate from the selected backup snapshot."""

    def __init__(self, args):
        self.args = args
        self.rawg = self.text = self.audio = None
        self.root = PROJECT_ROOT / "data/backup_runs" / uuid.uuid4().hex
        self.validated = False
        self.events = []

    def ensure_valid(self):
        if not self.validated:
            validate_backup(self.args.backup_dir)
            self.validated = True

    def resolve(self, step):
        mapping = {}
        if self.rawg:
            mapping.update({str(self.args.rawg_output): str(self.rawg),
                            str(self.args.rawg_output / "manifests/games.jsonl"): str(self.rawg / "manifests/games.jsonl"),
                            str(self.args.text_output): str(self.text)})
        if self.audio:
            mapping[str(self.args.audio_output / "raw/files")] = str(self.audio / "raw/files")
        return replace(step, arguments=tuple(mapping.get(value, value) for value in step.arguments))

    def skip(self, step):
        return bool(self.rawg and step.module == "src.ingestion.text_to_temporal_landing")

    def recover(self, step):
        if self.args.no_backup:
            return False
        if step.module not in {"src.data_collection.collect_rawg_images", "src.ingestion.text_to_temporal_landing",
                              "src.data_collection.collect_opengameart_audio"}:
            return False
        self.ensure_valid()
        if step.module == "src.data_collection.collect_opengameart_audio":
            self.restore_audio()
            source = "opengameart"
        else:
            self.restore_rawg()
            source = "rawg"
        self.events.append({"source": source, "failed_module": step.module, "replacement": "backup_data"})
        return True

    def copy(self, source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def restore_rawg(self):
        count = self.args.max_games
        if self.args.rawg_plan:
            entries = json.loads(self.args.rawg_plan.read_text(encoding="utf-8"))
            count = sum(int(entry.get("target_games", count)) for entry in entries)
        if count > 50 or self.args.screenshots_per_game != 1 or self.args.rawg_plan:
            raise ValueError("RAWG backup supports up to 50 games, one image per game and no stratified plan")
        backup = self.args.backup_dir
        self.rawg, self.text = self.root / "rawg", self.root / "texts"
        games = jsonl(backup / "rawg/manifests/games.jsonl")[:count]
        ids = {row["source_game_id"] for row in games}
        images = [row for row in jsonl(backup / "rawg/manifests/images.jsonl") if row["source_game_id"] in ids]
        texts = [row for row in jsonl(backup / "texts/manifests/text_sources.jsonl") if row["source_game_id"] in ids]
        for row in images:
            self.copy(backup / "rawg" / row["local_path"], self.rawg / row["local_path"])
        for row in texts:
            self.copy(backup / "texts" / row["raw_object_path"], self.text / row["raw_object_path"])
        for row in [*images, *texts]:
            row["retrieved_from"] = "backup_data"
        write_jsonl(self.rawg / "manifests/games.jsonl", games)
        write_jsonl(self.rawg / "manifests/images.jsonl", images)
        write_jsonl(self.text / "manifests/text_sources.jsonl", texts)

    def restore_audio(self):
        if self.args.max_audio_files > 16:
            raise ValueError("The offline backup contains at most 16 audio files")
        self.audio = self.root / "audio"
        backup = self.args.backup_dir / "audio"
        rows = jsonl(backup / "manifests/files.jsonl")[:self.args.max_audio_files]
        for row in rows:
            relative = row["local_path"].replace("\\", "/")
            self.copy(backup / relative, self.audio / relative)
            row["retrieved_from"] = "backup_data"
        write_jsonl(self.audio / "manifests/files.jsonl", rows)
        self.copy(backup / "manifests/submissions.jsonl", self.audio / "manifests/submissions.jsonl")

    def cleanup(self):
        root = self.root.resolve()
        parent = (PROJECT_ROOT / "data/backup_runs").resolve()
        if root.parent != parent:
            raise ValueError("Refusing to delete an unowned backup staging directory")
        if root.exists():
            shutil.rmtree(root)
