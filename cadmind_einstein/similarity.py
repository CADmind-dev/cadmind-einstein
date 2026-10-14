"""Copies between miners' outputs (DESIGN.md section 9): with one miner per job there is no round to
copy within, but a miner can hand in another job's output (its own sybil's, or a delivered one it
saw).

**What is compared.** Each output (the ``spec/ src/ tests/`` of its export) is reduced to a set of
*features* that survive cosmetic edits:

- Python is parsed and every statement is normalised -- comments, docstrings, ``pass`` and bare
  constants dropped; every identifier (names, attributes, arguments, functions, classes, keyword
  names) renamed to one placeholder; every string to one placeholder; numbers to one spelling
  (``2`` = ``2.0``) -- and each statement (a compound statement by its header), and each call of
  some size inside it, is one feature. Layout, comments, renames, reordering and inserted lines
  change none of the others.
- TOML is parsed and each leaf (its table's kind, its key, its value with case, whitespace and
  quoting normalised) is one feature, whatever the table is called.
- Any other text (or Python that does not parse): each normalised line of some length.

**What is subtracted.** Features of the project the job started from (its seed), of a configured
corpus of public example projects, and every feature *common* in the window: present in outputs
from at least ``common_coldkeys`` distinct coldkeys other than the two compared (only outputs that
were not copies count, so a copier cannot make its victim's features common). What remains is the
output's *distinctive* features.

**The verdict.** An output copies a recent output of ANOTHER hotkey when the platform's digest is
identical; or the two have the same normalised digest (every distinctive feature equal: a copy
with only comments, layout or names changed, at any size); or, when both have at least
``min_features`` distinctive features, the *containment* -- the share of the smaller one's
distinctive features found in the other -- reaches ``containment``. Containment, not Jaccard: a
copy padded with unrelated material still contains its victim. An output with fewer distinctive
features than ``min_features`` is compared by its normalised digest only (a few shared features
prove nothing either way)."""

from __future__ import annotations

import ast
import copy
import hashlib
import io
import re
import tomllib
import zipfile
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FEATURE_BYTES = 10
MAX_FILE = 2 * 1024 * 1024
"""bytes read of one file of an export (the rest is ignored)"""
MAX_TOTAL = 32 * 1024 * 1024
"""bytes read of one export in all"""
MAX_FEATURES = 50_000
MIN_LINE = 8
"""a text line shorter than this (normalised) is not a feature"""
MIN_CALL = 6
"""a call is its own feature when its normalised subtree has at least this many nodes"""
DIRS = ("spec", "src", "tests")


@dataclass(frozen=True)
class CopyPolicy:
    """The copy verdict's thresholds (calibrate on the hourly report's ``watch.copy_pairs``)."""

    containment: float = 0.7
    """share of the smaller output's distinctive features found in the other: a copy at or over"""
    min_features: int = 8
    """distinctive features both outputs need before containment decides; below, only the
    normalised digest does"""
    common_coldkeys: int = 3
    """a feature in non-copy outputs of this many distinct coldkeys (other than the two compared)
    is common, not evidence; 0 turns the learned common set off"""
    window: int = 500
    """recent outputs compared against"""


@dataclass(frozen=True)
class Fingerprint:
    features: frozenset[bytes]

    def digest(self, minus: frozenset[bytes] = frozenset()) -> str:
        """The normalised digest of the features left after ``minus`` ('' when none are)."""
        left = sorted(self.features - minus)
        return hashlib.sha256(b"".join(left)).hexdigest()[:32] if left else ""


def _h(kind: str, text: str) -> bytes:
    return hashlib.sha256(f"{kind}\0{text}".encode()).digest()[:FEATURE_BYTES]


# -- Python ---------------------------------------------------------------------------------------


def _num(v: float) -> str:
    try:
        return repr(float(v))
    except OverflowError:
        return str(v)


class _Canon(ast.NodeTransformer):
    """Names, strings and numbers to one spelling each; comments are already gone with the parse,
    docstrings and bare constants are dropped by the caller."""

    def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: ARG002 -- every name is one
        return ast.Name(id="V", ctx=ast.Load())

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        node.attr, node.ctx = "a", ast.Load()
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg, node.annotation, node.type_comment = "V", None, None
        return node

    def visit_keyword(self, node: ast.keyword) -> ast.AST:
        self.generic_visit(node)
        if node.arg is not None:
            node.arg = "k"
        return node

    def visit_alias(self, node: ast.alias) -> ast.AST:
        node.asname = None
        return node

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:  # noqa: ARG002 -- one string
        return ast.Constant(value="S")

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        v = node.value
        if isinstance(v, (str, bytes)):
            return ast.Constant(value="S")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return ast.Constant(value=_num(v))
        return node


_BODIES = ("body", "orelse", "finalbody", "handlers", "cases")


