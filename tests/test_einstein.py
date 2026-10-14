"""Einstein (one miner per job): the sealed agent key, the per-job score and its
gate, the hourly EMA with no owner weight, the recompute auditor, and the desk -- routing,
acceptance, the Watt gate, reassignment from scratch up to the cap, "didn't finish" not charged,
copies -- against an in-memory platform."""

from __future__ import annotations

import base64
import io
import json
import random
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import bittensor.http_auth as btauth
from bittensor.sp_core import Keypair

from cadmind_einstein.desk import GATE_RUN_S, Desk, DeskError
from cadmind_einstein.platform import PlatformError
from cadmind_einstein.scoring import (
    HOUR_MS,
    JobOutcome,
    Ledger,
    recompute,
    score_all,
    standing,
    vector,
)
from cadmind_einstein.sealing import new_keypair, open_sealed, seal
from cadmind_einstein.service import serve

VALIDATOR = Keypair.create_from_uri("//Validator")
A, B, C, D = (
    Keypair.create_from_uri(u).ss58_address for u in ("//Alice", "//Bob", "//Charlie", "//Dave")
)
OWNER = Keypair.create_from_uri("//Owner").ss58_address


def project_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel, text in files.items():
            z.writestr(f"ws/{rel}", text)
    return buf.getvalue()


GOOD = {
    "brief/brief.md": "A wall hook.",
    "spec/sources.toml": "[source.pla]\nurl = 'x'\n",
    "spec/requirements.toml": "[requirement.holds]\nstatement = 'holds a coat'\n",
    "tests/test_hook.py": "def test_holds():\n    pass\n",
    "src/parts/hook.py": "\n".join(f"line {i} of the hook" for i in range(40)),
}


