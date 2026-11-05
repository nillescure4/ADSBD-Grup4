from pathlib import Path
import socket
import sys

from streamlit.testing.v1 import AppTest

from src.common.pipeline_progress import STATUS_ENV, create_progress, read_progress, step_progress, update_progress
from src.pipelines import run_pipeline as pipeline
from src.pipelines import dashboard


def test_stage_failure_records_progress_and_does_not_mark_ready(tmp_path):
    plan = [pipeline.Step("First", "first"), pipeline.Step("Second", "second")]
    path = create_progress(tmp_path, plan)
    code, rows = pipeline.run_steps(plan, {STATUS_ENV: str(path)}, executor=lambda *args: 7)
    state = read_progress(path)
    assert code == 1 and len(rows) == 1
    assert [row["status"] for row in state["steps"]] == ["failed", "pending"]
    assert state["status"] == "running"  # Only final orchestration verification may publish ready.


def test_subprocess_output_is_bounded_and_redacted_before_publication(tmp_path):
    path = create_progress(tmp_path, [])
    import os
    env = {**os.environ, STATUS_ENV: str(path), "RAWG_API_KEY": "private-test-value"}
    code = pipeline.execute([sys.executable, "-c", "print('private-test-value')"], env)
    assert code == 0
    for index in range(90):
        update_progress(path, log=str(index))
    assert len(read_progress(path)["log"]) == 60
    assert "private-test-value" not in path.read_text()
    assert not list(tmp_path.glob(".progress-*"))


def test_preflight_failure_is_visible_without_launching_zone_scripts(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "preflight", lambda *args: (_ for _ in ()).throw(ValueError("FFmpeg missing")))
    monkeypatch.setattr(pipeline, "prepare_storage", lambda **kwargs: (_ for _ in ()).throw(AssertionError("No storage yet")))
    class Dashboard:
        def poll(self):
            return None
        def wait(self):
            return 0
    monkeypatch.setattr(pipeline, "start_dashboard", lambda *args: Dashboard())
    assert pipeline.main(["--serve", "--report-dir", str(tmp_path)]) == 1
    state = read_progress(next(tmp_path.glob("live_*.json")))
    assert state["status"] == "failed" and state["error"] == "FFmpeg missing"
    assert all(row["status"] == "pending" for row in state["steps"])


def test_progress_page_blocks_navigation_until_ready(tmp_path, monkeypatch):
    path = create_progress(tmp_path, pipeline.build_plan(pipeline.parse_args([])))
    update_progress(path, log="Technical details must stay outside the interface")
    monkeypatch.setenv(STATUS_ENV, str(path))
    from src.app.views import home
    monkeypatch.setattr(home, "dataset_status", lambda: ({"games": 8}, None))
    monkeypatch.setattr(home, "image_status", lambda: (8, None))
    monkeypatch.setattr(home, "audio_status", lambda: (2, None))
    entrypoint = Path(__file__).resolve().parents[2] / "streamlit_app.py"
    app = AppTest.from_file(entrypoint, default_timeout=15).run()
    assert not app.exception and app.title[0].value == "Preparing your data"
    assert app.metric[0].label == "Stages completed"
    assert len(app.metric) == 2
    assert app.metric[1].label == "Total time"
    assert not app.sidebar.caption
    assert app.metric[0].value == "0 / 6"
    assert not app.expander and not app.code
    table = app.dataframe[0].value
    assert table.columns[-1] == "Duration" and table["Duration"].tolist() == ["—"] * 6
    assert table["Stage"].tolist() == ["Sources", "Landing zone", "Formatted zone", "Trusted zone", "Exploitation zone", "Verification"]
    assert table["Data source"].tolist() == ["RAWG / OpenGameArt", "Sources", "Landing zone", "Formatted zone", "Trusted zone", "Exploitation zone"]
    assert not any(header.value == "Three ways to explore" for header in app.header)
    import time
    step_progress(path, 20, "running", started_at=time.time() - 125)
    app.run()
    assert any("Trusted screenshots with Jina CLIP v2" in info.value for info in app.info)
    assert app.subheader[0].value == "Exploitation zone"
    assert app.dataframe[0].value.iloc[4]["Duration"].startswith("2 min ")
    update_progress(path, status="failed", error="Source unavailable")
    app.run()
    assert app.error and app.title[0].value == "Preparing your data"
    update_progress(path, status="ready", phase="Verified")
    app.run()
    assert not app.exception and app.title[0].value == "One game. Many ways to discover it."


def test_different_runs_never_reuse_previous_ready_state(tmp_path):
    first = create_progress(tmp_path, [])
    update_progress(first, status="ready")
    second = create_progress(tmp_path, [])
    assert first != second and read_progress(second)["status"] == "running"


def test_occupied_port_does_not_launch_or_replace_another_server(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No new process")))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(RuntimeError, match="already in use"):
            dashboard.start_dashboard([], {}, listener.getsockname()[1], tmp_path)


def test_server_startup_failure_stops_its_own_process(tmp_path, monkeypatch):
    import pytest
    events = []
    class StoppedServer:
        def poll(self):
            return 1
        def terminate(self):
            events.append("terminate")
        def wait(self, timeout):
            events.append("wait")
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda *args, **kwargs: StoppedServer())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with pytest.raises(RuntimeError, match="stopped during startup"):
        dashboard.start_dashboard([], {}, port, tmp_path)
    assert events == ["terminate", "wait"]


def test_interrupt_stops_dashboard_and_never_publishes_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "preflight", lambda *args: None)
    monkeypatch.setattr(pipeline, "prepare_storage", lambda **kwargs: None)
    server = object()
    stopped = []
    monkeypatch.setattr(pipeline, "start_dashboard", lambda *args: server)
    monkeypatch.setattr(pipeline, "stop_dashboard", lambda process: stopped.append(process))
    monkeypatch.setattr(pipeline, "execute", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert pipeline.main(["--embeddings-only", "--serve", "--report-dir", str(tmp_path)]) == 130
    state = read_progress(next(tmp_path.glob("live_*.json")))
    assert state["status"] == "interrupted" and state["steps"][0]["status"] == "failed"
    assert stopped == [server]
