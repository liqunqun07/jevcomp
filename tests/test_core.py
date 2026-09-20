"""Unit tests with a fake Jev (never contacts TypeSafe). Mirrors the
scenarios of fast-jev-compaction's own test suite."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jevcomp.decide import (  # noqa: E402
    apply_decisions,
    batch_calls,
    compact,
    decide_call,
    questions_for,
    reduction_ratio,
    truncated_result_text,
)
from jevcomp.state import (  # noqa: E402
    collect_tool_calls,
    fit_state,
    estimate_tokens,
    goal_from_messages,
    is_pinned,
)
from jevcomp.types import (  # noqa: E402
    CallAnswer,
    CompactOptions,
    JevAsker,
    Message,
    ToolCall,
    ToolResult,
    ToolUse,
)


def convo(n_calls: int = 3, result_chars: int = 500) -> list:
    """user prompt, then n_calls rounds of (assistant call, user result),
    then trailing messages so early calls are not pinned."""
    messages = [Message(role="user", text="Fix the failing test. Never edit src/generated.")]
    for i in range(n_calls):
        messages.append(
            Message(
                role="assistant",
                text="",
                toolUses=[
                    ToolUse(
                        tool_use_id=f"toolu_{i + 1}",
                        tool="Read",
                        input={"file_path": f"src/file{i}.ts"},
                    )
                ],
            )
        )
        messages.append(
            Message(
                role="user",
                text="",
                toolResults=[ToolResult(tool_use_id=f"toolu_{i + 1}", text="x" * result_chars)],
            )
        )
    messages.append(Message(role="assistant", text="Done, all fixed."))
    messages.append(Message(role="user", text="thanks!"))
    return messages


class FakeAsker(JevAsker):
    """Answers from a {t1: (keepCall, keepResult)} table; records requests."""

    def __init__(self, table=None, fail=False):
        self.table = table or {}
        self.fail = fail
        self.requests = []

    def ask(self, state, questions):
        if self.fail:
            raise RuntimeError("Jev down")
        self.requests.append({"state": state, "questions": questions})
        answers = {}
        for name in questions:
            _, call_id = name.split("_", 1)
            keep_call, keep_result = self.table.get(call_id, (1.0, 1.0))
            if name.startswith("call_"):
                answers[name] = {"type": "noul", "noul": keep_call}
            else:
                answers[name] = {"type": "noul", "noul": keep_result}
        return {"model": "fake", "answers": answers}


class TestPinning(unittest.TestCase):
    def test_first_and_recent_are_pinned(self):
        self.assertTrue(is_pinned(0, 10, 6))
        self.assertFalse(is_pinned(1, 10, 6))
        self.assertTrue(is_pinned(4, 10, 6))
        self.assertTrue(is_pinned(9, 10, 6))

    def test_pinned_calls_never_dropped(self):
        messages = convo()  # 9 messages; preserve 6 → pinned when index >= 3
        calls = collect_tool_calls(messages, 6)
        self.assertEqual(len(calls), 3)
        self.assertFalse(calls[0].pinned)   # t1: call@1, result@2
        self.assertTrue(calls[1].pinned)    # t2: call@3, result@4
        self.assertTrue(calls[2].pinned)    # t3: call@5, result@6
        asker = FakeAsker({t.id: (0.0, 0.0) for t in calls})
        result = compact(messages, asker)
        self.assertEqual(result.stats.pinned, 2)
        self.assertEqual(result.stats.kept, 0)
        self.assertEqual(result.stats.callsDropped, 1)
        self.assertLess(len(result.messages), len(messages))

    def test_pinned_excluded_from_questions(self):
        messages = convo()
        asker = FakeAsker()
        compact(messages, asker)
        asked = set()
        for request in asker.requests:
            asked.update(request["questions"])
        self.assertNotIn("call_t2", asked)   # t2/t3 pinned (index >= 3)
        self.assertIn("call_t1", asked)      # t1 is a candidate

    def test_no_candidates_no_request(self):
        messages = [Message(role="user", text="hi"), Message(role="assistant", text="hello")]
        asker = FakeAsker()
        result = compact(messages, asker)
        self.assertEqual(asker.requests, [])
        self.assertEqual(result.stats.requests, 0)
        self.assertEqual(result.messages, messages)


class TestDecisions(unittest.TestCase):
    def _calls(self, n=6, total=12):
        return collect_tool_calls(convo(n, 100), total)

    def test_keep_result(self):
        call = ToolCall("t1", "id1", "Read", {}, 1, 2, 10, False, False)
        d = decide_call(call, CallAnswer(0.9, 0.9), CompactOptions())
        self.assertEqual((d.action, d.reason), ("keep", "kept"))

    def test_keep_call_drop_result(self):
        call = ToolCall("t1", "id1", "Read", {}, 1, 2, 10, False, False)
        d = decide_call(call, CallAnswer(0.9, 0.2), CompactOptions())
        self.assertEqual((d.action, d.reason), ("drop_result", "result_dropped"))

    def test_drop_call(self):
        call = ToolCall("t1", "id1", "Read", {}, 1, 2, 10, False, False)
        d = decide_call(call, CallAnswer(0.1, 0.1), CompactOptions())
        self.assertEqual((d.action, d.reason), ("drop_call", "call_dropped"))

    def test_truncated_result_text(self):
        text = "a" * 1000
        out = truncated_result_text(text, False, 300)
        self.assertTrue(out.startswith("a" * 300))
        self.assertIn("jevcomp truncated 700 chars", out)
        self.assertEqual(truncated_result_text("short", False, 300), "short")

    def test_drop_call_removes_both_sides(self):
        messages = convo(2, 500)
        # make both calls non-pinned: 6 messages + tail 2 → preserve 0
        calls = collect_tool_calls(messages, 0)
        decisions = [decide_call(c, CallAnswer(0.0, 0.0), CompactOptions()) for c in calls]
        kept = apply_decisions(messages, decisions, calls, 300)
        ids = {t.tool_use_id for m in kept for t in m.toolUses}
        self.assertEqual(ids, set())
        self.assertEqual(len(kept), 3)  # prompt + assistant text + user thanks

    def test_drop_result_truncates(self):
        messages = convo(2, 5000)
        calls = collect_tool_calls(messages, 0)
        decisions = [
            decide_call(c, CallAnswer(1.0, 0.0), CompactOptions()) for c in calls
        ]
        kept = apply_decisions(messages, decisions, calls, 300)
        result_texts = [r for m in kept for r in m.toolResults or []]
        self.assertEqual(len(result_texts), 2)
        for r in result_texts:
            self.assertLess(len(r.text), 500)
            self.assertIn("jevcomp truncated", r.text)

    def test_untouched_messages_round_trip_identity(self):
        messages = convo(3, 100)
        result = compact(messages, FakeAsker())  # all keep
        for original, kept in zip(messages, result.messages):
            self.assertIs(original, kept)

    def test_missing_answer_defaults_to_keep(self):
        messages = convo(3, 100)
        asker = FakeAsker(table={"t1": (0.0, 0.0)})  # t2, t3 missing
        calls = collect_tool_calls(messages, 0)
        result = compact(messages, asker, CompactOptions(preserveRecentMessages=0))
        self.assertEqual(result.stats.callsDropped + result.stats.kept, 3)


class TestQuestions(unittest.TestCase):
    def test_question_shape(self):
        call = ToolCall("t7", "toolu_7", "Bash", {"command": "ls"}, 1, 2, 4213, False, False)
        questions = questions_for(call)
        self.assertEqual(
            sorted(questions),
            ["call_t7", "result_t7"],
        )
        self.assertEqual(questions["call_t7"]["type"], "noul")
        self.assertIn("Bash", questions["call_t7"]["instructions"])
        self.assertIn("4213 chars", questions["result_t7"]["instructions"])

    def test_batching_splits_on_budget(self):
        calls = collect_tool_calls(convo(30, 100), 0)
        self.assertEqual(len(calls), 30)
        options = CompactOptions(maxStateTokens=25000, maxRequestTokens=25000 + 400)
        batches = batch_calls(calls, 25000, options)
        self.assertGreater(len(batches), 1)
        flat = [c for batch in batches for c in batch]
        self.assertEqual([c.id for c in flat], [c.id for c in calls])

    def test_batching_throws_when_no_room(self):
        calls = collect_tool_calls(convo(2, 100), 0)
        options = CompactOptions(maxStateTokens=25000, maxRequestTokens=25000 + 10)
        with self.assertRaises(ValueError):
            batch_calls(calls, 25000, options)


class TestStateFitting(unittest.TestCase):
    def test_full_fits_small_history(self):
        messages = convo(3, 500)
        calls = collect_tool_calls(messages, 6)
        fitted = fit_state(messages, calls, CompactOptions())
        self.assertEqual(fitted.stage, "full")
        self.assertGreater(fitted.tokens, 0)

    def test_stages_kick_in_for_huge_history(self):
        messages = convo(40, 4000)
        calls = collect_tool_calls(messages, 6)
        fitted = fit_state(messages, calls, CompactOptions(maxStateTokens=2000))
        self.assertLessEqual(fitted.tokens, 2000)
        self.assertNotEqual(fitted.stage, "full")

    def test_fold_stage_handles_huge_history(self):
        messages = convo(50, 20000)
        calls = collect_tool_calls(messages, 0)
        fitted = fit_state(messages, calls, CompactOptions(maxStateTokens=2000))
        self.assertLessEqual(fitted.tokens, 2000)
        self.assertTrue(
            "merged" in fitted.stage or "folded" in fitted.stage,
            f"unexpected stage: {fitted.stage}",
        )

    def test_goal_from_last_user_prompts(self):
        messages = [
            Message(role="user", text="first task"),
            Message(role="assistant", text="ok"),
            Message(role="user", text="second task"),
            Message(role="user", text="third task"),
            Message(role="user", text="fourth task"),
        ]
        goal = goal_from_messages(messages)
        self.assertIn("second task", goal)
        self.assertIn("fourth task", goal)
        self.assertNotIn("first task", goal)


class TestStats(unittest.TestCase):
    def test_stats_and_ratio(self):
        messages = convo(6, 3000)
        calls = collect_tool_calls(messages, 0)
        asker = FakeAsker({c.id: (0.0, 0.0) for c in calls})
        result = compact(messages, asker, CompactOptions(preserveRecentMessages=0))
        self.assertEqual(result.stats.messagesBefore, len(messages))
        self.assertLess(result.stats.messagesAfter, len(messages))
        self.assertEqual(result.stats.calls, 6)
        self.assertEqual(result.stats.callsDropped, 6)
        self.assertGreater(reduction_ratio(result), 0.5)
        self.assertEqual(result.stats.requests, len(asker.requests))

    def test_provenance_maps_every_input(self):
        messages = convo(4, 800)
        calls = collect_tool_calls(messages, 0)
        asker = FakeAsker({c.id: (1.0, 0.0) for c in calls})
        result = compact(messages, asker, CompactOptions(preserveRecentMessages=0))
        self.assertEqual(len(result.provenance), len(messages))
        outs = [out for _, out, _ in result.provenance]
        self.assertEqual(outs, sorted(o for o in outs if o is not None))


class TestTokens(unittest.TestCase):
    def test_estimate_tokens_monotone_and_small(self):
        tiny = estimate_tokens("hello world")
        big = estimate_tokens("hello " * 1000)
        self.assertGreater(big, tiny)
        self.assertGreater(tiny, 0)

    def test_estimate_digits(self):
        self.assertEqual(estimate_tokens("12345678"), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
