"""Einstein's chain side: metagraph reads, stake dominance, weights within the rate limit
(plain or commit-reveal) and never to the subnet owner, the hourly report's hash committed on
chain, the published report chain and its auditor -- against fakes of the bittensor 11 client (no
chain write in tests)."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import Any

import bittensor.http_auth as btauth
from bittensor.sp_core import Keypair

from cadmind_einstein.chain import (
    ChainCommitter,
    ChainState,
    ChainWeights,
    DryRunWeights,
    FileReader,
    LiveChain,
    Neuron,
    SubtensorReader,
    commitment_call,
    dominance,
    guard,
)
from cadmind_einstein.desk import Desk
from cadmind_einstein.scoring import (
    HOUR_MS,
    JobOutcome,
    Ledger,
    audit,
    report_bytes,
)
from cadmind_einstein.scoring import (
    main as recompute_main,
)
from cadmind_einstein.service import serve
from cadmind_einstein.store import Store
from tests.test_einstein import OWNER, A, B, C, FakePlatform, passing

ME = Keypair.create_from_uri("//Validator").ss58_address
OTHER_V = Keypair.create_from_uri("//Eve").ss58_address


def state(
    *,
    block: int = 10_000,
    my_stake: float = 900.0,
    other_stake: float = 100.0,
    other_last: int = 9_990,
    my_last: int = 9_000,
    permit: bool = True,
    rate: int = 100,
    commit_reveal: bool = True,
) -> ChainState:
    return ChainState(
        netuid=7,
        block=block,
        neurons=(
            Neuron(uid=0, hotkey=OWNER, stake=0.0),
            Neuron(uid=1, hotkey=ME, stake=my_stake, validator_permit=permit, last_update=my_last),
            Neuron(uid=2, hotkey=A),
            Neuron(uid=3, hotkey=B),
            Neuron(
                uid=4,
                hotkey=OTHER_V,
                stake=other_stake,
                validator_permit=True,
                last_update=other_last,
            ),
        ),
        owner_hotkey=OWNER,
        hyper={
            "kappa": 32767,
            "activity_cutoff": 4950,
            "weights_rate_limit": rate,
            "commit_reveal_weights_enabled": commit_reveal,
            "weights_version": 3,
        },
    )


class Fixed:
    def __init__(self, st: ChainState) -> None:
        self.st = st
        self.fail = False

    def read(self) -> ChainState:
        if self.fail:
            raise ConnectionError("chain down")
        return self.st


class Bal:
    def __init__(self, rao: int) -> None:
        self.rao = rao


class FakeSubtensor:
    """The bittensor 11 surface the validator touches: ``subnets.metagraph``,
    ``subnets.subnet_hyperparameters``, ``execute`` (an intent) and ``submit_call`` (a raw call).
    Records every write; writes nothing anywhere."""

    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.submitted: list[tuple[Any, str]] = []
        self.refuse = ""
        outer = self

        class Subnets:
            def metagraph(self, netuid: int, commitments: bool = True) -> Any:
                class N:
                    def __init__(self, uid: int, hk: str, stake: int, permit: bool) -> None:
                        self.uid, self.hotkey, self.total_stake = uid, hk, Bal(stake)
                        self.validator_permit, self.last_update = permit, 50

                class M:
                    block = 123
                    owner_hotkey = OWNER
                    neurons = (N(0, OWNER, 0, False), N(1, ME, 10**12, True), N(2, A, 5, False))

                return M()

            def subnet_hyperparameters(self, netuid: int) -> dict[str, Any]:
                return {"kappa": 32767, "weights_rate_limit": 100}

        self.subnets = Subnets()
        del outer

    def execute(self, intent: Any, wallet: Any) -> Any:
        if self.refuse:
            raise RuntimeError(self.refuse)
        self.executed.append(intent)

        class R:
            success = True

        return R()

    def submit_call(self, call: Any, wallet: Any, *, signer: str = "coldkey") -> Any:
        if self.refuse:
            raise RuntimeError(self.refuse)
        self.submitted.append((call, signer))

        class R:
            success = True
            extrinsic_id = "4242-3"

        return R()


class TestReads(unittest.TestCase):
    def test_a_metagraph_snapshot_from_the_sdk(self) -> None:
        st = SubtensorReader(FakeSubtensor(), 7).read()
        self.assertEqual(st.uid_by_hotkey, {OWNER: 0, ME: 1, A: 2})
        self.assertEqual(st.owner_hotkey, OWNER)
        self.assertEqual(st.by_hotkey(ME).stake, 10**12)  # type: ignore[union-attr]
        self.assertTrue(st.by_hotkey(ME).validator_permit)  # type: ignore[union-attr]
        self.assertAlmostEqual(st.kappa(), 0.5, places=4)

    def test_a_failed_read_keeps_the_last_snapshot(self) -> None:
        src = Fixed(state())
        live = LiveChain(src)
        src.fail = True
        self.assertFalse(live.sync())
        self.assertEqual(live.hotkeys, state().hotkeys)
        self.assertEqual(live.failures, 1)

    def test_a_file_metagraph_is_a_list_or_neurons(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps([OWNER, A]))
            self.assertEqual(FileReader(p).read().uid_by_hotkey, {OWNER: 0, A: 1})
            p.write_text(json.dumps({"neurons": [{"uid": 5, "hotkey": B}], "owner_hotkey": OWNER}))
            st = FileReader(p).read()
            self.assertEqual((st.uid_by_hotkey, st.owner_hotkey), ({B: 5}, OWNER))


class TestDominance(unittest.TestCase):
    def test_dominant_only_above_kappa_of_the_active_validators(self) -> None:
        d = dominance(state(), ME)
        self.assertTrue(d["dominant"])
        self.assertAlmostEqual(d["share"], 0.9)
        self.assertEqual(d["other_active_validators"], [OTHER_V])
        self.assertEqual(d["weights"], "commit-reveal")
        d = dominance(state(my_stake=100, other_stake=900), ME)
        self.assertFalse(d["dominant"])
        self.assertIn("not above kappa", d["why"])
        # a validator that stopped setting weights is not active: it does not dilute us
        d = dominance(state(my_stake=100, other_stake=900, other_last=1000), ME)
        self.assertTrue(d["dominant"])
        self.assertEqual(d["other_active_validators"], [])
        self.assertFalse(dominance(state(permit=False), ME)["dominant"])
        self.assertFalse(dominance(state(), C)["dominant"])  # not registered


class TestWeights(unittest.TestCase):
    def setUp(self) -> None:
        self.sub = FakeSubtensor()
        self.src = Fixed(state(block=10_000, my_last=9_950, rate=100))
        self.chain = LiveChain(self.src)
        self.calls: list[dict[str, Any]] = []

        def factory(**kw: Any) -> dict[str, Any]:
            self.calls.append(kw)
            return kw

        self.w = ChainWeights(
            self.sub, object(), self.chain, me=ME, call_factory=factory, stderr=io.StringIO()
        )

    def test_the_owner_and_our_own_uid_are_never_weighted(self) -> None:
        self.assertEqual(guard([(0, 9), (1, 9), (2, 7), (99, 5), (3, 0)], state(), me=ME), [(2, 7)])

    def test_the_rate_limit_is_respected_and_a_newer_vector_replaces_a_waiting_one(self) -> None:
        self.w.submit([(2, 65535), (3, 100)], hour=1)
        self.assertTrue(self.w.pump().startswith("rate limit: 50"))
        self.assertEqual(self.sub.executed, [])
        self.w.submit([(0, 65535), (2, 65535)], hour=2)  # the owner's uid is dropped
        self.src.st = state(block=10_060, my_last=9_950, rate=100)
        self.chain.sync()
        self.assertEqual(self.w.pump(), "set")
        self.assertEqual(
            self.calls, [{"netuid": 7, "uids": [2], "weights": [65535], "version_key": 3}]
        )
        self.assertIsNone(self.w.pending)
        self.assertEqual(self.w.pump(), "nothing pending")
        # just set: the next vector waits a full rate limit again
        self.w.submit([(2, 1)], hour=3)
        self.assertTrue(self.w.pump().startswith("rate limit"))

    def test_a_refusal_keeps_the_vector_pending(self) -> None:
        self.src.st = state(block=20_000, my_last=9_950)
        self.chain.sync()
        self.w.submit([(2, 65535)], hour=1)
        self.sub.refuse = "NotEnoughStakeToSetWeights"
        self.assertEqual(self.w.pump(), "refused")
        self.assertIsNotNone(self.w.pending)
        self.sub.refuse = ""
        self.assertEqual(self.w.pump(), "set")

    def test_the_sdk_intent_picks_the_commit_reveal_path(self) -> None:
        """No factory: the real ``bt.SetWeights`` intent is built (and handed to the fake client,
        which writes nothing). Built with the subnet's weights version."""
        import bittensor as bt

        self.src.st = state(block=20_000, my_last=9_950)
        self.chain.sync()
        w = ChainWeights(self.sub, object(), self.chain, me=ME, stderr=io.StringIO())
        w.submit([(2, 65535), (3, 7)], hour=1)
        self.assertEqual(w.pump(), "set")
        intent = self.sub.executed[-1]
        self.assertIsInstance(intent, bt.SetWeights)
        self.assertEqual((intent.netuid, list(intent.uids), intent.version_key), (7, [2, 3], 3))

    def test_nothing_to_weight_sets_nothing(self) -> None:
        self.w.submit([(0, 65535)], hour=1)  # only the owner: dropped, nothing left
        self.assertIsNone(self.w.pending)
        out = io.StringIO()
        dry = DryRunWeights(out)
        dry.submit([(2, 5)], hour=4)
        self.assertIn("dry run", out.getvalue())


