"""Question batching, decision rules and the compact() driver."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Dict, List, Optional, Sequence

from .request import noul_answer
from .state import collect_tool_calls, fit_state
from .tokens import estimate_tokens
from .types import (
    CallAnswer,
    CallDecision,
    CompactOptions,
    CompactResult,
    CompactStats,
    JevAsker,
    JevQuestions,
    Message,
    ToolCall,
    ToolResult,
    ToolUse,
)

REQUEST_OVERHEAD_TOKENS = 20  # the request envelope (model, key names)

DEFAULT_OPTIONS = CompactOptions()


def _finite(value: Optional[float], fallback: float) -> float:
    return value if isinstance(value, (int, float)) and value == value and value not in (float("inf"), float("-inf")) else fallback


def resolve_options(options: Optional[CompactOptions] = None) -> CompactOptions:
    options = options or CompactOptions()
    return CompactOptions(
        goal=options.goal or DEFAULT_OPTIONS.goal,
        keepThreshold=_finite(options.keepThreshold, DEFAULT_OPTIONS.keepThreshold),
        preserveRecentMessages=max(0, int(_finite(options.preserveRecentMessages, DEFAULT_OPTIONS.preserveRecentMessages))),
        maxStateTokens=max(1, int(_finite(options.maxStateTokens, DEFAULT_OPTIONS.maxStateTokens))),
        maxRequestTokens=max(1, int(_finite(options.maxRequestTokens, DEFAULT_OPTIONS.maxRequestTokens))),
        truncateHeadChars=max(0, int(_finite(options.truncateHeadChars, DEFAULT_OPTIONS.truncateHeadChars))),
    )


def questions_for(call: ToolCall) -> Dict[str, dict]:
    """The two noul questions asked about one call: keep the call, keep its result."""
    return {
        f"call_{call.id}": {
            "type": "noul",
            "instructions": (
                f"Tool call {call.id} ({call.tool}) should stay in the history: "
                "knowing this call was made, with its input, still matters for "
                "what the assistant does next"
            ),
        },
        f"result_{call.id}": {
            "type": "noul",
            "instructions": (
                f"The full output of tool call {call.id} ({call.tool}, "
                f"{call.resultChars} chars) should stay in the history verbatim: "
                "the assistant still needs its contents and re-running the tool "
                "would not do"
            ),
        },
    }


def batch_calls(
    calls: Sequence[ToolCall],
    state_tokens: int,
    options,  # with maxRequestTokens
) -> List[List[ToolCall]]:
    """Splits candidate calls into batches whose questions, together with the
    (always complete) state, fit one request."""
    budget = options.maxRequestTokens - state_tokens - REQUEST_OVERHEAD_TOKENS
    batches: List[List[ToolCall]] = []
    current: List[ToolCall] = []
    current_tokens = 0
    for call in calls:
        tokens = estimate_tokens(json.dumps(questions_for(call), ensure_ascii=False, separators=(",", ":")))
        if current and current_tokens + tokens > budget:
            batches.append(current)
            current = []
            current_tokens = 0
        if not current and tokens > budget:
            raise ValueError(
                f"state leaves no room for questions (~{state_tokens} of "
                f"{options.maxRequestTokens} tokens)"
            )
        current.append(call)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches


def decide_call(call: ToolCall, answer: CallAnswer, options) -> CallDecision:
    if call.pinned:
        action, reason = "keep", "pinned"
    elif answer.keepResult >= options.keepThreshold:
        action, reason = "keep", "kept"
    elif answer.keepCall >= options.keepThreshold:
        action, reason = "drop_result", "result_dropped"
    else:
        action, reason = "drop_call", "call_dropped"
    return CallDecision(
        id=call.id,
        tool=call.tool,
        keepCall=answer.keepCall,
        keepResult=answer.keepResult,
        action=action,
        reason=reason,
    )


def _ask_batch(asker: JevAsker, state, batch: Sequence[ToolCall]) -> Dict[str, CallAnswer]:
    questions: JevQuestions = {}
    for call in batch:
        questions.update(questions_for(call))
    response = asker.ask(state, questions)
    answers = response.get("answers") or {}
    return {
        call.id: CallAnswer(
            keepCall=noul_answer(answers, f"call_{call.id}"),
            keepResult=noul_answer(answers, f"result_{call.id}"),
        )
        for call in batch
    }


TRUNCATE_NOTE = (
    "[jevcomp truncated {n} chars of this tool result{err}; "
    "re-run the tool if needed]"
)


def truncated_result_text(text: str, is_error: bool, head_chars: int) -> str:
    if len(text) <= head_chars + 120:
        return text
    head = (text[:head_chars] + "\n") if head_chars > 0 else ""
    note = TRUNCATE_NOTE.format(n=len(text) - head_chars, err=" (error)" if is_error else "")
    return head + note


def apply_decisions(
    messages: Sequence[Message],
    decisions: Sequence[CallDecision],
    calls: Sequence[ToolCall],
    head_chars: int,
    provenance: Optional[List[tuple]] = None,
) -> List[Message]:
    """Rebuilds the conversation from the decisions. A dropped call disappears
    together with its result; a dropped result keeps a bounded head and note.
    Messages that lose all their content are removed; untouched messages are
    returned as the same objects they came in as."""
    by_id = {call.id: call for call in calls}
    actions: Dict[str, str] = {}
    for decision in decisions:
        call = by_id.get(decision.id)
        if call and decision.action != "keep":
            actions[call.tool_use_id] = decision.action

    kept: List[Message] = []
    for in_index, message in enumerate(messages):
        touched = any(actions.get(t.tool_use_id) for t in message.toolUses) or any(
            actions.get(r.tool_use_id) for r in (message.toolResults or [])
        )
        if not touched:
            kept.append(message)
            if provenance is not None:
                provenance.append((in_index, len(kept) - 1, False))
            continue

        dropped_here = False
        tool_uses: List[ToolUse] = []
        for tool in message.toolUses:
            action = actions.get(tool.tool_use_id)
            if action == "drop_call":
                dropped_here = True
                continue
            if action == "drop_result" and tool.text is not None:
                new_text = truncated_result_text(tool.text, bool(tool.isError), head_chars)
                if new_text != tool.text:
                    copy = replace(tool)
                    copy.text = new_text
                    tool = copy
            tool_uses.append(tool)

        tool_results: List[ToolResult] = []
        for result in message.toolResults or []:
            action = actions.get(result.tool_use_id)
            if action == "drop_call":
                dropped_here = True
                continue
            if action == "drop_result":
                new_text = truncated_result_text(result.text, bool(result.isError), head_chars)
                if new_text != result.text:
                    result = ToolResult(
                        tool_use_id=result.tool_use_id,
                        text=new_text,
                        isError=result.isError,
                    )
            tool_results.append(result)

        unchanged = (
            not dropped_here
            and len(tool_uses) == len(message.toolUses)
            and all(a is b for a, b in zip(tool_uses, message.toolUses))
            and len(tool_results) == len(message.toolResults or [])
            and all(a is b for a, b in zip(tool_results, message.toolResults or []))
        )
        if unchanged:
            kept.append(message)
            if provenance is not None:
                provenance.append((in_index, len(kept) - 1, False))
            continue

        if not message.text.strip() and not tool_uses and not tool_results:
            if provenance is not None:
                provenance.append((in_index, None, True))
            continue
        rebuilt = Message(role=message.role, text=message.text, toolUses=tool_uses)
        if tool_results:
            rebuilt.toolResults = tool_results
        kept.append(rebuilt)
        if provenance is not None:
            provenance.append((in_index, len(kept) - 1, True))
    return kept


def message_chars(message: Message) -> int:
    total = len(message.text)
    for tool in message.toolUses:
        try:
            total += len(json.dumps(tool.input, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            total += 20
    for result in message.toolResults or []:
        total += len(result.text)
    return total


def reduction_ratio(result: CompactResult) -> float:
    before, after = result.stats.charsBefore, result.stats.charsAfter
    return 0 if before == 0 else (before - after) / before


def _count(decisions: Sequence[CallDecision], reason: str) -> int:
    return sum(1 for d in decisions if d.reason == reason)


def compact(
    messages: Sequence[Message],
    asker: JevAsker,
    options: Optional[CompactOptions] = None,
) -> CompactResult:
    """Compacts a transcript by asking Jev, for every tool call outside the
    pinned first and newest messages, whether the call and whether its result
    must stay. The whole history (results omitted, fitted into maxStateTokens)
    is sent as state with every batch of questions. Raises when Jev fails or
    the history cannot be fitted; the caller decides whether to fall back."""
    import time

    started = time.time()
    resolved = resolve_options(options)
    calls = collect_tool_calls(messages, resolved.preserveRecentMessages)
    candidates = [c for c in calls if not c.pinned]
    chars_before = sum(message_chars(m) for m in messages)

    fitted = None
    batches: List[List[ToolCall]] = []
    answers: Dict[str, CallAnswer] = {}
    if candidates:
        fitted = fit_state(messages, calls, resolved)
        batches = batch_calls(candidates, fitted.tokens, resolved)

        def run(batch: List[ToolCall]) -> Dict[str, CallAnswer]:
            return _ask_batch(asker, fitted.state, batch)

        with ThreadPoolExecutor(max_workers=min(8, len(batches))) as pool:
            for mapping in pool.map(run, batches):
                answers.update(mapping)

    decisions = [
        decide_call(call, answers.get(call.id) or CallAnswer(keepCall=1.0, keepResult=1.0), resolved)
        for call in calls
    ]
    provenance: List[tuple] = []
    kept = apply_decisions(messages, decisions, calls, resolved.truncateHeadChars, provenance)
    stats = CompactStats(
        messagesBefore=len(messages),
        messagesAfter=len(kept),
        charsBefore=chars_before,
        charsAfter=sum(message_chars(m) for m in kept),
        calls=len(calls),
        kept=_count(decisions, "kept"),
        resultsDropped=_count(decisions, "result_dropped"),
        callsDropped=_count(decisions, "call_dropped"),
        pinned=_count(decisions, "pinned"),
        stateTokens=fitted.tokens if fitted else 0,
        stateStage=fitted.stage if fitted else "",
        requests=len(batches),
        ms=int((time.time() - started) * 1000),
    )
    return CompactResult(messages=kept, decisions=decisions, stats=stats, provenance=provenance)
