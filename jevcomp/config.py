"""API key resolution for the CLI: --api-key > TYPESAFE_API_KEY env >
~/.jevcomp.json > ~/Desktop/api-keys.json (typesafe provider)."""

from __future__ import annotations

import json
import os
from typing import Optional

HOME_CONFIG = os.path.expanduser("~/.jevcomp.json")
API_KEYS_JSON = os.path.expanduser("~/Desktop/api-keys.json")


def find_api_key(explicit: Optional[str] = None) -> tuple:
    """Returns (key, source)."""
    if explicit:
        return explicit, "--api-key"
    env = os.environ.get("TYPESAFE_API_KEY")
    if env:
        return env, "env: TYPESAFE_API_KEY"
    if os.path.isfile(HOME_CONFIG):
        try:
            config = json.load(open(HOME_CONFIG, encoding="utf-8"))
            key = config.get("api_key")
            if key:
                return key, HOME_CONFIG
        except (ValueError, OSError):
            pass
    if os.path.isfile(API_KEYS_JSON):
        try:
            registry = json.load(open(API_KEYS_JSON, encoding="utf-8"))
            for provider in registry.get("providers", []):
                name = str(provider.get("id", "")) + str(provider.get("name", "")).lower()
                if "typesafe" not in name and "jev" not in name:
                    continue
                for entry in provider.get("keys", []):
                    value = entry.get("value")
                    if value and str(entry.get("status", "active")).lower() not in ("expired", "disabled", "truncated"):
                        return value, f"{API_KEYS_JSON} ({provider.get('id')})"
        except (ValueError, OSError):
            pass
    return None, None
