"""Server-side structured generation; credentials never enter the UI or prompt."""

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit

from dotenv import dotenv_values
import requests


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    api_key: str = field(default="", repr=False)
    host: str = "http://127.0.0.1:11434"


def configuration(env_path=ENV_PATH):
    # Other pipeline helpers call load_dotenv; prefer current file values over
    # their potentially stale copies in the process environment.
    values = {**os.environ, **dotenv_values(env_path)}
    provider = (values.get("LLM_PROVIDER") or "").strip().lower()
    model = (values.get("LLM_MODEL") or "").strip()
    if provider not in {"gemini", "openai", "ollama"} or not model:
        return None, "Configure LLM_PROVIDER and LLM_MODEL in the server's .env file to enable the AI game finder."
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", model):
        return None, "The configured model name is invalid."
    key = (values.get("GEMINI_API_KEY") or values.get("GOOGLE_API_KEY") or "") if provider == "gemini" else (values.get("OPENAI_API_KEY") or "")
    if provider != "ollama" and not key.strip():
        return None, f"Add {('GEMINI_API_KEY' if provider == 'gemini' else 'OPENAI_API_KEY')} to the server's .env file. Never enter API keys in your search request."
    host = (values.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
    if provider == "ollama":
        try:
            parsed = urlsplit(host)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Invalid host")
        except ValueError:
            return None, "OLLAMA_HOST must be an HTTP(S) address without credentials, query parameters or fragments."
    return ModelConfig(provider, model, key.strip(), host), None


def generate(config: ModelConfig, system: str, prompt: str, schema: dict, *, images=None) -> dict:
    images = images or []
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if config.provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(config.model, safe='')}:generateContent"
        headers = {"x-goog-api-key": config.api_key}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema, "maxOutputTokens": 10000}}
        for image in images:
            body["contents"][0]["parts"].extend([
                {"text": image["label"]},
                {"inlineData": {"mimeType": image["mime_type"], "data": image["data"]}},
            ])
        if images and config.model == "gemini-3.5-flash-lite":
            # Balanced partitions and screenshot evidence require deliberate reasoning.
            body["generationConfig"].update(thinkingConfig={"thinkingLevel": "MEDIUM"}, maxOutputTokens=20000)
    elif config.provider == "openai":
        url, headers = "https://api.openai.com/v1/responses", {"Authorization": f"Bearer {config.api_key}"}
        body = {"model": config.model, "instructions": system, "input": prompt, "store": False,
                "max_output_tokens": 10000,
                "text": {"format": {"type": "json_schema", "name": "game_tree", "strict": True, "schema": schema}}}
        if images:
            content = [{"type": "input_text", "text": prompt}]
            for image in images:
                content.extend([{"type": "input_text", "text": image["label"]},
                                {"type": "input_image", "image_url": f"data:{image['mime_type']};base64,{image['data']}"}])
            body["input"] = [{"role": "user", "content": content}]
    elif config.provider == "ollama":
        url, headers = config.host + "/api/chat", {}
        body = {"model": config.model, "messages": messages, "format": schema, "stream": False}
        if images:
            # Separate messages preserve the association between each image and its game.
            body["messages"].extend({"role": "user", "content": image["label"], "images": [image["data"]]} for image in images)
    else:
        raise ValueError("Unsupported AI provider.")
    try:
        for attempt in range(2):
            try:
                response = requests.post(url, json=body, headers=headers, timeout=(5, 120 if images else 60))
            except requests.ConnectionError:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise
            if attempt == 0 and response.status_code in {500, 502, 503, 504}:
                time.sleep(1)
                continue
            break
        if response.status_code != 200:
            if response.status_code in {500, 502, 503, 504}:
                raise ValueError("The AI service is temporarily unavailable after a retry. Please try again later.")
            raise ValueError(f"The AI service rejected the request (HTTP {response.status_code}). Check the model, credentials and quota.")
        data = response.json()
        if config.provider == "gemini":
            candidate = data["candidates"][0]
            if candidate.get("finishReason") != "STOP":
                raise ValueError("The AI did not complete the decision tree. Please try again.")
            text = "".join(part.get("text", "") for part in candidate["content"]["parts"] if not part.get("thought"))
        elif config.provider == "openai":
            if data.get("status") != "completed":
                raise ValueError("The AI did not complete the decision tree. Please try again.")
            text = "".join(part["text"] for output in data["output"] if output.get("type") == "message"
                           for part in output.get("content", []) if part.get("type") == "output_text")
        else:
            if not data.get("done") or data.get("done_reason") not in {None, "stop"}:
                raise ValueError("The AI did not complete the decision tree. Please try again.")
            text = data["message"]["content"]
        return json.loads(text)
    except requests.Timeout:
        raise ValueError("The AI service timed out while preparing the questions. Please try again.") from None
    except requests.RequestException as exc:
        LOGGER.warning("AI transport failed (%s)", type(exc).__name__)
        raise ValueError("The AI service could not be reached or timed out. Please try again.") from None
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError):
        raise ValueError("The AI returned an unreadable response. No recommendation was created.") from None
