"""Optional AI analysis layer.

This module is completely independent from the migration engine.
If AI credentials are missing or the call fails, migration continues
normally.

The AI receives a safe subset of the migrated record (no credentials,
no connection strings) and returns structured JSON with an analysis
action.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import requests

from . import config
from .logger import get_logger, log_error


# ---------------------------------------------------------------------------
# Action types the AI may return
# ---------------------------------------------------------------------------

VALID_ACTIONS = {
    "analyze",
    "summarize",
    "classify",
    "detect_anomaly",
    "recommend",
    "notify",
    "no_action",
}


# ---------------------------------------------------------------------------
# Provider URL templates
# ---------------------------------------------------------------------------

_PROVIDER_URLS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}


def _is_available() -> bool:
    """Return True if AI is enabled and configured."""
    return (
        config.AI_ENABLED
        and bool(config.AI_API_KEY)
        and bool(config.AI_PROVIDER)
        and config.AI_PROVIDER.lower() in _PROVIDER_URLS
    )


def analyze_record(
    *,
    table_name: str,
    record: Dict[str, Any],
    migration_result: str = "success",
) -> Optional[Dict[str, Any]]:
    """Send a safe record summary to the AI provider.

    Returns the parsed AI response or ``None`` if AI is unavailable /
    fails.  The returned dict has at least:

        {
            "action": "<one of VALID_ACTIONS>",
            "priority": "low|medium|high",
            "reason": "...",
            "recommendation": "...",
            "confidence": 0.0-1.0
        }
    """
    if not _is_available():
        return None

    # Strip sensitive fields from the record before sending.
    safe_record = _sanitize_record(record)

    prompt = (
        f"You are a data-quality analyst for an automated database migration.\n"
        f"A record was migrated from Supabase table '{table_name}' to MongoDB.\n"
        f"Migration result: {migration_result}.\n\n"
        f"Record data:\n{json.dumps(safe_record, indent=2, default=str)}\n\n"
        f"Respond with a JSON object containing these fields:\n"
        f"  action: one of {sorted(VALID_ACTIONS)}\n"
        f"  priority: low | medium | high\n"
        f"  reason: brief explanation\n"
        f"  recommendation: what (if anything) should be done\n"
        f"  confidence: 0.0 to 1.0\n"
        f"Return ONLY valid JSON."
    )

    try:
        return _call_provider(prompt)
    except Exception as exc:
        log_error("AI analysis failed (non-critical)", exc)
        return None


# ---------------------------------------------------------------------------
# Provider calls
# ---------------------------------------------------------------------------

def _call_provider(prompt: str) -> Optional[Dict[str, Any]]:
    """Route to the correct provider and parse the response."""
    provider = config.AI_PROVIDER.lower()
    if provider in ("openai", "openrouter"):
        return _call_openai_compatible(prompt, provider)
    elif provider == "anthropic":
        return _call_anthropic(prompt)
    elif provider == "gemini":
        return _call_gemini(prompt)
    else:
        log_error("AI provider", Exception(f"Unknown provider: {provider}"))
        return None


def _call_openai_compatible(prompt: str, provider: str) -> Optional[Dict[str, Any]]:
    """Call OpenAI-compatible API (OpenAI or OpenRouter)."""
    url = _PROVIDER_URLS[provider]
    headers = {
        "Authorization": f"Bearer {config.AI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.AI_MODEL or "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 512,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _parse_json_response(content)


def _call_anthropic(prompt: str) -> Optional[Dict[str, Any]]:
    """Call Anthropic API."""
    url = _PROVIDER_URLS["anthropic"]
    headers = {
        "x-api-key": config.AI_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.AI_MODEL or "claude-3-haiku-20240307",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    content = resp.json()["content"][0]["text"]
    return _parse_json_response(content)


def _call_gemini(prompt: str) -> Optional[Dict[str, Any]]:
    """Call Google Gemini API."""
    model = config.AI_MODEL or "gemini-1.5-flash"
    url = _PROVIDER_URLS["gemini"].format(model=model)
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512},
    }
    resp = requests.post(
        url,
        headers=headers,
        json=payload,
        params={"key": config.AI_API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_json_response(content)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_response(text: str) -> Optional[Dict[str, Any]]:
    """Extract and validate a JSON object from AI text output."""
    text = text.strip()
    # Handle markdown code fences.
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log_error("AI response", Exception("Invalid JSON in AI response"))
        return None

    # Validate action.
    action = data.get("action", "no_action")
    if action not in VALID_ACTIONS:
        data["action"] = "no_action"

    return data


def _sanitize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Remove potentially sensitive fields before sending to AI."""
    sensitive_keys = {
        "password", "secret", "token", "api_key", "apikey",
        "authorization", "auth_token", "access_token", "refresh_token",
        "private_key", "credential", "ssn", "social_security",
    }
    clean = {}
    for k, v in record.items():
        if k.lower() in sensitive_keys:
            clean[k] = "***REDACTED***"
        elif k.startswith("_migration"):
            continue  # Never send migration metadata to AI.
        else:
            clean[k] = v
    return clean
