"""Einstein's desk under restarts, load and deregistration: every piece of state
survives a restart mid-job and mid-hour (SQLite, WAL, one transaction per mutation); N jobs run at
once without the desk waiting on any platform call; leases are heartbeated through a slow grant; a
deregistered hotkey loses its key and its job goes to another miner."""

from __future__ import annotations

import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from cadmind_einstein.desk import Desk, DeskError
from cadmind_einstein.platform import PlatformError
from cadmind_einstein.scoring import HOUR_MS, Ledger
from cadmind_einstein.sealing import new_keypair
from cadmind_einstein.store import Store
from tests.test_einstein import GOOD, OWNER, A, B, C, Clock, D, FakePlatform, wait_state


class Rig:
    """A desk on a store file, restartable: ``restart()`` drops the process's memory and builds a
    new desk from the same file (the platform, which lives elsewhere, is kept)."""

    def __init__(self, d: str, platform: FakePlatform | None = None) -> None:
        self.path = Path(d) / "einstein.sqlite3"
        self.reports = Path(d) / "reports"
        self.p = platform or FakePlatform()
        self.clock = Clock()
        self.reg = [A, B, C]
        self.store: Store | None = None
        self.desk: Desk | None = None
        self.boot()

    def boot(self) -> Desk:
        self.store = Store(self.path)
        ledger = Ledger(report_dir=self.reports, store=self.store)
        self.desk = Desk(
            self.p,
            ledger,
            registered=lambda: self.reg,
            clock=self.clock,
            rng=random.Random(3),  # noqa: S311 -- a test's seed
            store=self.store,
            excluded=[OWNER],
        )
        return self.desk

    def restart(self) -> Desk:
        assert self.desk is not None and self.store is not None
        self.desk.wait_idle()
        self.desk.stop()
        self.store.close()
        desk = self.boot()
        desk.start()
        return desk

    def tick(self) -> None:
        assert self.desk is not None
        self.desk.tick()
        assert self.desk.wait_idle()

    def admit(self, *hks: str) -> None:
        assert self.desk is not None
        for hk in hks:
            self.desk.agent_key(hk, new_keypair()[1])
            with self.desk._mut():
                self.desk.miners[hk].admitted = True

    def poll(self, *hks: str) -> dict[str, dict[str, Any]]:
        assert self.desk is not None
        return {hk: self.desk.work(hk) for hk in hks}

    def holder(self, job: str) -> tuple[str, dict[str, Any]]:
        for hk, w in self.poll(*self.reg).items():
            if w["job"] == job:
                return hk, w
        raise AssertionError(f"nobody holds {job}")


