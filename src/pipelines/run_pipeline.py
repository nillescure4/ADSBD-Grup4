"""Run source collection, all data zones and multimodal search preparation."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from src.common.config import PROJECT_ROOT, load_storage_config, load_yaml
from src.common.minio_client import create_minio_client, list_bucket_names, list_object_keys


@dataclass(frozen=True)
class Step:
    name: str
    module: str
    arguments: tuple[str, ...] = ()
    collection_root: Path | None = None
    modality: str | None = None


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("The value must be positive")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["run", "status"], default="run")
    parser.add_argument("--max-games", type=positive, default=8, help="New RAWG games to select (default: 8)")
    parser.add_argument("--screenshots-per-game", type=positive, default=2)
    parser.add_argument("--max-audio-files", type=positive, default=5, help="Total downloaded audio target (default: 5)")
    parser.add_argument("--audio-max-items", type=positive, default=30, help="Maximum CC0 submissions to inspect per run")
    parser.add_argument("--audio-query", default="")
    parser.add_argument("--rawg-plan", type=Path, help="Stratified RAWG plan; overrides the single-query game target")
    parser.add_argument("--rawg-output", type=Path, default=PROJECT_ROOT / "data/landing/rawg_full")
    parser.add_argument("--text-output", type=Path, default=PROJECT_ROOT / "data/landing/text_descriptions")
    parser.add_argument("--audio-output", type=Path, default=PROJECT_ROOT / "data/landing/opengameart_audio")
    parser.add_argument("--image-dataset", default="rawg_images")
    parser.add_argument("--report-dir", type=Path, default=PROJECT_ROOT / "reports/pipeline")
    parser.add_argument("--storage-config", type=Path, help="Storage YAML passed to every zone process")
    parser.add_argument("--ffmpeg-dir", type=Path, help="Directory containing ffmpeg and ffprobe")
    parser.add_argument("--start-minio", action="store_true", help="Start Docker Compose before connecting")
    parser.add_argument("--skip-collection", action="store_true", help="Resume zone processing without fetching sources")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without network requests or writes")
    parser.add_argument("--through", choices=["trusted", "exploitation"], default="exploitation")
    parser.add_argument("--embeddings-only", action="store_true", help="Resume from existing Trusted data")
    parser.add_argument("--db-path", type=Path, default=PROJECT_ROOT / "data/exploitation/chroma")
    parser.add_argument("--serve", action="store_true", help="Start Streamlit after a successful complete run")
    parser.add_argument("--port", type=positive, default=8501)
    args = parser.parse_args(argv)
    if args.port > 65535:
        parser.error("--port must be between 1 and 65535")
    if args.through == "trusted" and (args.embeddings_only or args.serve):
        parser.error("--embeddings-only and --serve require --through exploitation")
    for name in ("rawg_output", "text_output", "audio_output", "report_dir", "storage_config", "ffmpeg_dir", "rawg_plan", "db_path"):
        path = getattr(args, name)
        if path is not None:
            setattr(args, name, path.resolve())
    return args


def build_plan(args) -> list[Step]:
    images, audio, reports = args.rawg_output, args.audio_output, args.report_dir
    image_csv, audio_csv = reports / "images/quality.csv", reports / "audio/quality.csv"
    plan = []
    if not args.skip_collection:
        image_args = ["--output", str(images), "--max-games", str(args.max_games),
                      "--screenshots-per-game", str(args.screenshots_per_game)]
        if args.rawg_plan:
            image_args += ["--plan", str(args.rawg_plan)]
        plan.extend([
            Step("Collect RAWG screenshots", "src.data_collection.collect_rawg_images", tuple(image_args), images, "image"),
            Step("Collect and upload RAWG descriptions", "src.ingestion.text_to_temporal_landing",
                 ("--games-manifest", str(images / "manifests/games.jsonl"), "--output", str(args.text_output), "--upload-minio")),
            Step("Collect OpenGameArt music", "src.data_collection.collect_opengameart_audio",
                 ("--output", str(audio), "--max-audio-files", str(args.max_audio_files),
                  "--max-items", str(args.audio_max_items), "--query", args.audio_query,
                  "--no-include-archives", "--no-include-music-source"), audio, "audio"),
        ])
    plan.extend([
        Step("Upload images to Temporal", "src.ingestion.images.images_to_landing",
             ("--dataset", args.image_dataset, "--input-dir", str(images))),
        Step("Promote images to Persistent", "src.ingestion.images.images_temporal_to_persistent"),
        Step("Promote text to Persistent", "src.ingestion.text_temporal_to_persistent"),
        Step("Upload audio to Temporal", "src.ingestion.audio.audio_landing", ("--input-dir", str(audio / "raw/files"), "--all")),
        Step("Promote audio to Persistent", "src.ingestion.audio.audio_persistent", ("--all",)),
        Step("Format images", "src.formatting.images.persistent_to_formatted"),
        Step("Format text", "src.formatting.text_to_formatted_zone"),
        Step("Format audio", "src.formatting.audio.formatting_audio", ("--report", str(reports / "audio/formatting.csv"))),
        Step("Build Trusted images", "src.trusted.images.build_trusted_images", ("--csv_output", str(image_csv))),
        Step("Build Trusted text", "src.trusted.text_to_trusted_zone"),
        Step("Profile audio quality", "src.trusted.audio.analyze_audio_quality", ("--report", str(audio_csv))),
        Step("Build Trusted audio", "src.trusted.audio.trusted_audio", ("--quality-report", str(audio_csv), "--report", str(reports / "audio/processing.csv"))),
        Step("Report image quality", "src.trusted.images.quality_report", ("--csv_input", str(image_csv), "--output_dir", str(reports / "images/analysis"))),
        Step("Report audio quality", "src.trusted.audio.audio_quality_report", ("--report", str(audio_csv), "--output-dir", str(reports / "audio/analysis"))),
    ])
    plan.append(Step("Publish verified screenshot/game provenance", "src.trusted.images.publish_provenance",
                     ("--input-dir", str(images), "--dataset", args.image_dataset)))
    if args.embeddings_only:
        plan = []
    if args.through == "exploitation":
        database = ("--db-path", str(args.db_path))
        plan.extend([
            Step("Index Jina descriptions", "src.exploitation.text.build_embeddings", database),
            Step("Index Jina images", "src.exploitation.images.build_embeddings", database),
            Step("Index CLAP audio", "src.exploitation.audio.embedding_audio",
                 ("--all", "--chroma-path", str(args.db_path), "--report", str(reports / "audio/embeddings.csv"))),
            Step("Index CLAP descriptions", "src.exploitation.text.build_clap_embeddings", database),
            Step("Verify Exploitation and retrieval", "src.pipelines.verify_exploitation",
                 (*database, "--report", str(reports / "exploitation.json"))),
        ])
    return plan


def check_collection(root: Path, modality: str) -> None:
    """A successful exit alone must not hide an empty or incomplete collection."""
    summary = json.loads((root / "collection_summary.json").read_text(encoding="utf-8"))
    if summary.get("errors", 0):
        raise ValueError(f"The {modality} collector recorded errors; inspect its summary")
    check_inputs(root, modality)


def check_inputs(root: Path, modality: str) -> None:
    if modality == "image":
        from src.ingestion.images.images_to_landing import iter_image_files
        rows = [json.loads(line) for line in (root / "manifests/games.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows or any(type(row.get("source_game_id")) is not int or row["source_game_id"] < 1 for row in rows):
            raise ValueError("The RAWG game manifest is empty or invalid")
        if not any(iter_image_files(root)):
            raise ValueError("The RAWG collection contains no images")
    else:
        from src.ingestion.audio.audio_landing import discover_audio_files
        from src.ingestion.audio.audio_persistent import audio_identity
        directory = root / "raw/files"
        files = discover_audio_files(directory)
        if not files:
            raise ValueError("The OpenGameArt collection contains no playable audio")
        for path in files:
            audio_identity(path.relative_to(directory).as_posix())


def redact(message: str, env: dict[str, str]) -> str:
    for name in ("RAWG_API_KEY", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "GEMINI_API_KEY", "OPENAI_API_KEY", "HF_TOKEN"):
        value = env.get(name)
        if value:
            message = message.replace(quote(value, safe=""), "[REDACTED]").replace(value, "[REDACTED]")
    return message


def execute(command: list[str], env: dict[str, str]) -> int:
    """Use the current Python, a fixed working directory and streamed, redacted logs."""
    with subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") as child:
        for line in child.stdout:
            print(redact(line.rstrip(), env), flush=True)
        return child.wait()


def run_steps(plan: list[Step], env: dict[str, str], *, executor=execute) -> tuple[int, list[dict]]:
    records = []
    for index, step in enumerate(plan, 1):
        print(f"[{index}/{len(plan)}] {step.name}", flush=True)
        started, error = time.monotonic(), None
        try:
            code = executor([sys.executable, "-m", step.module, *step.arguments], env)
            if code == 0 and step.collection_root is not None:
                check_collection(step.collection_root, step.modality)
        except (OSError, ValueError) as exc:
            code, error = 1, redact(str(exc), env)
        records.append({"name": step.name, "module": step.module, "exit_code": code,
                        "seconds": round(time.monotonic() - started, 3), "error": error})
        if code:
            print(f"[ERROR] {step.name} failed (exit code {code}); later stages were not run.", file=sys.stderr)
            if error:
                print(error, file=sys.stderr)
            return 1, records
    return 0, records


def preflight(args, env: dict[str, str]) -> None:
    if not args.skip_collection and not args.embeddings_only and not env.get("RAWG_API_KEY"):
        raise ValueError("RAWG_API_KEY is missing. Configure it in .env.")
    for module in ("bs4", "PIL", "pyarrow", "cv2", "imagehash", "pandas", "matplotlib"):
        if importlib.util.find_spec(module) is None:
            raise ValueError(f"Missing dependency: {module}. Install the project dependencies.")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool, path=env.get("PATH")) is None:
            raise ValueError(f"{tool} was not found. Install FFmpeg or provide --ffmpeg-dir.")
    dataset = load_yaml("datasets.yml").get("datasets", {}).get(args.image_dataset, {})
    if dataset.get("source") != "rawg" or dataset.get("modality") != "image":
        raise ValueError("--image-dataset must select a configured RAWG image dataset")
    if args.rawg_plan:
        from src.data_collection.collect_rawg_images import load_plan
        entries = load_plan(args.rawg_plan)
        if not entries or any(int(entry.get("target_games", args.max_games)) < 1 for entry in entries):
            raise ValueError("The RAWG collection plan must contain positive game targets")
    if args.through == "exploitation":
        for module in ("chromadb", "torch", "transformers", "einops", "timm"):
            if importlib.util.find_spec(module) is None:
                raise ValueError(f"Missing dependency: {module}. Install the embeddings extra.")
    if args.serve and importlib.util.find_spec("streamlit") is None:
        raise ValueError("Install the app extra to start Streamlit")
    if args.skip_collection and not args.embeddings_only:
        check_inputs(args.rawg_output, "image")
        check_inputs(args.audio_output, "audio")
        if not (args.rawg_output / "manifests/images.jsonl").is_file():
            raise ValueError("The RAWG image provenance manifest is missing")


def prepare_storage(*, wait: bool = False) -> None:
    client = create_minio_client()
    for attempt in range(30 if wait else 1):
        try:
            existing = set(list_bucket_names(client))
            break
        except BotoCoreError:
            if not wait or attempt == 29:
                raise
            time.sleep(1)
    storage = load_storage_config()
    for zone in ("landing", "formatted", "trusted"):
        bucket = storage["buckets"][zone]
        if bucket not in existing:
            client.create_bucket(Bucket=bucket)
            print(f"[OK] Created {zone} bucket: {bucket}")


def verify_trusted() -> dict[str, int]:
    storage, client = load_storage_config(), create_minio_client()
    bucket = storage["buckets"]["trusted"]
    prefix = storage["trusted"]["texts_prefix"].strip("/") + "/rawg/"
    counts = {
        "text_partitions": sum(key.endswith("/game_details.parquet") for key in list_object_keys(client, bucket, prefix)),
        "images": sum(key.lower().endswith(".jpg") for key in list_object_keys(client, bucket, "images/")),
        "audio": sum(key.lower().endswith(".flac") for key in list_object_keys(client, bucket, "audio/")),
    }
    if not all(counts.values()):
        raise ValueError(f"Some modalities have no Trusted outputs: {counts}. Inspect the quality reports.")
    return counts


def check_status() -> int:
    existing = set(list_bucket_names(create_minio_client()))
    expected = {load_storage_config()["buckets"][zone] for zone in ("landing", "formatted", "trusted")}
    for bucket in sorted(expected):
        print(f"[{'OK' if bucket in existing else 'MISSING'}] {bucket}")
    return 0 if expected <= existing else 1


def main(argv=None) -> int:
    args = parse_args(argv)
    plan = build_plan(args)
    if args.dry_run:
        if args.start_minio:
            print("docker compose up -d")
        for step in plan:
            print(subprocess.list2cmdline([sys.executable, "-m", step.module, *step.arguments]))
        if args.serve:
            print(subprocess.list2cmdline(serve_command(args)))
        print(f"Plan only: no network requests or writes. Final zone: {args.through}.")
        return 0
    load_dotenv(PROJECT_ROOT / ".env")
    if args.storage_config:
        os.environ["ADSBD_STORAGE_CONFIG"] = str(args.storage_config)
    env = dict(os.environ)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", MPLBACKEND="Agg")
    env.setdefault("OMP_NUM_THREADS", "4")
    env.setdefault("MKL_NUM_THREADS", "4")
    env["ADSBD_CHROMA_PATH"] = str(args.db_path)
    tools = args.ffmpeg_dir or PROJECT_ROOT / "data/tools/ffmpeg/bin"
    if tools.is_dir():
        env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
    records, counts, error, code = [], {}, None, 1
    try:
        if args.command == "status":
            return check_status()
        preflight(args, env)
        if args.start_minio and execute(["docker", "compose", "up", "-d"], env):
            raise RuntimeError("Docker Compose could not start MinIO")
        prepare_storage(wait=args.start_minio)
        code, records = run_steps(plan, env)
        if code == 0:
            counts = verify_trusted()
            print(f"[OK] All three modalities reached Trusted: {counts}")
            if args.through == "exploitation":
                print(f"[OK] All four embedding collections verified in {args.db_path}")
    except (OSError, ValueError, RuntimeError, BotoCoreError, ClientError) as exc:
        error, code = redact(str(exc), env), 1
        print(f"[ERROR] {error}", file=sys.stderr)
    finally:
        if args.command == "run":
            args.report_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            report = args.report_dir / f"run_{timestamp}.json"
            report.write_text(json.dumps({"completed_at_utc": timestamp, "exit_code": code,
                                          "error": error, "steps": records, "trusted": counts,
                                          "through": args.through, "database": str(args.db_path)}, indent=2), encoding="utf-8")
            print(f"Run report: {report}")
    if code == 0 and args.serve:
        return execute(serve_command(args), env)
    return code


def serve_command(args):
    """Keep the interactive application in the foreground after preparation."""
    return [sys.executable, "-m", "streamlit", "run", str(PROJECT_ROOT / "streamlit_app.py"),
            "--server.address", "127.0.0.1", "--server.port", str(args.port),
            "--server.headless", "true",
            "--server.fileWatcherType", "poll", "--server.runOnSave", "true"]


if __name__ == "__main__":
    raise SystemExit(main())