class TestCommitment(unittest.TestCase):
    def test_the_report_hash_is_committed_by_the_hotkey_and_retried(self) -> None:
        sub, got = FakeSubtensor(), []
        c = ChainCommitter(
            sub,
            object(),
            7,
            call_factory=lambda n, sha: ("set_commitment", n, sha),
            on_committed=lambda sha, block: got.append((sha, block)),
            stderr=io.StringIO(),
        )
        sub.refuse = "SpaceLimitExceeded"
        c.submit("ab" * 32, hour=5)
        self.assertEqual(c.pump(), "refused")
        sub.refuse = ""
        self.assertEqual(c.pump(), "committed")
        self.assertEqual(sub.submitted, [(("set_commitment", 7, "ab" * 32), "hotkey")])
        self.assertEqual(got, [("ab" * 32, 4242)])

    def test_the_commitment_call_is_one_sha256_field(self) -> None:
        call = commitment_call(7, "AB" * 32)
        self.assertEqual((call.module, call.function), ("Commitments", "set_commitment"))
        self.assertEqual(call.params["info"], {"fields": [{"Sha256": "0x" + "ab" * 32}]})


class TestReports(unittest.TestCase):
    def test_reports_chain_by_hash_and_the_auditor_catches_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(report_dir=Path(d))
            uids = {OWNER: 0, A: 1, B: 2}
            ledger.add(passing("1", A, ended_ms=1000))
            r1 = ledger.report(2000, uids, owner_hotkey=OWNER, admitted=[A], reachable=[A])
            ledger.add(passing("2", B, ended_ms=HOUR_MS))
            r2 = ledger.report(
                2 * HOUR_MS, uids, owner_hotkey=OWNER, admitted=[A, B], reachable=[A, B]
            )
            self.assertEqual(r1["prev_sha256"], "")
            self.assertEqual(r2["prev_sha256"], r1["sha256"])
            p1, p2 = Path(d) / "hour-0.json", Path(d) / "hour-2.json"
            self.assertEqual(hashlib.sha256(p2.read_bytes()).hexdigest(), r2["sha256"])
            self.assertEqual(p2.read_bytes(), report_bytes(json.loads(p2.read_bytes())))
            self.assertTrue(audit([p1, p2], expect_sha256=r2["sha256"])["ok"])
            self.assertEqual(recompute_main([str(p1), str(p2), "--expect-sha256", r2["sha256"]]), 0)
            self.assertFalse(audit([p1, p2], expect_sha256="00" * 32)["ok"])
            # a changed score in an old report breaks the chain at the next one
            rep = json.loads(p1.read_bytes())
            rep["jobs"][0]["s"] = 0.5
            p1.write_bytes(report_bytes(rep))
            out = audit([p1, p2])
            self.assertFalse(out["ok"])
            self.assertIn("chain_break", out["reports"][1])

    def test_two_reports_in_one_hour_are_both_kept_and_chained(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(report_dir=Path(d))
            uids = {OWNER: 0, A: 1}
            r1 = ledger.report(10, uids, owner_hotkey=OWNER, admitted=[A], reachable=[A])
            r2 = ledger.report(20, uids, owner_hotkey=OWNER, admitted=[A], reachable=[A])
            names = sorted(p.name for p in Path(d).glob("*.json"))
            self.assertEqual(names, ["hour-0-1.json", "hour-0.json"])
            self.assertEqual(r2["prev_sha256"], r1["sha256"])
            paths = [Path(d) / "hour-0.json", Path(d) / "hour-0-1.json"]
            self.assertTrue(audit(paths, expect_sha256=r2["sha256"])["ok"])

    def test_an_hour_with_no_work_still_pays_miners_from_history(self) -> None:
        ledger = Ledger()
        ledger.add(passing("1", A, ended_ms=0))
        ledger.add(JobOutcome(job="2", hotkey=B, ended_ms=0, deliverable=False, kind="timeout"))
        rep = ledger.report(
            30 * HOUR_MS,
            {OWNER: 0, A: 1, B: 2},
            owner_hotkey=OWNER,
            admitted=[A, B],
            reachable=[A, B],
        )
        self.assertEqual((rep["vector"], rep["rule"]), ([[1, 65535]], "history"))
        # genesis: admitted AND registered AND reachable, never the owner
        rep = Ledger().report(
            0,
            {OWNER: 0, A: 1, B: 2},
            owner_hotkey=OWNER,
            admitted=[A, C, OWNER],  # C is not registered
            reachable=[A, C, OWNER],
            proven=[A, C, OWNER],
        )
        self.assertEqual(rep["vector"], [[1, 65535]])
        self.assertTrue(rep["rule"].startswith("genesis"))

    def test_the_reports_are_public_and_the_rest_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "s.sqlite3")
            ledger = Ledger(report_dir=Path(d) / "reports", store=store)
            rep = ledger.report(0, {A: 1}, owner_hotkey=OWNER, admitted=[], reachable=[])
            store.put("commit", rep["sha256"], {"block": 77})
            desk = Desk(FakePlatform(), ledger, registered=lambda: [A], store=store)
            server = serve(
                desk,
                "127.0.0.1",
                0,
                token="t" * 32,
                validator_hotkey=ME,
                verify=btauth.verify,
                close_hour=lambda: None,
                reports_dir=Path(d) / "reports",
                chain_info=lambda: {"dominant": False},
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/v1/reports", timeout=5) as r:  # noqa: S310
                    index = json.loads(r.read())["reports"]
                self.assertEqual(
                    index,
                    [{"report": "hour-0.json", "sha256": rep["sha256"], "committed_block": 77}],
                )
                with urllib.request.urlopen(base + "/v1/reports/hour-0.json", timeout=5) as r:  # noqa: S310
                    self.assertEqual(hashlib.sha256(r.read()).hexdigest(), rep["sha256"])
                for path in ("/v1/reports/../s.sqlite3", "/v1/scores", "/v1/chain"):
                    with self.assertRaises(urllib.error.HTTPError) as e:
                        urllib.request.urlopen(base + path, timeout=5)  # noqa: S310
                    self.assertIn(e.exception.code, (401, 404))
            finally:
                server.shutdown()
                desk.stop()


if __name__ == "__main__":
    unittest.main()
