"""Command-line conventions shared by the programs: Bittensor-style flags and logging.

Every program accepts the flag names Bittensor tooling uses (``--wallet.name``, ``--wallet.hotkey``,
``--wallet.path``, ``--subtensor.network``, ``--subtensor.chain_endpoint``, ``--netuid``,
``--logging.debug``, ``--logging.trace``) and, as aliases with the same meaning, the dashed names
earlier releases used (``--wallet-name``, ``--wallet-hotkey``, ``--network``): a deployment's
existing command line keeps working unchanged.

bittensor 11 removed the SDK's argparse helpers (``Wallet.add_args``, ``Subtensor.add_args``) and
``bittensor.logging``; its own log lines go to the standard ``logging`` module under the
``bittensor`` logger. So these flags are declared here with ``argparse``, and the programs log
through ``logging`` too: :func:`setup_logging` prints each line with the program's prefix, exactly
as before, and ``--logging.debug`` / ``--logging.trace`` raise the level of our loggers and of the
SDK's.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import IO, Any

from . import __spec_version__, __version__

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def add_version_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__} (spec {__spec_version__})",
    )
    ap.add_argument(
        "--no-version-check",
        action="store_true",
        help="do not read the latest GitHub release at startup and daily (it only warns)",
    )


def add_wallet_args(ap: argparse.ArgumentParser, *, help_name: str = "") -> None:
    """``--wallet.name`` / ``--wallet-name``, ``--wallet.hotkey`` / ``--wallet-hotkey`` and
    ``--wallet.path`` (default: bittensor's, ``~/.bittensor/wallets``)."""
    ap.add_argument(
        "--wallet.name",
        "--wallet-name",
        dest="wallet_name",
        help=help_name or "the bittensor wallet (coldkey) name",
    )
    ap.add_argument(
        "--wallet.hotkey", "--wallet-hotkey", dest="wallet_hotkey", help="the hotkey's name"
    )
    ap.add_argument(
        "--wallet.path",
        dest="wallet_path",
        help="the wallets directory (default: bittensor's)",
    )


def add_subtensor_args(
    ap: argparse.ArgumentParser, *, network_default: str = "test", netuid_default: int = 0
) -> None:
    """``--subtensor.network`` / ``--network``, ``--subtensor.chain_endpoint`` and ``--netuid``."""
    ap.add_argument(
        "--subtensor.network",
        "--network",
        dest="network",
        default=network_default,
        help="finney, test, local, ... or a ws(s):// URL"
        + (f" (default: {network_default})" if network_default else ""),
    )
    ap.add_argument(
        "--subtensor.chain_endpoint",
        dest="chain_endpoint",
        default="",
        help="a ws(s):// node endpoint; overrides --subtensor.network",
    )
    ap.add_argument("--netuid", type=int, default=netuid_default, help="the subnet's netuid")


def add_logging_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_mutually_exclusive_group()
    g.add_argument(
        "--logging.debug",
        dest="logging_debug",
        action="store_true",
        help="debug lines from this program and the bittensor SDK",
    )
    g.add_argument(
        "--logging.trace",
        dest="logging_trace",
        action="store_true",
        help="everything, including every library's lowest-level lines",
    )


def network_of(a: argparse.Namespace) -> str:
    """The network bittensor connects to: the chain endpoint when given, else the network name."""
    return str(getattr(a, "chain_endpoint", "") or getattr(a, "network", "") or "")


def wallet_kwargs(a: argparse.Namespace) -> dict[str, Any]:
    """``bt.Wallet`` keyword arguments from the wallet flags (unset ones keep bittensor's
    defaults)."""
    kw: dict[str, Any] = {"name": a.wallet_name, "hotkey": a.wallet_hotkey}
    if getattr(a, "wallet_path", None):
        kw["path"] = a.wallet_path
    return {k: v for k, v in kw.items() if v is not None}


class _Prefixed(logging.Formatter):
    def __init__(self, prefix: str) -> None:
        super().__init__("%(message)s")
        self.prefix = prefix

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if record.levelno < logging.INFO:
            line = f"{record.levelname} {record.name}: {line}"
        return f"{self.prefix} {line}" if self.prefix else line


def setup_logging(
    a: argparse.Namespace | None, *, prefix: str, stream: IO[str] | None = None
) -> logging.Logger:
    """Send ``cadmind_einstein`` and ``bittensor`` log records to ``stream`` (stderr by default),
    each line as ``<prefix> <message>``; INFO by default, DEBUG with ``--logging.debug``, every
    logger at TRACE with ``--logging.trace``. Answers the ``cadmind_einstein`` logger."""
    debug = bool(a is not None and getattr(a, "logging_debug", False))
    trace = bool(a is not None and getattr(a, "logging_trace", False))
    level = TRACE if trace else logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(_Prefixed(prefix))
    ours = logging.getLogger("cadmind_einstein")
    for name in ("cadmind_einstein", "bittensor"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            if getattr(h, "_cadmind_einstein", False):
                lg.removeHandler(h)
        lg.setLevel(level if name == "cadmind_einstein" or debug or trace else logging.WARNING)
    handler._cadmind_einstein = True  # type: ignore[attr-defined]  # noqa: SLF001 -- our marker
    ours.addHandler(handler)
    ours.propagate = False
    sdk = logging.getLogger("bittensor")
    sdk_handler = logging.StreamHandler(handler.stream)
    sdk_handler.setFormatter(_Prefixed(prefix))
    sdk_handler._cadmind_einstein = True  # type: ignore[attr-defined]  # noqa: SLF001
    sdk.addHandler(sdk_handler)
    sdk.propagate = False
    if trace:
        root = logging.getLogger()
        root.setLevel(TRACE)
        if not root.handlers:
            root.addHandler(sdk_handler)
    return ours
