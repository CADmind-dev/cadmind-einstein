"""The per-job score and the weights (DESIGN.md section 5). **Clients are the graders**: there is no
LLM grader.

A failed attempt scores 0 (timeouts and copies at once; rejections and gate failures only when
another miner passes the same job -- the desk decides that). A **passing** job scores::

    I = 0.5 + 0.3·E + 0.2·T                     immediate: passed the Watt gate, efficiency, on time
    s = 0.4·I + 0.6·(w·C + (1 - w)·I)

- ``E``, efficiency: half ``min(1, median compute / own)`` and half ``min(1, median wall time /
  own)``, the medians over the passing jobs of the same bucket (effort, brief size class) in the
  scoring window; 1 when the bucket has fewer than 5. ``T`` is 1 on time, 0 late.
- ``C``, the client's signal, in [0, 1], and ``w``, how much it counts, in [0, 1]:
  - stars (1-5) weigh most: ``C = (stars - 1)/4``, ``w = trust``;
  - a download, without stars: ``C = 0.75``, ``w = 0.5·trust``;
  - a re-run or a change request for the same problem: ``C`` lowered by 0.25 (each), and ``w`` at
    least ``0.5·trust``;
  - no signal: ``w = 0``, so ``s = I``. **An unrated job neither punishes nor inflates**: it counts
    on its immediate part, and a client signal moves 60% of the score from that baseline toward the
    client's opinion.
- ``trust``: 1 for a paid account (a card on file), 0.1 for a free one (the platform says which; a
  separate anti-abuse system guards free accounts). Miners never learn which client a job came from,
  and the router -- not the miner -- picks who gets a job, so a miner cannot steer its own fake
  client's job to itself.
- Signals arrive late and change the job's score when they do; the next hourly vector reads the
  current scores.

**Weights, every hour, never burned.** Each job counts with a weight halving every 24 hours of age
(a 7-day window). A miner's **weight** is the decayed SUM of its job scores (work done and how
good); its **quality**, for the router, is the decayed MEAN. The vector is proportional to the
weight over registered, admitted miners seen in the last 24 hours. An hour with no work or no
success still emits, from that history. **Genesis** -- no admitted miner has any history in the
window -- follows ``genesis``:

- ``delivered`` (the default): equal per COLDKEY among admitted, registered, reachable miners that
  have delivered at least one scored client job (ever), each coldkey's share split among its
  hotkeys. Admission alone (a cheap, unscored entry test) earns nothing, so hotkeys registered to
  farm genesis get no weight; before the first delivery nothing is weighted.
- ``coldkey``: equal per coldkey among admitted, registered, reachable miners (hotkeys of one owner
  share one share).
- ``hotkey``: equal per hotkey among them (an earlier rule; a report without ``genesis`` was made
  by it).

**The subnet owner's hotkey is never weighted**: weighting it burns miner emission. Nothing is
emitted when no miner qualifies.

Every hour a report holds the window's jobs with their facts and signals, and the vector;
:func:`recompute` rebuilds the scores and the vector from a report alone (``python -m
cadmind_einstein.scoring REPORT``).
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HALF_LIFE_H = 24.0
WINDOW_H = 7 * 24
HOUR_MS = 3_600_000
MIN_BUCKET = 5
TRUST = {"paid": 1.0, "free": 0.1}
W_IMMEDIATE, W_CLIENT = 0.4, 0.6


def size_class(brief: str) -> str:
    n = len(brief or "")
    return "short" if n < 600 else ("medium" if n < 2000 else "long")


@dataclass
class JobOutcome:
    job: str
    hotkey: str
    ended_ms: int
    effort: str = "medium"
    size: str = "medium"
    accepted: bool = True
    on_time: bool = True
    deliverable: bool = False
    stage: str = "none"
    compute_s: float = 0.0
    wall_s: float = 0.0
    copy_of: str = ""
    kind: str = ""
    """pass | reject | gate | timeout | copy"""
    signals: list[dict[str, Any]] = field(default_factory=list)
    """the client's: {"kind": download | stars | rerun | change, "stars": 1-5, "trust": paid |
    free, "at_ms"}"""
    s: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)

    @property
    def bucket(self) -> tuple[str, str]:
        return (self.effort, self.size)

    @property
    def passed(self) -> bool:
        return self.accepted and self.deliverable and not self.copy_of


def client_signal(signals: Iterable[Mapping[str, Any]]) -> tuple[float | None, float]:
    """(C, w): the client's verdict and how much it counts. (None, 0) when there is none."""
    c: float | None = None
    w = 0.0
    sig = list(signals)
    stars = [x for x in sig if x.get("kind") == "stars" and 1 <= int(x.get("stars") or 0) <= 5]
    if stars:
        last = stars[-1]
        c, w = (int(last["stars"]) - 1) / 4, TRUST.get(str(last.get("trust")), 0.0)
    else:
        dl = [TRUST.get(str(x.get("trust")), 0.0) for x in sig if x.get("kind") == "download"]
        if dl:
            c, w = 0.75, 0.5 * max(dl)
    redo = [
        TRUST.get(str(x.get("trust")), 0.0) for x in sig if x.get("kind") in ("rerun", "change")
    ]
    if redo:
        c = max(0.0, (0.75 if c is None else c) - 0.25 * len(redo))
        w = max(w, 0.5 * max(redo))
    return c, w


