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


@pytest.mark.parametrize("provider", ["gemini", "openai", "ollama"])
def test_labelled_images_are_attached_to_multimodal_requests(monkeypatch, provider):
    bodies = []
    def post(url, **kwargs):
        bodies.append(kwargs["json"])
        return type("Response", (), {"status_code": 400})()
    monkeypatch.setattr(service.requests, "post", post)
    image = {"label": "Candidate game ID 28", "mime_type": "image/jpeg", "data": "anBlZw=="}
    with pytest.raises(ValueError, match="HTTP 400"):
        service.generate(service.ModelConfig(provider, "vision", "secret"), "system", "prompt", {}, images=[image])
    body = json.dumps(bodies[0])
    assert "Candidate game ID 28" in body and "anBlZw==" in body and "secret" not in body


def test_configured_gemini_vision_model_uses_deliberate_reasoning(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs)
        return type("Response", (), {"status_code": 400})()
    monkeypatch.setattr(service.requests, "post", post)
    with pytest.raises(ValueError):
        service.generate(service.ModelConfig("gemini", "gemini-3.5-flash-lite", "secret"), "system", "prompt", {},
                         images=[{"label": "Candidate game ID 28", "mime_type": "image/jpeg", "data": "anBlZw=="}])
    assert calls[0]["json"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}
    assert calls[0]["timeout"] == (5, 120)


def test_connection_failure_before_a_response_is_retried_once(monkeypatch):
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {"done": True, "message": {"content": '{"ok": true}'}}
    def post(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise requests.ConnectionError("Connection interrupted")
        return Response()
    monkeypatch.setattr(service.requests, "post", post)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    assert service.generate(service.ModelConfig("ollama", "test"), "system", "request", {}) == {"ok": True}
    assert len(calls) == 2
