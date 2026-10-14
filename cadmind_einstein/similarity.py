"""Copies between miners' outputs (DESIGN.md section 9): with one miner per job there is no round to
copy within, but a miner can hand in another job's output (its own sybil's, or a delivered one it
saw). Each job's output is reduced to shingles -- 5 consecutive non-blank lines of ``spec/ src/
tests/`` -- minus the shingles of the project it started from; a Jaccard similarity of 0.8 or more
with a recent output of ANOTHER hotkey, or an identical digest, is a copy."""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

THRESHOLD = 0.8
WINDOW = 500
K = 5


def shingles(export_zip: bytes) -> frozenset[bytes]:
    out: set[bytes] = set()
    with zipfile.ZipFile(io.BytesIO(export_zip)) as z:
        for name in z.namelist():
            rel = name.split("/", 1)[1] if "/" in name else name
            if rel.split("/", 1)[0] not in ("spec", "src", "tests"):
                continue
            lines = [
                x.strip() for x in z.read(name).decode("utf-8", "replace").splitlines() if x.strip()
            ]
            for i in range(max(0, len(lines) - K + 1)):
                out.add(hashlib.sha256("\n".join(lines[i : i + K]).encode()).digest()[:10])
    return frozenset(out)


@dataclass
class CopyCheck:
    recent: deque[tuple[str, str, str, frozenset[bytes]]] = field(
        default_factory=lambda: deque(maxlen=WINDOW)
    )
    """(job, hotkey, digest, shingles) of recent outputs"""

    def check(
        self,
        job: str,
        hotkey: str,
        digest: str,
        sh: frozenset[bytes],
        base: frozenset[bytes] = frozenset(),
        on_pair: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, object] | None:
        """The closest other-hotkey output this one copies, or None; then remember this one.
        ``on_pair`` hears every comparison made -- its similarity, or why none was computed --
        for calibrating the threshold; it changes no verdict."""
        own = sh - base
        found: dict[str, object] | None = None
        for other_job, other_hk, other_digest, other_sh in self.recent:
            if other_hk == hotkey:
                continue
            theirs = other_sh - base
            if on_pair is not None:
                same = bool(digest) and digest == other_digest
                both = bool(own) and bool(theirs)
                on_pair(
                    {
                        "job": job,
                        "hotkey": hotkey,
                        "other_job": other_job,
                        "other_hotkey": other_hk,
                        "similarity": (
                            1.0
                            if same
                            else round(len(own & theirs) / len(own | theirs), 4)
                            if both
                            else None
                        ),
                        "why": (
                            "identical digest"
                            if same
                            else "shingles"
                            if both
                            else "skipped: no shingles"
                        ),
                        "sizes": [len(own), len(theirs)],
                    }
                )
            if digest and digest == other_digest:
                found = {
                    "job": other_job,
                    "hotkey": other_hk,
                    "similarity": 1.0,
                    "why": "identical digest",
                }
                break
            if not own or not theirs:
                continue
            sim = len(own & theirs) / len(own | theirs)
            if sim >= THRESHOLD and (found is None or sim > float(found["similarity"])):  # type: ignore[arg-type]
                found = {
                    "job": other_job,
                    "hotkey": other_hk,
                    "similarity": round(sim, 3),
                    "why": "shingles",
                }
        self.recent.append((job, hotkey, digest, sh))
        return found