class FakePlatform:
    """The operator routes and the validator's own workspaces, in memory. A miner 'works' by setting
    its
    workspace's files and verdict."""

    def __init__(self) -> None:
        self.ws: dict[str, dict[str, Any]] = {}
        self.grants: list[dict[str, Any]] = []
        self.keys: dict[str, str] = {}
        self.n = 0
        self.queue: list[dict[str, Any]] = []  # the platform's single queue (einstein's jobs)
        self.leased: list[str] = []
        self.lost: set[str] = set()
        self.completed: dict[str, dict[str, Any]] = {}
        self.suspended: set[str] = set()  # miners Watt's telemetry suspended (platform side)
        self.runs: list[tuple[str, str]] = []  # (workspace, command) of every watt run
        self.forks = 0

    def claim(self) -> dict[str, Any] | None:
        if not self.queue:
            return None
        job = self.queue.pop(0)
        self.leased.append(str(job["job"]))
        return job

    def lease(self, jobs: list[str]) -> dict[str, Any]:
        return {
            "renewed": [j for j in jobs if j not in self.lost],
            "lost": [j for j in jobs if j in self.lost],
        }

    def claimed(self) -> list[dict[str, Any]]:
        return [{"job": j, "brief": "A wall hook."} for j in self.leased if j not in self.completed]

    def complete(self, job: str, result: dict[str, Any]) -> None:
        self.completed[job] = result

    def _new(self, **kw: Any) -> str:
        self.n += 1
        name = f"ag-{self.n:020d}"
        self.ws[name] = {"files": {}, "deliverable": False, "frozen": False, "compute_ms": 0, **kw}
        return name

    def mint_key(self, hotkey: str) -> dict[str, Any]:
        if hotkey in self.suspended:
            raise PlatformError("suspended", 403, "agent_suspended")
        self.keys[hotkey] = f"cma_secret_for_{hotkey[:6]}"
        return {"key_id": f"k-{hotkey[:6]}", "secret": self.keys[hotkey]}

    def disable_key(self, hotkey: str) -> dict[str, Any]:
        self.keys.pop(hotkey, None)
        return {"deleted": []}

    def grant(self, hotkey: str, *, brief: str = "", seed: str = "", name: str = "job") -> str:
        if hotkey in self.suspended:
            raise PlatformError("suspended", 403, "agent_suspended")
        self.grants.append({"hotkey": hotkey, "brief": brief, "seed": seed})
        files = dict(self.ws[seed]["files"]) if seed else {"brief/brief.md": brief}
        return self._new(hotkey=hotkey, files=files)

    def freeze(self, ws: str) -> dict[str, Any]:
        self.ws[ws]["frozen"] = True
        return {"compute_ms": self.ws[ws]["compute_ms"]}

    def fork(self, ws: str) -> str:
        src = self.ws[ws]
        self.forks += 1
        return self._new(
            files=dict(src["files"]),
            deliverable=src["deliverable"],
            weak=src.get("weak", False),
            draw=src.get("draw", ""),
            hotkey="validator",
        )

    def run(self, ws: str, command: str, args: Any = None, **_: Any) -> dict[str, Any]:
        """``status``, and ``test --mutate`` as Watt's sampled guard on the operator's lane: a
        weak design (one only a lucky draw passes) is refused by the validator's draw, which then
        leaves the miner's record in place; a sound one is recorded with the validator's draw."""
        w = self.ws[ws]
        self.runs.append((ws, command))
        digest = str(abs(hash(json.dumps(w["files"], sort_keys=True))))
        if command == "test" and (args or {}).get("mutate"):
            if w.get("weak"):
                guard = {
                    "digest": digest,
                    "sampled": {"seed": "sha256:validator"},
                    "survivors": [{"label": "drop hook", "requirements": ["holds"]}],
                }
                return {"state": "done", "ok": False, "data": {"guard": guard}}
            w.update(deliverable=True, draw="sha256:validator")
            guard = {"digest": digest, "sampled": {"seed": "sha256:validator"}, "survivors": []}
            return {"state": "done", "ok": True, "data": {"guard": guard}}
        stage = "deliverable" if w["deliverable"] else "check"
        draw = {"sampled": [3, 9], "seed": w.get("draw") or "sha256:miner"}
        return {
            "state": "done",
            "ok": True,
            "data": {
                "status": {
                    "deliverable": w["deliverable"],
                    "stage": stage,
                    "digest": digest,
                    "guard_draw": draw if w["deliverable"] else None,
                }
            },
        }

    def export(self, ws: str) -> bytes:
        return project_zip(self.ws[ws]["files"])

    def delete(self, ws: str) -> None:
        self.ws.pop(ws, None)

    # the miner's side, for the tests
    def work(
        self,
        ws: str,
        files: dict[str, str],
        *,
        deliverable: bool,
        compute_ms: int = 60_000,
        weak: bool = False,
    ) -> None:
        """``deliverable``: the miner's own guard record says so; ``weak``: only because its draw
        was lucky (a re-rolled draw), so the validator's own draw refuses it."""
        assert not self.ws[ws]["frozen"]
        self.ws[ws].update(
            files={**self.ws[ws]["files"], **files},
            deliverable=deliverable,
            compute_ms=compute_ms,
            weak=weak,
            draw="sha256:miner",
        )


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def wait_state(
    desk: Desk, job: str, states: tuple[str, ...], timeout: float = 5.0
) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = desk.status(job)
        if st["state"] in states:
            return st
        time.sleep(0.02)
    raise AssertionError(f"{job} stayed {desk.status(job)['state']}")


class TestSealing(unittest.TestCase):
    def test_only_the_miner_opens_its_key(self) -> None:
        priv, pub = new_keypair()
        sealed = seal("cma_abc", pub, A)
        self.assertNotIn("cma_abc", json.dumps(sealed))
        self.assertEqual(open_sealed(sealed, priv, A), "cma_abc")
        with self.assertRaises(ValueError):
            open_sealed(sealed, priv, B)  # bound to the hotkey
        other, _ = new_keypair()
        with self.assertRaises(ValueError):
            open_sealed(sealed, other, A)


def passing(job: str, hk: str, *, ended_ms: int = 0, **kw: Any) -> JobOutcome:
    facts: dict[str, Any] = {"compute_s": 600, "wall_s": 1800, **kw}
    return JobOutcome(job=job, hotkey=hk, ended_ms=ended_ms, deliverable=True, kind="pass", **facts)


