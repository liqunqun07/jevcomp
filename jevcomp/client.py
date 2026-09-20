"""JevClient: asks Jev over HTTP with urllib (zero dependencies)."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .request import build_jev_request, parse_jev_response
from .types import JevAsker, JevQuestions, JevState

_PROBE_CACHE: Dict[str, bool] = {}


def _local_proxy_available(host: str = "127.0.0.1", port: int = 7890) -> bool:
    """True once per process when a local mixed proxy (Mihomo/Clash) answers."""
    key = f"{host}:{port}"
    if key not in _PROBE_CACHE:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                _PROBE_CACHE[key] = True
        except OSError:
            _PROBE_CACHE[key] = False
    return _PROBE_CACHE[key]


def _build_opener() -> urllib.request.OpenerDirector:
    """Proxy resolution: JEVCOMP_PROXY > https_proxy env > local Mihomo 7890
    when listening (direct TLS to api.typesafe.ai is reset on this network)."""
    proxy_url = (
        os.environ.get("JEVCOMP_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
    )
    if not proxy_url and _local_proxy_available():
        proxy_url = "http://127.0.0.1:7890"
    if proxy_url:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url})
        )
    return urllib.request.build_opener()


class JevClient(JevAsker):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 120.0,
        retries: int = 2,
        opener: Optional[urllib.request.OpenerDirector] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY") or ""
        self.model = model
        self.base_url = base_url or os.environ.get("JEVCOMP_API_BASE") or None
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or _build_opener()

    def ask(self, state: JevState, questions: JevQuestions) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("TYPESAFE_API_KEY is not configured")
        request = build_jev_request(state, questions, self.api_key, self.model, self.base_url)
        last_error: Optional[Exception] = None
        attempts = max(1, self.retries + 1)
        for attempt in range(attempts):
            if attempt:
                time.sleep(min(2.0 ** attempt, 4.0))
            req = urllib.request.Request(
                request["url"],
                data=request["body"].encode("utf-8"),
                headers=request["headers"],
                method=request["method"],
            )
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    return parse_jev_response(
                        response.status, 200 <= response.status < 300, response.read().decode("utf-8", "replace")
                    )
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", "replace")
                last_error = RuntimeError(f"Jev request failed ({error.code}): {body[:200]}")
                if error.code in (429, 500, 502, 503, 504):
                    continue
                raise last_error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = RuntimeError(f"Jev request failed: {error}")
                continue
        raise last_error or RuntimeError("Jev request failed")


def compact_messages(messages, options=None, **client_kwargs):
    """compact() with a JevClient built from the options (key from
    TYPESAFE_API_KEY by default)."""
    from .decide import compact

    client_options = {k: client_kwargs.pop(k) for k in list(client_kwargs) if k in ("api_key", "model", "base_url", "timeout", "retries")}
    return compact(messages, JevClient(**client_options), options)
