"""The einstein validator's exposure: the HTTP face refuses before it reads a body, the agent key
is rate-limited per hotkey and per address, abuse disables a miner, and the hourly report carries
the observations used to calibrate the anti-abuse thresholds.

Every test runs against the service or the desk in process, loopback only."""

from __future__ import annotations

import base64
import json
import random
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from typing import Any

import bittensor.http_auth as btauth
from bittensor.sp_core import Keypair

from cadmind_einstein.chain import ChainState, FileReader, Neuron
from cadmind_einstein.desk import AbusePolicy, Desk, DeskError
from cadmind_einstein.scoring import Ledger
from cadmind_einstein.sealing import new_keypair
from cadmind_einstein.service import MAX_BODY, serve
from cadmind_einstein.similarity import CopyCheck
from tests.test_einstein import GOOD, A, B, C, Clock, FakePlatform

VALIDATOR = Keypair.create_from_uri("//Validator")
ALICE = Keypair.create_from_uri("//Alice")
TOKEN = "t" * 32


class _Svc:
    """One service on a free loopback port over an in-memory platform."""

    def __init__(self, registered: list[str] | None = None, **kw: Any) -> None:
        self.p = FakePlatform()
        self.desk = Desk(self.p, Ledger(), registered=lambda: registered or [A])
        self.server = serve(
            self.desk,
            "127.0.0.1",
            0,
            token=TOKEN,
            validator_hotkey=VALIDATOR.ss58_address,
            verify=btauth.verify,
            close_hour=lambda: None,
            **kw,
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.desk.stop()

    def raw(self, head: str, body: bytes = b"", *, eof: bool = False, wait: float = 3.0) -> bytes:
        """Send a request head (and maybe some body), keep the socket open unless ``eof``, and
        answer what the server says within ``wait`` seconds (b'' if nothing)."""
        s = socket.create_connection(("127.0.0.1", self.port))
        s.settimeout(wait)
        try:
            s.sendall(head.encode() + b"\r\n" + body)
            if eof:
                s.shutdown(socket.SHUT_WR)
            try:
                return s.recv(4096)
            except TimeoutError:
                return b""
        finally:
            s.close()

    def signed(
        self, kp: Keypair, path: str, body: dict[str, Any], *, ip: str = ""
    ) -> tuple[int, dict[str, Any]]:
        raw = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            **btauth.sign(
                kp, method="POST", path=path, body=raw, receiver_ss58=VALIDATOR.ss58_address
            ),
        }
        if ip:
            headers["X-Real-IP"] = ip
        req = urllib.request.Request(self.base + path, data=raw, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 -- loopback
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


def status(resp: bytes) -> int:
    line = resp.split(b"\r\n", 1)[0].split()
    return int(line[1]) if len(line) > 1 else 0


class TestBodyGate(unittest.TestCase):
    """The size gate, the read and the credentials are decided before the body is read."""

    def setUp(self) -> None:
        self.svc = _Svc()

    def tearDown(self) -> None:
        self.svc.close()

    def head(self, path: str, *lines: str) -> str:
        return "\r\n".join([f"POST {path} HTTP/1.1", "Host: x", *lines, ""])

    def test_a_negative_content_length_is_refused_before_any_read(self) -> None:
        # A negative Content-Length must not skip the 413 and read the socket to EOF. Here
        # the socket stays open with no EOF: an answer at all proves nothing waited on the body.
        t0 = time.monotonic()
        out = self.svc.raw(
            self.head(
                "/v1/jobs/work",
                f"X-Bittensor-Hotkey: {A}",
                "X-Bittensor-Nonce: 1",
                "X-Bittensor-Signature: 0x00",
                "Content-Length: -1",
            ),
            b'{"x":1}',
        )
        self.assertEqual(status(out), 400, out)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIn(b"Connection: close", out)

    def test_missing_malformed_duplicated_or_chunked_lengths_are_refused(self) -> None:
        sig = (
            f"X-Bittensor-Hotkey: {A}",
            "X-Bittensor-Nonce: 1",
            "X-Bittensor-Signature: 0x00",
        )
        cases = {
            411: (),
            400: ("Content-Length: 1e3",),
        }
        for code, extra in cases.items():
            out = self.svc.raw(self.head("/v1/jobs/work", *sig, *extra))
            self.assertEqual(status(out), code, (extra, out))
        out = self.svc.raw(
            self.head("/v1/jobs/work", *sig, "Content-Length: 2", "Content-Length: 3")
        )
        self.assertEqual(status(out), 400, out)
        out = self.svc.raw(self.head("/v1/jobs/work", *sig, "Transfer-Encoding: chunked"))
        self.assertEqual(status(out), 400, out)

    def test_an_oversized_body_is_refused_unread(self) -> None:
        out = self.svc.raw(
            self.head(
                "/v1/jobs/work",
                f"X-Bittensor-Hotkey: {A}",
                "X-Bittensor-Nonce: 1",
                "X-Bittensor-Signature: 0x00",
                f"Content-Length: {MAX_BODY + 1}",
            )
        )
        self.assertEqual(status(out), 413, out)

    def test_an_unsigned_request_is_refused_before_its_body(self) -> None:
        # 1 MiB declared, nothing sent: the refusal must not wait for it.
        out = self.svc.raw(self.head("/v1/jobs/work", "Content-Length: 1048576"))
        self.assertEqual(status(out), 401, out)

    def test_an_unregistered_hotkey_is_refused_before_its_body(self) -> None:
        out = self.svc.raw(
            self.head(
                "/v1/jobs/work",
                f"X-Bittensor-Hotkey: {B}",
                "X-Bittensor-Nonce: 1",
                "X-Bittensor-Signature: 0x00",
                "Content-Length: 1048576",
            )
        )
        self.assertEqual(status(out), 403, out)

    def test_a_bearer_route_without_the_token_is_refused_before_its_body(self) -> None:
        out = self.svc.raw(self.head("/v1/signals", "Content-Length: 1048576"))
        self.assertEqual(status(out), 401, out)
        out = self.svc.raw(self.head("/v1/nowhere", "Content-Length: 1048576"))
        self.assertEqual(status(out), 404, out)

    def test_a_short_body_is_refused(self) -> None:
        out = self.svc.raw(
            self.head("/v1/signals", f"Authorization: Bearer {TOKEN}", "Content-Length: 50"),
            b"{}",
            eof=True,
        )
        self.assertEqual(status(out), 400, out)

    def test_the_signed_route_still_works(self) -> None:
        _priv, pub = new_keypair()
        code, out = self.svc.signed(
            ALICE, "/v1/miners/agent_key", {"x25519_public": base64.b64encode(pub).decode()}
        )
        self.assertEqual(code, 200, out)
        self.assertEqual(self.svc.signed(ALICE, "/v1/jobs/work", {}), (200, {"job": None}))


class TestConnectionLimits(unittest.TestCase):
    """No thread is pinned forever, and connections are bounded."""

    def test_a_stalled_connection_is_dropped_after_the_read_timeout(self) -> None:
        svc = _Svc(timeout_s=0.5, deadline_s=30)
        try:
            s = socket.create_connection(("127.0.0.1", svc.port))
            s.settimeout(5)
            s.sendall(b"POST /v1/jobs/work HTTP/1.1\r\n")  # and nothing more
            t0 = time.monotonic()
            self.assertEqual(s.recv(100), b"")  # closed by the server
            self.assertLess(time.monotonic() - t0, 3.0)
            s.close()
            self.assertTrue(_eventually(lambda: svc.server.open_connections() == 0))
        finally:
            svc.close()

    def test_a_trickling_request_is_cut_at_its_deadline(self) -> None:
        svc = _Svc(timeout_s=5, deadline_s=1.0)
        try:
            s = socket.create_connection(("127.0.0.1", svc.port))
            s.settimeout(0.2)
            s.sendall(b"POST /v1/jobs/work HTTP/1.1\r\n")
            t0, closed = time.monotonic(), False
            while time.monotonic() - t0 < 5:
                try:
                    s.sendall(b"X-Pad: y\r\n")  # one header line well inside the read timeout
                    if s.recv(100) == b"":
                        closed = True
                        break
                except TimeoutError:
                    continue
                except OSError:
                    closed = True
                    break
            self.assertTrue(closed)
            self.assertLess(time.monotonic() - t0, 3.5)
            s.close()
        finally:
            svc.close()

    def test_connections_past_the_cap_are_answered_busy(self) -> None:
        svc = _Svc(max_connections=2, max_per_ip=10, timeout_s=10)
        held = []
        try:
            for _ in range(2):
                held.append(socket.create_connection(("127.0.0.1", svc.port)))
            self.assertTrue(_eventually(lambda: svc.server.open_connections() == 2))
            out = svc.raw("GET /v1/reports HTTP/1.1\r\nHost: x\r\n")
            self.assertEqual(status(out), 503, out)
            for s in held:
                s.close()
            self.assertTrue(_eventually(lambda: svc.server.open_connections() == 0))
            self.assertNotEqual(status(svc.raw("GET /v1/nowhere HTTP/1.1\r\nHost: x\r\n")), 503)
        finally:
            for s in held:
                s.close()
            svc.close()

    def test_connections_per_address_are_capped_but_not_a_trusted_proxy(self) -> None:
        svc = _Svc(max_connections=10, max_per_ip=1, timeout_s=10)
        held = socket.create_connection(("127.0.0.1", svc.port))
        try:
            self.assertTrue(_eventually(lambda: svc.server.open_connections() == 1))
            self.assertEqual(status(svc.raw("GET /v1/x HTTP/1.1\r\nHost: x\r\n")), 503)
        finally:
            held.close()
            svc.close()
        svc = _Svc(max_connections=10, max_per_ip=1, timeout_s=10, trusted_proxies=["127.0.0.1"])
        held = socket.create_connection(("127.0.0.1", svc.port))
        try:
            self.assertTrue(_eventually(lambda: svc.server.open_connections() == 1))
            self.assertNotEqual(status(svc.raw("GET /v1/x HTTP/1.1\r\nHost: x\r\n")), 503)
        finally:
            held.close()
            svc.close()


class TestAgentKeyRate(unittest.TestCase):
    """Each agent key request mints on the platform; it is rate-limited."""

    def test_per_hotkey(self) -> None:
        t = [0.0]
        svc = _Svc(agent_key_per_hotkey=(2, 60.0), clock=lambda: t[0])
        try:
            pub = base64.b64encode(new_keypair()[1]).decode()
            codes = [
                svc.signed(ALICE, "/v1/miners/agent_key", {"x25519_public": pub})[0]
                for _ in range(3)
            ]
            self.assertEqual(codes, [200, 200, 429])
            t[0] = 61.0
            self.assertEqual(
                svc.signed(ALICE, "/v1/miners/agent_key", {"x25519_public": pub})[0], 200
            )
        finally:
            svc.close()

    def test_per_address_behind_a_trusted_proxy(self) -> None:
        bob = Keypair.create_from_uri("//Bob")
        svc = _Svc(
            registered=[A, B],
            agent_key_per_ip=(1, 60.0),
            trusted_proxies=["127.0.0.1"],
        )
        try:
            pub = base64.b64encode(new_keypair()[1]).decode()
            ok = svc.signed(ALICE, "/v1/miners/agent_key", {"x25519_public": pub}, ip="10.0.0.1")
            self.assertEqual(ok[0], 200)
            again = svc.signed(bob, "/v1/miners/agent_key", {"x25519_public": pub}, ip="10.0.0.1")
            self.assertEqual(again[0], 429)  # another hotkey, the same address
            other = svc.signed(bob, "/v1/miners/agent_key", {"x25519_public": pub}, ip="10.0.0.2")
            self.assertEqual(other[0], 200)
        finally:
            svc.close()


def _eventually(cond: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class _DeskCase(unittest.TestCase):
    policy: AbusePolicy | None = None

    def setUp(self) -> None:
        self.p = FakePlatform()
        self.clock = Clock()
        self.reg = [A, B, C, VALIDATOR.ss58_address]
        self.desk = Desk(
            self.p,
            Ledger(hour_ms=3_600_000),
            registered=lambda: self.reg,
            clock=self.clock,
            rng=random.Random(3),  # noqa: S311 -- a test's seed
            excluded=[VALIDATOR.ss58_address],
            abuse=self.policy or AbusePolicy(),
        )
        for hk in (A, B, C):
            self.desk.agent_key(hk, new_keypair()[1])
            self.desk.miners[hk].admitted = True

    def tearDown(self) -> None:
        self.desk.stop()

    def tick(self) -> None:
        self.desk.tick()
        self.assertTrue(self.desk.wait_idle())

    def holder(self, job: str) -> tuple[str, dict[str, Any]]:
        for hk in (A, B, C):
            w = self.desk.work(hk)
            if w["job"] == job:
                return hk, w
        raise AssertionError(f"nobody has {job}")

    def deliver(self, job: str, files: dict[str, str], *, by: str) -> str:
        """Enqueue a job; ``by`` (everyone else held off for the moment) delivers ``files``."""
        held = {hk: self.desk.miners[hk].blocked_until_ms for hk in (A, B, C) if hk != by}
        for hk in held:
            self.desk.miners[hk].blocked_until_ms = self.desk.now_ms() + 10**9
        self.p.queue.append({"job": job, "brief": "A wall hook."})
        for hk in (A, B, C):
            self.desk.work(hk)
        self.tick()
        for hk, was in held.items():
            self.desk.miners[hk].blocked_until_ms = was
        hk, w = self.holder(job)
        self.assertEqual(hk, by)
        self.desk.accept(hk, job)
        self.p.work(w["workspace"], files, deliverable=True)
        self.desk.submit(hk, job, w["workspace"], "d")
        self.assertTrue(self.desk.wait_idle())
        return hk


class TestAbuseDisables(_DeskCase):
    """The abuse flags disable a miner in the daemon's tick (``Desk.enforce``)."""

    policy = AbusePolicy(copies=1, admission_failures=2, suspensions=1, disable_s=3600.0)

    def test_a_copy_disables_the_copier_revokes_its_key_and_it_gets_no_job(self) -> None:
        self.deliver("j1", GOOD, by=A)
        copier = self.deliver("j2", GOOD, by=B)
        self.assertEqual(self.desk.status("j2")["attempts"][0]["kind"], "copy")
        self.assertEqual(self.desk.miners[copier].blocked_until_ms, 0)  # until the daemon's tick
        acted = self.desk.enforce()
        self.assertTrue(self.desk.wait_idle())
        self.assertEqual([(a["hotkey"], "cop" in a["reason"]) for a in acted], [(copier, True)])
        self.assertEqual(self.desk.enforce(), [])  # one strike per count
        self.assertTrue(self.desk.miners[copier].blocked_until_ms > self.desk.now_ms())
        self.assertEqual(self.desk.miners[copier].key_id, "")
        self.assertFalse(self.desk.miners[copier].admitted)
        self.assertNotIn(copier, self.p.keys)  # revoked the platform's way
        self.p.queue.append({"job": "j3", "brief": "A wall hook."})
        for hk in (A, B, C):
            self.desk.work(hk)
        self.tick()
        self.assertNotEqual(self.holder("j3")[0], copier)
        with self.assertRaises(DeskError):
            self.desk.agent_key(copier, new_keypair()[1])

    def test_rejection_abuse_and_suspension_and_admission_failures_disable(self) -> None:
        self.desk.rejected[A], self.desk.overturned[A], self.desk.assigned[A] = 3, 3, 3
        self.desk._suspended(B)
        self.desk.admission_failed[C] = 2
        acted = {a["hotkey"]: a["reason"] for a in self.desk.enforce()}
        self.assertIn("rejections", acted[A])
        self.assertIn("Watt suspension", acted[B])
        self.assertIn("admission", acted[C])
        self.assertEqual(self.desk.enforce(), [])  # counted once: no second strike
        for hk in (A, B, C):
            self.assertGreater(self.desk.miners[hk].blocked_until_ms, self.desk.now_ms())

    def test_a_second_strike_doubles_the_block(self) -> None:
        self.desk.admission_failed[C] = 2
        self.desk.enforce()
        first = self.desk.miners[C].blocked_until_ms - self.desk.now_ms()
        self.clock.t += first / 1000 + 1
        self.desk.admission_failed[C] = 4
        self.desk.enforce()
        second = self.desk.miners[C].blocked_until_ms - self.desk.now_ms()
        self.assertEqual(second, 2 * first)

    def test_the_owner_and_the_validator_are_never_disabled(self) -> None:
        v = VALIDATOR.ss58_address
        self.desk.admission_failed[v] = 99
        self.desk.rejected[v], self.desk.overturned[v], self.desk.assigned[v] = 9, 9, 9
        self.assertEqual(self.desk.enforce(), [])
        self.desk.disable(v)
        self.assertNotIn(v, self.desk.miners)

    def test_a_disabled_miners_job_goes_to_another_miner_unscored(self) -> None:
        self.p.queue.append({"job": "j1", "brief": "A wall hook."})
        for hk in (A, B, C):
            self.desk.work(hk)
        self.tick()
        hk, _w = self.holder("j1")
        self.desk.disable(hk, reason="test")
        self.assertTrue(self.desk.wait_idle())
        st = self.desk.status("j1")
        self.assertEqual(st["attempts"][0]["kind"], "disabled")
        self.assertEqual(st["state"], "queued")
        self.assertEqual([o for o in self.desk.ledger.outcomes if o.hotkey == hk], [])

    def test_the_policy_off_disables_nobody(self) -> None:
        self.desk.abuse = AbusePolicy(
            copies=0, admission_failures=0, suspensions=0, rejections=False
        )
        self.desk.rejected[A], self.desk.overturned[A], self.desk.assigned[A] = 9, 9, 9
        self.desk.admission_failed[C] = 9
        self.assertEqual(self.desk.enforce(), [])

    def test_the_counts_survive_a_restart(self) -> None:
        import tempfile
        from pathlib import Path

        from cadmind_einstein.store import Store

        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "s.sqlite3")
            desk = Desk(
                self.p,
                Ledger(store=store),
                registered=lambda: self.reg,
                clock=self.clock,
                store=store,
                abuse=self.policy,
            )
            with desk._mut():
                desk.admission_failed[C] = 2
            desk.enforce()
            desk.stop()
            again = Desk(
                self.p,
                Ledger(store=store),
                registered=lambda: self.reg,
                clock=self.clock,
                store=store,
                abuse=self.policy,
            )
            self.assertEqual(again.enforce(), [])  # the strike was recorded: not taken twice
            self.assertGreater(again.miners[C].blocked_until_ms, again.now_ms())
            again.stop()


class TestJudgingBusy(_DeskCase):
    """The platform refuses a second concurrent run on one key (409 ``run_in_progress``): two
    judgements at once must not fail a miner on our own busy key."""

    def test_a_busy_validator_key_is_retried_not_a_gate_failure(self) -> None:
        from cadmind_einstein.platform import PlatformError

        real, calls = self.p.run, {"n": 0}

        def busy_once(ws: str, command: str, args: Any = None, **kw: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise PlatformError("a command is still running", 409, "run_in_progress")
            return real(ws, command, args, **kw)

        self.p.run = busy_once  # type: ignore[method-assign]
        d = self.desk
        d.miners[A].admitted = False
        d.admission(A)
        self.tick()
        w = d.work(A)
        d.accept(A, w["job"])
        self.p.work(w["workspace"], GOOD, deliverable=True)
        d.submit(A, w["job"], w["workspace"], "")
        self.assertTrue(d.wait_idle())
        self.assertEqual(d.status(w["job"])["state"], "judging")  # retried, not failed
        d._retry_at.clear()
        self.tick()
        self.assertEqual(d.status(w["job"])["result"]["label"], "admitted")
        self.assertEqual(d.admission_failed.get(A, 0), 0)


class TestAdmissionFailuresCounted(_DeskCase):
    def test_a_failed_entry_test_is_counted(self) -> None:
        d = self.desk
        d.miners[A].admitted = False
        d.admission(A)
        self.tick()
        w = d.work(A)
        d.accept(A, w["job"])
        d.submit(A, w["job"], w["workspace"], "")
        self.assertTrue(d.wait_idle())
        self.assertEqual(d.admission_failed.get(A), 1)


class TestWatch(_DeskCase):
    """Observability only -- every copy comparison's similarity, the endings and the
    admitted hotkeys by coldkey, in the hourly report. Scores are unchanged."""

    policy = AbusePolicy(copies=0)

    def test_every_compared_pair_is_recorded_with_its_similarity(self) -> None:
        self.deliver("j1", GOOD, by=A)
        tweaked = dict(GOOD)
        tweaked["src/parts/hook.py"] = "\n".join(
            f"line {i} of the hook" if i < 25 else f"my own line {i}" for i in range(40)
        )
        self.deliver("j2", {**tweaked, "src/parts/extra.py": "x = 1\n"}, by=B)
        small = {"spec/a.toml": "a = 1\n", "src/b.py": "b = 2\n"}  # under 5 lines: no shingles
        self.deliver("j3", small, by=C)
        w = self.desk.watch({A: "cold-1", B: "cold-1", C: "cold-2"})
        pairs = w["copy_pairs"]
        self.assertTrue(pairs)
        for pr in pairs:
            self.assertEqual(
                set(pr),
                {"job", "hotkey", "other_job", "other_hotkey", "similarity", "why", "sizes"},
            )
        self.assertEqual(len(pairs), 3)  # j2 against j1; j3 against both
        j2 = [pr for pr in pairs if pr["job"] == "j2"]
        self.assertEqual([(pr["other_job"], pr["why"]) for pr in j2], [("j1", "shingles")])
        self.assertTrue(0.0 < j2[0]["similarity"] < 0.8, j2)
        self.assertEqual(self.desk.status("j2")["state"], "done")  # below 0.8: delivered
        j3 = [pr for pr in pairs if pr["job"] == "j3"]
        self.assertEqual([pr["why"] for pr in j3], ["skipped: no shingles"] * 2)
        self.assertTrue(all(pr["similarity"] is None for pr in j3))
        self.desk.clear_watch(len(pairs))
        self.assertEqual(self.desk.watch({})["copy_pairs"], [])

    def test_endings_and_admitted_hotkeys_by_coldkey(self) -> None:
        self.p.queue.append({"job": "j1", "brief": "A wall hook."})
        for hk in (A, B, C):
            self.desk.work(hk)
        self.tick()
        for _ in range(2):
            hk, _w = self.holder("j1")
            self.desk.reject(hk, "j1", "not-capable", "too hard")
            self.assertTrue(self.desk.wait_idle())
            for h in (A, B, C):
                self.desk.work(h)
            self.tick()
        self.assertEqual(self.desk.status("j1")["result"]["label"], "declined")
        cold = {A: "cold-1", B: "cold-1", C: "cold-2"}
        w = self.desk.watch(cold)
        rows = w["endings_by_coldkey"]
        self.assertEqual(sum(r["declined"] for r in rows.values()), 2)
        self.assertEqual(w["admitted_by_coldkey"], {"cold-1": sorted([A, B]), "cold-2": [C]})
        same = w["same_coldkey_endings"]
        rejecters = {a["hotkey"] for a in self.desk.status("j1")["attempts"]}
        self.assertEqual(bool(same), rejecters == {A, B})

    def test_the_report_carries_the_watch_and_recompute_ignores_it(self) -> None:
        from cadmind_einstein.scoring import recompute

        rep = self.desk.ledger.report(
            self.desk.now_ms(),
            {A: 1, B: 2, C: 3},
            owner_hotkey="",
            admitted=[A, B, C],
            reachable=[A, B, C],
            watch={"admitted_by_coldkey": {"x": [A]}},
        )
        self.assertEqual(rep["watch"], {"admitted_by_coldkey": {"x": [A]}})
        self.assertTrue(recompute(rep)["matches_published"])


class TestCopyCheckPairs(unittest.TestCase):
    def test_check_reports_every_comparison_without_changing_the_verdict(self) -> None:
        cc = CopyCheck()
        a = frozenset({b"1", b"2", b"3", b"4", b"5"})
        self.assertIsNone(cc.check("j1", A, "d1", a))
        seen: list[dict[str, Any]] = []
        found = cc.check("j2", B, "d2", a, on_pair=seen.append)
        self.assertEqual(found and found["similarity"], 1.0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["similarity"], 1.0)
        seen.clear()
        cc.check("j3", C, "d3", frozenset(), on_pair=seen.append)
        self.assertEqual([s["why"] for s in seen], ["skipped: no shingles"] * 2)


class TestColdkeys(unittest.TestCase):
    def test_the_metagraph_file_and_state_carry_coldkeys(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "m.json"
            f.write_text(json.dumps({"neurons": [{"uid": 0, "hotkey": A, "coldkey": "ck"}]}))
            st = FileReader(f).read()
        self.assertEqual(st.coldkey_by_hotkey, {A: "ck"})
        self.assertEqual(
            ChainState(netuid=0, block=0, neurons=(Neuron(uid=0, hotkey=B),)).coldkey_by_hotkey,
            {B: ""},
        )


if __name__ == "__main__":
    unittest.main()
