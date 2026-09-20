"""Generic adapter: jevcomp's own JSONL (round-trip safe), one message per line."""

from __future__ import annotations

import json
from typing import List, TextIO

from ..types import Message
from . import Transcript, from_generic_dict, to_generic_dict


def load(path: str) -> Transcript:
    messages: List[Message] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            messages.append(from_generic_dict(json.loads(line)))
    return Transcript(messages, records=list(range(len(messages))), source=path)


def dump(transcript: Transcript, kept: List[Message], out: TextIO) -> None:
    for message in kept:
        out.write(json.dumps(to_generic_dict(message), ensure_ascii=False) + "\n")
