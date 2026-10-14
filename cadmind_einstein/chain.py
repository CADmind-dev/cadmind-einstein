"""The einstein validator's chain (DESIGN.md sections 5, 7, 10): metagraph reads, our stake
dominance, weight setting and the hourly report's commitment -- bittensor 11, duck-typed.

**Reads** (free): :class:`SubtensorReader` takes one metagraph snapshot (``subtensor.subnets.
metagraph``: uid, hotkey, total stake, validator permit, last update per neuron, the subnet owner's
hotkey) and the subnet's hyperparameters (``kappa``, ``activity_cutoff``, ``weights_rate_limit``,
``commit_reveal_weights_enabled``, ``commit_reveal_period``, ``weights_version``) at once.
:class:`FileReader` is a local stack's fixed metagraph. :class:`LiveChain` keeps the newest snapshot
and swaps it whole on ``sync()``; a failed read keeps the previous one, so a chain hiccup never
deregisters everyone.

**Stake dominance** (:func:`dominance`): there is no consensus-mode setting on this chain. Our row
is the consensus only while our hotkey holds more than ``kappa`` of the stake of the validators that
set weights (a validator permit and a weight set within ``activity_cutoff`` blocks). That is
computed from the snapshot, logged, and published in every report as it is -- the validator never
claims a dominance it does not have.

**Writes** (only without ``--dry-run-weights``):

- :class:`ChainWeights` sets the hour's vector with ``bt.SetWeights``, which takes the plain path or
  the timelocked commit-reveal path by what the subnet has enabled. It respects
  ``weights_rate_limit`` (blocks since our last update, from the snapshot): a vector that may not
  be set yet waits and is retried, a newer vector replacing it. The subnet owner's hotkey and our
  own uid are dropped from any vector at this last step too, whatever the scorer said: the subnet
  owner's hotkey is never weighted.
- :class:`ChainCommitter` commits each hourly report's SHA-256 with the Commitments pallet's
  ``set_commitment`` (one ``Sha256`` field), signed by our hotkey; the fee is charged to its
  coldkey. Only the newest commitment is kept in chain state, which is why each report names the
  previous report's hash: the newest commitment authenticates the whole published chain.

:class:`DryRunWeights` and :class:`DryRunCommitter` log what they would have sent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from . import __spec_version__

DEFAULT_KAPPA = 0.5
DEFAULT_ACTIVITY_CUTOFF = 5000


@dataclass(frozen=True)
class Neuron:
    uid: int
    hotkey: str
    stake: float = 0.0
    validator_permit: bool = False
    last_update: int = 0
    coldkey: str = ""


@dataclass(frozen=True)
class ChainState:
    netuid: int
    block: int
    neurons: tuple[Neuron, ...]
    owner_hotkey: str = ""
    hyper: Mapping[str, Any] = field(default_factory=dict)

    @property
    def hotkeys(self) -> list[str]:
        return [n.hotkey for n in self.neurons]

    @property
    def uid_by_hotkey(self) -> dict[str, int]:
        return {n.hotkey: n.uid for n in self.neurons}

    @property
    def coldkey_by_hotkey(self) -> dict[str, str]:
        return {n.hotkey: n.coldkey for n in self.neurons}

    def by_hotkey(self, hotkey: str) -> Neuron | None:
        return next((n for n in self.neurons if n.hotkey == hotkey), None)

    def kappa(self) -> float:
        raw = self.hyper.get("kappa")
        if raw is None:
            return DEFAULT_KAPPA
        k = float(raw)
        return k / 65_535 if k > 1 else k

    def weights_rate_limit(self) -> int:
        return int(self.hyper.get("weights_rate_limit") or 0)

    def commit_reveal(self) -> bool:
        return bool(self.hyper.get("commit_reveal_weights_enabled"))


def _stake(value: Any) -> float:
    """A metagraph stake (a bittensor ``Balance`` in the subnet's alpha) as a plain number; only
    ratios are read."""
    rao = getattr(value, "rao", None)
    return float(rao) if rao is not None else float(value or 0)


class SubtensorReader:
    """One snapshot of the subnet from a bittensor 11 client (``bt.Subtensor``)."""

    def __init__(self, subtensor: Any, netuid: int) -> None:
        self.subtensor, self.netuid = subtensor, netuid

    def read(self) -> ChainState:
        mg = self.subtensor.subnets.metagraph(self.netuid, commitments=False)
        if mg is None:
            raise RuntimeError(f"netuid {self.netuid} does not exist")
        hyper = self.subtensor.subnets.subnet_hyperparameters(self.netuid) or {}
        return ChainState(
            netuid=self.netuid,
            block=int(mg.block),
            neurons=tuple(
                Neuron(
                    uid=int(n.uid),
                    hotkey=str(n.hotkey),
                    stake=_stake(n.total_stake),
                    validator_permit=bool(n.validator_permit),
                    last_update=int(n.last_update or 0),
                    coldkey=str(getattr(n, "coldkey", "") or ""),
                )
                for n in mg.neurons
            ),
            owner_hotkey=str(mg.owner_hotkey or ""),
            hyper=dict(hyper),
        )


class FileReader:
    """A local stack's metagraph: a JSON list of hotkeys (uid = position), or ``{"neurons":
    [{uid, hotkey, stake, validator_permit, last_update, coldkey}], "block", "owner_hotkey",
    "hyper"}``.
    Re-read on every sync, so a test can deregister a hotkey by editing the file."""

    def __init__(self, path: Path, netuid: int = 0) -> None:
        self.path, self.netuid = Path(path), netuid

    def read(self) -> ChainState:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            data = {"neurons": [{"uid": i, "hotkey": h} for i, h in enumerate(data)]}
        return ChainState(
            netuid=self.netuid,
            block=int(data.get("block") or 0),
            neurons=tuple(Neuron(**n) for n in data.get("neurons") or []),
            owner_hotkey=str(data.get("owner_hotkey") or ""),
            hyper=dict(data.get("hyper") or {}),
        )


class LiveChain:
    """The newest snapshot; ``sync()`` swaps it whole, and a failed read keeps the old one."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.state: ChainState = reader.read()
        self.failures = 0

    def sync(self) -> bool:
        try:
            state = self.reader.read()
        except Exception:  # noqa: BLE001 -- the chain boundary: keep the previous snapshot
            self.failures += 1
            return False
        self.state, self.failures = state, 0
        return True

    @property
    def hotkeys(self) -> list[str]:
        return self.state.hotkeys