class TestRestart(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Rig(self.tmp.name)
        self.r.admit(A, B, C)
        self.r.poll(A, B, C)

    def tearDown(self) -> None:
        if self.r.desk is not None:
            self.r.desk.stop()
        self.tmp.cleanup()

    def test_a_restart_mid_job_keeps_the_attempt_and_the_miner_finishes_it(self) -> None:
        self.r.p.queue.append({"job": "m1", "brief": "A wall hook."})
        self.r.tick()
        hk, w = self.r.holder("m1")
        assert self.r.desk is not None
        self.r.desk.accept(hk, "m1")
        deadline = self.r.desk.status("m1")["attempts"][0]["deadline_ms"]
        desk = self.r.restart()
        st = desk.status("m1")
        self.assertEqual((st["state"], st["attempts"][0]["deadline_ms"]), ("working", deadline))
        self.assertEqual(desk.work(hk)["job"], "m1")  # the miner still holds it
        self.assertEqual(set(desk.admitted()), {A, B, C})
        self.r.p.work(w["workspace"], GOOD, deliverable=True)
        desk.submit(hk, "m1", w["workspace"], "d")
        st = wait_state(desk, "m1", ("done",))
        self.assertEqual(st["result"]["label"], "delivered")
        self.r.tick()
        self.assertEqual(self.r.p.completed["m1"]["label"], "delivered")

    def test_a_restart_while_judging_judges_again_and_a_cut_off_grant_is_requeued(self) -> None:
        self.r.p.queue.append({"job": "m2", "brief": "A wall hook."})
        self.r.tick()
        hk, w = self.r.holder("m2")
        assert self.r.desk is not None
        self.r.desk.accept(hk, "m2")
        self.r.p.work(w["workspace"], GOOD, deliverable=True)
        # the process dies right after the submit is recorded, before the gate is read
        with self.r.desk._mut():
            job = self.r.desk.jobs["m2"]
            job.attempts[-1].submitted_ms = self.r.desk.now_ms()
            job.state = "judging"
            # and a second job whose grant was in flight
            self.r.desk.create({"job": "m3", "brief": "A shelf."})
            self.r.desk.jobs["m3"].state = "assigning"
        desk = self.r.restart()
        st = wait_state(desk, "m2", ("done",))
        self.assertEqual(st["result"]["label"], "delivered")
        self.assertEqual(desk.status("m3")["state"], "queued")
        self.r.poll(A, B, C)
        self.r.tick()
        self.assertEqual(desk.status("m3")["state"], "assigned")

    def test_a_restart_during_the_gates_guard_run_judges_again_on_the_same_fork(self) -> None:
        self.r.p.queue.append({"job": "m5", "brief": "A wall hook."})
        self.r.tick()
        hk, w = self.r.holder("m5")
        assert self.r.desk is not None
        self.r.desk.accept(hk, "m5")
        self.r.p.work(w["workspace"], GOOD, deliverable=True)
        real = self.r.p.run

        def cut_off(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            if command == "test":
                return {"state": "running", "run": "run-cut"}  # the validator dies waiting
            return real(ws, command, args, **kw)

        self.r.p.run = cut_off  # type: ignore[method-assign]
        self.r.desk.submit(hk, "m5", w["workspace"], "d")
        self.assertTrue(self.r.desk.wait_idle())
        fork = self.r.desk.jobs["m5"].attempts[-1].fork
        self.assertTrue(fork)
        self.r.p.run = real  # type: ignore[method-assign]
        desk = self.r.restart()
        self.assertEqual(desk.jobs["m5"].attempts[-1].fork, fork)  # persisted
        st = wait_state(desk, "m5", ("done",))
        self.assertEqual((st["result"]["label"], st["result"]["workspace"]), ("delivered", fork))
        self.assertEqual(self.r.p.forks, 1)

    def test_held_failures_telemetry_and_signals_survive(self) -> None:
        self.r.p.queue.append({"job": "m4", "brief": "A wall hook."})
        self.r.tick()
        rejecter, _ = self.r.holder("m4")
        assert self.r.desk is not None
        self.r.desk.reject(rejecter, "m4", "not-capable", "too hard")
        desk = self.r.restart()
        self.assertEqual(len(desk.jobs["m4"].pending), 1)  # held, not yet scored
        self.assertEqual(desk.telemetry()[rejecter]["rejected"], 1)
        self.r.poll(A, B, C)
        self.r.tick()
        hk, w = self.r.holder("m4")
        self.assertNotEqual(hk, rejecter)
        desk.accept(hk, "m4")
        self.r.p.work(w["workspace"], GOOD, deliverable=True)
        desk.submit(hk, "m4", w["workspace"], "d")
        wait_state(desk, "m4", ("done",))
        desk.signal("m4", {"kind": "stars", "stars": 5, "trust": "paid"})
        desk = self.r.restart()
        scored = {o.hotkey: o for o in desk.ledger.outcomes}
        self.assertEqual(scored[rejecter].s, 0.0)  # overturned
        self.assertEqual(scored[hk].signals[0]["stars"], 5)
        self.assertEqual(desk.telemetry()[rejecter]["overturned"], 1)

    def test_a_result_the_platform_did_not_take_is_sent_after_a_restart(self) -> None:
        down = {"on": True}
        complete = self.r.p.complete

        def flaky(job: str, result: dict[str, Any]) -> None:
            if down["on"]:
                raise PlatformError("platform unreachable")
            complete(job, result)

        self.r.p.complete = flaky  # type: ignore[method-assign]
        self.r.p.queue.append({"job": "m5", "brief": "A wall hook."})
        self.r.tick()
        hk, w = self.r.holder("m5")
        assert self.r.desk is not None
        self.r.desk.accept(hk, "m5")
        self.r.p.work(w["workspace"], GOOD, deliverable=True)
        self.r.desk.submit(hk, "m5", w["workspace"], "d")
        wait_state(self.r.desk, "m5", ("done",))
        self.r.tick()
        self.assertNotIn("m5", self.r.p.completed)
        down["on"] = False
        self.r.restart()
        self.r.tick()
        self.assertEqual(self.r.p.completed["m5"]["label"], "delivered")

    def test_the_copy_window_survives(self) -> None:
        for job in ("c1", "c2"):
            self.r.p.queue.append({"job": job, "brief": "A wall hook."})
            self.r.poll(A, B, C)
            self.r.tick()
        h1, w1 = self.r.holder("c1")
        assert self.r.desk is not None
        self.r.desk.accept(h1, "c1")
        self.r.p.work(w1["workspace"], GOOD, deliverable=True)
        self.r.desk.submit(h1, "c1", w1["workspace"], "d")
        wait_state(self.r.desk, "c1", ("done",))
        desk = self.r.restart()
        h2, w2 = self.r.holder("c2")
        self.assertNotEqual(h1, h2)
        desk.accept(h2, "c2")
        self.r.p.work(w2["workspace"], GOOD, deliverable=True)
        desk.submit(h2, "c2", w2["workspace"], "d")
        wait_state(desk, "c2", ("queued", "done"))
        self.assertEqual(desk.status("c2")["attempts"][0]["kind"], "copy")

    def test_a_restart_mid_hour_reports_what_an_uninterrupted_run_would(self) -> None:
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            uids = {OWNER: 0, A: 1, B: 2}

            def run(d: str, *, restart: bool) -> dict[str, Any]:
                store = Store(Path(d) / "s.sqlite3")
                led = Ledger(store=store)
                from tests.test_einstein import passing

                led.add(passing("1", A, ended_ms=HOUR_MS + 10))
                led.signal(
                    "1", {"kind": "stars", "stars": 2, "trust": "paid", "at_ms": HOUR_MS + 20}
                )
                if restart:
                    store.close()
                    store = Store(Path(d) / "s.sqlite3")
                    led = Ledger(store=store)
                led.add(passing("2", B, ended_ms=HOUR_MS + 30))
                rep = led.report(
                    2 * HOUR_MS, uids, owner_hotkey=OWNER, admitted=[A, B], reachable=[A, B]
                )
                store.close()
                return rep

            self.assertEqual(run(d1, restart=False), run(d2, restart=True))

    def test_a_failed_mutation_leaves_nothing_behind(self) -> None:
        assert self.r.store is not None
        with self.assertRaises(RuntimeError), self.r.store.tx():
            self.r.store.put("meta", "x", 1)
            raise RuntimeError("crash inside the transaction")
        self.assertIsNone(self.r.store.get("meta", "x"))


class SlowPlatform(FakePlatform):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.lease_calls: list[float] = []
        self.peak = 0
        self._now = 0
        self._mx = threading.Lock()

    def grant(self, hotkey: str, **kw: Any) -> str:
        with self._mx:
            self._now += 1
            self.peak = max(self.peak, self._now)
        time.sleep(self.delay)
        with self._mx:
            self._now -= 1
            return super().grant(hotkey, **kw)

    def lease(self, jobs: list[str]) -> dict[str, Any]:
        self.lease_calls.append(time.monotonic())
        return super().lease(jobs)


class TestConcurrency(unittest.TestCase):
    def test_n_jobs_at_once_without_blocking_and_the_lease_beats_through_a_slow_grant(self) -> None:
        p = SlowPlatform(delay=0.6)
        reg = [f"hk{i}" for i in range(6)]
        desk = Desk(p, Ledger(), registered=lambda: reg, max_inflight=6, lease_every_s=0.1)
        try:
            for hk in reg:
                desk.agent_key(hk, new_keypair()[1])
                desk.miners[hk].admitted = True
                desk.work(hk)
            for i in range(6):
                p.queue.append({"job": f"n{i}", "brief": "A wall hook."})
            desk.start()
            t0 = time.monotonic()
            desk.tick()
            self.assertLess(time.monotonic() - t0, 0.3)  # the grants run in the pool
            self.assertTrue(desk.wait_idle(5))
            self.assertLess(time.monotonic() - t0, 2.0)  # six 0.6 s grants, side by side
            self.assertEqual(p.peak, 6)
            held = {w["job"] for w in (desk.work(hk) for hk in reg) if w["job"]}
            self.assertEqual(held, {f"n{i}" for i in range(6)})  # one job each
            self.assertTrue(any(t0 < t < t0 + 0.6 for t in p.lease_calls))  # beat while granting
        finally:
            desk.stop()


class TestDeregistration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Rig(self.tmp.name)
        self.r.admit(A, B, C)
        self.r.poll(A, B, C)

    def tearDown(self) -> None:
        if self.r.desk is not None:
            self.r.desk.stop()
        self.tmp.cleanup()

    def test_a_deregistered_miner_loses_its_key_and_its_job_goes_to_another(self) -> None:
        self.r.p.queue.append({"job": "g1", "brief": "A wall hook."})
        self.r.tick()
        gone, _ = self.r.holder("g1")
        desk = self.r.desk
        assert desk is not None
        self.r.reg = [h for h in self.r.reg if h != gone]
        self.assertEqual(desk.sync_registered(self.r.reg), [gone])
        desk.wait_idle()
        self.assertNotIn(gone, self.r.p.keys)  # revoked on the platform
        self.assertFalse(desk.miners[gone].admitted)
        self.assertEqual(desk.status("g1")["attempts"][0]["kind"], "dereg")
        self.r.poll(*self.r.reg)
        self.r.tick()
        hk, _ = self.r.holder("g1")
        self.assertNotEqual(hk, gone)
        self.assertEqual(desk.ledger.outcomes, [])  # nobody scored for leaving
        with self.assertRaises(DeskError):
            desk.work(gone)
        with self.assertRaises(DeskError):
            desk.agent_key(gone, new_keypair()[1])

    def test_a_revocation_the_platform_refused_is_retried_and_survives_a_restart(self) -> None:
        disable = self.r.p.disable_key
        down = {"on": True}

        def flaky(hk: str) -> dict[str, Any]:
            if down["on"]:
                raise PlatformError("platform unreachable")
            return disable(hk)

        self.r.p.disable_key = flaky  # type: ignore[method-assign,assignment]
        self.r.reg = [A, B]
        assert self.r.desk is not None
        self.r.desk.sync_registered(self.r.reg)
        self.r.desk.wait_idle()
        self.assertIn(C, self.r.p.keys)
        desk = self.r.restart()
        down["on"] = False
        self.r.tick()
        self.assertNotIn(C, self.r.p.keys)
        self.assertEqual(desk._revoke, [])

    def test_only_registered_hotkeys_and_never_the_owner(self) -> None:
        assert self.r.desk is not None
        with self.assertRaises(DeskError):
            self.r.desk.agent_key(D, new_keypair()[1])  # not registered
        self.r.reg.append(OWNER)
        with self.assertRaises(DeskError):
            self.r.desk.agent_key(OWNER, new_keypair()[1])  # the owner never mines


if __name__ == "__main__":
    unittest.main()
