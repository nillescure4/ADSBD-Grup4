import json
import os
from pathlib import Path
import sys

import pytest

from src.pipelines import run_pipeline as pipeline


def test_plan_shares_rawg_manifest_and_indexes_all_modalities(tmp_path):
    args = pipeline.parse_args(["--rawg-output", str(tmp_path / "images"), "--audio-output", str(tmp_path / "music")])
    plan = pipeline.build_plan(args)
    modules = [step.module for step in plan]
    assert len(plan) == 23
    assert modules.index("src.trusted.audio.analyze_audio_quality") < modules.index("src.trusted.audio.trusted_audio")
    assert modules[-5:] == ["src.exploitation.text.build_embeddings", "src.exploitation.images.build_embeddings",
                            "src.exploitation.audio.embedding_audio", "src.exploitation.text.build_clap_embeddings",
                            "src.pipelines.verify_exploitation"]
    assert str(tmp_path / "images/manifests/games.jsonl") in plan[1].arguments
    assert str(tmp_path / "music/raw/files") in next(s.arguments for s in plan if s.module.endswith("audio_landing"))
    assert "--all" in next(s.arguments for s in plan if s.module.endswith("audio_persistent"))


def test_skip_collection_has_no_source_requests():
    plan = pipeline.build_plan(pipeline.parse_args(["--skip-collection"]))
    assert len(plan) == 20
    assert not any("collect_" in step.module or step.module.endswith("text_to_temporal_landing") for step in plan)


def test_trusted_only_and_embeddings_only_have_independent_plans(tmp_path):
    trusted = pipeline.build_plan(pipeline.parse_args(["--through", "trusted"]))
    assert len(trusted) == 18 and not any("exploitation" in s.module for s in trusted)
    plan = pipeline.build_plan(pipeline.parse_args(["--embeddings-only", "--db-path", str(tmp_path)]))
    assert len(plan) == 5
    assert all("exploitation" in s.module for s in plan)
    assert all(str(tmp_path) in s.arguments for s in plan)


@pytest.mark.parametrize("flag", ["--embeddings-only", "--serve"])
def test_trusted_only_rejects_incompatible_options(flag):
    with pytest.raises(SystemExit):
        pipeline.parse_args(["--through", "trusted", flag])


@pytest.mark.parametrize("outcome", [0, 1])
def test_application_starts_only_after_success_and_uses_selected_database(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(pipeline, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(pipeline, "preflight", lambda *args: None)
    monkeypatch.setattr(pipeline, "prepare_storage", lambda **kwargs: None)
    monkeypatch.setattr(pipeline, "verify_trusted", lambda: {"text_partitions": 1, "images": 8, "audio": 2})
    monkeypatch.setattr(pipeline, "run_steps", lambda *args: (outcome, []))
    calls = []
    monkeypatch.setattr(pipeline, "execute", lambda command, env: calls.append((command, env)) or 0)
    result = pipeline.main(["--embeddings-only", "--serve", "--db-path", str(tmp_path / "chroma"),
                            "--report-dir", str(tmp_path / "reports")])
    assert result == outcome
    assert len(calls) == (0 if outcome else 1)
    if calls:
        command, env = calls[0]
        assert command[:4] == [sys.executable, "-m", "streamlit", "run"]
        assert env["ADSBD_CHROMA_PATH"] == str(tmp_path / "chroma")
    report = json.loads(next((tmp_path / "reports").glob("run_*.json")).read_text())
    assert report["exit_code"] == outcome


def test_dry_run_never_connects_or_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "create_minio_client", lambda: pytest.fail("Must not connect"))
    monkeypatch.setattr(pipeline, "load_dotenv", lambda *args: pytest.fail("Must not load credentials"))
    reports = tmp_path / "reports"
    assert pipeline.main(["--dry-run", "--report-dir", str(reports)]) == 0
    assert not reports.exists()
    assert "src.trusted.audio.trusted_audio" in capsys.readouterr().out


@pytest.mark.parametrize("code", [1, 2, 7])
def test_failure_blocks_all_downstream_stages(code):
    seen = []
    def executor(command, env):
        seen.append(command)
        return code
    outcome, rows = pipeline.run_steps([pipeline.Step("one", "first"), pipeline.Step("two", "second")], {}, executor=executor)
    assert outcome == 1 and len(rows) == len(seen) == 1
    assert seen[0][:2] == [sys.executable, "-m"]
    assert rows[0]["exit_code"] == code


def test_zero_exit_does_not_hide_collector_errors(tmp_path):
    (tmp_path / "collection_summary.json").write_text(json.dumps({"errors": 1}))
    outcome, rows = pipeline.run_steps([pipeline.Step("collect", "collector", collection_root=tmp_path, modality="image")], {}, executor=lambda *args: 0)
    assert outcome == 1 and "recorded errors" in rows[0]["error"]


def test_empty_collection_is_rejected(tmp_path):
    (tmp_path / "collection_summary.json").write_text('{"errors": 0}')
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests/games.jsonl").write_text("")
    with pytest.raises(ValueError, match="empty"):
        pipeline.check_collection(tmp_path, "image")


def test_secret_redaction_in_plain_text_and_encoded_urls():
    env = {"RAWG_API_KEY": "api/secret", "MINIO_ROOT_PASSWORD": "password-value"}
    result = pipeline.redact("?key=api%2Fsecret api/secret password-value", env)
    assert "api" not in result and "password-value" not in result
    assert result.count("[REDACTED]") == 3


def test_storage_preparation_creates_only_missing_backbone_buckets(monkeypatch):
    created = []
    class Client:
        def create_bucket(self, Bucket):
            created.append(Bucket)
    monkeypatch.setattr(pipeline, "create_minio_client", Client)
    monkeypatch.setattr(pipeline, "list_bucket_names", lambda client: ["1.landing-zone"])
    pipeline.prepare_storage()
    assert created == ["2.formatted-zone", "3.trusted-zone"]


def test_missing_modality_cannot_be_reported_as_success(monkeypatch):
    monkeypatch.setattr(pipeline, "create_minio_client", object)
    monkeypatch.setattr(pipeline, "list_object_keys", lambda *args: [])
    with pytest.raises(ValueError, match="no Trusted outputs"):
        pipeline.verify_trusted()


def test_preflight_fails_before_collection_without_key():
    with pytest.raises(ValueError, match="RAWG_API_KEY"):
        pipeline.preflight(pipeline.parse_args([]), {})


@pytest.mark.parametrize("flag", ["--max-games", "--max-audio-files", "--audio-max-items", "--screenshots-per-game"])
def test_positive_batch_limits(flag):
    with pytest.raises(SystemExit):
        pipeline.parse_args([flag, "0"])


def test_child_process_keeps_environment_working_directory_and_hides_secrets(monkeypatch, capsys):
    class Child:
        stdout = ["progress api-key-secret\n"]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait(self): return 0
    def popen(command, **options):
        assert command == ["python", "-m", "example"]
        assert options["cwd"] == pipeline.PROJECT_ROOT
        assert options["env"]["ADSBD_STORAGE_CONFIG"] == "isolated.yml"
        assert not options.get("shell")
        return Child()
    monkeypatch.setattr(pipeline.subprocess, "Popen", popen)
    assert pipeline.execute(["python", "-m", "example"], {"RAWG_API_KEY": "api-key-secret", "ADSBD_STORAGE_CONFIG": "isolated.yml"}) == 0
    assert "api-key-secret" not in capsys.readouterr().out