def _bare(node: ast.stmt) -> bool:
    """A statement that does nothing: a docstring, a bare constant, ``pass``."""
    return isinstance(node, ast.Pass) or (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    )


def _header(node: ast.stmt) -> ast.stmt:
    """A compound statement without its blocks (they are features of their own)."""
    out = copy.copy(node)
    for f in _BODIES:
        if hasattr(out, f):
            setattr(out, f, [])
    if isinstance(out, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.name = "F"
    return out


def _size(node: ast.AST) -> int:
    return sum(1 for _ in ast.walk(node))


def _python(text: str) -> Iterator[bytes]:
    tree = ast.parse(text)
    canon = _Canon()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or _bare(node):
            continue
        head = _header(node) if any(hasattr(node, f) for f in _BODIES) else node
        c = canon.visit(copy.deepcopy(head))
        yield _h("py", ast.dump(c, annotate_fields=False, include_attributes=False))
        for sub in ast.walk(c):
            if isinstance(sub, ast.Call) and _size(sub) >= MIN_CALL:
                yield _h("call", ast.dump(sub, annotate_fields=False, include_attributes=False))


# -- TOML and text ---------------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _toml(text: str) -> Iterator[bytes]:
    data = tomllib.loads(text)

    def walk(kind: str, key: str, v: Any) -> Iterator[bytes]:
        if isinstance(v, dict):
            for k, x in v.items():
                yield from walk(kind, str(k), x)
        elif isinstance(v, list) and any(isinstance(x, dict) for x in v):
            for x in v:
                yield from walk(kind, key, x)
        else:
            if isinstance(v, list):
                val = "[" + ",".join(sorted(_norm(str(x)) for x in v)) + "]"
            elif isinstance(v, bool) or not isinstance(v, (int, float)):
                val = _norm(str(v))
            else:
                val = _num(v)
            yield _h("toml", f"{kind}\0{key}\0{val}")

    for kind, v in data.items():
        yield from walk(str(kind), str(kind), v)


_COMMENT = re.compile(r"^\s*(#|//|;|--)")


def _lines(text: str) -> Iterator[bytes]:
    for line in text.splitlines():
        if _COMMENT.match(line):
            continue
        n = _norm(line)
        if len(n) >= MIN_LINE:
            yield _h("line", n)


def _file_features(rel: str, text: str) -> Iterator[bytes]:
    if rel.endswith(".py"):
        try:
            yield from _python(text)
            return
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            pass
    elif rel.endswith(".toml"):
        try:
            yield from _toml(text)
            return
        except (tomllib.TOMLDecodeError, ValueError, RecursionError):
            pass
    yield from _lines(text)


def _files(export_zip: bytes) -> Iterator[tuple[str, str]]:
    total = 0
    with zipfile.ZipFile(io.BytesIO(export_zip)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            rel = name.split("/", 1)[1] if "/" in name else name
            if rel.split("/", 1)[0] not in DIRS:
                continue
            with z.open(info) as f:
                raw = f.read(MAX_FILE)
            total += len(raw)
            if total > MAX_TOTAL:
                return
            yield rel, raw.decode("utf-8", "replace")


def fingerprint_files(files: Iterable[tuple[str, str]]) -> Fingerprint:
    out: set[bytes] = set()
    for rel, text in files:
        for f in _file_features(rel, text):
            out.add(f)
            if len(out) >= MAX_FEATURES:
                return Fingerprint(frozenset(out))
    return Fingerprint(frozenset(out))


def fingerprint(export_zip: bytes) -> Fingerprint:
    """An export's features (``spec/ src/ tests/``; the export's top folder is ignored)."""
    if not export_zip:
        return Fingerprint(frozenset())
    try:
        return fingerprint_files(_files(export_zip))
    except zipfile.BadZipFile:
        return Fingerprint(frozenset())


def fingerprint_dir(root: Path) -> Fingerprint:
    """A project on disk (a corpus entry)."""
    files: list[tuple[str, str]] = []
    for d in DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.is_symlink():
                files.append(
                    (
                        p.relative_to(root).as_posix(),
                        p.read_bytes()[:MAX_FILE].decode("utf-8", "replace"),
                    )
                )
    return fingerprint_files(files)


def load_corpus(paths: Iterable[Path]) -> frozenset[bytes]:
    """The features of public example projects, subtracted from every comparison: each path is a
    project folder, an export zip, or a folder of those."""
    out: set[bytes] = set()
    for path in paths:
        if path.is_file() and path.suffix == ".zip":
            out |= fingerprint(path.read_bytes()).features
        elif path.is_dir() and any((path / d).is_dir() for d in DIRS):
            out |= fingerprint_dir(path).features
        elif path.is_dir():
            out |= load_corpus(sorted(path.iterdir()))
    return frozenset(out)


# -- the check -------------------------------------------------------------------------------------


@dataclass
class Entry:
    job: str
    hotkey: str
    digest: str
    features: frozenset[bytes]
    coldkey: str = ""
    copy: bool = False
    """judged a copy: compared against, but its features never count toward the common set"""


@dataclass
class CopyCheck:
    policy: CopyPolicy = field(default_factory=CopyPolicy)
    corpus: frozenset[bytes] = frozenset()
    recent: deque[Entry] = field(default_factory=deque)
    _df: dict[bytes, dict[str, int]] = field(default_factory=dict)
    """per feature, the non-copy outputs in the window that have it, per coldkey"""

    def _count(self, e: Entry, step: int) -> None:
        if e.copy:
            return
        for f in e.features:
            row = self._df.setdefault(f, {})
            n = row.get(e.coldkey, 0) + step
            if n > 0:
                row[e.coldkey] = n
            else:
                row.pop(e.coldkey, None)
                if not row:
                    del self._df[f]

    def remember(self, e: Entry) -> None:
        """Add an output to the window (the oldest leaves past ``policy.window``)."""
        self.recent.append(e)
        self._count(e, 1)
        while len(self.recent) > max(1, self.policy.window):
            self._count(self.recent.popleft(), -1)

    def _common(self, feats: frozenset[bytes], pair: set[str]) -> frozenset[bytes]:
        need = self.policy.common_coldkeys
        if need <= 0:
            return frozenset()
        return frozenset(f for f in feats if len(set(self._df.get(f, {})) - pair) >= need)

    def compare(
        self,
        a: Fingerprint | frozenset[bytes],
        b: Fingerprint | frozenset[bytes],
        *,
        base: frozenset[bytes] = frozenset(),
        coldkeys: tuple[str, str] = ("", ""),
    ) -> dict[str, Any]:
        """One comparison: the distinctive sizes, Jaccard, containment and the verdict's reason
        ('' when not a copy). Identical platform digests are the caller's."""
        fa = a.features if isinstance(a, Fingerprint) else a
        fb = b.features if isinstance(b, Fingerprint) else b
        fixed = base | self.corpus
        common = self._common((fa | fb) - fixed, {c for c in coldkeys if c})
        own, theirs = fa - fixed - common, fb - fixed - common
        both = own & theirs
        small = min(len(own), len(theirs))
        jac = len(both) / len(own | theirs) if own or theirs else 0.0
        cont = len(both) / small if small else 0.0
        same = bool(own) and own == theirs
        pol = self.policy
        why = ""
        if same:
            why = "same normalised project"
        elif small >= pol.min_features and cont >= pol.containment:
            why = "containment"
        return {
            "similarity": round(jac, 4),
            "containment": round(cont, 4),
            "sizes": [len(own), len(theirs)],
            "common": len(common),
            "why": why,
            "decided_by": ("normalised digest" if small < pol.min_features else "containment"),
        }

    def check(
        self,
        job: str,
        hotkey: str,
        digest: str,
        sh: Fingerprint | frozenset[bytes],
        base: frozenset[bytes] = frozenset(),
        on_pair: Callable[[dict[str, Any]], None] | None = None,
        *,
        coldkey: str = "",
        remember: bool = True,
    ) -> dict[str, object] | None:
        """The closest other-hotkey output this one copies, or None; then remember this one.
        ``on_pair`` hears every comparison made -- its similarity, containment, the distinctive
        sizes and the reason -- for calibrating the thresholds; it changes no verdict."""
        feats = sh.features if isinstance(sh, Fingerprint) else sh
        found: dict[str, object] | None = None
        best = -1.0
        for e in list(self.recent):
            if e.hotkey == hotkey:
                continue
            identical = bool(digest) and digest == e.digest
            cmp = self.compare(feats, e.features, base=base, coldkeys=(coldkey, e.coldkey))
            if identical:
                cmp["why"] = "identical digest"
            if on_pair is not None:
                on_pair(
                    {
                        "job": job,
                        "hotkey": hotkey,
                        "other_job": e.job,
                        "other_hotkey": e.hotkey,
                        "similarity": 1.0 if identical else cmp["similarity"],
                        "containment": 1.0 if identical else cmp["containment"],
                        "sizes": cmp["sizes"],
                        "common": cmp["common"],
                        "decided_by": "identical digest" if identical else cmp["decided_by"],
                        "copy": bool(cmp["why"]),
                    }
                )
            if not cmp["why"]:
                continue
            score = 2.0 if cmp["why"] != "containment" else float(cmp["containment"])
            if score > best:
                best = score
                found = {
                    "job": e.job,
                    "hotkey": e.hotkey,
                    "similarity": 1.0 if identical else cmp["similarity"],
                    "containment": 1.0 if identical else cmp["containment"],
                    "why": cmp["why"],
                }
        if remember:
            self.remember(Entry(job, hotkey, digest, feats, coldkey, copy=found is not None))
        return found