class TestScoring(unittest.TestCase):
    def test_an_unrated_job_counts_on_its_immediate_part_and_signals_move_it(self) -> None:
        plain = score_all([passing("a", A)])[0]
        self.assertAlmostEqual(
            plain.s, 1.0
        )  # gate passed, efficient (no history), on time; no signal
        late = score_all([passing("b", A, on_time=False)])[0]
        self.assertAlmostEqual(late.s, 0.8)
        failed = score_all([JobOutcome(job="c", hotkey=A, ended_ms=0, deliverable=False)])[0]
        self.assertEqual(failed.s, 0.0)

        def with_signals(*sig: dict[str, Any]) -> float:
            return score_all([passing("d", A, signals=list(sig))])[0].s

        self.assertAlmostEqual(with_signals({"kind": "stars", "stars": 1, "trust": "paid"}), 0.4)
        self.assertAlmostEqual(with_signals({"kind": "stars", "stars": 5, "trust": "paid"}), 1.0)
        free_one_star = with_signals({"kind": "stars", "stars": 1, "trust": "free"})
        self.assertGreater(free_one_star, 0.9)  # a free account barely moves it
        self.assertAlmostEqual(
            with_signals({"kind": "download", "trust": "paid"}),
            0.4 + 0.6 * (0.5 * 0.75 + 0.5 * 1.0),
        )
        rerun = with_signals(
            {"kind": "download", "trust": "paid"}, {"kind": "rerun", "trust": "paid"}
        )
        self.assertLess(rerun, with_signals({"kind": "download", "trust": "paid"}))

    def test_efficiency_is_against_similar_jobs(self) -> None:
        jobs = [passing(f"m{i}", B) for i in range(6)]
        slow = passing("slow", A, compute_s=1200, wall_s=3600)
        score_all([*jobs, slow])
        self.assertAlmostEqual(
            slow.parts["E"], 0.5 * (600 / 1200) + 0.5 * (1800 / 3600) + 0.0, places=3
        )
        self.assertLess(slow.s, jobs[0].s)

    def test_weights_from_history_genesis_and_never_the_owner(self) -> None:
        uids = {OWNER: 0, A: 1, B: 2, C: 3}
        now = 10 * HOUR_MS
        jobs = score_all(
            [
                passing("1", A, ended_ms=now - HOUR_MS),
                JobOutcome(job="2", hotkey=B, ended_ms=now, deliverable=False),
            ]
        )
        weight, _quality = standing(jobs, now)
        self.assertGreater(weight[A], 0)
        self.assertEqual(weight[B], 0.0)
        vec, rule = vector(
            weight, uids, owner_hotkey=OWNER, admitted=[A, B, C, OWNER], reachable=[A, B, C]
        )
        self.assertEqual((vec, rule), ([(1, 65_535)], "history"))
        # an hour later, no new work: still emitted from history
        weight2, _ = standing(jobs, now + 5 * HOUR_MS)
        self.assertEqual(
            vector(weight2, uids, owner_hotkey=OWNER, admitted=[A, B], reachable=[A, B])[0],
            [(1, 65_535)],
        )
        # genesis: no history at all -- equal among admitted, reachable miners; never the owner
        vec, rule = vector(
            {}, uids, owner_hotkey=OWNER, admitted=[A, B, OWNER], reachable=[A, B, OWNER]
        )
        self.assertEqual(vec, [(1, 65_535), (2, 65_535)])
        self.assertTrue(rule.startswith("genesis"))
        self.assertEqual(vector({}, uids, owner_hotkey=OWNER, admitted=[], reachable=[A])[0], [])

    def test_reports_recompute_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(report_dir=Path(d))
            ledger.add(passing("1", A, ended_ms=1000))
            ledger.add(JobOutcome(job="2", hotkey=B, ended_ms=2000, deliverable=False))
            self.assertIsNotNone(
                ledger.signal("1", {"kind": "stars", "stars": 4, "trust": "paid", "at_ms": 3000})
            )
            self.assertIsNone(
                ledger.signal("2", {"kind": "stars", "stars": 5, "trust": "paid"})
            )  # not delivered
            rep = ledger.report(
                4000, {OWNER: 0, A: 1, B: 2}, owner_hotkey=OWNER, admitted=[A, B], reachable=[A, B]
            )
            audit = recompute(json.loads(next(Path(d).glob("hour-*.json")).read_text()))
            self.assertTrue(audit["matches_published"])
            self.assertEqual(audit["mismatches"], [])
            self.assertEqual(rep["vector"], [[1, 65_535]])
            again = Ledger(report_dir=Path(d))
            self.assertEqual(len(again.outcomes), 2)
            self.assertEqual(again.outcomes[0].signals[0]["stars"], 4)


