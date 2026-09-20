"""Transcript adapters: load agent session files into the core Message shape.

Each adapter module exposes:
    load(...)   -> Transcript (messages + per-message provenance)
    dump(...)   -> write a compacted transcript back out
Read-only adapters (zcode) emit the generic format instead.
"""

from __future__ import annotations

from typing import List

from ..types import Message, ToolResult, ToolUse


class Transcript:
    """Parsed messages plus, when the source is a file, per-message records
    so write-back can preserve untouched lines verbatim."""

    def __init__(self, messages: List[Message], records=None, source: str = "", meta: dict | None = None) -> None:
        self.messages = messages
        self.records = records or []  # adapter-specific provenance per message
        self.source = source
        self.meta = meta or {}  # adapter-specific handle info (e.g. zcode part_refs)


def from_generic_dict(data: dict) -> Message:
    message = Message(
        role=data.get("role", "user"),
        text=data.get("text", ""),
        toolUses=[
            ToolUse(
                tool_use_id=t.get("tool_use_id", ""),
                tool=t.get("tool", ""),
                input=t.get("input") or {},
                text=t.get("text"),
                isError=t.get("isError"),
            )
            for t in data.get("toolUses") or []
        ],
    )
    results = data.get("toolResults")
    if results:
        message.toolResults = [
            ToolResult(
                tool_use_id=r.get("tool_use_id", ""),
                text=r.get("text", ""),
                isError=r.get("isError"),
            )
            for r in results
        ]
    return message


def to_generic_dict(message: Message) -> dict:
    out = {
        "role": message.role,
        "text": message.text,
        "toolUses": [
            {
                "tool_use_id": t.tool_use_id,
                "tool": t.tool,
                "input": t.input,
                **({"text": t.text} if t.text is not None else {}),
                **({"isError": True} if t.isError else {}),
            }
            for t in message.toolUses
        ],
    }
    if message.toolResults:
        out["toolResults"] = [
            {
                "tool_use_id": r.tool_use_id,
                "text": r.text,
                "isError": bool(r.isError),
            }
            for r in message.toolResults
        ]
    return out


# registry (imported last to keep the package import cycle-free)
from . import claude as claude  # noqa: E402
from . import codex as codex  # noqa: E402
from . import generic as generic  # noqa: E402
from . import zcode as zcode  # noqa: E402
