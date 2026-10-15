"""The client's answers in a delivered project: the validator's own check (DESIGN.md section 4).

While a job is built, its client may answer the builder's open client decisions (the questions the
client sees in the chat). The platform writes each answer into the job workspace's ``brief/`` -- the
client's words, which the builder cannot write -- as the file Watt's own designer gets:

    brief/answer-<YYYY-MM-DD>-<decision>-<HHMMSS>.md      (UTC; the stamp is when it arrived)

and a change asked mid-job as ``brief/change-<YYYY-MM-DD>-<HHMMSS>.md``. The builder reads them (the
platform's ``client_answers`` tool), records each answer in its decision -- status ``answered`` (or
``decided``), the chosen option, and ``answer = { from = "client: <quote> (cadmind.ai, <date>)" }``
quoting the file -- and re-runs what the answer reaches.

**The check** (:func:`check`), run by the validator on its own fork of the frozen workspace, from
the project's own files (the export), trusting nothing the builder says:

- every answer file that arrived before the hand-over (the miner's submit, or its deadline) is
  recorded: its decision exists, is no longer open, and an ``answer.from`` of it (or of the decision
  that superseded it) quotes the file; a chosen option (with no words of the client's) is the
  recorded choice. When a decision was answered more than once, its latest answer is the one that
  counts. An answer that arrived after the hand-over never counts against the builder: it is the
  next round's.
- a job whose client asked not to be asked questions (``no_questions``) leaves no client decision
  open: each would-be question is the builder's own decision (``by = "agent"``, ``status =
  "decided"``).

Pure: the caller hands in the export's bytes, so the reading is tested apart from any platform.
"""

from __future__ import annotations

import calendar
import io
import re
import time
import tomllib
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ANSWER_NAME = re.compile(r"^answer-(\d{4}-\d{2}-\d{2})-(.+?)(?:-(\d{6}))?\.md$")
"""``answer-<date>-<decision>-<HHMMSS>.md`` (the stamp is optional in older files)."""
RECEIVED = re.compile(r"\(received (\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}) UTC")
MAX_SPEC_BYTES = 2_000_000
"""A spec file larger than this is not read (it is not a hand-written project file)."""
MAX_ANSWER_BYTES = 64_000
MAX_CHAIN = 16
"""How far a ``superseded_by`` chain is followed."""
SAID = 3
"""How many problems the gate's reason names (the rest are counted)."""


@dataclass(frozen=True)
class AnswerFile:
    """One client answer under ``brief/``."""

    path: str
    """its path in the project (``brief/answer-...md``)"""
    decision: str
    at_ms: int
    """when it arrived (UTC), from its name; 0 when the name says no time"""
    text: str
    choice: str = ""
    """the option the client picked (``Answer: <id>``), or ''"""
    words: bool = False
    """whether the client also wrote their own words"""


def _stamp_ms(date: str, hhmmss: str | None) -> int:
    try:
        t = time.strptime(f"{date} {hhmmss or '000000'}", "%Y-%m-%d %H%M%S")
    except ValueError:
        return 0
    return calendar.timegm(t) * 1000


def parse_answer(path: str, text: str) -> AnswerFile | None:
    """An answer file by its path in the project and its text; None for any other file."""
    name = path.rsplit("/", 1)[-1]
    m = ANSWER_NAME.match(name)
    if not m or not path.startswith("brief/") or path.count("/") != 1:
        return None
    date, decision, hhmmss = m.group(1), m.group(2), m.group(3)
    at = _stamp_ms(date, hhmmss)
    if not hhmmss:
        got = RECEIVED.search(text)
        if got:
            at = _stamp_ms(got.group(1), f"{got.group(2)}{got.group(3)}59")
    choice = ""
    for line in text.splitlines():
        if line.startswith("Answer: "):
            choice = line.removeprefix("Answer: ").split(" (", 1)[0].strip()
            break
    return AnswerFile(
        path=path,
        decision=decision,
        at_ms=at,
        text=text,
        choice=choice,
        words="In the client's words:" in text,
    )


def _norm(text: str) -> str:
    return " ".join(text.replace("“", '"').replace("”", '"').split()).casefold()


def quote_of(answer_from: str) -> str:
    """The quote of an ``answer.from`` (``client: <quote> (<channel>, <date>)``), unquoted; '' when
    it is none."""
    s = str(answer_from or "").strip()
    if not s.lower().startswith("client:"):
        return ""
    s = s[len("client:") :].strip()
    s = re.sub(r"\s*\([^()]*\)\s*$", "", s).strip()
    if len(s) >= 2 and s[0] in "\"'“" and s[-1] in "\"'”":
        s = s[1:-1].strip()
    return s


def quotes(answer_from: str, file: AnswerFile) -> bool:
    """Whether ``answer_from`` quotes ``file``: its quote, whitespace and case aside, is in the
    file's text."""
    q = _norm(quote_of(answer_from))
    return bool(q) and q in _norm(file.text)


def _from_of(decision: Mapping[str, Any]) -> str:
    answer = decision.get("answer")
    if isinstance(answer, Mapping):
        return str(answer.get("from") or "")
    return ""


