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

# Groq is the fallback provider, tried ONLY when Gemini returns a quota/rate-limit
# error (429/403). LLaMA 3.3 70b via Groq's OpenAI-compatible endpoint.
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_TIMEOUT = float(os.getenv("GROQ_TIMEOUT", "30.0"))


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


def query_llm(prompt: str, *, system: str | None = None, json_mode: bool = True) -> tuple[str, str]:
    """Run the provider cascade and return (response_text, provider_tag).

    Priority:
      1. Gemini 2.5 Flash — always tried first. provider_tag = "gemini".
      2. Groq LLaMA 3.3 70b — tried ONLY when Gemini returns a 429/403 quota
         error. provider_tag = "groq".
      3. Neither available → raise. Callers degrade to a deterministic generator.

    Raises:
      NoLLMKey      — GEMINI_API_KEY is unset (Groq is NOT used in this case;
                      it is reserved strictly for Gemini quota exhaustion). The
                      planner catches this and tags the decision "fallback".
      LLMQuotaError — Gemini hit quota AND Groq is unavailable or also failed.
                      The planner catches this and tags "quota_exceeded_fallback".
      httpx.HTTPError on non-quota transport/HTTP failure.
    """
    try:
        return _query_gemini(prompt, system=system, json_mode=json_mode), "gemini"
    except LLMQuotaError as exc:
        # Gemini is out of budget — this is the ONLY trigger for the Groq fallback.
        logger.warning("Gemini quota exhausted (%s) — falling back to Groq", exc)
        try:
            return query_groq(prompt, system=system, json_mode=json_mode), "groq"
        except NoLLMKey:
            # No Groq key configured: surface as a quota error so callers degrade
            # to the deterministic generator and tag "quota_exceeded_fallback".
            raise LLMQuotaError("Gemini quota exhausted and GROQ_API_KEY not set") from exc
        except httpx.HTTPError as groq_exc:
            logger.warning("Groq fallback also failed (%s)", groq_exc)
            raise LLMQuotaError(
                f"Gemini quota exhausted and Groq failed: {groq_exc}"
            ) from exc


def _query_gemini(prompt: str, *, system: str | None = None, json_mode: bool = True) -> str:
    """Call Gemini and return the raw model response text.

    Raises NoLLMKey if no API key is configured, LLMQuotaError on 429/403, and
    httpx errors on other transport/HTTP failures.
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
        # budget for now" rather than a bug — trigger the Groq fallback.
        if resp.status_code in (429, 403):
            logger.warning(
                "Gemini quota/rate limit hit (HTTP %s): %s",
                resp.status_code, resp.text[:200],
            )
            raise LLMQuotaError(f"Gemini returned HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()

    return data["candidates"][0]["content"]["parts"][0]["text"]


def query_groq(prompt: str, system: str | None = None, *, json_mode: bool = True) -> str:
    """Call Groq's OpenAI-compatible chat endpoint and return the response text.

    Temperature is 0.6 (slightly higher than Gemini's 0.4) for more creative,
    varied analysis when Groq steps in as the fallback. Raises NoLLMKey when
    GROQ_API_KEY is unset; httpx errors on transport/HTTP failure.
    """
    if not GROQ_API_KEY:
        raise NoLLMKey("GROQ_API_KEY not set")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.6,
    }
    if json_mode:
        # Only force JSON when the caller expects an object; the adaptation
        # summary asks for plain prose (json_mode=False) and must not be forced.
        payload["response_format"] = {"type": "json_object"}
    with httpx.Client(timeout=GROQ_TIMEOUT) as client:
        response = client.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


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
