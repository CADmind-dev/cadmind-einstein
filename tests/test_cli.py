"""The command lines: Bittensor-style flags, the earlier dashed names as aliases, logging, and the
spec version and release check."""

from __future__ import annotations

import io
import logging
import unittest
from unittest import mock

from cadmind_einstein import __spec_version__, __version__, _spec, agent_miner, daemon, scoring
from cadmind_einstein.cli import network_of, setup_logging, wallet_kwargs
from cadmind_einstein.version import check_latest, spec_of

DAEMON_BASE = [
    "--state-dir",
    "S",
    "--token-file",
    "T",
    "--platform",
    "https://platform.example",
    "--operator-key-file",
    "K",
]
MINER_BASE = [
    "--validator",
    "https://validator.example",
    "--validator-hotkey",
    "VHK",
    "--platform",
    "https://platform.example",
    "--state",
    "S",
]


class TestDaemonFlags(unittest.TestCase):
    def parse(self, *extra: str):
        return daemon.parser().parse_args([*DAEMON_BASE, *extra])

    def test_the_dashed_flags_of_a_running_deployment_still_work(self) -> None:
        a = self.parse(
            "--wallet-name", "vw", "--wallet-hotkey", "vh", "--network", "test", "--netuid", "9"
        )
        self.assertEqual(
            (a.wallet_name, a.wallet_hotkey, a.network, a.netuid), ("vw", "vh", "test", 9)
        )
        self.assertEqual(network_of(a), "test")
        self.assertEqual(wallet_kwargs(a), {"name": "vw", "hotkey": "vh"})

    def test_bittensor_style_flags_mean_the_same(self) -> None:
        dotted = self.parse(
            "--wallet.name", "vw", "--wallet.hotkey", "vh", "--subtensor.network", "test",
            "--netuid", "9",
        )  # fmt: skip
        dashed = self.parse(
            "--wallet-name", "vw", "--wallet-hotkey", "vh", "--network", "test", "--netuid", "9"
        )
        self.assertEqual(vars(dotted), vars(dashed))

    def test_chain_endpoint_overrides_the_network_and_wallet_path_is_passed(self) -> None:
        a = self.parse(
            "--subtensor.network", "finney",
            "--subtensor.chain_endpoint", "ws://127.0.0.1:9944",
            "--wallet.name", "vw", "--wallet.hotkey", "vh", "--wallet.path", "/wallets",
        )  # fmt: skip
        self.assertEqual(network_of(a), "ws://127.0.0.1:9944")
        self.assertEqual(wallet_kwargs(a), {"name": "vw", "hotkey": "vh", "path": "/wallets"})

    def test_defaults_are_unchanged(self) -> None:
        a = self.parse()
        self.assertEqual(
            (a.network, a.netuid, a.wallet_name, a.wallet_hotkey), ("test", 0, None, None)
        )
        self.assertEqual(wallet_kwargs(a), {})  # bittensor's own defaults
        self.assertFalse(a.logging_debug or a.logging_trace or a.no_version_check)

    def test_logging_flags(self) -> None:
        self.assertTrue(self.parse("--logging.debug").logging_debug)
        self.assertTrue(self.parse("--logging.trace").logging_trace)
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            self.parse("--logging.debug", "--logging.trace")

    def test_version_flag(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            self.parse("--version")
        self.assertIn(f"{__version__} (spec {__spec_version__})", out.getvalue())


class TestMinerFlags(unittest.TestCase):
    def test_both_styles(self) -> None:
        p = agent_miner.parser()
        dashed = p.parse_args([*MINER_BASE, "--wallet-name", "mw", "--wallet-hotkey", "m1"])
        dotted = p.parse_args([*MINER_BASE, "--wallet.name", "mw", "--wallet.hotkey", "m1"])
        self.assertEqual(vars(dashed), vars(dotted))
        self.assertEqual(wallet_kwargs(dotted), {"name": "mw", "hotkey": "m1"})
        self.assertEqual(dotted.netuid, 0)  # no --netuid: no chain call at startup

    def test_netuid_and_network(self) -> None:
        a = agent_miner.parser().parse_args(
            [*MINER_BASE, "--wallet.name", "mw", "--wallet.hotkey", "m1",
             "--netuid", "9", "--subtensor.network", "test"]
        )  # fmt: skip
        self.assertEqual((a.netuid, network_of(a)), (9, "test"))

    def test_a_wallet_is_still_required(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit):
            agent_miner.main([*MINER_BASE])
        self.assertIn("--wallet.name and --wallet.hotkey", err.getvalue())


class TestRecomputeFlags(unittest.TestCase):
    def test_both_network_flags_read_the_commitment(self) -> None:
        for flag in ("--network", "--subtensor.network"):
            with (
                mock.patch.object(scoring, "onchain_sha256", return_value="") as read,
                mock.patch("sys.stderr", io.StringIO()),
            ):
                code = scoring.main(
                    ["hour-1.json", flag, "test", "--netuid", "9", "--validator-hotkey", "VHK"]
                )
            self.assertEqual(code, 1)
            read.assert_called_once_with("test", 9, "VHK", None)


class TestLogging(unittest.TestCase):
    def tearDown(self) -> None:
        setup_logging(None, prefix="[t]", stream=io.StringIO())

    def test_lines_keep_the_program_prefix_and_debug_is_off_by_default(self) -> None:
        out = io.StringIO()
        setup_logging(
            daemon.parser().parse_args(DAEMON_BASE), prefix="[cadmind-einstein]", stream=out
        )
        daemon._log("operator roles at startup: ok")
        logging.getLogger("cadmind_einstein.daemon").debug("hidden")
        self.assertEqual(out.getvalue(), "[cadmind-einstein] operator roles at startup: ok\n")

    def test_debug_shows_debug_lines(self) -> None:
        out = io.StringIO()
        a = daemon.parser().parse_args([*DAEMON_BASE, "--logging.debug"])
        setup_logging(a, prefix="[cadmind-einstein]", stream=out)
        logging.getLogger("cadmind_einstein.daemon").debug("metagraph read")
        logging.getLogger("bittensor").debug("sdk line")
        self.assertIn("DEBUG cadmind_einstein.daemon: metagraph read", out.getvalue())
        self.assertIn("sdk line", out.getvalue())


class TestVersion(unittest.TestCase):
    def test_spec_encoding(self) -> None:
        self.assertEqual(_spec("0.2.0"), 200)
        self.assertEqual(_spec("1.12.3"), 11203)
        self.assertEqual(__spec_version__, _spec(__version__))
        self.assertEqual(spec_of("v1.0.0"), 10000)
        self.assertIsNone(spec_of("nightly"))
        with self.assertRaises(ValueError):
            _spec("0.100.0")

    def test_an_older_validator_is_warned(self) -> None:
        said: list[str] = []
        self.assertEqual(check_latest(lambda: "v9.0.0", say=said.append, current=200), "older")
        self.assertIn("v9.0.0", said[0])
        self.assertTrue(said[0].startswith("WARNING"))

    def test_current_or_unknown_says_nothing(self) -> None:
        said: list[str] = []
        self.assertEqual(check_latest(lambda: "v0.2.0", say=said.append, current=200), "current")
        self.assertEqual(check_latest(lambda: "garbage", say=said.append), "unknown")

        def down() -> str:
            raise OSError("offline")

        self.assertEqual(check_latest(down, say=said.append), "unknown")
        self.assertEqual(said, [])


if __name__ == "__main__":
    unittest.main()
