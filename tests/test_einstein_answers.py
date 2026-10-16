"""The client's answers in a delivered project (DESIGN.md section 4): the validator's own check on
its export -- every answer that arrived before the hand-over recorded in its decision, a
no-questions job with no client decision open -- and the desk refusing a delivery that fails it,
with the reason in the miner's receipt."""

from __future__ import annotations

import calendar
import time
import unittest
from typing import Any

from cadmind_einstein.agent_miner import PROMPT, questions_rule
from cadmind_einstein.answers import (
    check,
    decisions_of,
    open_client,
    parse_answer,
    quote_of,
    quotes,
    unrecorded,
)
from tests.test_einstein import GOOD, TestDesk, project_zip, wait_state

T0 = calendar.timegm(time.strptime("2026-10-15 12:00:00", "%Y-%m-%d %H:%M:%S"))
"""A job handed over at noon (UTC) on 2026-10-15."""

ANSWER = """# The client's answer to decision mount

Question: Screwed to the wall, or stuck on?

Answer: tape (foam tape: holds less)

(received 2026-10-15 11:58 UTC on cadmind.ai)
"""
ANSWER_WORDS = """# The client's answer to decision mount

Question: Screwed to the wall, or stuck on?

In the client's words:

Stuck on please, the wall is tiled and I rent the flat

(received 2026-10-15 11:58 UTC on cadmind.ai)
"""
OPEN = """[decision.mount]
question = "Screwed to the wall, or stuck on?"
options = { screws = "two screws: needs a drill", tape = "foam tape: holds less" }
by = "client"
status = "open"
assumption = "screws"
because = "the load is a bunch of keys"
affects = ["R.holds"]
"""
ANSWERED = """[decision.mount]
question = "Screwed to the wall, or stuck on?"
options = { screws = "two screws: needs a drill", tape = "foam tape: holds less" }
by = "client"
status = "answered"
choice = "tape"
because = "the client chose it"
answer = { from = "client: tape (foam tape: holds less) (cadmind.ai, 2026-10-15)" }
affects = ["R.holds"]
"""
DECIDED = """[decision.mount]
question = "Screwed to the wall, or stuck on?"
options = { screws = "two screws: needs a drill", tape = "foam tape: holds less" }
by = "agent"
status = "decided"
choice = "tape"
because = "the client asked not to be asked; tape needs no drill"
affects = ["R.holds"]
"""
PATH = "brief/answer-2026-10-15-mount-115800.md"


def exported(files: dict[str, str]) -> bytes:
    return project_zip({"brief/brief.md": "A key hook.", **files})


class TestAnswerFiles(unittest.TestCase):
    def test_an_answer_file_is_read_by_its_name_and_its_text(self) -> None:
        a = parse_answer(PATH, ANSWER)
        assert a is not None
        self.assertEqual((a.decision, a.choice, a.words), ("mount", "tape", False))
        self.assertEqual(a.at_ms, (T0 - 120) * 1000)
        old = parse_answer(
            "brief/answer-2026-10-15-mount.md", ANSWER
        )  # no stamp: the received line
        assert old is not None
        self.assertEqual(old.at_ms, (T0 - 120 + 59) * 1000)
        dashed = parse_answer("brief/answer-2026-10-15-wall-mount-115800.md", ANSWER)
        assert dashed is not None
        self.assertEqual(dashed.decision, "wall-mount")
        self.assertIsNone(parse_answer("brief/brief.md", "x"))
        self.assertIsNone(parse_answer("spec/answer-2026-10-15-mount-115800.md", ANSWER))

    def test_a_quote_is_the_words_between_client_and_the_channel(self) -> None:
        self.assertEqual(
            quote_of('client: "stuck on please" (cadmind.ai, 2026-10-15)'), "stuck on please"
        )
        self.assertEqual(quote_of("estimate: a guess"), "")
        a = parse_answer(PATH, ANSWER_WORDS)
        assert a is not None
        self.assertTrue(quotes("client: the wall is  TILED and I rent (cadmind.ai, 2026-10-15)", a))
        self.assertFalse(quotes("client: screw it to the wall (cadmind.ai, 2026-10-15)", a))
        self.assertFalse(quotes("client:  (cadmind.ai, 2026-10-15)", a))


