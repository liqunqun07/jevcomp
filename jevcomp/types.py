"""Core data shapes, agent-agnostic (mirrors fast-jev-compaction's types.ts)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

Role = str  # 'user' | 'assistant'


@dataclass
class ToolUse:
    tool_use_id: str
    tool: str
    input: Dict[str, Any]
    # Set by adapters whose transcript attaches the outcome to the call block
    # (Claude Code's function-hook shape); jsonl adapters leave them unset.
    text: Optional[str] = None
    isError: Optional[bool] = None


@dataclass
class ToolResult:
    tool_use_id: str
    text: str
    isError: Optional[bool] = None


@dataclass
class Message:
    role: Role
    text: str
    toolUses: List[ToolUse] = field(default_factory=list)
    toolResults: Optional[List[ToolResult]] = None


@dataclass
class ToolCall:
    """A tool call paired with its result by tool_use_id."""

    id: str  # short id used in state and question names ('t1', 't2', ...)
    tool_use_id: str
    tool: str
    input: Dict[str, Any]
    callIndex: int
    resultIndex: int
    resultChars: int
    isError: bool
    pinned: bool


@dataclass
class CallAnswer:
    keepCall: float
    keepResult: float


CallAction = str  # 'keep' | 'drop_result' | 'drop_call'
CallReason = str  # 'pinned' | 'kept' | 'result_dropped' | 'call_dropped'


@dataclass
class CallDecision:
    id: str
    tool: str
    keepCall: float
    keepResult: float
    action: CallAction
    reason: CallReason


@dataclass
class HistoryToolCall:
    id: str
    tool: str
    input: str
    result: str


# tool_calls per history entry: structured dicts, or one compact line per call
HistoryToolCallList = List[Union[HistoryToolCall, str]]


@dataclass
class HistoryEntry:
    i: int
    role: Role
    text: str
    tool_calls: Optional[HistoryToolCallList] = None


@dataclass
class CompactionState:
    context: str
    goal: str
    history: List[HistoryEntry]


@dataclass
class FittedState:
    state: CompactionState
    tokens: int
    stage: str  # which fitting stage produced the state, for diagnostics


@dataclass
class CompactOptions:
    goal: Optional[str] = None
    keepThreshold: float = 0.5
    preserveRecentMessages: int = 6
    maxStateTokens: int = 25_000
    maxRequestTokens: int = 30_000
    truncateHeadChars: int = 300


@dataclass
class CompactStats:
    messagesBefore: int = 0
    messagesAfter: int = 0
    charsBefore: int = 0
    charsAfter: int = 0
    calls: int = 0
    kept: int = 0
    resultsDropped: int = 0
    callsDropped: int = 0
    pinned: int = 0
    stateTokens: int = 0
    stateStage: str = ""
    requests: int = 0
    ms: int = 0


@dataclass
class CompactResult:
    messages: List[Message]
    decisions: List[CallDecision]
    stats: CompactStats = field(default_factory=CompactStats)
    # (input_index, output_index or None when removed, rebuilt flag); lets
    # file adapters place each output message back onto its source record.
    provenance: List[tuple] = field(default_factory=list)


# ---- Jev wire types (System One) ----

NoulQuestion = Dict[str, Any]  # {'type': 'noul', 'instructions': str, ...}
JevQuestions = Dict[str, NoulQuestion]
JevState = Union[str, Any]  # any JSON-serialisable state
JevResponse = Dict[str, Any]  # must hold 'answers'


class JevAsker:
    """Anything that can answer Jev questions."""

    def ask(self, state: JevState, questions: JevQuestions) -> JevResponse:
        raise NotImplementedError