def score_all(jobs: Iterable[JobOutcome]) -> list[JobOutcome]:
    """Fill every job's ``s`` and ``parts`` from its facts, its signals, and the window's
    medians."""
    jobs = list(jobs)
    comp: dict[tuple[str, str], list[float]] = defaultdict(list)
    wall: dict[tuple[str, str], list[float]] = defaultdict(list)
    for o in jobs:
        if o.passed:
            comp[o.bucket].append(o.compute_s)
            wall[o.bucket].append(o.wall_s)
    for o in jobs:
        if not o.passed:
            o.s, o.parts = 0.0, {"I": 0.0, "C": -1.0, "w": 0.0}
            continue
        e = 1.0
        if len(comp[o.bucket]) >= MIN_BUCKET:
            mc, mw = statistics.median(comp[o.bucket]), statistics.median(wall[o.bucket])
            ec = min(1.0, mc / o.compute_s) if o.compute_s > 0 else 1.0
            ew = min(1.0, mw / o.wall_s) if o.wall_s > 0 else 1.0
            e = 0.5 * ec + 0.5 * ew
        i = 0.5 + 0.3 * e + 0.2 * (1.0 if o.on_time else 0.0)
        c, w = client_signal(o.signals)
        client = w * c + (1 - w) * i if c is not None else i
        o.s = round(W_IMMEDIATE * i + W_CLIENT * client, 6)
        o.parts = {
            "I": round(i, 4),
            "E": round(e, 4),
            "C": round(c, 4) if c is not None else -1.0,
            "w": round(w, 4),
        }
    return jobs


def decay(age_ms: int) -> float:
    return float(0.5 ** (max(0, age_ms) / (HALF_LIFE_H * HOUR_MS)))


def standing(jobs: Iterable[JobOutcome], now_ms: int) -> tuple[dict[str, float], dict[str, float]]:
    """(weight, quality) per miner: the decayed sum and the decayed mean of its job scores."""
    total: dict[str, float] = defaultdict(float)
    norm: dict[str, float] = defaultdict(float)
    for o in jobs:
        k = decay(now_ms - o.ended_ms)
        total[o.hotkey] += k * o.s
        norm[o.hotkey] += k
    quality = {h: total[h] / norm[h] for h in norm if norm[h] > 0}
    return dict(total), quality


GENESIS = ("delivered", "coldkey", "hotkey")