def decisions_of(spec_files: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Every ``[decision.<id>]`` of the project's ``spec/*.toml`` (path -> text), by id. A file that
    does not parse is skipped (Watt's own checks refuse it long before)."""
    out: dict[str, dict[str, Any]] = {}
    for _, text in sorted(spec_files.items()):
        try:
            table = tomllib.loads(text).get("decision")
        except tomllib.TOMLDecodeError:
            continue
        if isinstance(table, Mapping):
            for ident, d in table.items():
                if isinstance(d, Mapping):
                    out[str(ident)] = dict(d)
    return out


def open_client(decisions: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The ids of the open client decisions: the questions still put to the client."""
    return sorted(
        i for i, d in decisions.items() if d.get("by") == "client" and d.get("status") == "open"
    )


def _chain(
    decisions: Mapping[str, Mapping[str, Any]], ident: str
) -> list[tuple[str, Mapping[str, Any]]]:
    out: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    while ident in decisions and ident not in seen and len(out) < MAX_CHAIN:
        seen.add(ident)
        d = decisions[ident]
        out.append((ident, d))
        if d.get("status") != "superseded":
            break
        ident = str(d.get("superseded_by") or "")
    return out


def unrecorded(
    answers: Iterable[AnswerFile],
    decisions: Mapping[str, Mapping[str, Any]],
    *,
    before_ms: int = 0,
) -> list[str]:
    """What is wrong with how the project records its client's answers, one line each; [] when every
    answer that arrived before ``before_ms`` (0: every one) is recorded."""
    latest: dict[str, AnswerFile] = {}
    for a in answers:
        if before_ms and a.at_ms > before_ms:
            continue  # arrived after the hand-over: the next round's, never this builder's
        was = latest.get(a.decision)
        if was is None or (a.at_ms, a.path) > (was.at_ms, was.path):
            latest[a.decision] = a
    problems: list[str] = []
    for ident, a in sorted(latest.items()):
        chain = _chain(decisions, ident)
        if not chain:
            problems.append(
                f"{a.path} answers decision {ident}, which the project no longer has (never"
                " delete a decision: answer it, or supersede it)"
            )
            continue
        status = str(chain[0][1].get("status") or "")
        if status == "open":
            problems.append(
                f"{a.path} answers decision {ident}, which is still open: record it (status"
                ' "answered", its choice, answer = { from = "client: <quote> (cadmind.ai,'
                ' <date>)" }) and re-run what it reaches'
            )
            continue
        last_id = chain[-1][0]
        recorded = [
            (i, d)
            for i, d in chain
            if d.get("status") in ("answered", "decided") and quotes(_from_of(d), a)
        ]
        if not recorded:
            where = ident if last_id == ident else f"{ident} (superseded by {last_id})"
            problems.append(
                f"decision {where} does not quote the client's answer {a.path}: its answer.from"
                ' must be "client: <words from that file> (cadmind.ai, <date>)"'
            )
            continue
        rid, rec = recorded[-1]
        picked = a.choice and not a.words and a.choice in (rec.get("options") or {})
        if picked and str(rec.get("choice") or "") != a.choice:
            problems.append(
                f"decision {rid} records choice {rec.get('choice')!r}; the client chose"
                f" {a.choice!r} ({a.path})"
            )
    return problems


def project_files(export: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """``(brief answer files, spec files)`` of a project export (a zip): path in the project ->
    text. The export's entries may sit under one top directory (the workspace's name); either way
    paths are the project's own."""
    briefs: dict[str, str] = {}
    specs: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(export)) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        names = [i.filename for i in infos]
        top = ""
        if names and all("/" in n for n in names):
            first = {n.split("/", 1)[0] for n in names}
            if len(first) == 1 and not any(n.startswith(("brief/", "spec/")) for n in names):
                top = first.pop() + "/"
        for info in infos:
            rel = (
                info.filename[len(top) :]
                if top and info.filename.startswith(top)
                else info.filename
            )
            if ".." in rel.split("/"):
                continue
            top_level = rel.count("/") == 1
            if top_level and rel.startswith("brief/") and ANSWER_NAME.match(rel[6:]):
                if info.file_size <= MAX_ANSWER_BYTES:
                    briefs[rel] = z.read(info).decode("utf-8", errors="replace")
            elif (
                top_level
                and rel.startswith("spec/")
                and rel.endswith(".toml")
                and info.file_size <= MAX_SPEC_BYTES
            ):
                specs[rel] = z.read(info).decode("utf-8", errors="replace")
    return briefs, specs


def check(export: bytes, *, before_ms: int = 0, no_questions: bool = False) -> str:
    """The gate's answers check on a project export: '' when it passes, else the reason (for the
    miner)."""
    try:
        briefs, specs = project_files(export)
    except (zipfile.BadZipFile, OSError, ValueError):
        return "the delivered project could not be read to check the client's answers"
    decisions = decisions_of(specs)
    answers = [a for a in (parse_answer(p, t) for p, t in briefs.items()) if a is not None]
    problems = unrecorded(answers, decisions, before_ms=before_ms)
    said: list[str] = []
    if problems:
        more = f" (and {len(problems) - SAID} more)" if len(problems) > SAID else ""
        said.append(
            "the client's answers are not all recorded: " + "; ".join(problems[:SAID]) + more
        )
    if no_questions:
        still = open_client(decisions)
        if still:
            said.append(
                "the client asked not to be asked questions, and client decision(s) "
                f'{", ".join(still[:8])} are open: decide each yourself (by = "agent", status ='
                ' "decided", its choice and because)'
            )
    return "; and ".join(said)