class TestCheck(unittest.TestCase):
    def test_an_answer_recorded_in_its_decision_passes(self) -> None:
        self.assertEqual(check(exported({PATH: ANSWER, "spec/decisions.toml": ANSWERED})), "")

    def test_an_answer_left_open_fails_with_a_reason_naming_it(self) -> None:
        why = check(exported({PATH: ANSWER, "spec/decisions.toml": OPEN}))
        self.assertIn("still open", why)
        self.assertIn(PATH, why)

    def test_an_answer_that_does_not_quote_the_file_fails(self) -> None:
        wrong = ANSWERED.replace(
            "client: tape (foam tape: holds less)", "client: whatever you like"
        )
        why = check(exported({PATH: ANSWER, "spec/decisions.toml": wrong}))
        self.assertIn("does not quote", why)

    def test_the_recorded_choice_must_be_the_one_the_client_picked(self) -> None:
        other = ANSWERED.replace('choice = "tape"', 'choice = "screws"')
        self.assertIn(
            "the client chose 'tape'", check(exported({PATH: ANSWER, "spec/d.toml": other}))
        )
        # with their own words beside it, the words decide: the choice is the builder's reading of
        # them
        words = ANSWERED.replace(
            "client: tape (foam tape: holds less)", "client: the wall is tiled and I rent the flat"
        )
        self.assertEqual(check(exported({PATH: ANSWER_WORDS, "spec/d.toml": words})), "")

    def test_a_deleted_decision_fails_and_a_superseded_one_counts_where_it_went(self) -> None:
        self.assertIn("no longer has", check(exported({PATH: ANSWER})))
        superseded = OPEN.replace(
            'status = "open"', 'status = "superseded"\nsuperseded_by = "mount2"'
        )
        superseded += ANSWERED.replace("[decision.mount]", "[decision.mount2]")
        self.assertEqual(check(exported({PATH: ANSWER, "spec/decisions.toml": superseded})), "")

    def test_only_the_latest_answer_to_a_decision_counts(self) -> None:
        later = "brief/answer-2026-10-15-mount-115900.md"
        changed = ANSWER.replace(
            "Answer: tape (foam tape: holds less)", "Answer: screws (two screws: needs a drill)"
        )
        files = {PATH: ANSWER, later: changed, "spec/decisions.toml": ANSWERED}
        why = check(
            exported(files)
        )  # the decision still quotes the first answer, which the client changed
        self.assertIn("does not quote", why)
        self.assertIn(later, why)
        files["spec/decisions.toml"] = ANSWERED.replace(
            'choice = "tape"', 'choice = "screws"'
        ).replace(
            "client: tape (foam tape: holds less)", "client: screws (two screws: needs a drill)"
        )
        self.assertEqual(check(exported(files)), "")

    def test_an_answer_after_the_hand_over_never_counts_against_the_builder(self) -> None:
        files = {PATH: ANSWER, "spec/decisions.toml": OPEN}
        self.assertIn("still open", check(exported(files), before_ms=T0 * 1000))
        self.assertEqual(check(exported(files), before_ms=(T0 - 600) * 1000), "")

    def test_no_questions_leaves_no_client_decision_open(self) -> None:
        self.assertIn(
            "asked not to be asked",
            check(exported({"spec/decisions.toml": OPEN}), no_questions=True),
        )
        self.assertEqual(check(exported({"spec/decisions.toml": DECIDED}), no_questions=True), "")
        # without the flag an open question is the client's to answer, and passes
        self.assertEqual(check(exported({"spec/decisions.toml": OPEN})), "")

    def test_an_unreadable_export_fails_closed(self) -> None:
        self.assertIn("could not be read", check(b"not a zip"))

    def test_decisions_are_read_from_every_spec_file(self) -> None:
        d = decisions_of(
            {
                "spec/a.toml": OPEN,
                "spec/b.toml": "not = [toml",
                "spec/c.toml": DECIDED.replace("mount", "colour"),
            }
        )
        self.assertEqual(sorted(d), ["colour", "mount"])
        self.assertEqual(open_client(d), ["mount"])
        a = parse_answer(PATH, ANSWER)
        assert a is not None
        self.assertEqual(
            unrecorded([a], d, before_ms=0)[0].split(",")[0], f"{PATH} answers decision mount"
        )


