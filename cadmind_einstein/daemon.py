"""``cadmind-einstein``: the einstein validator (see DESIGN.md) -- job desk, router, gate, scores
from the clients' signals, hourly weights and hourly reports.

    python -m cadmind_einstein.daemon --state-dir DIR --token-file T \\
        --platform https://cadmind.ai --operator-key-file K \\
        --wallet-name v --wallet-hotkey h --network test --netuid N [--dry-run-weights]
    # a local stack: a development key and a fixed metagraph (always a dry run)
    python -m cadmind_einstein.daemon --state-dir DIR --token-file T \\
        --platform http://127.0.0.1:PORT --operator-key-file K \\
        --dev-uri //Validator --metagraph-file hotkeys.json

Every hour the report is written to ``STATE/reports/hour-N.json`` (canonical JSON, its SHA-256
beside it, each naming the previous one's), the vector is set on chain within the weights rate
limit (plain or commit-reveal, whichever the subnet runs) and the report's SHA-256 is committed
with ``set_commitment``. ``--dry-run-weights`` makes no chain write at all: both are logged. The
reports are served publicly at ``GET /v1/reports`` for ``cadmind-recompute``.

State lives in ``STATE/einstein.sqlite3`` (WAL): a restart -- mid-job or mid-hour -- loses nothing
(see :mod:`.store`). The metagraph is re-read every ``--sync-s``; a hotkey that left the subnet is
deregistered on the desk (key revoked, job reassigned). Our stake dominance is computed on every
read, logged when it changes, and published in each report.

Abuse (DESIGN.md sections 9 and 11): every tick the desk's :meth:`~.desk.Desk.enforce` disables a
miner whose copies, Watt suspensions, failed entry tests or rejection pattern cross the
``--abuse-*`` thresholds (each disable logged and listed in the next report's ``watch``). The
report's ``watch`` also carries observations used to calibrate the anti-abuse thresholds: every
copy comparison's similarity and containment, declined and out-of-reach endings, rejections,
failures and admitted hotkeys per coldkey.

Copies (``--copy-*``, :class:`~.similarity.CopyPolicy`): normalised features, containment of the
smaller output in the other (``--copy-containment`` 0.7) once both have ``--copy-min-features`` (8)
distinctive features, the normalised digest below that; the features of ``--copy-corpus`` projects
(public examples) and those common to ``--copy-common-coldkeys`` (3) other coldkeys are subtracted.
Second opinions (``--second-opinion-*``, :class:`~.desk.SecondOpinion`): a rejected or failed job
goes to another coldkey (``--second-opinion-wait-s`` 1800 before any other miner may take it), and
only ``--second-opinion-coldkeys`` (2) distinct coldkeys end it declined or out of reach. Genesis
(``--genesis``, :func:`~.scoring.vector`): ``delivered`` (default), ``coldkey`` or ``hotkey``.
Every answer on a miner's route is signed with the validator's hotkey.

The HTTP face listens on 127.0.0.1 by default; in production nginx stands in front (TLS, body
size, header and body timeouts, ``limit_conn``/``limit_req``) and is named with
``--trusted-proxy`` so its ``X-Real-IP`` is what the per-address limits count.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .chain import (
    ChainCommitter,
    ChainWeights,
    DryRunCommitter,
    DryRunWeights,
    FileReader,
    LiveChain,
    SubtensorReader,
    dominance,
)
from .desk import AbusePolicy, Desk, SecondOpinion
from .platform import OperatorClient
from .scoring import GENESIS, Ledger
from .service import serve
from .similarity import CopyPolicy, load_corpus
from .store import Store


def _log(line: str) -> None:
    print(f"[cadmind-einstein] {line}", file=sys.stderr, flush=True)  # noqa: T201 -- the daemon's log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cadmind-einstein")
    ap.add_argument("--state-dir", type=Path, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8793)
    ap.add_argument("--token-file", type=Path, required=True, help="the platform's Bearer token")
    ap.add_argument("--platform", required=True, help="the platform's base URL")
    ap.add_argument(
        "--queue-url", default="", help="the job queue's base URL (default: --platform)"
    )
    ap.add_argument(
        "--operator-key-file", type=Path, required=True, help="the validator's agent key"
    )
    ap.add_argument("--wallet-name")
    ap.add_argument("--wallet-hotkey")
    ap.add_argument("--dev-uri", help="a development key (//Validator): local runs only")
    ap.add_argument("--network", default="test")
    ap.add_argument("--netuid", type=int, default=0)
    ap.add_argument("--metagraph-file", type=Path, help="a local stack: JSON list of hotkeys")
    ap.add_argument("--owner-hotkey", default="", help="never weighted (default: the chain's)")
    ap.add_argument(
        "--dry-run-weights",
        action="store_true",
        help="no chain write: the vector and the report hash are logged, not set or committed",
    )
    ap.add_argument(
        "--max-inflight", type=int, default=8, help="jobs at once: the bench's capacity"
    )
    ap.add_argument("--workers", type=int, default=0, help="desk worker threads (default 2N+2)")
    ap.add_argument("--accept-s", type=float, default=120.0)
    ap.add_argument("--hour-s", type=int, default=3600)
    ap.add_argument("--sync-s", type=float, default=300.0, help="metagraph re-read interval")
    ap.add_argument(
        "--admission-brief-file", type=Path, help="the entry test's brief (default: built in)"
    )
    ap.add_argument("--abuse-copies", type=int, default=AbusePolicy.copies, help="0: off")
    ap.add_argument("--abuse-suspensions", type=int, default=AbusePolicy.suspensions, help="0: off")
    ap.add_argument(
        "--abuse-admission-failures",
        type=int,
        default=AbusePolicy.admission_failures,
        help="0: off",
    )
    ap.add_argument("--abuse-overturned", type=int, default=AbusePolicy.overturned, help="0: off")
    ap.add_argument("--abuse-rejections-min", type=int, default=AbusePolicy.rejections_min)
    ap.add_argument("--abuse-rejection-ratio", type=float, default=AbusePolicy.rejection_ratio)
    ap.add_argument(
        "--no-abuse-rejections",
        action="store_true",
        help="do not disable on the rejection telemetry's flag",
    )
    ap.add_argument("--abuse-disable-s", type=float, default=AbusePolicy.disable_s)
    ap.add_argument(
        "--trusted-proxy",
        action="append",
        default=[],
        help="a reverse proxy's address whose X-Real-IP is trusted (repeatable)",
    )
    ap.add_argument(
        "--copy-containment",
        type=float,
        default=CopyPolicy.containment,
        help="share of the smaller output's distinctive features found in the other: a copy",
    )
    ap.add_argument(
        "--copy-min-features",
        type=int,
        default=CopyPolicy.min_features,
        help="distinctive features both outputs need for containment; below, the digest decides",
    )
    ap.add_argument(
        "--copy-common-coldkeys",
        type=int,
        default=CopyPolicy.common_coldkeys,
        help="a feature in outputs of this many other coldkeys is common (0: off)",
    )
    ap.add_argument("--copy-window", type=int, default=CopyPolicy.window)
    ap.add_argument(
        "--copy-corpus",
        type=Path,
        action="append",
        default=[],
        help="public example projects (folders or export zips) whose features are subtracted",
    )
    ap.add_argument(
        "--second-opinion-coldkeys",
        type=int,
        default=SecondOpinion.coldkeys,
        help="distinct coldkeys whose rejections/failures end a job declined or out of reach",
    )
    ap.add_argument(
        "--second-opinion-wait-s",
        type=float,
        default=SecondOpinion.wait_s,
        help="how long a rejected/failed job waits for another coldkey's miner",
    )
    ap.add_argument(
        "--genesis",
        choices=GENESIS,
        default="delivered",
        help="who is weighted when no admitted miner has history",
    )
    ap.add_argument("--max-connections", type=int, default=64)
    ap.add_argument("--max-connections-per-ip", type=int, default=16)
    a = ap.parse_args(argv)

    a.state_dir.mkdir(parents=True, exist_ok=True)
    subtensor: Any = None
    if a.dev_uri:
        from bittensor.sp_core import Keypair  # noqa: PLC0415

        wallet: Any = Keypair.create_from_uri(a.dev_uri)
        me = str(wallet.ss58_address)
    else:
        import bittensor as bt  # noqa: PLC0415

        wallet = bt.Wallet(name=a.wallet_name, hotkey=a.wallet_hotkey)
        me = str(wallet.hotkey.ss58_address)
    if a.metagraph_file:
        chain = LiveChain(FileReader(a.metagraph_file, a.netuid))
    else:
        import bittensor as bt  # noqa: PLC0415

        subtensor = bt.Subtensor(network=a.network)
        chain = LiveChain(SubtensorReader(subtensor, a.netuid))
    dry = a.dry_run_weights or subtensor is None or bool(a.dev_uri)
    owner = a.owner_hotkey or chain.state.owner_hotkey
    excluded = {h for h in (owner, chain.state.owner_hotkey, me) if h}

    import bittensor.http_auth as btauth  # noqa: PLC0415

    store = Store(a.state_dir / "einstein.sqlite3")
    platform = OperatorClient(
        a.platform, a.operator_key_file.read_text().strip(), queue_url=a.queue_url
    )
    ledger = Ledger(report_dir=a.state_dir / "reports", hour_ms=a.hour_s * 1000, store=store)
    extra: dict[str, Any] = {}
    if a.admission_brief_file:
        extra["admission_brief"] = a.admission_brief_file.read_text(encoding="utf-8")
    abuse = AbusePolicy(
        copies=a.abuse_copies,
        suspensions=a.abuse_suspensions,
        admission_failures=a.abuse_admission_failures,
        rejections=not a.no_abuse_rejections,
        rejections_min=a.abuse_rejections_min,
        rejection_ratio=a.abuse_rejection_ratio,
        overturned=a.abuse_overturned,
        disable_s=a.abuse_disable_s,
    )
    _log(f"abuse policy: {abuse}")
    copy_policy = CopyPolicy(
        containment=a.copy_containment,
        min_features=a.copy_min_features,
        common_coldkeys=a.copy_common_coldkeys,
        window=a.copy_window,
    )
    corpus = load_corpus(a.copy_corpus)
    second = SecondOpinion(coldkeys=a.second_opinion_coldkeys, wait_s=a.second_opinion_wait_s)
    _log(
        f"copy policy: {copy_policy}, corpus {len(corpus)} feature(s); {second}; "
        f"genesis {a.genesis}"
    )
    desk = Desk(
        platform,
        ledger,
        registered=lambda: chain.hotkeys,
        accept_s=a.accept_s,
        max_inflight=a.max_inflight,
        store=store,
        workers=a.workers,
        excluded=excluded,
        abuse=abuse,
        log=_log,
        coldkeys=lambda: chain.state.coldkey_by_hotkey,
        copy_policy=copy_policy,
        copy_corpus=corpus,
        second_opinion=second,
        **extra,
    )

    def committed(sha: str, block: int) -> None:
        store.put("commit", sha, {"block": block, "at_ms": desk.now_ms()})

    weights: Any
    commits: Any
    if dry:
        weights, commits = DryRunWeights(), DryRunCommitter()
    else:
        weights = ChainWeights(subtensor, wallet, chain, me=me)
        commits = ChainCommitter(subtensor, wallet, a.netuid, on_committed=committed)

    said = {"dominance": ""}

    def chain_info() -> dict[str, Any]:
        dom = dominance(chain.state, me)
        dom["weights_mode"] = "dry-run" if dry else dom["weights"]
        dom["metagraph_failures"] = chain.failures
        return dom

    def say_dominance() -> None:
        dom = chain_info()
        line = (
            f"stake-dominant: {dom['why']}"
            if dom["dominant"]
            else f"NOT stake-dominant -- our weights are not the consensus: {dom['why']}"
        )
        if dom["other_active_validators"]:
            line += f"; {len(dom['other_active_validators'])} other validator(s) set weights"
        if line != said["dominance"]:
            said["dominance"] = line
            _log(line)

    def close() -> dict[str, Any]:
        watch = desk.watch(chain.state.coldkey_by_hotkey)
        rep = ledger.report(
            desk.now_ms(),
            chain.state.uid_by_hotkey,
            owner_hotkey=owner,
            admitted=desk.admitted(),
            reachable=desk.reachable(),
            chain=chain_info(),
            watch=watch,
            genesis=a.genesis,
            coldkeys=chain.state.coldkey_by_hotkey,
            proven=desk.proven(),
        )
        desk.clear_watch(len(watch["copy_pairs"]))
        pairs = watch["copy_pairs"]
        conts = [float(p.get("containment") or 0.0) for p in pairs]
        _log(
            f"hour {rep['hour']} watch: {len(pairs)} copy comparison(s)"
            + (f", max containment {max(conts):.3f}" if conts else "")
            + f", {sum(1 for p in pairs if p.get('decided_by') == 'normalised digest')} by digest"
            + f", {sum(1 for p in pairs if p.get('copy'))} copies; "
            f"admitted per coldkey {[len(v) for v in watch['admitted_by_coldkey'].values()]}; "
            f"{len(watch['same_coldkey_endings'])} declined/out-of-reach ending(s) by one coldkey; "
            f"{len(watch['disabled'])} disable(s)"
        )
        if rep["vector"]:
            weights.submit(rep["vector"], hour=rep["hour"])
        commits.submit(rep["sha256"], hour=rep["hour"])
        store.put("meta", "last_hour", rep["hour"])
        _log(f"hour {rep['hour']}: {rep['rule']}; weights {rep['vector']}; sha256 {rep['sha256']}")
        return rep

    def loop() -> None:
        last_hour = int(store.get("meta", "last_hour", desk.now_ms() // ledger.hour_ms))
        store.put("meta", "last_hour", last_hour)
        last_sync = last_commit = 0.0
        while True:
            try:
                if time.monotonic() - last_sync > a.sync_s:
                    last_sync = time.monotonic()
                    if chain.sync():
                        gone = desk.sync_registered(chain.hotkeys)
                        if gone:
                            _log(f"deregistered on the subnet: {len(gone)} miner(s); keys revoked")
                    else:
                        _log(f"metagraph read failed ({chain.failures} in a row); kept the last")
                    say_dominance()
                desk.tick()
                desk.enforce()
                if desk.now_ms() // ledger.hour_ms > last_hour:  # an hour missed while down too
                    close()
                    last_hour = desk.now_ms() // ledger.hour_ms
                weights.pump()
                if time.monotonic() - last_commit > 30:
                    last_commit = time.monotonic()
                    commits.pump()
            except Exception as e:  # noqa: BLE001 -- the loop outlives a hiccup
                _log(f"loop: {type(e).__name__}: {e}")
            time.sleep(2)

    say_dominance()
    try:
        _log(f"reconciled {desk.reconcile()} claimed job(s) from the platform")
    except Exception as e:  # noqa: BLE001 -- the loop retries through the lease
        _log(f"reconcile failed: {type(e).__name__}: {e}")
    desk.start()
    threading.Thread(target=loop, daemon=True).start()
    server = serve(
        desk,
        a.host,
        a.port,
        token=a.token_file.read_text().strip(),
        validator_hotkey=me,
        verify=btauth.verify,
        close_hour=close,
        reports_dir=a.state_dir / "reports",
        chain_info=chain_info,
        trusted_proxies=a.trusted_proxy,
        signer=wallet,
        max_connections=a.max_connections,
        max_per_ip=a.max_connections_per_ip,
    )
    _log(
        f"validator {me[:10]} on {a.host}:{a.port}; "
        + ("weights DRY RUN (no chain writes)" if dry else f"weights on netuid {a.netuid}")
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
