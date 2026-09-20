"""Tool-call pairing and the staged state fitter."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Dict, List, Optional, Sequence

from .tokens import abridge, estimate_tokens, truncate
from .types import (
    CompactionState,
    FittedState,
    HistoryEntry,
    HistoryToolCall,
    Message,
    ToolCall,
)

STATE_CONTEXT = (
    "A coding assistant conversation is being compacted to free context. "
    "`history` is the whole conversation so far, oldest first; tool outputs "
    "are replaced by a short `result` note and long texts may be abridged. "
    "Each question asks whether one tool call, or the full output of that "
    "call, still needs to stay in the history verbatim. Whatever is not kept "
    "is deleted permanently, but the assistant can always re-run a tool or "
    "re-read a file."
)

INPUT_CHARS = [1000, 200, 60]


def is_pinned(index: int, total: int, preserve_recent_messages: int) -> bool:
    return index == 0 or index >= total - preserve_recent_messages


def collect_tool_calls(
    messages: Sequence[Message], preserve_recent_messages: int
) -> List[ToolCall]:
    """Pairs every tool_use with its tool_result by tool_use_id. Calls without
    a result are not candidates (there is nothing to drop yet)."""
    results: Dict[str, tuple] = {}
    for index, message in enumerate(messages):
        for result in message.toolResults or []:
            results[result.tool_use_id] = (index, result)
    calls: List[ToolCall] = []
    for call_index, message in enumerate(messages):
        for tool in message.toolUses:
            found = results.get(tool.tool_use_id)
            if found is None:
                continue
            result_index, result = found
            calls.append(
                ToolCall(
                    id=f"t{len(calls) + 1}",
                    tool_use_id=tool.tool_use_id,
                    tool=tool.tool,
                    input=tool.input,
                    callIndex=call_index,
                    resultIndex=result_index,
                    resultChars=len(result.text),
                    isError=bool(result.isError),
                    pinned=(
                        is_pinned(call_index, len(messages), preserve_recent_messages)
                        or is_pinned(result_index, len(messages), preserve_recent_messages)
                    ),
                )
            )
    return calls


def _input_text(obj: dict, limit: int) -> str:
    try:
        serialized = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = "[unserializable input]"
    return truncate(serialized, limit)


def _result_note(call: ToolCall) -> str:
    status = "error" if call.isError else "ok"
    return f"{status}, {call.resultChars} chars (omitted)"


def _compact_call(call: ToolCall) -> str:
    """One call as a single line, for when the structured form is too costly."""
    parts = []
    for key, value in call.input.items():
        if isinstance(value, str):
            text = value
        else:
            text = _input_text({key: value}, 200)
        parts.append(f"{key}={' '.join(text.split())}")
    joined = " ".join(parts)
    status = "error" if call.isError else "ok"
    return f"{call.id} {call.tool} {truncate(joined, INPUT_CHARS[2])} → {status} {call.resultChars}ch"


def _entry_json(entry: HistoryEntry) -> str:
    return json.dumps(asdict(entry), ensure_ascii=False, separators=(",", ":"))


def _entry_tokens(entry: HistoryEntry) -> int:
    return estimate_tokens(_entry_json(entry)) + 1


def _merge_call_runs(
    history: List[HistoryEntry], pinned
) -> List[HistoryEntry]:
    """Folds runs of adjacent call-only entries into one entry each, so the
    per-entry envelope is paid once per run; the call lines keep their ids."""

    def foldable(e: HistoryEntry) -> bool:
        return (
            not pinned(e)
            and len(e.text) == 0
            and e.tool_calls is not None
            and len(e.tool_calls) > 0
            and isinstance(e.tool_calls[0], str)
        )

    merged: List[HistoryEntry] = []
    for entry in history:
        previous = merged[-1] if merged else None
        if previous is not None and foldable(previous) and foldable(entry) and previous.role == entry.role:
            previous.tool_calls = list(previous.tool_calls or []) + list(entry.tool_calls or [])
            continue
        merged.append(
            HistoryEntry(
                i=entry.i,
                role=entry.role,
                text=entry.text,
                tool_calls=list(entry.tool_calls) if entry.tool_calls is not None else None,
            )
        )
    return merged


def _calls_by_message(calls: Sequence[ToolCall]) -> Dict[int, List[ToolCall]]:
    by_message: Dict[int, List[ToolCall]] = {}
    for call in calls:
        by_message.setdefault(call.callIndex, []).append(call)
    return by_message


def _history_entries(
    messages: Sequence[Message], calls: Sequence[ToolCall], input_chars: int
) -> List[HistoryEntry]:
    by_message = _calls_by_message(calls)
    entries: List[HistoryEntry] = []
    for i, message in enumerate(messages):
        tool_calls: List[HistoryToolCall] = [
            HistoryToolCall(
                id=call.id,
                tool=call.tool,
                input=_input_text(call.input, input_chars),
                result=_result_note(call),
            )
            for call in by_message.get(i, [])
        ]
        if not message.text.strip() and not tool_calls:
            continue
        entry = HistoryEntry(i=i, role=message.role, text=message.text)
        if tool_calls:
            entry.tool_calls = tool_calls
        entries.append(entry)
    return entries


def goal_from_messages(messages: Sequence[Message]) -> str:
    """The last three user prompts, as the default goal."""
    prompts = [
        m
        for m in messages
        if m.role == "user" and m.text.strip() and not (m.toolResults or [])
    ]
    return "\n".join(truncate(m.text, 500) for m in prompts[-3:])


def fit_state(
    messages: Sequence[Message],
    calls: Sequence[ToolCall],
    options,  # CompactOptions (duck-typed for testability)
) -> FittedState:
    """Builds the Jev state from the whole conversation and shrinks it in
    stages until it fits maxStateTokens: tool inputs are truncated, then long
    texts abridged oldest-first (pinned last), then old messages collapse to a
    one-line note, then old tool calls shrink to one line each, then old
    call-less messages are left out, then runs of old call-only messages fold
    into one entry. Raises when even that is too big."""

    goal = options.goal or goal_from_messages(messages)

    def state_of(history: List[HistoryEntry]) -> dict:
        return {"context": STATE_CONTEXT, "goal": goal, "history": [asdict(h) for h in history]}

    base_tokens = estimate_tokens(json.dumps(state_of([]), ensure_ascii=False, separators=(",", ":")))

    history: List[HistoryEntry] = []
    per_entry: List[int] = []
    tokens = 0

    def rebuild(input_chars: int) -> None:
        nonlocal history, per_entry, tokens
        history = _history_entries(messages, calls, input_chars)
        per_entry = [_entry_tokens(e) for e in history]
        tokens = base_tokens + sum(per_entry)

    def fits() -> bool:
        return tokens <= options.maxStateTokens

    def fitted(stage: str) -> FittedState:
        return FittedState(state=CompactionState(context=STATE_CONTEXT, goal=goal, history=history), tokens=tokens, stage=stage)

    def shrink(index: int, change) -> None:
        nonlocal tokens
        entry = history[index]
        if entry is None:
            return
        change(entry)
        now = _entry_tokens(entry)
        tokens += now - (per_entry[index] or 0)
        per_entry[index] = now

    rebuild(INPUT_CHARS[0])
    if fits():
        return fitted("full")

    for limit in INPUT_CHARS[1:]:
        rebuild(limit)
        if fits():
            return fitted(f"inputs<={limit}")

    def pinned(entry: HistoryEntry) -> bool:
        return is_pinned(entry.i, len(messages), options.preserveRecentMessages)

    indices = list(range(len(history)))
    order = [i for i in indices if not pinned(history[i])] + [
        i for i in indices if pinned(history[i])
    ]

    for index in order:
        entry = history[index]
        if len(entry.text) <= 400 + 150 + 40:
            continue
        original_tokens = per_entry[index]
        shrink(index, lambda e: setattr(e, "text", abridge(e.text)))
        if fits():
            return fitted("texts abridged")
        del original_tokens

    for index in order:
        entry = history[index]
        if pinned(entry) or len(entry.text) == 0:
            continue
        original = len(messages[entry.i].text) if entry.i < len(messages) else len(entry.text)
        shrink(index, lambda e: setattr(e, "text", f"[… {original} chars omitted …]"))
        if fits():
            return fitted("old messages collapsed")

    by_message = _calls_by_message(calls)
    for index in order:
        entry = history[index]
        own = by_message.get(entry.i)
        if pinned(entry) or not own:
            continue
        shrink(index, lambda e: setattr(e, "tool_calls", [_compact_call(c) for c in own]))
        if fits():
            return fitted("old calls compacted")

    left = set()
    for index in order:
        entry = history[index]
        if pinned(entry) or entry.tool_calls:
            continue
        left.add(index)
        tokens -= per_entry[index] or 0
        if fits():
            history = [h for i, h in enumerate(history) if i not in left]
            return fitted("old messages left out")

    history = _merge_call_runs([h for i, h in enumerate(history) if i not in left], pinned)
    per_entry = [_entry_tokens(e) for e in history]
    tokens = base_tokens + sum(per_entry)
    if fits():
        return fitted("old calls merged")

    # 终极阶段：最老的 call-only 条目折叠为一条汇总注记。state 只是 Jev 的
    # 视野，真实历史不动；被折叠的调用仍会收到提问，只是看不到细节。
    call_only = [
        i for i, entry in enumerate(history)
        if not pinned(entry) and not entry.text and isinstance(entry.tool_calls, list)
    ]
    if call_only:
        for keep_count in range(max(0, len(call_only) - 1), -1, -1):
            drop = call_only[:-keep_count] if keep_count else list(call_only)
            summary = HistoryEntry(
                i=history[drop[0]].i,
                role=history[drop[0]].role,
                text=f"[… {len(drop)} older tool calls omitted …]",
            )
            trial_tokens = tokens - sum(per_entry[i] or 0 for i in drop) + _entry_tokens(summary)
            if trial_tokens <= options.maxStateTokens or keep_count == 0:
                dropped = set(drop)
                kept: List[HistoryEntry] = []
                inserted = False
                for i, entry in enumerate(history):
                    if i in dropped:
                        if not inserted:
                            kept.append(summary)
                            inserted = True
                        continue
                    kept.append(entry)
                return FittedState(
                    state=CompactionState(context=STATE_CONTEXT, goal=goal, history=kept),
                    tokens=trial_tokens,
                    stage=f"old call lines folded ({len(drop)} folded)",
                )

    raise ValueError(
        f"history too large for Jev (~{tokens} tokens after truncation, "
        f"limit {options.maxStateTokens})"
    )