class TestDeskAnswers(unittest.TestCase):
    """The desk on the in-memory platform: the answers check is part of the gate, on the validator's
    fork."""

    # TestDesk's scaffolding, borrowed (not inherited: its own tests run once, there)
    tearDown = TestDesk.tearDown
    tick = TestDesk.tick
    enqueue = TestDesk.enqueue
    poll = TestDesk.poll
    assigned = TestDesk.assigned

    def setUp(self) -> None:
        TestDesk.setUp(self)  # type: ignore[arg-type]
        self.clock.t = float(T0)

    def run_job(self, job: str, files: dict[str, str], **body: Any) -> dict[str, Any]:
        self.poll(*self.reg)
        self.enqueue({"job": job, "brief": "A key hook.", **body})
        hk, w = self.assigned()
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], {**GOOD, **files}, deliverable=True)
        self.desk.submit(hk, job, w["workspace"], "d")
        st = wait_state(self.desk, job, ("queued", "done"))
        st["hotkey"] = hk
        st["work"] = w
        return st

    def test_a_recorded_answer_is_delivered(self) -> None:
        st = self.run_job("ja", {PATH: ANSWER, "spec/decisions.toml": ANSWERED})
        self.assertEqual(st["result"]["label"], "delivered")

    def test_an_answer_left_unrecorded_fails_the_gate_and_the_miner_reads_why(self) -> None:
        st = self.run_job("jb", {PATH: ANSWER, "spec/decisions.toml": OPEN})
        self.assertEqual(st["state"], "queued")  # another miner gets it, from scratch
        failures = self.desk.failures(st["hotkey"])
        self.assertEqual(failures[-1]["kind"], "gate")
        self.assertIn("still open", failures[-1]["why"])
        self.assertIn("client's answers", st["attempts"][-1]["outcome"])

    def test_a_no_questions_job_says_so_and_refuses_an_open_question(self) -> None:
        self.poll(*self.reg)
        self.enqueue({"job": "jc", "brief": "A key hook.", "no_questions": True})
        hk, w = self.assigned()
        self.assertIs(w["no_questions"], True)
        self.desk.accept(hk, "jc")
        self.p.work(w["workspace"], {**GOOD, "spec/decisions.toml": OPEN}, deliverable=True)
        self.desk.submit(hk, "jc", w["workspace"], "d")
        st = wait_state(self.desk, "jc", ("queued", "done"))
        self.assertEqual(st["state"], "queued")
        self.assertIn("asked not to be asked", self.desk.failures(hk)[-1]["why"])

    def test_a_no_questions_job_with_its_questions_decided_is_delivered(self) -> None:
        st = self.run_job("jd", {"spec/decisions.toml": DECIDED}, no_questions=True)
        self.assertEqual(st["result"]["label"], "delivered")

    def test_a_client_job_without_the_flag_is_not_no_questions(self) -> None:
        self.poll(*self.reg)
        self.enqueue(
            {"job": "je", "brief": "A key hook.", "no_questions": "yes"}
        )  # only true is true
        _, w = self.assigned()
        self.assertIs(w["no_questions"], False)


class TestMinerPrompt(unittest.TestCase):
    def test_the_prompt_checks_answers_after_each_step_or_decides_them(self) -> None:
        ask = PROMPT.format(
            ws="ag-1",
            job="j",
            brief="b",
            minutes=9,
            deadline_utc="12:00",
            server="s",
            questions=questions_rule({}, "ag-1"),
            no_questions="no",
        )
        self.assertIn("client_answers", ask)
        self.assertIn("After each `watt` command", ask)
        self.assertIn("ag-1", ask)
        decide = questions_rule({"no_questions": True}, "ag-1")
        self.assertIn("NOT to be asked questions", decide)
        self.assertIn('by = "agent", status = "decided"', decide)
        # an older template (no {questions}) still formats
        old = "{ws} {job}"
        extra = {"questions": "q", "no_questions": "no"}
        self.assertEqual(old.format(ws="w", job="j", **extra), "w j")


if __name__ == "__main__":
    unittest.main()
