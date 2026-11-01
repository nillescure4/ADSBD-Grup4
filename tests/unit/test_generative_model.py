import json

import pytest
import requests

from src.app import generative_model as service


@pytest.fixture(autouse=True)
def clear_model_environment(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_MODEL", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "OLLAMA_HOST"):
        monkeypatch.delenv(name, raising=False)


def test_configuration_requires_explicit_provider_model_and_key(tmp_path):
    path = tmp_path / ".env"
    assert service.configuration(path)[0] is None
    path.write_text("LLM_PROVIDER=gemini\nLLM_MODEL=test-model\n")
    config, error = service.configuration(path)
    assert config is None and "GEMINI_API_KEY" in error
    path.write_text("LLM_PROVIDER=gemini\nLLM_MODEL=test-model\nGEMINI_API_KEY=fake-secret\n")
    config, error = service.configuration(path)
    assert not error and config.provider == "gemini"
    assert "fake-secret" not in repr(config)
    # Re-read the file so a server restart is not required after configuration.
    path.write_text("LLM_PROVIDER=ollama\nLLM_MODEL=test-local\n")
    assert service.configuration(path)[0].provider == "ollama"


@pytest.mark.parametrize("host", ["file:///tmp", "http://key:secret@localhost:11434", "http://localhost?key=secret", "http://[invalid"])
def test_invalid_local_server_addresses_are_rejected(tmp_path, host):
    path = tmp_path / ".env"
    path.write_text(f"LLM_PROVIDER=ollama\nLLM_MODEL=test\nOLLAMA_HOST={host}\n")
    assert service.configuration(path)[0] is None


def test_file_changes_override_stale_dotenv_process_copies(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "old-model")
    path.write_text("LLM_PROVIDER=ollama\nLLM_MODEL=new-model\n")
    assert service.configuration(path)[0].model == "new-model"
    path.write_text("LLM_PROVIDER=\nLLM_MODEL=\n")
    assert service.configuration(path)[0] is None


@pytest.mark.parametrize("provider", ["gemini", "openai", "ollama"])
def test_structured_request_and_response_for_each_provider(monkeypatch, provider):
    calls, output = [], {"nodes": [], "recommendations": []}
    class Response:
        status_code = 200
        def json(self):
            text = json.dumps(output)
            return {"gemini": {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]},
                    "openai": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]},
                    "ollama": {"done": True, "done_reason": "stop", "message": {"content": text}}}[provider]
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    monkeypatch.setattr(service.requests, "post", post)
    assert service.generate(service.ModelConfig(provider, "test-model", "secret"), "system", "user request", {"type": "object"}) == output
    assert len(calls) == 1 and calls[0][1]["timeout"] == (5, 60)
    assert "secret" not in json.dumps(calls[0][1]["json"])
    if provider == "openai":
        assert calls[0][1]["json"]["store"] is False
        assert calls[0][1]["json"]["text"]["format"]["strict"] is True
    elif provider == "gemini":
        assert calls[0][1]["headers"]["x-goog-api-key"] == "secret"
        assert calls[0][1]["json"]["generationConfig"]["responseJsonSchema"] == {"type": "object"}
    else:
        assert calls[0][1]["json"]["stream"] is False
        assert calls[0][1]["json"]["format"] == {"type": "object"}


@pytest.mark.parametrize("failure", ["timeout", "http", "invalid_json", "refused", "incomplete"])
def test_failed_model_responses_are_safe_and_never_fabricated(monkeypatch, failure):
    class Response:
        status_code = 429 if failure == "http" else 200
        def json(self):
            if failure == "invalid_json":
                return {}
            return {"status": "incomplete" if failure == "incomplete" else "completed",
                    "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}
    def post(*args, **kwargs):
        if failure == "timeout":
            raise requests.Timeout("Sensitive URL secret")
        return Response()
    monkeypatch.setattr(service.requests, "post", post)
    with pytest.raises(ValueError) as exc:
        service.generate(service.ModelConfig("openai", "test", "secret"), "system", "request", {})
    assert "secret" not in str(exc.value)


def test_transient_server_failure_is_retried_once(monkeypatch):
    calls = []
    class Response:
        def __init__(self, status):
            self.status_code = status
        def json(self):
            return {"done": True, "message": {"content": '{"ok": true}'}}
    def post(*args, **kwargs):
        calls.append(True)
        return Response(503 if len(calls) == 1 else 200)
    monkeypatch.setattr(service.requests, "post", post)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    assert service.generate(service.ModelConfig("ollama", "test"), "system", "request", {}) == {"ok": True}
    assert len(calls) == 2


def test_retries_are_bounded_for_persistent_outages(monkeypatch):
    calls = []
    class Response:
        status_code = 503
    def post(*args, **kwargs):
        calls.append(True)
        return Response()
    monkeypatch.setattr(service.requests, "post", post)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="temporarily unavailable"):
        service.generate(service.ModelConfig("ollama", "test"), "system", "request", {})
    assert len(calls) == 2
