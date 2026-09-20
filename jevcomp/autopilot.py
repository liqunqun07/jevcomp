"""jevcomp autopilot: keep ended sessions slim across claude / zcode / codex.

Safety rules:
  - only sessions idle for longer than --min-idle-minutes are touched
    (claude/codex: file mtime; zcode: latest part activity)
  - only sessions larger than --min-bytes are worth compacting
  - claude/codex write in place with a .jev-bak-* backup; zcode is only
    written with the conservative path (UPDATE single part rows, no deletes)
    and is disabled unless --enable-zcode-write is given
  - per-run session budget (default 3) keeps API spend bounded
  - a state file (~/.jevcomp/state.json) prevents re-compacting unchanged
    sessions; an flock prevents overlapping runs
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import datetime
from typing import Callable, List, Optional

HOME = os.path.expanduser("~")
STATE_PATH = os.path.expanduser("~/.jevcomp/state.json")
LOG_PATH = os.path.expanduser("~/.jevcomp/autopilot.log")
LOCK_PATH = os.path.expanduser("~/.jevcomp/autopilot.lock")

MIN_BYTES_DEFAULT = 300_000        # ~75k chars of transcript
MIN_IDLE_MINUTES_DEFAULT = 30      # claude/codex file mtime
ZCODE_MIN_IDLE_HOURS = 24          # zcode DB rows: only long-closed sessions
MAX_SESSIONS_PER_RUN = 3


def _log(message: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now().strftime('%m-%d %H:%M:%S')} {message}\n")


def _load_state() -> dict:
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _daemonize() -> None:
    """Double-fork so the hook/notify caller returns immediately."""
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
    sys.stdout.flush()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)


def _candidates_claude(min_bytes: float, min_idle: float) -> List[dict]:
    from .adapters import claude as a_claude

    now = time.time()
    out = []
    for session in a_claude.discover():
        idle_min = (now - os.path.getmtime(session["path"])) / 60
        if idle_min >= min_idle and session["bytes"] >= min_bytes:
            out.append({"agent": "claude", "key": f"claude:{session['id']}",
                        "path": session["path"], "bytes": session["bytes"]})
    return out


def _candidates_codex(min_bytes: float, min_idle: float) -> List[dict]:
    from .adapters import codex as a_codex

    now = time.time()
    out = []
    for session in a_codex.discover():
        idle_min = (now - os.path.getmtime(session["path"])) / 60
        if idle_min >= min_idle and session["bytes"] >= min_bytes:
            out.append({"agent": "codex", "key": f"codex:{session['id']}",
                        "path": session["path"], "bytes": session["bytes"]})
    return out


def _candidates_zcode(min_bytes: float, db_path: str) -> List[dict]:
    import sqlite3

    if not os.path.isfile(db_path):
        return []
    cutoff = time.time() - ZCODE_MIN_IDLE_HOURS * 3600
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT s.id, MAX(p.time_updated)/1000.0, SUM(LENGTH(p.data)) "
            "FROM session s JOIN part p ON p.session_id = s.id "
            "GROUP BY s.id HAVING MAX(p.time_updated)/1000.0 < ? "
            "ORDER BY SUM(LENGTH(p.data)) DESC",
            (cutoff,),
        ).fetchall()
    finally:
        con.close()
    return [
        {"agent": "zcode", "key": f"zcode:{r[0]}", "session_id": r[0], "bytes": r[2] or 0}
        for r in rows
        if (r[2] or 0) >= min_bytes
    ]


def _compact_file_session(candidate: dict, agent_module, min_idle: float) -> dict:
    from .client import JevClient
    from .config import find_api_key
    from .decide import compact
    from .types import CompactOptions

    key, _ = find_api_key(None)
    if not key:
        return {**candidate, "ok": False, "error": "no API key"}
    transcript = agent_module.load(candidate["path"])
    result = compact(transcript.messages, JevClient(api_key=key), CompactOptions())
    out_path = candidate["path"] + ".autopilot.jsonl"
    with open(out_path, "w", encoding="utf-8") as fh:
        agent_module.dump(transcript, result.messages, fh, result.provenance)
    stats = result.stats
    return {
        **candidate,
        "ok": True,
        "output": out_path,
        "reduction": round((stats.charsBefore - stats.charsAfter) / max(1, stats.charsBefore), 3),
        "callsDropped": stats.callsDropped,
    }


def _compact_zcode_session(candidate: dict, db_path: str) -> dict:
    from .adapters import zcode as a_zcode
    from .client import JevClient
    from .config import find_api_key
    from .decide import compact
    from .types import CompactOptions

    key, _ = find_api_key(None)
    if not key:
        return {**candidate, "ok": False, "error": "no API key"}
    transcript = a_zcode.zcode_load(candidate["session_id"], db_path)
    result = compact(transcript.messages, JevClient(api_key=key), CompactOptions())
    # conservative DB write-back (single-row UPDATEs, full backup first)
    actions = {}
    from .state import collect_tool_calls
    calls = collect_tool_calls(transcript.messages, 6)
    for call, decision in zip(calls, result.decisions):
        if decision.action != "keep":
            actions[call["tool_use_id"]] = decision.action
    import json as _json
    import sqlite3

    backup = f"{db_path}.jev-bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup)
    with dst:
        src.backup(dst)
    dst.close()
    changed = 0
    cur = src.cursor()
    with src:
        for ref in transcript.meta["part_refs"]:
            for pid, pdata in ref["parts"]:
                try:
                    part = _json.loads(pdata)
                except ValueError:
                    continue
                if part.get("type") != "tool" or part.get("state", {}).get("status") != "completed":
                    continue
                action = actions.get(part.get("callID"))
                if not action:
                    continue
                state = part["state"]
                if action == "drop_call":
                    state = dict(state, input={}, output="[jevcomp autopilot removed this content]")
                else:
                    output = state.get("output", "")
                    if not isinstance(output, str):
                        output = _json.dumps(output, ensure_ascii=False)
                    head = output[:300] + "\n" if len(output) > 420 else ""
                    state = dict(state, output=f"{head}[jevcomp truncated {len(output)} chars]")
                cur.execute("UPDATE part SET data=? WHERE id=?",
                            (_json.dumps(dict(part, state=state), ensure_ascii=False), pid))
                changed += 1
    src.close()
    stats = result.stats
    return {
        **candidate,
        "ok": True,
        "output": f"db updated ({changed} parts), backup {backup}",
        "reduction": round((stats.charsBefore - stats.charsAfter) / max(1, stats.charsBefore), 3),
        "callsDropped": stats.callsDropped,
    }


def run_once(
    dry_run: bool = False,
    min_bytes: int = MIN_BYTES_DEFAULT,
    min_idle_minutes: float = MIN_IDLE_MINUTES_DEFAULT,
    max_sessions: int = MAX_SESSIONS_PER_RUN,
    enable_zcode_write: bool = False,
    log: Callable[[str], None] = _log,
) -> dict:
    from .adapters import claude as a_claude
    from .adapters import codex as a_codex
    from .adapters import zcode as a_zcode

    candidates = (
        _candidates_claude(min_bytes, min_idle_minutes)
        + _candidates_codex(min_bytes, min_idle_minutes)
        + (_candidates_zcode(min_bytes, a_zcode.DEFAULT_DB) if enable_zcode_write else [])
    )
    state = _load_state()
    fresh = [
        c for c in candidates
        if state.get(c["key"], {}).get("bytes") != c["bytes"]
    ]
    fresh.sort(key=lambda c: -c["bytes"])
    report = {
        "scanned": len(candidates),
        "eligible": len(fresh),
        "compacted": [],
        "skipped_budget": max(0, len(fresh) - max_sessions),
        "dry_run": dry_run,
    }
    for candidate in fresh[:max_sessions]:
        if dry_run:
            report["compacted"].append({**candidate, "ok": None, "note": "dry-run"})
            continue
        try:
            if candidate["agent"] == "claude":
                outcome = _compact_file_session(candidate, a_claude, min_idle_minutes)
            elif candidate["agent"] == "codex":
                outcome = _compact_file_session(candidate, a_codex, min_idle_minutes)
            else:
                outcome = _compact_zcode_session(candidate, a_zcode.DEFAULT_DB)
        except Exception as error:  # noqa: BLE001 — autopilot must survive one failure
            outcome = {**candidate, "ok": False, "error": f"{type(error).__name__}: {error}"}
        report["compacted"].append(outcome)
        if outcome.get("ok"):
            state[candidate["key"]] = {"bytes": candidate["bytes"], "at": time.time()}
            log(f"compacted {candidate['key']} -{round(outcome.get('reduction', 0) * 100)}%")
        else:
            log(f"failed {candidate['key']}: {outcome.get('error')}")
    if not dry_run:
        _save_state(state)
    return report


def cli(args: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="jevcomp autopilot")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-bytes", type=int, default=MIN_BYTES_DEFAULT)
    parser.add_argument("--min-idle-minutes", type=float, default=MIN_IDLE_MINUTES_DEFAULT)
    parser.add_argument("--max-sessions", type=int, default=MAX_SESSIONS_PER_RUN)
    parser.add_argument("--enable-zcode-write", action="store_true",
                        help="also compact long-closed zcode sessions in the DB (conservative, backup kept)")
    parser.add_argument("--daemonize", action="store_true",
                        help="run in background (for hooks/notify)")
    parsed = parser.parse_args(args)

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("autopilot: another run is active", file=sys.stderr)
        return 0
    if parsed.daemonize:
        _daemonize()
    report = run_once(
        dry_run=parsed.dry_run,
        min_bytes=parsed.min_bytes,
        min_idle_minutes=parsed.min_idle_minutes,
        max_sessions=parsed.max_sessions,
        enable_zcode_write=parsed.enable_zcode_write,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
