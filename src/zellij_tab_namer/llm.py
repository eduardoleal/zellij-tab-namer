"""Optional OpenAI-compatible label compression."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Mapping, Optional
from urllib import request


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = ""
    api_key: Optional[str] = None
    model: str = "llama3.2"
    timeout_seconds: float = 0.8


def config_from_env(env: Optional[Mapping[str, str]] = None) -> LLMConfig:
    values = env if env is not None else os.environ
    timeout = _float_or_default(values.get("ZELLIJ_TAB_NAMER_TIMEOUT"), 0.8)
    api_key = values.get("ZELLIJ_TAB_NAMER_API_KEY") or None
    return LLMConfig(
        base_url=(values.get("ZELLIJ_TAB_NAMER_BASE_URL") or "").rstrip("/"),
        api_key=api_key,
        model=values.get("ZELLIJ_TAB_NAMER_MODEL") or "llama3.2",
        timeout_seconds=timeout,
    )


def compress_label(
    text: str,
    max_chars: int,
    config: LLMConfig,
    opener=request.urlopen,
) -> Optional[str]:
    """Return a compact label, or None when the endpoint is unavailable."""

    if not config.base_url or max_chars < 1:
        return None

    payload = {
        "model": config.model,
        "temperature": 0,
        "max_tokens": 24,
        "messages": [
            {
                "role": "system",
                "content": "Return only a short, readable Zellij tab label.",
            },
            {
                "role": "user",
                "content": (
                    f"Source label: {text}\n"
                    f"Maximum characters: {max_chars}\n"
                    "Use compact words or slash-separated nouns. No explanation."
                ),
            },
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    http_request = request.Request(
        f"{config.base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if config.api_key:
        http_request.add_header("Authorization", f"Bearer {config.api_key}")

    try:
        with opener(http_request, timeout=config.timeout_seconds) as response:
            raw_response = response.read()
        parsed = json.loads(raw_response.decode("utf-8"))
        content = parsed["choices"][0]["message"]["content"]
    except Exception:
        return None

    cleaned = _clean_model_label(str(content))
    if not cleaned or len(cleaned) > max_chars:
        return None
    return cleaned


def _clean_model_label(label: str) -> str:
    cleaned = re.sub(r"\s+", " ", label).strip()
    cleaned = cleaned.strip("`\"'")
    return cleaned.strip()


def _float_or_default(value: Optional[str], default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default
