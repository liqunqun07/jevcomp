"""zcode adapter (read-only): sessions live in ~/.zcode/cli/db/db.sqlite.

Tables: session(id, title, ...), message(id, session_id, data JSON with role,
sequence), part(message_id, data JSON with type text|tool|reasoning|...).
A tool part carries both input and output:
  {"type":"tool","callID","tool","state":{"status","input","output"}}
so one part maps to a tool_use paired with its result on the same message.
Compacted output is written in the generic format; the live DB is never
modified.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import List, Optional

from ..types import Message, ToolResult, ToolUse
from . import Transcript

DEFAULT_DB = os.path.expanduser("~/.zcode/cli/db/db.sqlite")


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"zcode db not found: {db_path}")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def list_sessions(db_path: str = DEFAULT_DB, limit: int = 20) -> List[dict]:
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT id, title, time_created FROM session "
            "WHERE id NOT LIKE 'sess_subagent_%' ORDER BY time_created DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"id": row["id"], "title": (row["title"] or "")[:60], "time_created": row["time_created"]}
        for row in rows
    ]


def resolve_session(session_id: str, db_path: str = DEFAULT_DB) -> str:
    if session_id and session_id != "latest":
        return session_id
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT id FROM session WHERE id NOT LIKE 'sess_subagent_%' "
            "ORDER BY time_created DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise RuntimeError("no zcode sessions found")
    return row["id"]


def load(session_id: str, db_path: str = DEFAULT_DB) -> Transcript:
    session_id = resolve_session(session_id, db_path)
    with _connect(db_path) as connection:
        messages = connection.execute(
            "SELECT id, data, sequence FROM message WHERE session_id = ? "
            "ORDER BY sequence, time_created, id",
            (session_id,),
        ).fetchall()
        parts = connection.execute(
            "SELECT id, message_id, data, sequence FROM part WHERE session_id = ? "
            "ORDER BY message_id, sequence, time_created, id",
            (session_id,),
        ).fetchall()
    parts_by_message: dict = {}
    for part in parts:
        parts_by_message.setdefault(part["message_id"], []).append(
            (part["id"], json.loads(part["data"]))
        )

    parsed: List[Message] = []
    part_refs: List[dict] = []
    for message in messages:
        part_rows = parts_by_message.get(message["id"], [])
        data = json.loads(message["data"])
        role = data.get("role")
        if role not in ("user", "assistant"):
            continue
        tool_uses: List[ToolUse] = []
        tool_results: List[ToolResult] = []
        text_parts: List[str] = []
        for _part_id, part in part_rows:
            ptype = part.get("type")
            if ptype == "text" and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            elif ptype == "tool":
                state = part.get("state") or {}
                call_id = str(part.get("callID", ""))
                tool = str(part.get("tool", ""))
                tool_uses.append(
                    ToolUse(
                        tool_use_id=call_id,
                        tool=tool,
                        input=state.get("input") if isinstance(state.get("input"), dict) else {},
                    )
                )
                output = state.get("output")
                if output is not None:
                    status = str(state.get("status", ""))
                    tool_results.append(
                        ToolResult(
                            tool_use_id=call_id,
                            text=str(output),
                            isError=status in ("failed", "error"),
                        )
                    )
        entry = Message(
            role=role,
            text="\n".join(text_parts),
            toolUses=tool_uses,
            toolResults=tool_results or None,
        )
        # skip pure bookkeeping messages (empty user context snapshots)
        if not entry.text and not tool_uses and not tool_results:
            continue
        part_refs.append({"msg_index": len(parsed), "parts": list(part_rows)})
        parsed.append(entry)
    return Transcript(
        parsed,
        records=list(range(len(parsed))),
        source=f"zcode:{session_id}",
        meta={"session_id": session_id, "db_path": db_path, "part_refs": part_refs},
    )