class TestDesk(unittest.TestCase):
    def setUp(self) -> None:
        self.p = FakePlatform()
        self.clock = Clock()
        self.ledger = Ledger(hour_ms=3_600_000)
        self.reg = [A, B, C]
        self.desk = Desk(
            self.p,
            self.ledger,
            registered=lambda: self.reg,
            clock=self.clock,
            rng=random.Random(7),  # noqa: S311 -- a test's seed,
        )
        for hk in (A, B, C):
            self.desk.agent_key(hk, new_keypair()[1])
            self.desk.miners[hk].admitted = True  # the entry test has its own test

    def tearDown(self) -> None:
        self.desk.stop()

    def tick(self) -> None:
        """One pass of the desk, and its worker tasks (grants, judgements, results) finished."""
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def enqueue(self, body: dict[str, Any]) -> None:
        """A client job enters the platform's queue; the validator claims it on its next tick."""
        self.p.queue.append(dict(body))
        self.tick()

    def poll(self, *hks: str) -> dict[str, dict[str, Any]]:
        return {hk: self.desk.work(hk) for hk in hks}

    def assigned(self) -> tuple[str, dict[str, Any]]:
        for hk, w in self.poll(A, B, C).items():
            if w["job"]:
                return hk, w
        raise AssertionError("nobody has the job")

    def test_one_miner_gets_the_job_and_a_pass_is_delivered_and_charged(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j1", "brief": "A wall hook for my coat."})
        hk, w = self.assigned()
        self.assertEqual(
            sum(1 for x in self.poll(A, B, C).values() if x["job"]), 1
        )  # one miner per job
        self.desk.accept(hk, "j1")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, "j1", w["workspace"], "d")
        st = wait_state(self.desk, "j1", ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("delivered", True))
        self.assertTrue(self.p.ws[w["workspace"]]["frozen"])
        self.assertAlmostEqual(self.ledger.outcomes[-1].s, 1.0)  # unrated: its immediate part
        self.tick()
        self.assertEqual(self.p.completed["j1"]["label"], "delivered")  # back to the platform

    def attempt(self, job: str, *, deliverable: bool, files: dict[str, str] | None = None) -> str:
        hk, w = self.assigned()
        self.desk.accept(hk, job)
        self.p.work(
            w["workspace"], files or {"notes.md": f"tried by {hk[:6]}"}, deliverable=deliverable
        )
        self.desk.submit(hk, job, w["workspace"], "d")
        wait_state(self.desk, job, ("queued", "done"))
        self.poll(A, B, C)
        self.tick()
        return hk

    def reject(self, job: str) -> str:
        hk, _ = self.assigned()
        self.desk.reject(hk, job, "not-allowed", f"{hk[:6]}: this is a weapon part")
        self.poll(A, B, C)
        self.tick()
        return hk

    def scored(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for o in self.ledger.outcomes:
            out.setdefault(o.hotkey, []).append(o.s)
        return out

    def test_a_failed_gate_goes_to_another_miner_from_scratch_and_counts_if_it_passes(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j2", "brief": "A wall hook."})
        first = self.attempt("j2", deliverable=False, files={"src/parts/broken.py": "x"})
        self.assertEqual(self.ledger.outcomes, [])  # held: nobody has passed this job yet
        second, w2 = self.assigned()
        self.assertNotEqual(second, first)
        self.assertEqual(self.p.grants[-1]["seed"], "")  # from the brief, not the broken project
        self.assertNotIn("src/parts/broken.py", self.p.ws[w2["workspace"]]["files"])
        self.desk.accept(second, "j2")
        self.p.work(w2["workspace"], GOOD, deliverable=True)
        self.desk.submit(second, "j2", w2["workspace"], "d")
        st = wait_state(self.desk, "j2", ("done",))
        self.assertEqual(st["result"]["label"], "delivered")
        scores = self.scored()
        self.assertEqual(scores[first], [0.0])  # another miner passed: the failure counts
        self.assertGreater(scores[second][0], 0.5)

    def test_two_gate_failures_are_out_of_reach_not_charged_and_penalise_nobody(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j3", "brief": "A perpetual motion machine."})
        self.attempt("j3", deliverable=False)
        self.attempt("j3", deliverable=False)
        st = self.desk.status("j3")
        self.assertEqual(
            (st["state"], st["result"]["label"], st["result"]["charge"]),
            ("done", "out of reach", False),
        )
        self.assertEqual(self.ledger.outcomes, [])

    def test_two_rejections_decline_the_job_with_the_messages_and_penalise_nobody(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j7", "brief": "A suppressor for my rifle."})
        r1 = self.reject("j7")
        r2 = self.reject("j7")
        self.assertNotEqual(r1, r2)
        st = self.desk.status("j7")
        res = st["result"]
        self.assertEqual((res["label"], res["charge"], res["review"]), ("declined", False, True))
        self.assertEqual(len(res["messages"]), 2)
        self.assertEqual(res["messages"][0]["category"], "not-allowed")
        self.assertEqual(self.ledger.outcomes, [])
        self.assertEqual(self.desk.telemetry()[r1]["rejected"], 1)

    def test_a_rejection_overturned_by_a_pass_counts_against_the_rejecter(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j8", "brief": "A wall hook."})
        rejecter = self.reject("j8")
        hk, w = self.assigned()
        self.desk.accept(hk, "j8")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, "j8", w["workspace"], "d")
        wait_state(self.desk, "j8", ("done",))
        self.assertEqual(self.scored()[rejecter], [0.0])
        self.assertEqual(self.desk.telemetry()[rejecter]["overturned"], 1)
        with self.assertRaises(DeskError):
            self.desk.reject(hk, "j8", "maybe", "x")  # a closed category

    def test_an_unaccepted_job_times_out_into_the_same_path(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "j4", "brief": "A wall hook."})
        first, _ = self.assigned()
        self.clock.t += 200  # past the 120 s acceptance window
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.ledger.outcomes[-1].hotkey, first)
        self.assertEqual(self.ledger.outcomes[-1].s, 0.0)
        second, _ = self.assigned()
        self.assertNotEqual(second, first)

    def test_four_timeouts_end_the_job_didnt_finish_and_each_counts(self) -> None:
        self.reg.append(D)
        self.desk.agent_key(D, new_keypair()[1])
        self.desk.miners[D].admitted = True
        for _ in range(4):
            self.poll(A, B, C, D)
            if not self.desk.jobs:
                self.enqueue({"job": "j9", "brief": "A wall hook."})
            self.tick()
            self.clock.t += 200
        self.poll(A, B, C, D)
        self.tick()
        st = self.desk.status("j9")
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("didn't finish", False))
        self.assertEqual(
            [o.s for o in self.ledger.outcomes], [0.0] * 4
        )  # reliability is the miner's

    def test_a_copy_of_another_miners_output_fails(self) -> None:
        self.poll(A, B, C)
        for job in ("j5", "j6"):
            self.enqueue({"job": job, "brief": "A wall hook."})
            self.poll(A, B, C)
            self.tick()
        jobs = {w["job"]: (hk, w) for hk, w in self.poll(A, B, C).items() if w["job"]}
        (h5, w5), (h6, w6) = jobs["j5"], jobs["j6"]
        self.assertNotEqual(h5, h6)
        for hk, w, job in ((h5, w5, "j5"), (h6, w6, "j6")):
            self.desk.accept(hk, job)
            self.p.work(w["workspace"], GOOD, deliverable=True)
            self.desk.submit(hk, job, w["workspace"], "d")
            wait_state(self.desk, job, ("done", "queued"))
        states = sorted(self.desk.status(j)["state"] for j in ("j5", "j6"))
        self.assertEqual(states, ["done", "queued"])  # the second identical output is a copy

    def test_private_jobs_never_reach_miners(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "p1", "brief": "secret", "privacy": "private"})
        self.assertEqual(
            (self.p.completed["p1"]["label"], self.p.completed["p1"]["charge"]), ("refused", False)
        )
        self.assertTrue(all(w["job"] is None for w in self.poll(A, B, C).values()))

    def test_the_router_prefers_the_better_miner_and_still_explores(self) -> None:
        for hk, good in ((A, True), (B, False), (C, False)):
            for i in range(6):
                self.ledger.add(
                    JobOutcome(
                        job=f"{hk}{i}", hotkey=hk, ended_ms=self.desk.now_ms(), deliverable=good
                    )
                )
        picks = {A: 0, B: 0, C: 0}
        from cadmind_einstein.desk import Job

        for _ in range(400):
            self.poll(A, B, C)
            m = self.desk.route(Job(job="x", brief="b"))
            assert m is not None
            picks[m.hotkey] += 1
        self.assertGreater(picks[A], picks[B] + picks[C])
        self.assertGreater(picks[B] + picks[C], 20)

    def test_only_admitted_miners_get_client_jobs_and_the_entry_test_admits(self) -> None:
        for hk in (A, B, C):
            self.desk.miners[hk].admitted = False
        self.poll(A, B, C)
        self.enqueue({"job": "c1", "brief": "A wall hook."})
        self.assertEqual([j["job"] for j in self.p.queue], ["c1"])  # nobody admitted: not claimed
        adm = self.desk.admission(A)
        self.assertTrue(self.desk.wait_idle())
        w = self.desk.work(A)
        self.assertEqual(w["job"], adm["job"])  # assigned to A alone
        self.assertIsNone(self.desk.work(B)["job"])
        self.desk.accept(A, w["job"])
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(A, w["job"], w["workspace"], "d")
        st = wait_state(self.desk, w["job"], ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("admitted", False))
        self.assertEqual(self.desk.admitted(), [A])
        self.assertEqual(self.ledger.outcomes, [])  # the entry test is never scored
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.desk.work(A)["job"], "c1")

    def test_with_no_free_admitted_miner_nothing_is_claimed_and_jobs_stay_on_the_platform(
        self,
    ) -> None:
        for hk in (A, B, C):
            self.desk.miners[hk].admitted = False
        self.enqueue({"job": "q1", "brief": "A wall hook."})
        self.enqueue({"job": "q2", "brief": "A wall hook."})
        self.clock.t += 10 * 3600  # hours pass: never rejected, never claimed without a miner
        self.tick()
        self.assertEqual([j["job"] for j in self.p.queue], ["q1", "q2"])
        self.assertEqual(self.desk.jobs, {})
        cap = self.desk.capacity()
        self.assertEqual(
            (cap["active_miners"], cap["free_miners"], cap["median_job_s"]), (0, 0, None)
        )
        self.desk.miners[A].admitted = True
        self.poll(A, B, C)
        self.tick()  # one free admitted miner: one claim, assigned at once
        self.assertEqual([j["job"] for j in self.p.queue], ["q2"])
        self.assertEqual(self.desk.work(A)["job"], "q1")
        st = self.desk.cancel("q1")  # the client cancels a claimed job: not charged, miner freed
        self.assertEqual((st["result"]["label"], st["result"]["charge"]), ("cancelled", False))
        self.assertIsNone(self.desk.work(A)["job"])
        self.assertEqual(self.ledger.outcomes, [])

    def test_a_reassignment_with_no_free_miner_holds_the_claim_and_renews_its_lease(self) -> None:
        for hk in (B, C):
            self.desk.miners[hk].admitted = False
        self.poll(A, B, C)
        self.enqueue({"job": "h1", "brief": "A wall hook."})
        self.enqueue({"job": "h2", "brief": "A wall hook."})
        self.assertEqual([j["job"] for j in self.p.queue], ["h2"])  # one miner, one claim
        self.desk.reject(A, "h1", "not-capable", "too hard for me")
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status("h1")["state"], "queued")  # held, still ours
        self.clock.t += 60
        self.poll(A, B, C)
        self.tick()
        self.assertEqual([j["job"] for j in self.p.queue], ["h2"])  # nothing claimed while held
        self.assertIn("h1", self.p.leased)
        self.desk.miners[B].admitted = True
        self.poll(A, B, C)
        self.tick()
        self.assertEqual(self.desk.work(B)["job"], "h1")  # the held job takes the free miner first

    def test_a_lost_lease_drops_the_job_and_a_restart_reconciles(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "l1", "brief": "A wall hook."})
        hk, _ = self.assigned()
        self.p.lost.add("l1")
        self.clock.t += 60
        self.tick()
        self.assertEqual(self.desk.status("l1")["result"]["label"], "cancelled")
        self.assertIsNone(self.desk.work(hk)["job"])
        fresh = Desk(self.p, Ledger(), registered=lambda: self.reg, clock=self.clock)
        self.p.leased.append("l2")
        self.assertEqual(fresh.reconcile(), 2)  # l1 is still leased on this fake, l2 too
        self.assertEqual(fresh.status("l2")["state"], "queued")

    def test_client_signals_and_a_rerun_update_the_score(self) -> None:
        self.poll(A, B, C)
        self.enqueue({"job": "s1", "brief": "A wall hook."})
        hk, w = self.assigned()
        self.desk.accept(hk, "s1")
        self.p.work(w["workspace"], GOOD, deliverable=True)
        self.desk.submit(hk, "s1", w["workspace"], "d")
        wait_state(self.desk, "s1", ("done",))
        out = self.desk.signal("s1", {"kind": "stars", "stars": 2, "trust": "paid"})
        self.assertAlmostEqual(out["score"], 0.4 + 0.6 * 0.25)
        self.enqueue({"job": "s2", "brief": "A wall hook.", "rerun_of": "s1", "trust": "paid"})
        self.assertLess(self.ledger.outcomes[0].s, out["score"])  # a re-run is a mild negative
        self.poll(A, B, C)
        self.tick()
        nxt = [h for h, x in self.poll(A, B, C).items() if x["job"] == "s2"]
        self.assertTrue(nxt and nxt[0] != hk)  # a different miner
        with self.assertRaises(DeskError):
            self.desk.signal("s1", {"kind": "stars", "stars": 9, "trust": "paid"})

    def test_a_miner_watt_suspended_gets_no_job_and_no_new_key(self) -> None:
        self.p.suspended.add(A)  # the platform deleted A's key after Watt's telemetry suspended it
        self.poll(A, B, C)
        for n in range(3):
            self.enqueue({"job": f"s{n}", "brief": "A wall hook."})
            self.poll(A, B, C)
            self.tick()
        self.assertEqual(self.desk.miners[A].key_id, "")
        owners = {a.hotkey for j in self.desk.jobs.values() for a in j.attempts}
        self.assertNotIn(A, owners)
        self.assertLessEqual(sum(1 for g in self.p.grants if g["hotkey"] == A), 1)
        with self.assertRaises(DeskError) as e:
            self.desk.agent_key(A, new_keypair()[1])
        self.assertEqual(e.exception.status, 403)