def vector(
    weight: Mapping[str, float],
    uid_by_hotkey: Mapping[str, int],
    *,
    owner_hotkey: str,
    admitted: Iterable[str],
    reachable: Iterable[str],
    genesis: str = "delivered",
    coldkeys: Mapping[str, str] | None = None,
    proven: Iterable[str] = (),
) -> tuple[list[tuple[int, int]], str]:
    """The weights and the rule that made them. Never the subnet owner's hotkey; never burned.
    ``coldkeys`` (hotkey -> coldkey; a hotkey with none stands alone) and ``proven`` (hotkeys with
    a delivered client job) serve the genesis rule only."""
    if genesis not in GENESIS:
        raise ValueError(f"genesis is one of {GENESIS}")
    adm = {h for h in admitted if h in uid_by_hotkey and h != owner_hotkey}
    live = adm & set(reachable)

    def scaled(w: Mapping[str, float]) -> list[tuple[int, int]]:
        top = max(w.values())
        return sorted((uid_by_hotkey[h], max(1, round(65_535 * v / top))) for h, v in w.items())

    earned = {h: v for h, v in weight.items() if v > 0 and h in live}
    if earned:
        return scaled(earned), "history"
    if genesis == "hotkey" and live:
        return sorted(
            (uid_by_hotkey[h], 65_535) for h in live
        ), "genesis: equal among admitted, reachable miners"
    pool = live & set(proven) if genesis == "delivered" else live
    if pool:
        owner_of = {h: str((coldkeys or {}).get(h) or "") or f"hotkey:{h}" for h in pool}
        per = {ck: sum(1 for x in owner_of.values() if x == ck) for ck in owner_of.values()}
        what = "delivered miners" if genesis == "delivered" else "admitted, reachable miners"
        return sorted(
            (uid_by_hotkey[h], max(1, round(65_535 / per[owner_of[h]]))) for h in pool
        ), f"genesis: equal per coldkey among {what}"
    earned = {h: v for h, v in weight.items() if v > 0 and h in adm}
    if earned:
        return scaled(earned), "history (no admitted miner reachable)"
    if genesis == "delivered" and live:
        return [], "genesis: no admitted miner has delivered a client job yet: nothing to weight"
    return [], "no admitted miner: nothing to weight"


def report_bytes(rep: Mapping[str, Any]) -> bytes:
    """A report's published bytes: canonical JSON (sorted keys, no spaces). Its SHA-256 is what
    goes on chain, and every report names the previous report's, so the newest commitment
    authenticates the whole published history."""
    return json.dumps(rep, sort_keys=True, separators=(",", ":")).encode()


def report_order(path: Path) -> tuple[int, int]:
    """``hour-N.json`` sorts as (N, 0); a second report closed in the same hour, ``hour-N-K.json``,
    as (N, K)."""
    parts = path.stem.split("-")
    return int(parts[1]), int(parts[2]) if len(parts) > 2 else 0


def _key(o: JobOutcome) -> str:
    return f"{o.job}/{o.hotkey}/{o.ended_ms}"


def _row(o: JobOutcome) -> dict[str, Any]:
    return {k: v for k, v in asdict(o).items() if k not in ("s", "parts")}


