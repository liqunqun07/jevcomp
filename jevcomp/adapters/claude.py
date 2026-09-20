"""Claude Code adapter: ~/.claude/projects/**.jsonl session transcripts.

Record shape (per line): {"type":"user"|"assistant", "message":{"role",
"content": string | [blocks]}, ...}. Blocks: {type:"text"}, {type:"tool_use",
id,name,input}, {type:"tool_result",tool_use_id,content,is_error}.
Write-back keeps every non-message record verbatim, drops removed messages'
lines, and rewrites tool_result blocks of truncated results.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional, TextIO

from ..types import Message, ToolResult, ToolUse
from . import Transcript, to_generic_dict


class ClaudeRecord:
    def __init__(self, line_index: int, raw: str, record: dict, content) -> None:
        self.line_index = line_index
        self.raw = raw
        self.record = record
        self.content = content  # original message.content (string or list)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def discover(limit: int = 50) -> List[dict]:
    """Recent Claude Code sessions, newest first."""
    import glob
    import os

    sessions = []
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        sessions.append({
            "agent": "claude",
            "id": os.path.basename(path)[: -len(".jsonl")],
            "path": path,
            "bytes": stat.st_size,
            "mtime": stat.st_mtime,
        })
    sessions.sort(key=lambda s: -s["mtime"])
    return sessions[:limit]


def _parse_message(record: dict) -> Optional[Message]:
    inner = record.get("message")
    if not isinstance(inner, dict):
        return None
    role = inner.get("role")
    if role not in ("user", "assistant"):
        return None
    content = inner.get("content")
    text_parts: List[str] = []
    tool_uses: List[ToolUse] = []
    tool_results: List[ToolResult] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block_type == "tool_use":
                tool_uses.append(
                    ToolUse(
                        tool_use_id=str(block.get("id", "")),
                        tool=str(block.get("name", "")),
                        input=block.get("input") if isinstance(block.get("input"), dict) else {},
                    )
                )
            elif block_type == "tool_result":
                tool_results.append(
                    ToolResult(
                        tool_use_id=str(block.get("tool_use_id", "")),
                        text=_result_text(block.get("content")),
                        isError=bool(block.get("is_error")),
                    )
                )
    return Message(
        role=role,
        text="\n".join(text_parts),
        toolUses=tool_uses,
        toolResults=tool_results or None,
    )


def load(path: str) -> Transcript:
    messages: List[Message] = []
    records: List[ClaudeRecord] = []
    with open(path, encoding="utf-8") as handle:
        for line_index, raw in enumerate(handle):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("isSidechain"):
                continue
            if record.get("type") not in ("user", "assistant"):
                continue
            message = _parse_message(record)
            if message is None:
                continue
            messages.append(message)
            records.append(ClaudeRecord(line_index, raw.rstrip("\n"), record, record["message"].get("content")))
    return Transcript(messages, records=records, source=path)


def _rewrite_line(record: ClaudeRecord, message: Message) -> str:
    """Rebuilds one jsonl line from the compacted message: text blocks and
    tool_use blocks filtered, tool_result contents replaced."""
    record_json = json.loads(record.raw)
    inner = record_json.get("message", {})
    text_uses = {t.tool_use_id: t for t in message.toolUses}
    new_blocks = []
    original_content = record.content if isinstance(record.content, list) else []
    result_texts = {r.tool_use_id: r for r in message.toolResults or []}
    for block in original_content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            new_blocks.append(block)
        elif block_type == "tool_use":
            if block.get("id") in text_uses:
                new_blocks.append(block)
        elif block_type == "tool_result":
            kept_result = result_texts.get(block.get("tool_use_id"))
            if kept_result is None:
                continue
            rewritten = dict(block)
            rewritten["content"] = kept_result.text
            new_blocks.append(rewritten)
    if isinstance(record.content, str):
        inner["content"] = message.text
    else:
        inner["content"] = new_blocks
    record_json["message"] = inner
    return json.dumps(record_json, ensure_ascii=False)


def dump(transcript: Transcript, kept: List[Message], out: TextIO, provenance=None) -> None:
    """Writes the full session file back: untouched lines verbatim, rebuilt
    lines rewritten, removed messages' lines gone.

    `provenance` is CompactResult.provenance: (in_index, out_index|None,
    rebuilt) per input message, in input order."""
    if provenance is None:
        provenance = [
            (in_index, out_index, False)
            for in_index, message in enumerate(transcript.messages)
            for out_index in [next((i for i, m in enumerate(kept) if m is message), None)]
        ]
    lines = {record.line_index: record.raw for record in transcript.records}
    for in_index, out_index, rebuilt in provenance:
        record = transcript.records[in_index]
        if out_index is None:
            lines.pop(record.line_index, None)
        elif rebuilt:
            lines[record.line_index] = _rewrite_line(record, kept[out_index])
        else:
            lines[record.line_index] = record.raw
    for line_index in sorted(lines):
        out.write(lines[line_index] + "\n")


def dump_generic(kept: List[Message], out: TextIO) -> None:
    for message in kept:
        out.write(json.dumps(to_generic_dict(message), ensure_ascii=False) + "\n")