class TestGateDraw(unittest.TestCase):
    """The gate draws its own sample of the mutation guard: a miner's record is one draw it may have
    re-rolled (any change draws anew) until a weak design passed, so the validator runs
    ``watt test --mutate`` on its fork (the operator's lane, its own secret draw) and passes only a
    record that run wrote."""

    setUp = TestDesk.setUp
    tearDown = TestDesk.tearDown
    tick = TestDesk.tick
    enqueue = TestDesk.enqueue
    poll = TestDesk.poll
    assigned = TestDesk.assigned

    def submit(self, job: str, *, weak: bool, deliverable: bool = True) -> tuple[str, str]:
        self.poll(A, B, C)
        self.enqueue({"job": job, "brief": "A wall hook."})
        hk, w = self.assigned()
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], GOOD, deliverable=deliverable, weak=weak)
        self.desk.submit(hk, job, w["workspace"], "d")
        return hk, w["workspace"]

    def gate_runs(self, ws: str) -> list[str]:
        return [c for w, c in self.p.runs if w != ws]

    def test_a_re_rolled_draw_the_validators_draw_refuses_is_not_accepted(self) -> None:
        hk, ws = self.submit("r1", weak=True)
        st = wait_state(self.desk, "r1", ("queued", "done"))
        self.assertEqual(st["state"], "queued")  # back to another miner, not delivered
        a = self.desk.jobs["r1"].attempts[0]
        self.assertEqual((a.hotkey, a.kind), (hk, "gate"))
        self.assertIn("validator's own mutation guard did not pass", a.outcome)
        self.assertIn("unguarded R.holds", a.outcome)
        self.assertNotIn("r1", self.p.completed)
        self.assertEqual(self.ledger.outcomes, [])  # held, as any gate failure
        self.assertEqual(self.gate_runs(ws), ["status", "test"])  # the miner's own record said yes
        self.assertTrue(self.p.ws[ws]["deliverable"])  # ... and the miner's workspace still does

    def test_a_sound_design_passes_on_the_validators_own_draw(self) -> None:
        hk, ws = self.submit("r2", weak=False)
        st = wait_state(self.desk, "r2", ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["hotkey"]), ("delivered", hk))
        self.assertEqual(self.gate_runs(ws), ["status", "test", "status"])
        fork = self.desk.jobs["r2"].attempts[0].fork
        self.assertEqual(st["result"]["workspace"], fork)
        self.assertEqual(self.p.ws[fork]["draw"], "sha256:validator")  # the record is ours
        self.assertEqual(self.p.ws[ws]["draw"], "sha256:miner")  # the miner's is untouched
        self.assertEqual(self.p.forks, 1)

    def test_a_record_the_validators_run_did_not_write_is_refused(self) -> None:
        real = self.p.run

        def stale(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "test":  # a run that says it passed, but the record is not its draw
                self.p.runs.append((ws, command))
                guard = {
                    "digest": real(ws, "status")["data"]["status"]["digest"],
                    "sampled": {"seed": "sha256:validator"},
                }
                return {"state": "done", "ok": True, "data": {"guard": guard}}
            return real(ws, command, args, **kw)

        self.p.run = stale  # type: ignore[method-assign]
        self.submit("r3", weak=False)
        st = wait_state(self.desk, "r3", ("queued", "done"))
        self.assertEqual(st["state"], "queued")
        self.assertEqual(self.desk.jobs["r3"].attempts[0].kind, "gate")

    def test_no_guard_run_when_the_miners_own_record_is_not_deliverable(self) -> None:
        _, ws = self.submit("r4", weak=False, deliverable=False)
        wait_state(self.desk, "r4", ("queued", "done"))
        self.assertEqual(self.gate_runs(ws), ["status"])  # bounded: nothing heavy for a no

    def test_a_guard_run_that_does_not_end_is_retried_on_the_same_fork(self) -> None:
        real, calls = self.p.run, {"test": 0}

        def slow_once(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "test":
                calls["test"] += 1
                self.assertEqual(kw.get("budget_s"), GATE_RUN_S)
                if calls["test"] == 1:
                    return {"state": "running", "run": "run-1"}
            return real(ws, command, args, **kw)

        self.p.run = slow_once  # type: ignore[method-assign]
        hk, _ = self.submit("r5", weak=False)
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual(self.desk.status("r5")["state"], "judging")  # our side: retried
        self.desk._retry_at.clear()
        self.tick()
        st = wait_state(self.desk, "r5", ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["hotkey"]), ("delivered", hk))
        self.assertEqual(self.p.forks, 1)  # the same fork, its guard outcomes reused

    def test_a_guard_run_stopped_by_its_wall_clock_limit_does_not_pass(self) -> None:
        real = self.p.run

        def wall(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "test":
                return {"state": "done", "ok": False, "stopped": "wall", "data": None}
            return real(ws, command, args, **kw)

        self.p.run = wall  # type: ignore[method-assign]
        self.submit("r6", weak=False)
        st = wait_state(self.desk, "r6", ("queued", "done"))
        self.assertEqual(st["state"], "queued")
        self.assertIn("(stopped: wall)", self.desk.jobs["r6"].attempts[0].outcome)


class TestService(unittest.TestCase):
    def test_a_signed_agent_key_request_answers_a_sealed_key(self) -> None:
        p = FakePlatform()
        desk = Desk(p, Ledger(), registered=lambda: [A])
        server = serve(
            desk,
            "127.0.0.1",
            0,
            token="t" * 32,
            validator_hotkey=VALIDATOR.ss58_address,
            verify=btauth.verify,
            close_hour=lambda: None,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        alice = Keypair.create_from_uri("//Alice")
        priv, pub = new_keypair()
        body = json.dumps({"x25519_public": base64.b64encode(pub).decode()}).encode()
        headers = {
            "Content-Type": "application/json",
            **btauth.sign(
                alice,
                method="POST",
                path="/v1/miners/agent_key",
                body=body,
                receiver_ss58=VALIDATOR.ss58_address,
            ),
        }
        req = urllib.request.Request(  # noqa: S310 -- loopback
            base + "/v1/miners/agent_key", data=body, headers=headers
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 -- loopback
            out = json.loads(r.read())
        self.assertEqual(open_sealed(out["sealed"], priv, A), p.keys[A])
        req = urllib.request.Request(base + "/v1/jobs/work", data=b"{}")  # noqa: S310 -- unsigned
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)  # noqa: S310
        self.assertEqual(e.exception.code, 401)
        server.shutdown()


if __name__ == "__main__":
    unittest.main()


def test_every_priced_effort_has_its_own_deadline() -> None:
    """`max` is priced as the highest effort, so it must not fall back to medium's hour."""
    from cadmind_einstein.desk import DEADLINE_S

    assert DEADLINE_S == {"low": 1800, "medium": 3600, "high": 7200, "max": 10800}