@dataclass
class Ledger:
    """Job outcomes and late client signals in; hourly snapshots out.

    With a :class:`~.store.Store` every outcome and signal is written as it lands (inside the
    desk's transaction when there is one), and a restart reloads them all -- mid-hour too. Without
    one (tests, recompute) a restart falls back to the newest report's window.
    """

    report_dir: Path | None = None
    hour_ms: int = HOUR_MS
    store: Any = None
    jobs: dict[str, JobOutcome] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if self.store is not None:
            self._lock = self.store.lock
            for row in self.store.items("outcome").values():
                o = _job(row)
                self.jobs[_key(o)] = o
            if self.jobs:
                return
        if self.report_dir is None or not self.report_dir.is_dir():
            return
        reports = sorted(self.report_dir.glob("hour-*.json"), key=report_order)
        if reports:
            for row in json.loads(reports[-1].read_text(encoding="utf-8"))["jobs"]:
                o = _job(row)
                self.jobs[_key(o)] = o
                self._save(o)

    def _save(self, o: JobOutcome) -> None:
        if self.store is not None:
            self.store.put("outcome", _key(o), _row(o))

    @property
    def outcomes(self) -> list[JobOutcome]:
        with self._lock:
            return list(self.jobs.values())

    def add(self, o: JobOutcome) -> float:
        with self._lock:
            self.jobs[_key(o)] = o
            self._save(o)
            score_all(self._window(o.ended_ms))
            return o.s

    def signal(self, job: str, sig: Mapping[str, Any]) -> float | None:
        """A client's late signal on a delivered job (its passing attempt). None when the job is
        unknown."""
        with self._lock:
            for o in self.jobs.values():
                if o.job == job and o.passed:
                    o.signals.append(dict(sig))
                    self._save(o)
                    score_all(self._window(sig.get("at_ms") or o.ended_ms))
                    return o.s
        return None

    def _window(self, now_ms: int) -> list[JobOutcome]:
        return [o for o in self.jobs.values() if now_ms - o.ended_ms <= WINDOW_H * HOUR_MS]

    def snapshot(self, now_ms: int) -> tuple[list[JobOutcome], dict[str, float], dict[str, float]]:
        with self._lock:
            window = score_all(self._window(now_ms))
            old = [k for k, o in self.jobs.items() if now_ms - o.ended_ms > WINDOW_H * HOUR_MS]
            for key in old:
                del self.jobs[key]
                if self.store is not None:
                    self.store.delete("outcome", key)
            weight, quality = standing(window, now_ms)
            return window, weight, quality

    def quality(self, now_ms: int) -> dict[str, float]:
        return self.snapshot(now_ms)[2]

    def last_sha256(self) -> str:
        """The newest published report's SHA-256 ('' before the first)."""
        if self.store is not None:
            return str(self.store.get("meta", "report_sha256", ""))
        if self.report_dir is None or not self.report_dir.is_dir():
            return ""
        sums = sorted(self.report_dir.glob("hour-*.sha256"), key=report_order)
        return sums[-1].read_text().split()[0] if sums else ""

    def report(
        self,
        now_ms: int,
        uid_by_hotkey: Mapping[str, int],
        *,
        owner_hotkey: str,
        admitted: Iterable[str],
        reachable: Iterable[str],
        chain: Mapping[str, Any] | None = None,
        watch: Mapping[str, Any] | None = None,
        genesis: str = "delivered",
        coldkeys: Mapping[str, str] | None = None,
        proven: Iterable[str] = (),
    ) -> dict[str, Any]:
        """The hour's report, written as its canonical bytes (``hour-N.json``) beside its SHA-256
        (``hour-N.sha256``), both atomically. ``chain`` (the block, our stake share, the weights
        path) and ``watch`` (the desk's observations: copy similarities, endings and
        admissions per coldkey, disables) ride along for readers; recompute needs neither. The
        genesis rule, the admitted miners' coldkeys and the ``proven`` hotkeys are published:
        recompute reads them."""
        window, weight, quality = self.snapshot(now_ms)
        adm, reach = sorted(admitted), sorted(reachable)
        cold = {h: str((coldkeys or {}).get(h) or "") for h in adm}
        prov = sorted(set(proven) & set(adm))
        vec, rule = vector(
            weight,
            uid_by_hotkey,
            owner_hotkey=owner_hotkey,
            admitted=adm,
            reachable=reach,
            genesis=genesis,
            coldkeys=cold,
            proven=prov,
        )
        rep = {
            "hour": now_ms // self.hour_ms,
            "at_ms": now_ms,
            "prev_sha256": self.last_sha256(),
            "jobs": [asdict(o) for o in window],
            "weight": weight,
            "quality": quality,
            "uids": dict(uid_by_hotkey),
            "owner": owner_hotkey,
            "admitted": adm,
            "reachable": reach,
            "genesis": genesis,
            "coldkeys": cold,
            "proven": prov,
            "vector": [list(x) for x in vec],
            "rule": rule,
            "chain": dict(chain or {}),
            "watch": dict(watch or {}),
            "constants": {
                "half_life_h": HALF_LIFE_H,
                "window_h": WINDOW_H,
                "immediate": W_IMMEDIATE,
                "client": W_CLIENT,
                "trust": TRUST,
            },
        }
        raw = report_bytes(rep)
        sha = hashlib.sha256(raw).hexdigest()
        if self.report_dir is not None:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            name, k = f"hour-{rep['hour']}", 0
            while (self.report_dir / f"{name}.json").exists():  # closed twice in one hour
                k += 1
                name = f"hour-{rep['hour']}-{k}"
            _atomic_write(self.report_dir / f"{name}.json", raw)
            _atomic_write(self.report_dir / f"{name}.sha256", f"{sha}  {name}.json\n".encode())
        if self.store is not None:
            self.store.put("meta", "report_sha256", sha)
        rep["sha256"] = sha  # not part of the hashed bytes: the caller's handle on them
        return rep


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _job(row: Mapping[str, Any]) -> JobOutcome:
    return JobOutcome(**{k: v for k, v in row.items() if k not in ("s", "parts")})


