"""System One (Jev) wire format: request building and response validation."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional

SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def build_jev_request(
    state: Any,
    questions: Dict[str, Any],
    api_key: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """The HTTP request for one Jev call, for any transport."""
    return {
        "url": base_url or SYSTEM_ONE_URL,
        "method": "POST",
        "headers": {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        "body": json.dumps(
            {"model": model or DEFAULT_MODEL, "state": _to_jsonable(state), "questions": questions},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def parse_jev_response(status: int, ok: bool, text: str) -> Dict[str, Any]:
    """Validates a Jev response body; raises on anything but an answers object."""
    if not ok:
        raise RuntimeError(f"Jev request failed ({status}): {text[:200]}")
    try:
        parsed = json.loads(text)
    except ValueError:
        raise RuntimeError("Jev returned malformed JSON")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), dict):
        raise RuntimeError("Jev response is missing answers")
    return parsed


def noul_answer(answers: Dict[str, Any], name: str) -> float:
    """The noul probability of one answer; raises when it is not there."""
    answer = answers.get(name)
    if not isinstance(answer, dict):
        raise RuntimeError(f"Invalid Jev answer for {name}")
    value = answer.get("noul")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value or value in (float("inf"), float("-inf")):
        raise RuntimeError(f"Invalid Jev answer for {name}")
    return float(value)