def dominance(state: ChainState, me: str) -> dict[str, Any]:
    """Our share of the stake of the validators that set weights, against ``kappa``.

    Active = holds a validator permit and set (or committed) weights within ``activity_cutoff``
    blocks; we always count ourselves. ``dominant`` only when we are registered, hold a permit and
    our share is above ``kappa``: then our row is the consensus under Yuma.
    """
    kappa = state.kappa()
    cutoff = int(state.hyper.get("activity_cutoff") or DEFAULT_ACTIVITY_CUTOFF)
    mine = state.by_hotkey(me)
    active = [
        n
        for n in state.neurons
        if n.hotkey == me
        or (n.validator_permit and n.stake > 0 and state.block - n.last_update <= cutoff)
    ]
    total = sum(n.stake for n in active)
    share = (mine.stake / total) if mine is not None and total > 0 else 0.0
    others = sorted(n.hotkey for n in active if n.hotkey != me)
    dominant = bool(mine is not None and mine.validator_permit and share > kappa)
    if mine is None:
        why = "our hotkey is not registered on the subnet"
    elif not mine.validator_permit:
        why = "our hotkey holds no validator permit"
    elif not dominant:
        why = f"our share {share:.3f} of the active validator stake is not above kappa {kappa:.3f}"
    else:
        why = f"our share {share:.3f} of the active validator stake is above kappa {kappa:.3f}"
    return {
        "block": state.block,
        "netuid": state.netuid,
        "validator": me,
        "registered": mine is not None,
        "validator_permit": bool(mine and mine.validator_permit),
        "share": round(share, 6),
        "kappa": round(kappa, 6),
        "dominant": dominant,
        "other_active_validators": others,
        "why": why,
        "weights": "commit-reveal" if state.commit_reveal() else "plain",
        "weights_rate_limit": state.weights_rate_limit(),
    }


