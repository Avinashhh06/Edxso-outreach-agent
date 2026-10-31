"""Thin, dependency-light LLM clients (plain HTTPS via requests).

Providers
  gemini             - Google Generative Language REST API
  openai_compatible  - any /chat/completions server: OpenAI, Groq, Together, Ollama, ...
  none               - no LLM; personalization falls back to the offline template engine

No model name is assumed: LLM_MODEL must be set by you, so nothing is guessed.
"""
from __future__ import annotations

import json
import re
import time

import requests

from .config import Settings


class ConfigError(RuntimeError):
    """Raised when a required setting is missing. The message says exactly what to provide."""


class LLMError(RuntimeError):
    pass


def check_config(s: Settings) -> None:
    p = s.llm_provider
    if p == "none":
        return
    if p not in {"gemini", "openai_compatible"}:
        raise ConfigError(f"LLM_PROVIDER='{p}' unsupported. Use gemini | openai_compatible | none.")
    missing = []
    if not s.llm_api_key and not (p == "openai_compatible" and "localhost" in s.llm_base_url):
        missing.append("LLM_API_KEY")
    if not s.llm_model:
        missing.append("LLM_MODEL")
    if p == "openai_compatible" and not s.llm_base_url:
        missing.append("LLM_BASE_URL")
    if missing:
        raise ConfigError(f"Missing in .env for provider '{p}': {', '.join(missing)}")


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"No JSON object in model output: {text[:120]!r}")
    return json.loads(text[start:end + 1])


def complete_json(s: Settings, system: str, user: str, timeout: int = 60, retries: int = 2) -> dict:
    """Call the configured provider, return parsed JSON. Retries on transient HTTP errors."""
    check_config(s)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if s.llm_provider == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{s.llm_model}:generateContent"
                r = requests.post(
                    url,
                    headers={"x-goog-api-key": s.llm_api_key, "Content-Type": "application/json"},
                    json={
                        "systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"role": "user", "parts": [{"text": user}]}],
                        "generationConfig": {"temperature": 0.8, "responseMimeType": "application/json"},
                    },
                    timeout=timeout,
                )
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            else:
                headers = {"Content-Type": "application/json"}
                if s.llm_api_key:
                    headers["Authorization"] = f"Bearer {s.llm_api_key}"
                r = requests.post(
                    s.llm_base_url.rstrip("/") + "/chat/completions",
                    headers=headers,
                    json={
                        "model": s.llm_model,
                        "temperature": 0.8,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    },
                    timeout=timeout,
                )
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"]
            return _extract_json(text)
        except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError, LLMError) as exc:
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {400, 401, 403, 404}:      # not transient: bad key/model/request
                raise LLMError(f"LLM request rejected (HTTP {status}): {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"LLM failed after {retries + 1} attempts: {last}")
