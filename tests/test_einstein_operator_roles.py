"""The validator's three operator keys: each call goes out with its role's key (and the queue's to
the queue URL); a role the platform froze stops at once, alerts once, is asked again only every
few minutes and resumes on its own; a key that does not hold its role is refused loudly; the
platform's back-pressure is honoured; and none of it ever fails a miner or loses a result."""

from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cadmind_einstein.daemon import read_key
from cadmind_einstein.desk import Desk, DeskError
from cadmind_einstein.platform import (
    OperatorClient,
    PlatformError,
    RoleFrozenError,
    RoleRefusedError,
)
from cadmind_einstein.scoring import Ledger
from cadmind_einstein.sealing import new_keypair
from cadmind_einstein.store import Store
from tests.test_einstein import GOOD, A, Clock, FakePlatform


class _Stub:
    """A platform on loopback: answers per (method, path prefix) from ``rules`` (status, body,
    headers), records (method, path, bearer) of every request."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str]] = []
        self.rules: dict[tuple[str, str], tuple[int, dict[str, Any], dict[str, str]]] = {}
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                return None

            def _go(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                auth = str(self.headers.get("Authorization") or "").removeprefix("Bearer ")
                stub.seen.append((self.command, self.path, auth))
                code, body, headers = 200, {}, {}
                for (method, prefix), rule in stub.rules.items():
                    if method == self.command and self.path.startswith(prefix):
                        code, body, headers = rule
                        break
                else:
                    if self.path.endswith("/claimed"):
                        body = {"jobs": []}
                    elif self.path.endswith("/claim"):
                        body = {"job": None}
                    elif "/subnet/keys" in self.path:
                        body = {"key_id": "k1", "secret": "s1"}
                    elif self.path.endswith("/workspaces") or self.path.endswith("/fork"):
                        body = {"workspace": "ws-1"}
                data = json.dumps(body).encode()
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _go  # noqa: N815 -- http.server's names

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.platform, self.queue = _Stub(), _Stub()
        self.addCleanup(self.platform.close)
        self.addCleanup(self.queue.close)
        self.t = [0.0]
        self.logs: list[str] = []

    def client(self, **kw: Any) -> OperatorClient:
        return OperatorClient(
            self.platform.url,
            "op",
            queue_url=self.queue.url,
            log=self.logs.append,
            clock=lambda: self.t[0],
            **kw,
        )


class TestRoleRouting(_Case):
    def test_each_call_carries_its_roles_key(self) -> None:
        c = self.client(mint_key="mint", queue_key="queue")
        c.mint_key(A)
        c.disable_key(A)
        c.grant(A, brief="b")
        c.state("w")
        c.freeze("w")
        c.fork("w")
        c.run("w", "status")
        c.export("w")
        c.delete("w")
        c.claim()
        c.lease(["j"])
        c.claimed()
        c.complete("j", {"label": "delivered"})
        by_path = {(m, p.split("?")[0]): k for m, p, k in self.platform.seen}
        self.assertEqual(by_path[("POST", "/api/agent/subnet/keys")], "mint")
        self.assertEqual(by_path[("POST", "/api/agent/subnet/keys/disable")], "mint")
        others = [k for (_m, p), k in by_path.items() if "/subnet/keys" not in p]
        self.assertEqual(set(others), {"op"})
        self.assertEqual(len(others), 7)
        self.assertEqual({k for _m, _p, k in self.queue.seen}, {"queue"})
        self.assertEqual(len(self.queue.seen), 4)
        self.assertFalse(any("/engine/" in p for _m, p, _k in self.platform.seen))

    def test_an_unset_role_key_is_the_operator_key(self) -> None:
        c = self.client()
        c.mint_key(A)
        c.claim()
        self.assertEqual({k for _m, _p, k in self.platform.seen + self.queue.seen}, {"op"})


class TestFrozenRole(_Case):
    def frozen(self, status: int = 403, code: str = "operator_frozen") -> None:
        self.queue.rules[("POST", "/api/engine/einstein/claim")] = (
            status,
            {"error": "frozen", "code": code},
            {},
        )

    def test_a_frozen_role_stops_alerts_once_and_is_asked_again_every_five_minutes(self) -> None:
        c = self.client(queue_key="queue")
        self.frozen()
        with self.assertRaises(RoleFrozenError) as e:
            c.claim()
        self.assertTrue(e.exception.ours)
        self.assertEqual(e.exception.status, 503)  # our side: never a miner's failure
        sent = len(self.queue.seen)
        for _ in range(50):  # the desk's ticks: no request goes out, no new alert
            self.t[0] += 5
            with self.assertRaises(RoleFrozenError):
                c.complete("j", {"label": "delivered"})
        self.assertEqual(len(self.queue.seen), sent)
        self.assertEqual(sum("ALERT" in x and "froze" in x for x in self.logs), 1)
        self.assertEqual(c.frozen(), ["queue"])
        c.mint_key(A)  # the other roles go on
        c.grant(A, brief="b")
        self.t[0] += 60  # past the 5 minutes: asked again (still frozen)
        with self.assertRaises(RoleFrozenError):
            c.claim()
        self.assertEqual(len(self.queue.seen), sent + 1)
        self.queue.rules.clear()  # the owner enabled the key again
        self.t[0] += 301
        self.assertIsNone(c.claim())
        self.assertEqual(c.frozen(), [])
        self.assertTrue(any("works again" in x for x in self.logs))

    def test_the_ceiling_answer_freezes_too(self) -> None:
        c = self.client()
        self.frozen(429, "operator_ceiling")
        with self.assertRaises(RoleFrozenError):
            c.claim()
        self.assertEqual(c.frozen(), ["queue"])


class TestWrongKeyAndBackPressure(_Case):
    def test_a_key_without_its_role_is_refused_loudly_and_stops_the_start(self) -> None:
        self.platform.rules[("GET", "/api/agent/subnet/workspaces/")] = (
            403,
            {"error": "no", "code": "operator_role"},
            {},
        )
        c = self.client()
        with self.assertRaises(RoleRefusedError):
            c.probe()
        self.assertTrue(any("ALERT" in x and "does not hold" in x for x in self.logs))

    def test_the_probe_reads_both_checkable_roles(self) -> None:
        self.platform.rules[("GET", "/api/agent/subnet/workspaces/")] = (404, {}, {})
        self.assertEqual(
            self.client().probe(), {"keys": "unchecked", "workspaces": "ok", "queue": "ok"}
        )
        self.queue.rules[("GET", "/api/engine/einstein/claimed")] = (
            403,
            {"code": "operator_frozen"},
            {},
        )
        self.assertEqual(self.client().probe()["queue"], "frozen")

    def test_a_401_on_the_queue_says_key_or_allowlist(self) -> None:
        self.queue.rules[("POST", "/api/engine/einstein/claim")] = (401, {"error": "no"}, {})
        c = self.client()
        with self.assertRaises(PlatformError) as e:
            c.claim()
        self.assertEqual(e.exception.status, 401)
        self.assertTrue(any("ALERT" in x and "allowlisted" in x for x in self.logs))
        self.queue.rules[("GET", "/api/engine/einstein/claimed")] = (401, {"error": "no"}, {})
        with self.assertRaises(PlatformError):
            self.client().probe()  # the daemon refuses to start

    def test_too_many_in_flight_honours_retry_after(self) -> None:
        self.platform.rules[("POST", "/api/agent/subnet/workspaces")] = (
            429,
            {"code": "too_many_in_flight"},
            {"Retry-After": "7"},
        )
        c = self.client()
        with self.assertRaises(PlatformError) as e:
            c.grant(A, brief="b")
        self.assertTrue(e.exception.ours)
        self.platform.rules.clear()
        sent = len(self.platform.seen)
        self.t[0] += 6
        with self.assertRaises(PlatformError):
            c.state("w")  # still held: not sent
        self.assertEqual(len(self.platform.seen), sent)
        self.t[0] += 2
        c.state("w")
        self.assertEqual(len(self.platform.seen), sent + 1)

    def test_busy_is_held_briefly_then_retried(self) -> None:
        self.queue.rules[("POST", "/api/engine/einstein/claim")] = (503, {"error": "busy"}, {})
        c = self.client()
        with self.assertRaises(PlatformError) as e:
            c.claim()
        self.assertEqual((e.exception.status, e.exception.ours), (503, True))
        self.queue.rules.clear()
        self.t[0] += 6
        self.assertIsNone(c.claim())


class TestDeskUnderAFrozenRole(unittest.TestCase):
    """The desk on a frozen role: no miner is failed, no result is lost."""

    def test_a_frozen_queue_keeps_results_durably_and_sends_them_after(self) -> None:
        p, clock = FakePlatform(), Clock()
        real = p.complete
        state = {"frozen": True}

        def complete(job: str, result: dict[str, Any]) -> None:
            if state["frozen"]:
                raise RoleFrozenError("frozen", 503, "operator_frozen", ours=True)
            real(job, result)

        p.complete = complete  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "s.sqlite3")
            desk = Desk(
                p,
                Ledger(store=store),
                registered=lambda: [A],
                clock=clock,
                store=store,
                rng=random.Random(1),  # noqa: S311 -- a test's seed
            )
            desk.agent_key(A, new_keypair()[1])
            desk.miners[A].admitted = True
            p.queue.append({"job": "j1", "brief": "A hook."})
            desk.work(A)
            desk.tick()
            self.assertTrue(desk.wait_idle())
            w = desk.work(A)
            desk.accept(A, "j1")
            p.work(w["workspace"], GOOD, deliverable=True)
            desk.submit(A, "j1", w["workspace"], "")
            self.assertTrue(desk.wait_idle())
            for _ in range(3):
                desk.tick()
                self.assertTrue(desk.wait_idle())
            self.assertEqual(desk.status("j1")["result"]["label"], "delivered")
            self.assertNotIn("j1", p.completed)
            desk.stop()
            again = Desk(p, Ledger(store=store), registered=lambda: [A], clock=clock, store=store)
            self.assertEqual(again._outbox, ["j1"])  # survived a restart
            state["frozen"] = False
            again.tick()
            self.assertTrue(again.wait_idle())
            self.assertEqual(p.completed["j1"]["label"], "delivered")
            again.stop()

    def test_a_frozen_workspaces_role_never_fails_the_miner(self) -> None:
        p, clock = FakePlatform(), Clock()
        desk = Desk(p, Ledger(), registered=lambda: [A], clock=clock)
        self.addCleanup(desk.stop)
        desk.agent_key(A, new_keypair()[1])
        desk.miners[A].admitted = True
        p.queue.append({"job": "j1", "brief": "A hook."})
        desk.work(A)
        desk.tick()
        self.assertTrue(desk.wait_idle())
        w = desk.work(A)
        desk.accept(A, "j1")
        p.work(w["workspace"], GOOD, deliverable=True)
        real = p.freeze

        def frozen(ws: str) -> dict[str, Any]:
            raise RoleFrozenError("frozen", 503, "operator_frozen", ours=True)

        p.freeze = frozen  # type: ignore[method-assign]
        desk.submit(A, "j1", w["workspace"], "")
        self.assertTrue(desk.wait_idle())
        self.assertEqual(desk.status("j1")["state"], "judging")  # retried later, not failed
        self.assertEqual(desk.ledger.outcomes, [])
        p.freeze = real  # type: ignore[method-assign]
        desk._retry_at.clear()
        desk.tick()
        self.assertTrue(desk.wait_idle())
        self.assertEqual(desk.status("j1")["result"]["label"], "delivered")

    def test_a_refused_or_frozen_mint_is_a_503_to_the_miner(self) -> None:
        p = FakePlatform()

        def mint(_hotkey: str) -> dict[str, Any]:
            raise RoleFrozenError("frozen", 503, "operator_frozen", ours=True)

        p.mint_key = mint  # type: ignore[method-assign]
        desk = Desk(p, Ledger(), registered=lambda: [A])
        self.addCleanup(desk.stop)
        with self.assertRaises(DeskError) as e:
            desk.agent_key(A, new_keypair()[1])
        self.assertEqual(e.exception.status, 503)


class TestKeyFiles(unittest.TestCase):
    def test_a_role_key_file_must_be_0600(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "k"
            f.write_text("secret\n")
            f.chmod(0o644)
            with self.assertRaises(SystemExit):
                read_key(f)
            self.assertEqual(read_key(f, strict=False), "secret")  # the operator key: warned
            f.chmod(0o600)
            self.assertEqual(read_key(f), "secret")


if __name__ == "__main__":
    unittest.main()