def _log_to(stream: IO[str] | None) -> Callable[[str], None]:
    """The ``cadmind_einstein.chain`` logger, or (tests) lines written to ``stream``."""
    if stream is None:
        return logging.getLogger("cadmind_einstein.chain").info
    out = stream

    def log(line: str) -> None:
        out.write(f"[cadmind-einstein] {line}\n")
        out.flush()

    return log


def guard(vector: Sequence[Sequence[int]], state: ChainState, *, me: str) -> list[tuple[int, int]]:
    """The vector as it may go on chain: never the subnet owner's uid, never our own, only
    registered uids, only positive weights."""
    banned = {n.uid for n in state.neurons if n.hotkey in (state.owner_hotkey, me) and n.hotkey}
    known = {n.uid for n in state.neurons}
    return [
        (int(uid), int(w))
        for uid, w in vector
        if int(uid) in known and int(uid) not in banned and int(w) > 0
    ]


class DryRunWeights:
    """Logs the vector it would set; sets nothing (``--dry-run-weights``)."""

    def __init__(self, stderr: IO[str] | None = None) -> None:
        self.log = _log_to(stderr)
        self.pending: list[tuple[int, int]] | None = None
        self.last: list[tuple[int, int]] = []

    def submit(self, vector: Sequence[Sequence[int]], *, hour: int) -> None:
        self.last = [(int(u), int(w)) for u, w in vector]
        self.log(f"weights (dry run) for hour {hour}: {[list(x) for x in self.last]}")

    def pump(self) -> str:
        return "dry run"


class ChainWeights:
    """Sets the vector on chain through ``bt.SetWeights``, within the weights rate limit.

    ``submit`` makes a vector pending (a newer one replaces it); ``pump`` -- called every loop --
    sends it once our uid's last update is ``weights_rate_limit`` blocks old (read from the live
    snapshot, re-read after a set). A refusal keeps it pending and is logged once per distinct
    reason. ``current_block`` defaults to the snapshot's block (the snapshot is re-read every few
    minutes); pass the client's block read for a fresher answer.

    The weights carry ``version_key`` -- the package's ``__spec_version__`` -- so the subnet's
    ``weights_version`` hyperparameter can require a minimum validator release; when it requires
    more than this code is, a WARNING says so (the chain refuses the weights).
    """

    def __init__(
        self,
        subtensor: Any,
        wallet: Any,
        chain: LiveChain,
        *,
        me: str,
        call_factory: Callable[..., Any] | None = None,
        current_block: Callable[[], int] | None = None,
        stderr: IO[str] | None = None,
        version_key: int = __spec_version__,
    ) -> None:
        self.subtensor, self.wallet, self.chain, self.me = subtensor, wallet, chain, me
        self.version_key = version_key
        self.call_factory = call_factory
        self.current_block = current_block
        self.log = _log_to(stderr)
        self.pending: list[tuple[int, int]] | None = None
        self.pending_hour = -1
        self.last: list[tuple[int, int]] = []
        self.last_set_block = -1
        self._said = ""

    def _say(self, line: str) -> None:
        if line != self._said:
            self._said = line
            self.log(line)

    def submit(self, vector: Sequence[Sequence[int]], *, hour: int) -> None:
        self.pending = guard(vector, self.chain.state, me=self.me)
        self.pending_hour = hour
        if not self.pending:
            self.pending = None
            self._say(f"hour {hour}: nothing to weight; no weights set")

    def _call(self, netuid: int, uids: list[int], weights: list[int], version: int) -> Any:
        if self.call_factory is not None:
            return self.call_factory(netuid=netuid, uids=uids, weights=weights, version_key=version)
        import bittensor as bt  # noqa: PLC0415 -- the SDK stays out of the import graph

        return bt.SetWeights(netuid=netuid, uids=uids, weights=weights, version_key=version)

    def pump(self) -> str:
        """Try the pending vector; answers what happened, in words."""
        if self.pending is None:
            return "nothing pending"
        state = self.chain.state
        mine = state.by_hotkey(self.me)
        if mine is None:
            self._say("weights not set: our hotkey is not registered on the subnet")
            return "not registered"
        block = self.current_block() if self.current_block is not None else state.block
        last = max(mine.last_update, self.last_set_block)
        wait = state.weights_rate_limit() - (block - last)
        if wait > 0:
            return f"rate limit: {wait} block(s) to wait"
        vec = guard(self.pending, state, me=self.me)
        uids, weights = [u for u, _ in vec], [w for _, w in vec]
        version = self.version_key
        required = int(state.hyper.get("weights_version") or 0)
        if required > version:
            self._say(
                f"WARNING: the subnet requires weights version {required} but this validator is "
                f"spec {version}: the chain will refuse its weights -- update the validator"
            )
        path = "commit-reveal" if state.commit_reveal() else "plain"
        try:
            result = self.subtensor.execute(
                self._call(state.netuid, uids, weights, version), self.wallet
            )
        except Exception as e:  # noqa: BLE001 -- the chain boundary: kept pending, retried
            self._say(f"set_weights ({path}) refused: {type(e).__name__}: {str(e)[:300]}")
            return "refused"
        ok = getattr(result, "success", result)
        if isinstance(ok, tuple):
            ok = ok[0]
        if not ok:
            self._say(f"set_weights ({path}) not accepted: {str(result)[:300]}")
            return "not accepted"
        self.last, self.pending, self.last_set_block = vec, None, block
        self._said = ""
        self.log(f"weights set ({path}) for hour {self.pending_hour} at block ~{block}: {vec}")
        return "set"


