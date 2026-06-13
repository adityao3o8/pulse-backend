"""Gemini Flash client wrapper (ARCHITECTURE.md §6, §8).

The provider lives behind this module so it's swappable. We call Google's
Generative Language `generateContent` endpoint with raw httpx (no SDK
dependency).

The LLM never executes SQL — it only returns JSON text. Callers (planner.py)
parse that text with `extract_json` and validate it against an allowlist before
anything touches the database. This is the prompt-injection / correctness
boundary.

If GEMINI_API_KEY is unset, `query_llm` raises NoLLMKey; the planner catches it
and falls back to a deterministic generator so the system runs with no key.
"""
from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# gemini-1.5-flash is retired on the Generative Language API; gemini-2.5-flash is
# the current GA Flash successor. Override with GEMINI_MODEL if needed.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = os.getenv(
    "GEMINI_URL",
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
)
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "30.0"))


class NoLLMKey(RuntimeError):
    """Raised when GEMINI_API_KEY is not configured."""


class LLMQuotaError(RuntimeError):
    """Raised when Gemini returns 429 (rate limit) or 403 (quota exceeded).

    Callers fall back to the deterministic generator and tag the decision source
    as 'quota_exceeded_fallback' so the UI can surface a friendly notice.
    """


class LLMParseError(ValueError):
    """Raised when the model response cannot be coerced to a JSON object."""


def _api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY")


def query_llm(prompt: str, *, system: str | None = None, json_mode: bool = True) -> str:
    """Call Gemini and return the raw model response text.

    Raises NoLLMKey if no API key is configured (planner uses this to switch to
    the offline fallback). Raises httpx errors on transport/HTTP failure.
    """
    key = _api_key()
    if not key:
        raise NoLLMKey("GEMINI_API_KEY is not set")

    generation_config: dict = {"temperature": 0.4}
    if json_mode:
        # Gemini honours a JSON response MIME type — yields a parseable object,
        # no markdown fences.
        generation_config["responseMimeType"] = "application/json"

    body: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    if system:
        # Gemini takes the system prompt out-of-band rather than as a turn.
        body["systemInstruction"] = {"parts": [{"text": system}]}

    with httpx.Client(timeout=GEMINI_TIMEOUT) as client:
        resp = client.post(
            GEMINI_URL,
            params={"key": key},
            headers={"Content-Type": "application/json"},
            json=body,
        )
        # 429 = rate limit, 403 = quota/billing exhausted. Both mean "out of
        # budget for now" rather than a bug — degrade to the offline generator.
        if resp.status_code in (429, 403):
            logger.warning(
                "Gemini quota/rate limit hit (HTTP %s): %s",
                resp.status_code, resp.text[:200],
            )
            raise LLMQuotaError(f"Gemini returned HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()

    return data["candidates"][0]["content"]["parts"][0]["text"]


def extract_json(text: str) -> dict:
    """Coerce a model response to a JSON object, defensively.

    Tries a direct parse first; then strips markdown fences and extracts the
    first balanced {...} block. Raises LLMParseError if nothing parses.
    """
    if text is None:
        raise LLMParseError("empty response")

    candidate = text.strip()

    # Fast path: already valid JSON.
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Strip ```json ... ``` or ``` ... ``` fences.
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Last resort: first balanced {...} substring.
    start = candidate.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(candidate)):
            if candidate[i] == "{":
                depth += 1
            elif candidate[i] == "}":
                depth -= 1
                if depth == 0:
                    chunk = candidate[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break

    raise LLMParseError(f"could not extract JSON object from response: {text[:200]!r}")
