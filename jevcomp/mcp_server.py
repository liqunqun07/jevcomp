"""jevcomp MCP server: exposes jevcomp as tools over stdio (MCP / JSON-RPC 2.0).

Zero dependencies, pure stdlib — any MCP client (zcode, Codex, Claude Code,
Gemini CLI, ...) can register it:

    { "type": "stdio", "command": "/usr/bin/python3", "args": ["-m", "jevcomp", "mcp"] }

Tools:
    jevcomp_list_sessions {agent?}        -> recent sessions for claude|zcode|codex
    jevcomp_compact {agent, session, dry_run?} -> compact one session (Jev API)
    jevcomp_autopilot {dry_run?}          -> compact all stale large sessions
"""

from __future__ import annotations

import json
import sys
from typing import Any

SERVER_INFO = {"name": "jevcomp", "version": "0.1.0"}

TOOLS = [
    {
        "name": "jevcomp_list_sessions",
        "description": (
            "List recent agent sessions with sizes. agent: 'claude' | 'zcode' | "
            "'codex' (optional, default all three)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["claude", "zcode", "codex"]},
            },
        },
    },
    {
        "name": "jevcomp_compact",
        "description": (
            "Compact one agent session with Jev (deletion instead of summary; "
            "kept content stays verbatim). agent: 'claude'|'zcode'|'codex'; "
            "session: file path (claude/codex), session id or 'latest' (zcode). "
            "dry_run=true plans only. Output goes to <file>.jevcomp.jsonl / a "
            "generic export; originals are never modified unless in_place=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["claude", "zcode", "codex"]},
                "session": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "in_place": {"type": "boolean", "default": False},
                "goal": {"type": "string"},
            },
            "required": ["agent", "session"],
        },
    },
    {
        "name": "jevcomp_autopilot",
        "description": (
            "Compact every stale, large, already-ended session across claude/"
            "zcode/codex (safe: backups are kept, running sessions untouched)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"dry_run": {"type": "boolean", "default": False}},
        },
    },
]


def _tool_list_sessions(args: dict) -> str:
    from .adapters import claude as a_claude
    from .adapters import codex as a_codex
    from .adapters import zcode as a_zcode

    agent = args.get("agent")
    out = {}
    if agent in (None, "claude"):
        out["claude"] = a_claude.discover(10)
    if agent in (None, "zcode"):
        out["zcode"] = a_zcode.list_sessions()[:10]
    if agent in (None, "codex"):
        out["codex"] = a_codex.discover(10)
    return json.dumps(out, ensure_ascii=False, indent=1)


def _tool_compact(args: dict) -> str:
    from .adapters import claude as a_claude
    from .adapters import codex as a_codex
    from .adapters import zcode as a_zcode
    from .client import JevClient
    from .config import find_api_key
    from .decide import compact
    from .types import CompactOptions

    agent = args.get("agent")
    session = args.get("session", "")
    dry_run = bool(args.get("dry_run", True))
    in_place = bool(args.get("in_place", False))

    loader = {"claude": a_claude.load, "codex": a_codex.load}.get(agent)
    if agent == "zcode":
        transcript = a_zcode.load(session if session else "latest")
    elif loader:
        transcript = loader(session)
    else:
        raise ValueError(f"unknown agent: {agent}")

    plan = {
        "agent": agent,
        "source": session,
        "messages": len(transcript.messages),
    }
    if dry_run:
        return json.dumps({"ok": True, "dry_run": True, "plan": plan}, ensure_ascii=False)

    key, key_source = find_api_key(None)
    if not key:
        raise RuntimeError("no TypeSafe API key configured (~/.jevcomp.json)")
    result = compact(transcript.messages, JevClient(api_key=key), CompactOptions(goal=args.get("goal")))
    saver = {"claude": a_claude.dump, "codex": a_codex.dump}.get(agent)
    if saver is not None:
        if in_place:
            import shutil

            shutil.copyfile(transcript.source, transcript.source + ".jev-bak-manual")
            with open(transcript.source, "w", encoding="utf-8") as fh:
                saver(transcript, result.messages, fh, result.provenance)
            out_path = transcript.source
        else:
            out_path = transcript.source + ".jevcomp.jsonl"
            with open(out_path, "w", encoding="utf-8") as fh:
                saver(transcript, result.messages, fh, result.provenance)
    else:
        out_path = None
    return json.dumps(
        {
            "ok": True,
            "stats": result.stats if hasattr(result, "stats") else result["stats"],
            "output": out_path or "(zcode: read-only via MCP; use CLI for DB write-back)",
        },
        ensure_ascii=False,
        default=lambda o: vars(o) if hasattr(o, "__dict__") else str(o),
    )


def _tool_autopilot(args: dict) -> str:
    from .autopilot import run_once

    report = run_once(dry_run=bool(args.get("dry_run", False)), log=lambda s: None)
    return json.dumps(report, ensure_ascii=False, indent=1)


TOOL_HANDLERS = {
    "jevcomp_list_sessions": _tool_list_sessions,
    "jevcomp_compact": _tool_compact,
    "jevcomp_autopilot": _tool_autopilot,
}


def _result(text: str, is_error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


def handle(request: dict) -> dict | None:
    method = request.get("method", "")
    request_id = request.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": (request.get("params") or {}).get(
                    "protocolVersion", "2024-11-05"
                ),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "notifications/initialized" or method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name", "")
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _result(f"unknown tool: {name}", is_error=True),
            }
        try:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _result(handler(params.get("arguments") or {})),
            }
        except Exception as error:  # noqa: BLE001 — surface as tool error
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _result(f"{type(error).__name__}: {error}", is_error=True),
            }
    if request_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return None


def serve() -> int:
    """stdio loop: one JSON-RPC message per line (newline-delimited)."""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except ValueError:
            continue
        response = handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