class DryRunCommitter:
    """Logs the report hash it would commit (``--dry-run-weights``)."""

    def __init__(self, stderr: IO[str] | None = None) -> None:
        self.log = _log_to(stderr)
        self.pending: str = ""

    def submit(self, sha256: str, *, hour: int) -> None:
        self.log(f"report hour {hour}: sha256 {sha256} (dry run: not committed)")

    def pump(self) -> str:
        return "dry run"


def commitment_call(netuid: int, sha256: str) -> Any:
    """``Commitments.set_commitment`` with one ``Sha256`` field (composed and checked against the
    testnet runtime, spec 477)."""
    import bittensor as bt  # noqa: PLC0415

    return bt.calls.Commitments.set_commitment(
        netuid, {"fields": [{"Sha256": "0x" + sha256.lower()}]}
    )


class ChainCommitter:
    """Commits each report's SHA-256 on chain, signed by our hotkey; a failure keeps the newest
    hash pending and retries (the hash chain makes a skipped hour harmless: the next commitment
    covers it)."""

    def __init__(
        self,
        subtensor: Any,
        wallet: Any,
        netuid: int,
        *,
        call_factory: Callable[[int, str], Any] = commitment_call,
        on_committed: Callable[[str, int], None] | None = None,
        stderr: IO[str] | None = None,
    ) -> None:
        self.subtensor, self.wallet, self.netuid = subtensor, wallet, netuid
        self.call_factory, self.on_committed = call_factory, on_committed
        self.log = _log_to(stderr)
        self.pending, self.pending_hour = "", -1
        self._said = ""

    def submit(self, sha256: str, *, hour: int) -> None:
        self.pending, self.pending_hour = sha256, hour

    def pump(self) -> str:
        if not self.pending:
            return "nothing pending"
        try:
            result = self.subtensor.submit_call(
                self.call_factory(self.netuid, self.pending), self.wallet, signer="hotkey"
            )
        except Exception as e:  # noqa: BLE001 -- the chain boundary: kept pending, retried
            line = f"report commitment refused: {type(e).__name__}: {str(e)[:300]}"
            if line != self._said:
                self._said = line
                self.log(line)
            return "refused"
        ok = getattr(result, "success", result)
        if not ok:
            self.log(f"report commitment not accepted: {str(result)[:300]}")
            return "not accepted"
        ext = str(getattr(result, "extrinsic_id", "") or "")
        block = int(ext.split("-", maxsplit=1)[0]) if ext.split("-", maxsplit=1)[0].isdigit() else 0
        self.log(
            f"report hour {self.pending_hour}: sha256 {self.pending} committed (block {block})"
        )
        if self.on_committed is not None:
            self.on_committed(self.pending, block)
        self.pending, self._said = "", ""
        return "committed"
