"""jevcomp CLI.

Examples:
  python3 -m jevcomp claude   ~/.claude/projects/<project>/<session>.jsonl
  python3 -m jevcomp codex    ~/.codex/sessions/2026/.../rollout-*.jsonl
  python3 -m jevcomp zcode    latest            # read-only, emits generic jsonl
  python3 -m jevcomp generic  transcript.jsonl  # jevcomp's own format
  python3 -m jevcomp compact-stdin               # hook protocol (JSON on stdin)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import adapters
from .client import JevClient
from .config import find_api_key
from .decide import compact, reduction_ratio, resolve_options
from .state import collect_tool_calls, fit_state
from .types import CompactOptions, Message


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-key", help="TypeSafe API key (else TYPESAFE_API_KEY / ~/.jevcomp.json / api-keys.json)")
    parser.add_argument("--model", default=None, help="Jev model (default jev-latest)")
    parser.add_argument("--base-url", default=None, help="System One endpoint override")
    parser.add_argument("--goal", default=None, help="task description overriding the last user prompts")
    parser.add_argument("--keep-threshold", type=float, default=0.5)
    parser.add_argument("--preserve-recent", type=int, default=6)
    parser.add_argument("--max-state-tokens", type=int, default=25000)
    parser.add_argument("--max-request-tokens", type=int, default=30000)
    parser.add_argument("--truncate-head", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="fit state and plan requests without calling Jev")
    parser.add_argument("--json", action="store_true", help="machine-readable stats on stdout")
    parser.add_argument("--stdout", action="store_true", help="write the compacted transcript to stdout")
    parser.add_argument("-o", "--output", default=None, help="output path (default <input>.jevcomp.jsonl)")
    parser.add_argument("--in-place", action="store_true", help="rewrite the input file (a .bak copy is kept)")


def _options_from(args) -> CompactOptions:
    return CompactOptions(
        goal=args.goal,
        keepThreshold=args.keep_threshold,
        preserveRecentMessages=args.preserve_recent,
        maxStateTokens=args.max_state_tokens,
        maxRequestTokens=args.max_request_tokens,
        truncateHeadChars=args.truncate_head,
    )


def _human_stats(result) -> str:
    stats = result.stats
    parts = [
        f"messages {stats.messagesBefore} → {stats.messagesAfter}",
        f"chars {stats.charsBefore} → {stats.charsAfter} ({reduction_ratio(result)*100:.0f}% reduction)",
        f"calls {stats.calls}: {stats.kept} kept, {stats.resultsDropped} results truncated, {stats.callsDropped} dropped, {stats.pinned} pinned",
        f"state ~{stats.stateTokens} tokens ({stats.stateStage}) in {stats.requests} request(s), {stats.ms} ms",
    ]
    return "; ".join(parts)


def _decisions_line(result) -> str:
    return " ".join(
        f"{d.id}:{d.tool}:{d.action}/call={d.keepCall:.2f}/result={d.keepResult:.2f}"
        for d in result.decisions
        if d.reason != "pinned"
    )


def _run_compact(transcript, args, format_name: str):
    options = _options_from(args)
    resolved = resolve_options(options)
    messages = transcript.messages
    calls = collect_tool_calls(messages, resolved.preserveRecentMessages)
    candidates = [c for c in calls if not c.pinned]

    if args.dry_run:
        plan = {"format": format_name, "source": transcript.source,
                "messages": len(messages), "candidates": len(candidates), "pinned": len(calls) - len(candidates)}
        if candidates:
            fitted = fit_state(messages, calls, resolved)
            from .decide import batch_calls
            batches = batch_calls(candidates, fitted.tokens, resolved)
            plan.update(stateTokens=fitted.tokens, stateStage=fitted.stage,
                        questions=len(candidates) * 2, requests=len(batches))
        else:
            plan.update(stateTokens=0, stateStage="", questions=0, requests=0)
        print(json.dumps(plan, ensure_ascii=False))
        return 0

    key, key_source = find_api_key(args.api_key)
    if not key:
        print("error: no TypeSafe API key (--api-key, TYPESAFE_API_KEY, ~/.jevcomp.json, ~/Desktop/api-keys.json)",
              file=sys.stderr)
        return 2
    if not args.json:
        print(f"key: {key[:8]}…{key[-4:]} ({key_source})", file=sys.stderr)
    client = JevClient(api_key=key, model=args.model, base_url=args.base_url)
    result = compact(messages, client, options)

    if args.json:
        print(json.dumps({
            "source": transcript.source,
            "stats": vars(result.stats),
            "reductionRatio": round(reduction_ratio(result), 4),
            "decisions": [vars(d) for d in result.decisions if d.reason != "pinned"],
        }, ensure_ascii=False, default=str))

    # write compacted transcript
    generic_only = format_name in ("zcode",)
    if args.stdout:
        _dump_output(transcript, result, sys.stdout, format_name, provenance=None if generic_only else result.provenance, generic_only=generic_only)
        if not args.json:
            print(_human_stats(result), file=sys.stderr)
            decisions = _decisions_line(result)
            if decisions:
                print(f"decisions: {decisions[:4000]}", file=sys.stderr)
        return 0
    if args.in_place and not generic_only:
        import shutil
        shutil.copyfile(transcript.source, transcript.source + ".bak")
        with open(transcript.source, "w", encoding="utf-8") as out:
            _dump_output(transcript, result, out, format_name, result.provenance, generic_only=False)
        out_path = transcript.source
    else:
        default_name = transcript.source if generic_only else transcript.source + ".jevcomp.jsonl"
        out_path = args.output or (f"zcode-{transcript.source.split(':')[-1][:8]}.compacted.jsonl" if generic_only else default_name)
        with open(out_path, "w", encoding="utf-8") as out:
            _dump_output(transcript, result, out, format_name, None if generic_only else result.provenance, generic_only=generic_only)
    if not args.json:
        print(_human_stats(result), file=sys.stderr)
        print(f"wrote {out_path}", file=sys.stderr)
        decisions = _decisions_line(result)
        if decisions:
            print(f"decisions: {decisions}", file=sys.stderr)
    return 0


def _dump_output(transcript, result, out, format_name: str, provenance, generic_only: bool) -> None:
    module = {
        "claude": adapters.claude,
        "codex": adapters.codex,
        "generic": adapters.generic,
    }.get(format_name)
    if generic_only or module is None:
        adapters.generic.dump(None, result.messages, out)
        return
    if format_name in ("claude", "codex"):
        module.dump(transcript, result.messages, out, provenance)
    else:
        module.dump(transcript, result.messages, out)


def _cmd_compact_stdin(argv: List[str]) -> int:
    """Hook protocol: {"messages":[...], "options":{...}} on stdin →
    {"messages":[{"orig":i} | {"message":{...}}], "stats":{...}} on stdout.
    Untouched messages round-trip as their input index so the caller can hand
    the engine's own objects back; rebuilt ones carry their new content."""
    payload = json.load(sys.stdin)
    messages = [adapters.from_generic_dict(m) for m in payload.get("messages", [])]
    raw_options = payload.get("options") or {}
    options = CompactOptions(
        goal=raw_options.get("goal"),
        keepThreshold=float(raw_options.get("keepThreshold", 0.5)),
        preserveRecentMessages=int(raw_options.get("preserveRecentMessages", 6)),
        maxStateTokens=int(raw_options.get("maxStateTokens", 25000)),
        maxRequestTokens=int(raw_options.get("maxRequestTokens", 30000)),
        truncateHeadChars=int(raw_options.get("truncateHeadChars", 300)),
    )
    key, _ = find_api_key(raw_options.get("apiKey"))
    if not key:
        raise RuntimeError("no TypeSafe API key configured")
    client = JevClient(
        api_key=key,
        model=raw_options.get("model"),
        base_url=raw_options.get("baseUrl"),
    )
    result = compact(messages, client, options)
    by_in_index = {}
    for in_index, out_index, rebuilt in result.provenance:
        by_in_index[in_index] = (out_index, rebuilt)
    out_messages = []
    for in_index in range(len(messages)):
        entry = by_in_index.get(in_index)
        if entry is None or entry[0] is None:
            continue
        out_index, rebuilt = entry
        if rebuilt:
            out_messages.append({"message": adapters.to_generic_dict(result.messages[out_index])})
        else:
            out_messages.append({"orig": in_index})
    print(json.dumps({
        "messages": out_messages,
        "stats": vars(result.stats),
        "decisions": [vars(d) for d in result.decisions if d.reason != "pinned"],
    }, ensure_ascii=False))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if argv and argv[0] == "autopilot":  # own parser, bypass the common options
        from . import autopilot as _autopilot

        return _autopilot.cli(argv[1:])

    parser = argparse.ArgumentParser(prog="jevcomp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="format", required=True)

    for name, help_text in [
        ("claude", "Claude Code session .jsonl"),
        ("codex", "Codex rollout .jsonl"),
        ("generic", "jevcomp generic .jsonl"),
    ]:
        p = sub.add_parser(name, help=help_text)
        _add_common_options(p)
        p.add_argument("path", help="transcript file path")

    p = sub.add_parser("zcode", help="zcode session (read-only, SQLite)")
    _add_common_options(p)
    p.add_argument("session", nargs="?", default="latest", help="session id or 'latest'")
    p.add_argument("--db", default=adapters.zcode.DEFAULT_DB, help="path to zcode db.sqlite")
    p.add_argument("--list", action="store_true", help="list recent sessions and exit")

    sub.add_parser("compact-stdin", help="hook protocol: JSON on stdin, JSON on stdout")

    p = sub.add_parser("mcp", help="run jevcomp as an MCP stdio server (zcode/codex/claude)")
    p.add_argument("--check", action="store_true", help="answer one initialize+tools/list then exit")

    args = parser.parse_args(argv)

    if args.format == "compact-stdin":
        return _cmd_compact_stdin(args)

    if args.format == "mcp":
        from . import mcp_server

        if getattr(args, "check", False):
            import json as _json

            print(_json.dumps(mcp_server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05"}}), ensure_ascii=False))
            print(_json.dumps(mcp_server.handle(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), ensure_ascii=False)[:200])
            return 0
        return mcp_server.serve()

    if args.format == "zcode":
        if args.list:
            for session in adapters.zcode.list_sessions(args.db):
                print(f"{session['id']}  {session['title']}")
            return 0
        transcript = adapters.zcode.load(args.session, args.db)
        return _run_compact(transcript, args, "zcode")

    module = {"claude": adapters.claude, "codex": adapters.codex, "generic": adapters.generic}[args.format]
    transcript = module.load(args.path)
    return _run_compact(transcript, args, args.format)


if __name__ == "__main__":
    sys.exit(main())