def recompute(report: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild every job's score and the vector from one published report; list any job that
    differs."""
    jobs = score_all(_job(r) for r in report["jobs"])
    mismatches = [
        {"job": o.job, "hotkey": o.hotkey, "published": r["s"], "recomputed": o.s}
        for o, r in zip(jobs, report["jobs"], strict=True)
        if abs(o.s - float(r["s"])) > 1e-6
    ]
    weight, _ = standing(jobs, int(report["at_ms"]))
    vec, rule = vector(
        weight,
        report["uids"],
        owner_hotkey=report["owner"],
        admitted=report["admitted"],
        reachable=report["reachable"],
        genesis=str(report.get("genesis") or "hotkey"),
        coldkeys=report.get("coldkeys") or {},
        proven=report.get("proven") or (),
    )
    same = [list(x) for x in vec] == [list(x) for x in report["vector"]]
    return {"vector": vec, "rule": rule, "matches_published": same, "mismatches": mismatches}


def onchain_sha256(network: str, netuid: int, hotkey: str, block: int | None = None) -> str:
    """The SHA-256 the validator's hotkey last committed on ``netuid`` (at ``block`` on an archive
    node), '' when none. A free read."""
    import bittensor as bt  # noqa: PLC0415 -- the auditor's optional chain read

    c = bt.Subtensor(network=network).identity.commitment(netuid, hotkey, block=block)
    data = str(getattr(c, "data", "") or "")
    for part in data.split():
        if part.startswith("sha256:"):
            return part.split(":", 1)[1].removeprefix("0x").lower()
    return ""


def audit(paths: list[Path], *, expect_sha256: str = "") -> dict[str, Any]:
    """Recompute every report and check the hash chain: each report's ``prev_sha256`` is the SHA-256
    of the report before it, and the newest one's SHA-256 is ``expect_sha256`` (what is on chain)
    when given."""
    out: list[dict[str, Any]] = []
    ok, prev = True, ""
    for i, path in enumerate(paths):
        raw = path.read_bytes()
        rep = json.loads(raw)
        sha = hashlib.sha256(raw).hexdigest()
        row: dict[str, Any] = {"report": str(path), "sha256": sha, **recompute(rep)}
        row["canonical"] = report_bytes(rep) == raw
        if i and rep.get("prev_sha256") != prev:
            row["chain_break"] = {"prev_sha256": rep.get("prev_sha256"), "expected": prev}
            ok = False
        ok = ok and row["matches_published"] and not row["mismatches"] and row["canonical"]
        prev = sha
        out.append(row)
    result: dict[str, Any] = {"reports": out, "ok": ok}
    if expect_sha256:
        result["expected_sha256"] = expect_sha256
        result["newest_matches_expected"] = bool(out) and out[-1]["sha256"] == expect_sha256
        result["ok"] = ok and result["newest_matches_expected"]
    return result


def main(argv: list[str] | None = None) -> int:
    """``cadmind-recompute REPORT [REPORT ...]``: rebuild the scores and the vector of each
    published report, check the reports chain together in the order given, and (optionally) that
    the newest one is what the validator committed on chain."""
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="cadmind-recompute")
    ap.add_argument("reports", nargs="+", type=Path, help="hour-N.json, oldest first")
    ap.add_argument("--expect-sha256", default="", help="the newest report's committed hash")
    ap.add_argument("--network", default="", help="read the committed hash from this chain")
    ap.add_argument("--netuid", type=int, default=0)
    ap.add_argument("--validator-hotkey", default="")
    ap.add_argument("--block", type=int, default=None, help="read the commitment at this block")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    expect = a.expect_sha256.lower().removeprefix("0x")
    if a.network:
        expect = onchain_sha256(a.network, a.netuid, a.validator_hotkey, a.block)
        if not expect:
            print("no sha256 commitment found for that hotkey", file=sys.stderr)  # noqa: T201
            return 1
    out = audit(a.reports, expect_sha256=expect)
    print(json.dumps(out, indent=1))  # noqa: T201 -- a CLI
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
