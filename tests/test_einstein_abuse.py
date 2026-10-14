"""The einstein validator's anti-abuse rules, each against the attack it answers: a copy disguised
by comments, renames, reordering, inserted statements or padding is still a copy, and a small
project is still compared; honest builds of one brief sharing the toolkit's idioms are not copies;
genesis weight cannot be farmed with hotkeys; hotkeys of one owner cannot end a hard job for free;
the validator's answers are signed."""

from __future__ import annotations

import ast
import base64
import json
import random
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

import bittensor.http_auth as btauth
from bittensor.sp_core import Keypair

from cadmind_einstein.desk import AbusePolicy, Desk, SecondOpinion
from cadmind_einstein.fake_miner import signed
from cadmind_einstein.scoring import Ledger, recompute, vector
from cadmind_einstein.sealing import new_keypair
from cadmind_einstein.service import serve, verify_response
from cadmind_einstein.similarity import (
    CopyCheck,
    CopyPolicy,
    Entry,
    fingerprint,
    load_corpus,
)
from tests.test_einstein import A, B, C, Clock, D, FakePlatform, project_zip

VALIDATOR = Keypair.create_from_uri("//Validator")
ALICE = Keypair.create_from_uri("//Alice")
TOKEN = "t" * 32

# -- a project the way the toolkit's primer writes one --------------------------------------------

IMPORTS = (
    "from watt import Part, mm, deg, Z\n"
    "from watt.lib import Cylinder, Box, Peg, SnapHook, Polar\n"
    "from watt.testing import measure, note, verifies\n"
    "from spec import R, machines, materials, world\n"
)


def _part(name: str, dims: list[float]) -> str:
    """A part file: the primer's header, then one feature per dimension of this design."""
    lines = [
        f'"""The {name}."""',
        "",
        IMPORTS,
        f'{name} = Part("{name}", made_on=machines.printer, material=materials.pla)',
    ]
    for i, d in enumerate(dims):
        lines.append(
            f"f{i} = {name}.add(Cylinder(d={d}*mm, h={d / 2 + i}*mm), on={name}.top, "
            f"at=({i * d}*mm, {d + 3}*mm), serves=[R.r{i % 3}])"
        )
    return "\n".join(lines) + "\n"


def _tests(name: str, n: int) -> str:
    """The primer's test skeleton, one test per requirement."""
    out = [IMPORTS, f"from parts.{name} import {name}", ""]
    for i in range(n):
        out += [
            f'@verifies(R.r{i}, why="the {name} meets requirement {i}")',
            f"def test_r{i}():",
            f"    c = measure.clearance(a={name}, b=world.ceiling)",
            "    note(c=c.value)",
            f'    assert c.value <= R.r{i}.gap_max, "within the gap"',
            "",
        ]
    return "\n".join(out)


def _spec(n: int, brief_number: str) -> str:
    out = []
    for i in range(n):
        out += [
            f"[requirement.r{i}]",
            'level = "must"',
            f'statement = "requirement {i} of the client"',
            f'params = {{ gap_max = {{ v = "{brief_number}", from = "client" }} }}',
            "",
        ]
    return "\n".join(out)


def project(name: str, dims: list[float], *, brief_number: str = "0.6 mm") -> dict[str, str]:
    return {
        "spec/requirements.toml": _spec(3, brief_number),
        f"src/parts/{name}.py": _part(name, dims),
        f"tests/test_{name}.py": _tests(name, 3),
    }


VICTIM = project("plug", [67.0, 8.0, 6.0, 2.0, 32.8, 1.2, 9.9, 1.4, 0.8, 16.0, 3.5, 12.5])


def _interleave(text: str) -> str:
    """One comment line after every line: no 5-line window of the original survives."""
    return "\n".join(f"{line}\n# note {i}" for i, line in enumerate(text.splitlines()))


