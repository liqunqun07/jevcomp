"""Codex adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.

Lines: {"timestamp","ordinal","type":"session_meta"|"response_item"|...,
"payload":{...}}. response_item payloads of interest:
  {"type":"message","role":"user"|"assistant","content":[{"type":"input_text"
   |"output_text","text":...}]}
  {"type":"function_call","name","arguments"(json string),"call_id"}
  {"type":"function_call_output","call_id","output"}
Reasoning and other records are skipped for the model messages but preserved
verbatim on write-back.
"""

from __future__ import annotations

import json
from typing import List, Optional, TextIO

from ..types import Message, ToolResult, ToolUse
from . import Transcript, to_generic_dict


class CodexRecord:
    def __init__(self, line_index: int, raw: str) -> None:
        self.line_index = line_index
        self.raw = raw


def _parse_payload(payload: dict) -> Optional[Message]:
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return None
        parts = [
            block.get("text", "")
            for block in payload.get("content") or []
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return Message(role=role, text="\n".join(parts))
    if ptype == "function_call":
        try:
            arguments = json.loads(payload.get("arguments") or "{}")
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
        except ValueError:
            arguments = {"raw": str(payload.get("arguments"))}
        return Message(
            role="assistant",
            text="",
            toolUses=[
                ToolUse(
                    tool_use_id=str(payload.get("call_id", "")),
                    tool=str(payload.get("name", "")),
                    input=arguments,
                )
            ],
        )
    if ptype == "custom_tool_call":
        # newer Codex: input is a plain string (often a JS snippet), not JSON
        raw_input = payload.get("input")
        if isinstance(raw_input, str):
            arguments = {"input": raw_input}
        elif isinstance(raw_input, dict):
            arguments = raw_input
        else:
            arguments = {}
        return Message(
            role="assistant",
            text="",
            toolUses=[
                ToolUse(
                    tool_use_id=str(payload.get("call_id", "")),
                    tool=str(payload.get("name", "")),
                    input=arguments,
                )
            ],
        )
    if ptype in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        if isinstance(output, list):  # [{type:"input_text",text},...]
            output = "\n".join(
                block.get("text", "") for block in output if isinstance(block, dict)
            )
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False) if output is not None else ""
        return Message(
            role="user",
            text="",
            toolResults=[
                ToolResult(tool_use_id=str(payload.get("call_id", "")), text=output)
            ],
        )
    return None


def discover(limit: int = 50) -> List[dict]:
    """Recent Codex rollout sessions, newest first."""
    import glob
    import os

    sessions = []
    for path in glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        sessions.append({
            "agent": "codex",
            "id": os.path.basename(path)[len("rollout-"): -len(".jsonl")],
            "path": path,
            "bytes": stat.st_size,
            "mtime": stat.st_mtime,
        })
    sessions.sort(key=lambda s: -s["mtime"])
    return sessions[:limit]


def load(path: str) -> Transcript:
    messages: List[Message] = []
    records: List[CodexRecord] = []
    with open(path, encoding="utf-8") as handle:
        for line_index, raw in enumerate(handle):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            message = _parse_payload(payload)
            if message is None:
                continue
            messages.append(message)
            records.append(CodexRecord(line_index, raw.rstrip("\n")))
    return Transcript(messages, records=records, source=path)


def _rewrite_line(record: CodexRecord, message: Message) -> str:
    record_json = json.loads(record.raw)
    payload = record_json.get("payload", {})
    if payload.get("type") == "message":
        payload["content"] = [
            {"type": "output_text" if payload.get("role") == "assistant" else "input_text",
             "text": message.text}
        ]
    elif payload.get("type") == "function_call":
        keep = {t.tool_use_id for t in message.toolUses}
        if str(payload.get("call_id")) in keep:
            payload["arguments"] = json.dumps(
                message.toolUses[0].input if message.toolUses else {}, ensure_ascii=False
            )
    elif payload.get("type") == "custom_tool_call":
        keep = {t.tool_use_id for t in message.toolUses}
        if str(payload.get("call_id")) in keep:
            tool = message.toolUses[0] if message.toolUses else None
            payload["input"] = tool.input.get("input", "") if tool else ""
    elif payload.get("type") in ("function_call_output", "custom_tool_call_output"):
        results = message.toolResults or []
        if payload.get("type") == "custom_tool_call_output" and results:
            payload["output"] = [{"type": "input_text", "text": results[0].text}]
        else:
            payload["output"] = results[0].text if results else ""
    record_json["payload"] = payload
    return json.dumps(record_json, ensure_ascii=False)


def dump(transcript: Transcript, kept: List[Message], out: TextIO, provenance=None) -> None:
    if provenance is None:
        provenance = [
            (in_index, next((i for i, m in enumerate(kept) if m is message), None), False)
            for in_index, message in enumerate(transcript.messages)
        ]
    message_lines = {record.line_index for record in transcript.records}
    emitted = set()
    for in_index, out_index, rebuilt in provenance:
        record = transcript.records[in_index]
        if out_index is None:
            emitted.discard(record.line_index)
            message_lines.discard(record.line_index)
        else:
            emitted.add(record.line_index)
            if rebuilt:
                record.raw = _rewrite_line(record, kept[out_index])
    with open(transcript.source, encoding="utf-8") as handle:
        for line_index, raw in enumerate(handle):
            if line_index in message_lines:
                if line_index in emitted:
                    out.write(
                        next(r.raw for r in transcript.records if r.line_index == line_index) + "\n"
                    )
                continue
            out.write(raw)


def dump_generic(kept: List[Message], out: TextIO) -> None:
    for message in kept:
        out.write(json.dumps(to_generic_dict(message), ensure_ascii=False) + "\n")
