"""Tokenizer-free token estimation (same heuristic as fast-jev-compaction)."""

from __future__ import annotations

import re
import math

TOKEN_PIECES = re.compile(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]")


def estimate_tokens(text: str) -> int:
    """A word costs one token per six letters, a digit half a token, any other
    symbol nine tenths. Calibrated to land a little above the counts Jev
    reports (2-18% over); a plain characters-per-token ratio undercounts
    JSON-heavy states by up to 40%."""
    tokens = 0.0
    for match in TOKEN_PIECES.finditer(text):
        piece = match.group(0)
        first = ord(piece[0])
        if 48 <= first <= 57:  # digits
            tokens += len(piece) / 2
        elif (65 <= first <= 90) or (97 <= first <= 122):  # letters
            tokens += 1 + (len(piece) - 1) // 6
        else:
            tokens += 0.9
    return math.ceil(tokens)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


TEXT_HEAD = 400
TEXT_TAIL = 150


def abridge(text: str, head: int = TEXT_HEAD, tail: int = TEXT_TAIL) -> str:
    if len(text) <= head + tail + 40:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[… {omitted} chars omitted …]\n{text[-tail:]}"