class _Rename(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id not in ("R", "mm", "deg", "Z"):
            node.id = "my_" + node.id
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.name = "check_" + node.name
        return node


class _Junk(ast.NodeTransformer):
    """A dead statement after every statement, in every block."""

    def generic_visit(self, node: ast.AST) -> ast.AST:
        super().generic_visit(node)
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
            out: list[ast.stmt] = []
            for i, stmt in enumerate(body):
                out += [stmt, ast.parse(f"_unused_{i} = {i * 7}").body[0]]
            node.body = out  # type: ignore[attr-defined]
        return node


def _py(files: dict[str, str], *transforms: type[ast.NodeTransformer]) -> dict[str, str]:
    out = {}
    for name, text in files.items():
        out[name] = text
        if name.endswith(".py"):
            tree = ast.parse(text)
            for t in transforms:
                tree = t().visit(tree)
            fns = [s for s in tree.body if isinstance(s, ast.FunctionDef)]
            # functions in the reverse order
            tree.body = [s for s in tree.body if not isinstance(s, ast.FunctionDef)] + fns[::-1]
            out[name] = ast.unparse(ast.fix_missing_locations(tree))
    return out


def fp(files: dict[str, str]) -> frozenset[bytes]:
    return fingerprint(project_zip(files)).features


class TestDisguisedCopies(unittest.TestCase):
    """Line-window shingles fell to one comment per window, skipped a small project and trusted
    only an exact digest; the normalised features do not."""

    def caught(self, copy: dict[str, str], victim: dict[str, str] = VICTIM) -> dict[str, Any]:
        cc = CopyCheck()
        self.assertIsNone(cc.check("j1", A, "digest-1", fp(victim)))
        found = cc.check("j2", B, "digest-2", fp(copy))  # never the same platform digest
        self.assertIsNotNone(found, "the disguised copy was not caught")
        assert found is not None
        return found

    def test_a_comment_in_every_window_is_still_a_copy(self) -> None:
        found = self.caught({k: _interleave(v) for k, v in VICTIM.items()})
        self.assertEqual(found["why"], "same normalised project")

    def test_renamed_reordered_and_padded_with_dead_statements_is_still_a_copy(self) -> None:
        found = self.caught(_py(VICTIM, _Rename, _Junk))
        self.assertEqual(found["why"], "containment")
        self.assertEqual(found["containment"], 1.0)

    def test_padded_with_another_project_is_still_a_copy(self) -> None:
        other = project("clip", [11.0, 4.5, 3.25, 7.75, 19.0, 2.2, 5.5, 0.9])
        padded = {**VICTIM, **{k.replace("clip", "extra"): v for k, v in other.items()}}
        found = self.caught(padded)
        self.assertEqual((found["why"], found["containment"]), ("containment", 1.0))

    def test_a_small_project_is_compared_not_skipped_and_one_byte_does_not_hide_it(self) -> None:
        tiny = {"spec/a.toml": "[x]\nd = '68 mm'\n", "src/p.py": "a = 1\nb = 2\n"}
        found = self.caught({**tiny, "src/p.py": "a = 1  # mine\nb = 2.0\n"}, victim=tiny)
        self.assertEqual(found["why"], "same normalised project")
        cc = CopyCheck()
        cc.check("j1", A, "d1", fp(tiny))
        other = {**tiny, "src/p.py": "a = 1\nb = 3\n"}  # a different (small) design
        self.assertIsNone(cc.check("j2", B, "d2", fp(other)))

    def test_layout_strings_and_number_spelling_do_not_matter(self) -> None:
        a = fp({"src/x.py": "x = f(d=2, why='a')\n"})
        b = fp({"src/x.py": 'x = f(\n    d=2.0,\n    why="another reason",\n)\n'})
        self.assertEqual(a, b)

    def test_the_desk_scores_a_disguised_copy_as_a_copy(self) -> None:
        p, clock = FakePlatform(), Clock()
        desk = Desk(
            p,
            Ledger(),
            registered=lambda: [A, B],
            clock=clock,
            rng=random.Random(1),  # noqa: S311 -- a test's seed
            abuse=AbusePolicy(copies=0),
        )
        try:
            for hk in (A, B):
                desk.agent_key(hk, new_keypair()[1])
                desk.miners[hk].admitted = True
            for job, files in (("j1", VICTIM), ("j2", _py(VICTIM, _Rename, _Junk))):
                p.queue.append({"job": job, "brief": "A plug."})
                for hk in (A, B):
                    desk.work(hk)
                desk.tick()
                self.assertTrue(desk.wait_idle())
                hk = next(h for h in (A, B) if desk.work(h)["job"] == job)
                w = desk.work(hk)
                desk.accept(hk, job)
                p.work(w["workspace"], files, deliverable=True)
                desk.submit(hk, job, w["workspace"], "")
                self.assertTrue(desk.wait_idle())
                # the next job must go to the other miner
                desk.miners[hk].blocked_until_ms = desk.now_ms() + 10**9
            self.assertEqual(desk.status("j1")["result"]["label"], "delivered")
            self.assertEqual(desk.status("j2")["attempts"][0]["kind"], "copy")
        finally:
            desk.stop()


class TestHonestBuildsAreNotCopies(unittest.TestCase):
    """Two miners building one brief from the same primer share its idioms (imports, the test
    skeleton, the spec's schema, the brief's numbers): that alone must not make a copy."""

    # Small designs: most of their features are the primer's.
    MINE = project("plug", [67.0, 8.0])
    THEIRS = project("plug", [66.5, 7.0])
    OTHERS = (
        project("hook", [20.0, 4.0, 2.5]),
        project("clip", [11.0, 3.0, 1.5]),
        project("stand", [80.0, 9.5, 4.25]),
        project("bracket", [45.0, 2.75, 13.0]),
    )

    def test_without_subtraction_the_shared_idioms_would_collide(self) -> None:
        cmp = CopyCheck(policy=CopyPolicy(common_coldkeys=0)).compare(
            fp(self.MINE), fp(self.THEIRS)
        )
        self.assertGreaterEqual(cmp["containment"], 0.7, cmp)  # the false positive, unguarded
        self.assertEqual(cmp["why"], "containment")

    def test_the_common_set_learned_from_other_owners_removes_them(self) -> None:
        cc = CopyCheck()
        # four other owners delivered with the primer (compared against one of them, three
        # others still share each idiom)
        for i, other in enumerate(self.OTHERS):
            cc.remember(Entry(f"o{i}", f"hk{i}", f"d{i}", fp(other), coldkey=f"owner-{i}"))
        self.assertIsNone(cc.check("j1", A, "dm", fp(self.MINE), coldkey="owner-a"))
        seen: list[dict[str, Any]] = []
        found = cc.check("j2", B, "dt", fp(self.THEIRS), coldkey="owner-b", on_pair=seen.append)
        self.assertIsNone(found, seen)
        mine = next(s for s in seen if s["other_job"] == "j1")
        self.assertGreater(mine["common"], 0)
        self.assertLess(mine["containment"], 0.7)

    def test_a_corpus_of_example_projects_removes_them_from_the_start(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ex = Path(d) / "worked-example"
            for rel, text in self.OTHERS[0].items():
                (ex / rel).parent.mkdir(parents=True, exist_ok=True)
                (ex / rel).write_text(text)
            corpus = load_corpus([Path(d)])
        self.assertTrue(corpus)
        cc = CopyCheck(corpus=corpus)
        cc.check("j1", A, "d1", fp(self.MINE))
        self.assertIsNone(cc.check("j2", B, "d2", fp(self.THEIRS)))

    def test_one_owners_hotkeys_cannot_make_features_common(self) -> None:
        cc = CopyCheck()
        for i in range(3):  # the same features from three hotkeys of ONE coldkey
            cc.remember(Entry(f"o{i}", f"hk{i}", f"d{i}", fp(VICTIM), coldkey="owner-x"))
        cc.remember(Entry("v", A, "dv", fp(VICTIM), coldkey="owner-v"))
        self.assertIsNotNone(cc.check("c", B, "dc", fp(VICTIM), coldkey="owner-c"))

    def test_copies_never_count_toward_the_common_set(self) -> None:
        cc = CopyCheck()
        cc.check("v", A, "dv", fp(VICTIM), coldkey="owner-v")
        for i in range(3):  # three other owners hand in the victim's project: each a copy
            self.assertIsNotNone(cc.check(f"c{i}", f"x{i}", f"dc{i}", fp(VICTIM), coldkey=f"o{i}"))
        # so a fourth copier is still caught: the victim's features did not become common
        self.assertIsNotNone(cc.check("c9", B, "dc9", fp(VICTIM), coldkey="owner-9"))

    def test_the_window_forgets_and_its_counts_follow(self) -> None:
        cc = CopyCheck(policy=CopyPolicy(window=2))
        for i in range(4):
            cc.remember(Entry(f"o{i}", f"hk{i}", f"d{i}", fp(self.OTHERS[i % 3]), f"c{i}"))
        self.assertEqual(len(cc.recent), 2)
        alive = fp(self.OTHERS[2]) | fp(self.OTHERS[0])
        self.assertTrue(set(cc._df) <= alive)


class TestGenesisSybils(unittest.TestCase):
    """At genesis the equal split paid per hotkey: 1 honest miner + 9 sybil hotkeys took 90%."""

    HONEST = "honest"
    SYBILS = tuple(f"sybil{i}" for i in range(9))

    def setUp(self) -> None:
        self.adm = [self.HONEST, *self.SYBILS]
        self.uids = {h: i for i, h in enumerate(self.adm)}

    def share(self, vec: list[tuple[int, int]]) -> float:
        total = sum(w for _u, w in vec)
        return dict(vec).get(0, 0) / total if total else 0.0

    def test_the_old_rule_is_the_attack(self) -> None:
        vec, _ = vector(
            {}, self.uids, owner_hotkey="", admitted=self.adm, reachable=self.adm, genesis="hotkey"
        )
        self.assertAlmostEqual(self.share(vec), 0.1)

    def test_delivered_genesis_pays_only_who_delivered(self) -> None:
        vec, rule = vector(
            {},
            self.uids,
            owner_hotkey="",
            admitted=self.adm,
            reachable=self.adm,
            proven=[self.HONEST],
        )
        self.assertEqual(vec, [(0, 65_535)])
        self.assertIn("per coldkey among delivered", rule)

    def test_delivered_genesis_before_any_delivery_weights_nobody(self) -> None:
        vec, rule = vector({}, self.uids, owner_hotkey="", admitted=self.adm, reachable=self.adm)
        self.assertEqual(vec, [])
        self.assertIn("nothing to weight", rule)

    def test_coldkey_genesis_gives_one_share_per_owner(self) -> None:
        cold = {self.HONEST: "owner-h", **dict.fromkeys(self.SYBILS, "owner-s")}
        vec, _ = vector(
            {},
            self.uids,
            owner_hotkey="",
            admitted=self.adm,
            reachable=self.adm,
            genesis="coldkey",
            coldkeys=cold,
        )
        self.assertAlmostEqual(self.share(vec), 0.5, places=3)

    def test_delivered_and_coldkey_together(self) -> None:
        cold = {self.HONEST: "owner-h", **dict.fromkeys(self.SYBILS, "owner-s")}
        proven = [self.HONEST, *self.SYBILS[:3]]  # three sybils of one owner did deliver
        vec, _ = vector(
            {},
            self.uids,
            owner_hotkey="",
            admitted=self.adm,
            reachable=self.adm,
            coldkeys=cold,
            proven=proven,
        )
        self.assertAlmostEqual(self.share(vec), 0.5, places=3)
        self.assertEqual(len(vec), 4)

    def test_the_report_publishes_the_rule_and_recompute_follows_it(self) -> None:
        rep = Ledger().report(
            0,
            self.uids,
            owner_hotkey="",
            admitted=self.adm,
            reachable=self.adm,
            coldkeys={self.HONEST: "owner-h"},
            proven=[self.HONEST, "not-admitted"],
        )
        self.assertEqual((rep["genesis"], rep["proven"]), ("delivered", [self.HONEST]))
        self.assertEqual(rep["coldkeys"][self.HONEST], "owner-h")
        self.assertTrue(recompute(rep)["matches_published"])
        legacy = {k: v for k, v in rep.items() if k not in ("genesis", "coldkeys", "proven")}
        legacy["vector"] = [[i, 65_535] for i in range(10)]  # what the earlier rule published
        self.assertTrue(recompute(legacy)["matches_published"])

    def test_the_desk_records_who_delivered(self) -> None:
        p = FakePlatform()
        desk = Desk(p, Ledger(), registered=lambda: [A], clock=Clock())
        try:
            desk.agent_key(A, new_keypair()[1])
            desk.miners[A].admitted = True
            self.assertEqual(desk.proven(), [])
            p.queue.append({"job": "j1", "brief": "A hook."})
            desk.work(A)
            desk.tick()
            self.assertTrue(desk.wait_idle())
            w = desk.work(A)
            desk.accept(A, "j1")
            p.work(w["workspace"], VICTIM, deliverable=True)
            desk.submit(A, "j1", w["workspace"], "")
            self.assertTrue(desk.wait_idle())
            self.assertEqual(desk.proven(), [A])
        finally:
            desk.stop()


class TestSecondOpinions(unittest.TestCase):
    """Two rejections ended a job declined, two failures out of reach, with nobody penalised; the
    second opinion only needed another hotkey -- so two hotkeys of one owner ended hard jobs for
    free."""

    COLD: ClassVar[dict[str, str]] = {A: "owner-1", B: "owner-1", C: "owner-2", D: "owner-3"}

    def make(self, **kw: Any) -> Desk:
        self.p, self.clock = FakePlatform(), Clock()
        desk = Desk(
            self.p,
            Ledger(),
            registered=lambda: [A, B, C, D],
            clock=self.clock,
            rng=random.Random(5),  # noqa: S311 -- a test's seed
            coldkeys=lambda: self.COLD,
            abuse=AbusePolicy(rejections=False),
            **kw,
        )
        self.addCleanup(desk.stop)
        for hk in (A, B, C, D):
            desk.agent_key(hk, new_keypair()[1])
            desk.miners[hk].admitted = True
        return desk

    def only(self, desk: Desk, *free: str) -> None:
        """Only these miners are free (and alive) for the next tick."""
        for hk in (A, B, C, D):
            desk.miners[hk].blocked_until_ms = 0 if hk in free else desk.now_ms() + 10**9
            desk.work(hk)

    def holder(self, desk: Desk, job: str) -> str:
        return next(h for h in (A, B, C, D) if desk.work(h)["job"] == job)

    def tick(self, desk: Desk) -> None:
        desk.tick()
        self.assertTrue(desk.wait_idle())

    def test_the_second_opinion_goes_to_another_coldkey(self) -> None:
        desk = self.make()
        self.p.queue.append({"job": "j1", "brief": "A hard device."})
        self.only(desk, A)
        self.tick(desk)
        desk.reject(A, "j1", "not-capable", "too hard")
        self.assertTrue(desk.wait_idle())
        self.only(desk, B, C)  # B is A's owner's other hotkey
        for _ in range(5):
            self.tick(desk)
        self.assertEqual(self.holder(desk, "j1"), C)

    def test_after_the_wait_the_same_owner_may_try_but_cannot_end_it(self) -> None:
        desk = self.make(second_opinion=SecondOpinion(wait_s=60.0))
        self.p.queue.append({"job": "j1", "brief": "A hard device."})
        self.only(desk, A)
        self.tick(desk)
        desk.reject(A, "j1", "not-capable", "too hard")
        self.assertTrue(desk.wait_idle())
        self.only(desk, B)
        self.tick(desk)
        self.assertEqual(desk.status("j1")["state"], "queued")  # B waits behind the rule
        self.clock.t += 61
        self.only(desk, B)
        self.tick(desk)
        self.assertEqual(self.holder(desk, "j1"), B)
        desk.reject(B, "j1", "not-capable", "too hard as well")
        self.assertTrue(desk.wait_idle())
        self.assertEqual(desk.status("j1")["state"], "queued")  # one owner: not declined
        self.only(desk, C)
        self.tick(desk)
        desk.reject(C, "j1", "not-capable", "too hard for me too")
        self.assertTrue(desk.wait_idle())
        self.assertEqual(desk.status("j1")["result"]["label"], "declined")  # two owners
        self.assertEqual(desk.ledger.outcomes, [])  # an independent "nobody could": no 0s

    def test_held_failures_count_when_one_owner_ran_the_job_out(self) -> None:
        desk = self.make(second_opinion=SecondOpinion(wait_s=0.0), max_attempts=2)
        self.p.queue.append({"job": "j1", "brief": "A hard device."})
        self.only(desk, A)
        self.tick(desk)
        desk.reject(A, "j1", "not-capable", "too hard")
        self.assertTrue(desk.wait_idle())
        self.only(desk, B)
        self.tick(desk)
        desk.reject(B, "j1", "not-capable", "too hard")
        self.assertTrue(desk.wait_idle())
        self.assertEqual(desk.status("j1")["result"]["label"], "didn't finish")
        self.assertEqual(
            sorted((o.hotkey, o.s) for o in desk.ledger.outcomes), sorted([(A, 0.0), (B, 0.0)])
        )

    def test_one_coldkey_per_opinion_restores_the_hotkey_rule(self) -> None:
        desk = self.make(second_opinion=SecondOpinion(coldkeys=1, wait_s=0.0))
        self.p.queue.append({"job": "j1", "brief": "A hard device."})
        self.only(desk, A)
        self.tick(desk)
        desk.reject(A, "j1", "not-capable", "too hard")
        self.assertTrue(desk.wait_idle())
        self.only(desk, B)
        self.tick(desk)
        desk.reject(B, "j1", "not-capable", "too hard")
        self.assertTrue(desk.wait_idle())
        self.assertEqual(desk.status("j1")["result"]["label"], "declined")
        self.assertEqual(desk.ledger.outcomes, [])

    def test_rejections_are_summed_per_coldkey(self) -> None:
        desk = self.make()
        desk.abuse = AbusePolicy(rejections_min=5, rejection_ratio=2.0, overturned=0)
        with desk._mut():
            desk.assigned.update({A: 3, B: 3, C: 20})
            desk.rejected.update({A: 3, B: 3})  # under 5 each; 6 for their owner
        acted = desk.enforce()
        self.assertEqual(sorted(a["hotkey"] for a in acted), sorted([A, B]))
        self.assertTrue(all("coldkey owner-1" in a["reason"] for a in acted))
        self.assertEqual(desk.enforce(), [])  # one strike per count

    def test_the_watch_reports_per_coldkey(self) -> None:
        desk = self.make()
        with desk._mut():
            desk.assigned.update({A: 2, B: 1})
            desk.rejected.update({A: 1, B: 1})
            desk.failed.update({A: 1})
        row = desk.watch(self.COLD)["by_coldkey"]["owner-1"]
        self.assertEqual(
            (row["hotkeys"], row["assigned"], row["rejected"], row["gate_failed"]),
            (sorted([A, B]), 3, 2, 1),
        )


class TestSignedAnswers(unittest.TestCase):
    """The validator's answers were plain JSON: integrity rested on TLS alone."""

    def setUp(self) -> None:
        self.p = FakePlatform()
        self.desk = Desk(self.p, Ledger(), registered=lambda: [A])
        self.server = serve(
            self.desk,
            "127.0.0.1",
            0,
            token=TOKEN,
            validator_hotkey=VALIDATOR.ss58_address,
            verify=btauth.verify,
            close_hour=lambda: None,
            signer=VALIDATOR,
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.desk.stop()

    def raw(
        self, kp: Keypair, path: str, body: dict[str, Any]
    ) -> tuple[int, bytes, dict[str, str], dict[str, str]]:
        data = json.dumps(body).encode()
        auth = btauth.sign(
            kp, method="POST", path=path, body=data, receiver_ss58=VALIDATOR.ss58_address
        )
        req = urllib.request.Request(self.base + path, data=data, headers=auth)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 -- loopback
                return r.status, r.read(), dict(r.headers.items()), auth
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers.items()), auth

    def check(
        self,
        code: int,
        body: bytes,
        headers: dict[str, str],
        auth: dict[str, str],
        path: str,
        **override: Any,
    ) -> bool:
        kw: dict[str, Any] = {
            "status": code,
            "method": "POST",
            "path": path,
            "nonce": auth["X-Bittensor-Nonce"],
            "hotkey": auth["X-Bittensor-Hotkey"],
            "validator": VALIDATOR.ss58_address,
        }
        kw.update(override)
        return verify_response(headers, body, **kw)

    def test_an_answer_verifies_and_a_tampered_one_does_not(self) -> None:
        code, body, headers, auth = self.raw(ALICE, "/v1/jobs/work", {})
        self.assertEqual(code, 200)
        self.assertTrue(self.check(code, body, headers, auth, "/v1/jobs/work"))
        forged = body.replace(b"null", b'"job-1"')
        self.assertFalse(self.check(code, forged, headers, auth, "/v1/jobs/work"))
        self.assertFalse(self.check(code, body, headers, auth, "/v1/jobs/work", nonce="1"))
        self.assertFalse(self.check(code, body, headers, auth, "/v1/jobs/accept"))
        self.assertFalse(self.check(500, body, headers, auth, "/v1/jobs/work"))
        self.assertFalse(self.check(code, body, headers, auth, "/v1/jobs/work", hotkey=B))
        other = Keypair.create_from_uri("//Mallory").ss58_address
        self.assertFalse(self.check(code, body, headers, auth, "/v1/jobs/work", validator=other))

    def test_a_refusal_is_signed_too(self) -> None:
        mallory = Keypair.create_from_uri("//Mallory")  # not registered
        code, body, headers, auth = self.raw(mallory, "/v1/jobs/work", {})
        self.assertEqual(code, 403)
        self.assertTrue(self.check(code, body, headers, auth, "/v1/jobs/work"))

    def test_the_reference_miner_checks_and_refuses_an_unsigned_answer(self) -> None:
        pub = base64.b64encode(new_keypair()[1]).decode()
        code, out = signed(
            ALICE, self.base, "/v1/miners/agent_key", {"x25519_public": pub}, VALIDATOR.ss58_address
        )
        self.assertEqual(code, 200, out)
        plain = serve(
            self.desk,
            "127.0.0.1",
            0,
            token=TOKEN,
            validator_hotkey=VALIDATOR.ss58_address,
            verify=btauth.verify,
            close_hour=lambda: None,
        )
        threading.Thread(target=plain.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{plain.server_address[1]}"
            code, out = signed(ALICE, url, "/v1/jobs/work", {}, VALIDATOR.ss58_address)
            self.assertEqual(code, 0)
            self.assertIn("not signed", out["error"])
        finally:
            plain.shutdown()
            plain.server_close()

    def test_a_signer_that_is_not_the_validator_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            serve(
                self.desk,
                "127.0.0.1",
                0,
                token=TOKEN,
                validator_hotkey=VALIDATOR.ss58_address,
                verify=btauth.verify,
                close_hour=lambda: None,
                signer=ALICE,
            )


if __name__ == "__main__":
    unittest.main()
