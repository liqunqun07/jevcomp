"""jevcomp: agent-universal context compaction via Jev (TypeSafe System One).

Implements the `fast-jev-compaction` algorithm (tamaratran, MIT), redesigned to be agent-agnostic so it can serve Claude Code, zcode, Codex and any MCP client:
instead of summarizing old turns, every tool call and its result is scored by
Jev with calibrated yes/no (`noul`) questions; stale ones are deleted or
truncated, everything kept stays verbatim. User and assistant text is never
rewritten.
"""

from .decide import compact, reduction_ratio
from .state import collect_tool_calls, fit_state
from .client import JevClient

__version__ = "0.1.0"

__all__ = [
    "compact",
    "reduction_ratio",
    "collect_tool_calls",
    "fit_state",
    "JevClient",
]
