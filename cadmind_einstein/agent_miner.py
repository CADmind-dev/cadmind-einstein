"""A reference miner: the signed validator protocol around an agent CLI that builds over MCP.

The protocol side is the same as :mod:`.fake_miner` (agent key sealed to an ephemeral X25519 key,
admission, ``work`` / ``accept`` / ``submit`` / ``reject``); the build is an agent CLI run in an
empty directory, restricted to the hosted CADmind Watt MCP tools. One job at a time; a heartbeat
thread keeps polling ``work`` while the agent runs, so the validator keeps seeing the miner alive.

The default agent is Claude Code's CLI (``claude -p``), because its flags restrict an agent to one
MCP server; any other agent CLI works through ``--agent-command`` (see below). The miner chooses,
configures and pays for its own model and CLI: this program passes no credential of its own and
assumes no particular plan. **Follow your model provider's terms** -- many consumer plans do not
allow automated or commercial use.

    cadmind-einstein-miner --wallet.name miner --wallet.hotkey m1 \\
        --validator https://VALIDATOR --validator-hotkey 5... \\
        --platform https://cadmind.ai --state ~/.local/share/cadmind-einstein-miner \\
        [--agent-cli claude] [--model MODEL] [--agent-arg ARG ...]

    # any other agent CLI: an argv template; the prompt also arrives on stdin
    cadmind-einstein-miner ... \\
        --agent-command 'my-agent --mcp {mcp_config} --prompt-file {prompt_file}'

Placeholders in ``--agent-command``: ``{prompt_file}``, ``{mcp_config}`` (a standard
``mcpServers`` JSON file naming the ``cadmind-watt`` server and the local ``cadmind-fetch`` server,
mode 0600), ``{model}``,
``{workspace}`` and ``{job}``. A ``--prompt-file`` template takes ``{ws}``, ``{job}``, ``{brief}``,
``{minutes}``, ``{deadline_utc}``, ``{server}``, ``{questions}`` (the rule for the client's
questions and answers: check
``client_answers`` after each ``watt`` step, record each answer in its decision -- or, when the
client asked not to be asked questions, decide every one) and ``{no_questions}`` (``yes``/``no``).
The agent's last line must be ``DONE <digest>`` or
``REJECT <not-allowed|not-capable>: <message for the client>``.

The agent also gets one local tool, ``fetch_to_workspace`` (:mod:`.fetch_mcp`, a stdio MCP server
named ``cadmind-fetch``): it downloads a public document from the miner's own connection (many sites
refuse the platform's address) and uploads it into the job's workspace, as the hosted ``fetch``
stores one. It is a fenced download, not a shell; ``--no-local-fetch`` leaves it out.

Flags follow Bittensor's names (``--wallet.name``, ``--wallet.hotkey``, ``--wallet.path``,
``--logging.debug``, ``--logging.trace``; the dashed ``--wallet-name``/``--wallet-hotkey`` remain
aliases). With ``--netuid`` (and ``--subtensor.network``/``--subtensor.chain_endpoint``) the miner
reads the metagraph once at startup and warns when its hotkey is not registered there; without
``--netuid`` it makes no chain call. A newer GitHub release is logged as a WARNING at startup
(``--no-version-check`` turns that off).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __spec_version__, __version__
from .cli import (
    add_logging_args,
    add_subtensor_args,
    add_version_args,
    add_wallet_args,
    network_of,
    setup_logging,
    wallet_kwargs,
)
from .fake_miner import signed
from .fetch_mcp import SERVER_NAME as FETCH_SERVER
from .fetch_mcp import TIME_TOOL
from .fetch_mcp import TOOL as FETCH_TOOL
from .fetch_mcp import server_config as fetch_server_config
from .sealing import new_keypair, open_sealed
from .version import check_latest

MCP_SERVER = "cadmind-watt"

PROMPT = """You are a CADmind einstein miner. Build this client's device in the Watt workspace \
`{ws}` (made from the brief), using ONLY the {server} MCP tools (and the local \
`fetch_to_workspace` tool when it is there).

Start with `watt_guide` (brief, then primer, then scope). Work fast and carefully:
1. First write DESIGN-NOTES.md: how the device is made, installed, used and serviced, what can go \
wrong at each step and the requirement/test proving it does not; every critical number sourced \
(fetched into the project's assets/), never invented. Prefer the maker's own product page, \
datasheet or manual over retailer listings. Fetch maker pages and datasheets with \
`fetch_to_workspace` when you have it (it downloads from this builder's own connection, which many \
sites that block the hosted `fetch` answer; its source records add fetched_by = "builder"); the \
hosted `fetch` remains available. A site that blocks both: move on. Fetch only what the first \
sketch needs, then build.
2. Iterate with check / build / test; edit files, never rewrite them whole; end with ONE \
`report` with mutate (the full guard and the delivery's files in one run; `watt_wait` on it), \
only after a passing scoped guard (mutate with several `only`, one run) covering every must \
requirement, never as a first check.
3. Self-review: load paths, retention, clearances, every clause tested; fix the weakest points.
Never weaken a test to pass. {questions}
A `watt` run far longer than usual: stop it with `watt_cancel` and change the design.

If the scope guide says we may not or cannot build it, end with exactly one line:
REJECT <not-allowed|not-capable>: <message for the client>
Otherwise work until `watt` status says deliverable and `client_answers` has nothing new (an \
answer that came is recorded first, and the design made deliverable again), then end with exactly \
one line:
DONE <digest>
(the digest `watt` status reports). You have about {minutes} minutes, until {deadline_utc} \
UTC. You cannot feel time pass: check it with `time_left` (never guess it), and never stop \
or hand over early because you think time is up.

Job: {job}
Brief:
{brief}
"""


QUESTIONS = """What only the client can decide is an open client decision (by = "client", \
status = "open"), designed around your best sourced assumption: the client sees it in their chat \
and may answer while you work. Keep building on the assumption; never wait for an answer. After \
each `watt` command, call `client_answers` (workspace `{ws}`, `after` = the last `cursor` it \
gave): each new answer is a file under brief/ (the client's words). Record it in its decision \
(status = "answered", choice = the option they picked, answer = { from = "client: <their words, \
quoted from that file> (cadmind.ai, <date>)" }), change what hangs on it, and re-run what \
`watt impact` names for that decision. A change request (brief/change-*.md) is the client's too: \
do what it asks. The validator refuses a delivery that leaves an answer unrecorded. Use \
`wait_for_answer` only when no useful work can go on under the assumptions (it waits briefly). \
If `client_answers` says `no_questions: true`, the client asked not to be asked: decide every \
would-be question yourself instead (by = "agent", status = "decided", choice, because)."""

NO_QUESTIONS = """The client asked NOT to be asked questions: leave no client decision open. \
Decide every would-be question yourself as an agent decision (by = "agent", status = "decided", \
choice = the option you build on, because = why), so the client sees it as an assumption on the \
result. \
The validator refuses a delivery with an open client decision. Still call `client_answers` after \
each `watt` command: a change the client sends mid-job arrives there (brief/change-*.md)."""


def questions_rule(work: dict[str, Any], ws: str) -> str:
    """What the prompt says of the client's questions and answers: ask and keep building (and check
    for answers after each step), or, when the client asked not to be asked (``no_questions``),
    decide."""
    return (NO_QUESTIONS if work.get("no_questions") else QUESTIONS).replace("{ws}", ws)


def _write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)


def agent_argv(
    a: argparse.Namespace, *, prompt: str, prompt_file: Path, mcp_file: Path, ws: str, job: str
) -> list[str]:
    """The agent's command line: the ``--agent-command`` template, or Claude Code's CLI
    restricted to the Watt MCP server."""
    if a.agent_command:
        fields = {
            "prompt_file": str(prompt_file),
            "mcp_config": str(mcp_file),
            "model": a.model or "",
            "workspace": ws,
            "job": job,
        }
        return [part.format(**fields) for part in shlex.split(a.agent_command)]
    argv = [
        a.agent_cli,
        "-p",
        prompt,
        "--mcp-config",
        str(mcp_file),
        "--strict-mcp-config",
        "--allowedTools",
        allowed_tools(a),
        "--output-format",
        "text",
    ]
    if a.model:
        argv += ["--model", a.model]
    return argv + list(a.agent_arg)


def allowed_tools(a: argparse.Namespace) -> str:
    """Claude Code's ``--allowedTools``: the Watt MCP server's tools, and the local fetch tool."""
    tools = [f"mcp__{MCP_SERVER}"]
    if getattr(a, "local_fetch", True):
        tools.append(f"mcp__{FETCH_SERVER}__{FETCH_TOOL}")
        tools.append(f"mcp__{FETCH_SERVER}__{TIME_TOOL}")
    return ",".join(tools)


def deadline_file(a: argparse.Namespace) -> Path | None:
    """Where the miner writes each job's deadline (epoch ms) for the local `time_left` tool."""
    state = getattr(a, "state", None)
    return Path(state) / "deadline" if state else None


def mcp_config(a: argparse.Namespace, key: str, key_file: Path) -> dict[str, Any]:
    """The agent's ``mcpServers``: the platform's Watt server, and the local fetch server (unless
    ``--no-local-fetch``), which reads the agent key from ``key_file``."""
    servers: dict[str, Any] = {
        MCP_SERVER: {
            "type": "http",
            "url": a.platform.rstrip("/") + "/mcp",
            "headers": {"Authorization": f"Bearer {key}"},
        }
    }
    if getattr(a, "local_fetch", True):
        # absolute: the agent (and so the server) runs in the empty work directory
        deadline = deadline_file(a)
        servers[FETCH_SERVER] = fetch_server_config(
            a.platform.rstrip("/"), key_file.resolve(), deadline.resolve() if deadline else None
        )
    return {"mcpServers": servers}


def registered_on(network: str, netuid: int, hotkey: str) -> bool:
    """Whether ``hotkey`` is in the subnet's metagraph (one read)."""
    import bittensor as bt  # noqa: PLC0415

    from .chain import SubtensorReader  # noqa: PLC0415

    state = SubtensorReader(bt.Subtensor(network=network), netuid).read()
    return state.by_hotkey(hotkey) is not None


def parser() -> argparse.ArgumentParser:
    """The miner's flags (Bittensor's names, with the earlier dashed names as aliases)."""
    ap = argparse.ArgumentParser(prog="cadmind-einstein-miner")
    add_version_args(ap)
    add_wallet_args(ap, help_name="the bittensor wallet holding the miner's hotkey")
    add_subtensor_args(ap, network_default="finney", netuid_default=0)
    add_logging_args(ap)
    ap.add_argument("--dev-uri", help="a development key (//Alice): local stacks only")
    ap.add_argument("--validator", required=True, help="the validator's base URL")
    ap.add_argument("--validator-hotkey", required=True, help="the validator's SS58 hotkey")
    ap.add_argument("--platform", required=True, help="the CADmind platform's base URL")
    ap.add_argument("--state", type=Path, required=True, help="a private directory for the key")
    ap.add_argument("--agent-cli", default="claude", help="Claude Code's CLI (default: claude)")
    ap.add_argument("--model", default="", help="passed to the agent; empty: the CLI's default")
    ap.add_argument("--agent-arg", action="append", default=[], help="extra CLI argument")
    ap.add_argument("--agent-command", default="", help="any agent CLI: an argv template")
    ap.add_argument("--prompt-file", type=Path, help="a prompt template replacing the built-in one")
    ap.add_argument("--max-jobs", type=int, default=0, help="0: run forever")
    ap.add_argument(
        "--no-local-fetch",
        dest="local_fetch",
        action="store_false",
        help="no local fetch_to_workspace tool: the agent fetches through the platform only",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    a = ap.parse_args(argv)

    if a.dev_uri:
        from bittensor.sp_core import Keypair  # noqa: PLC0415

        kp: Any = Keypair.create_from_uri(a.dev_uri)
    else:
        if not (a.wallet_name and a.wallet_hotkey):
            ap.error("--wallet.name and --wallet.hotkey (or --dev-uri) are required")
        import bittensor as bt  # noqa: PLC0415

        kp = bt.Wallet(**wallet_kwargs(a)).hotkey
    me = kp.ss58_address
    setup_logging(a, prefix=f"[einstein-miner {me[:8]}]", stream=sys.stdout)
    logger = logging.getLogger("cadmind_einstein.miner")
    template = a.prompt_file.read_text(encoding="utf-8") if a.prompt_file else PROMPT
    a.state.mkdir(parents=True, exist_ok=True)
    a.state.chmod(0o700)
    logs = a.state / "logs"
    logs.mkdir(exist_ok=True)
    work_dir = a.state / "empty"
    work_dir.mkdir(exist_ok=True)

    def log(msg: str) -> None:
        logger.info("%s %s", time.strftime("%H:%M:%S"), msg)

    log(f"cadmind-einstein-miner {__version__} (spec {__spec_version__})")
    if not a.no_version_check:
        check_latest(say=log)
    if a.netuid and not a.dev_uri:
        try:
            if not registered_on(network_of(a), a.netuid, me):
                log(
                    f"WARNING: hotkey {me} is not registered on netuid {a.netuid} "
                    f"({network_of(a)}): the validator will refuse it"
                )
        except Exception as e:  # noqa: BLE001 -- a startup hint only, never fatal
            log(f"registration check skipped: {type(e).__name__}: {e}")

    def call(path: str, body: dict[str, Any]) -> tuple[int, Any]:
        return signed(kp, a.validator, path, body, a.validator_hotkey)

    key_file, mcp_file = a.state / "agent.key", a.state / "watt-mcp.json"
    if key_file.exists():
        key = key_file.read_text().strip()
        log("agent key loaded from state")
    else:
        priv, pub = new_keypair()
        code, got = call("/v1/miners/agent_key", {"x25519_public": base64.b64encode(pub).decode()})
        if code != 200:
            log(f"agent_key refused: {code} {got}")
            return 1
        key = open_sealed(got["sealed"], priv, me)
        _write_private(key_file, key + "\n")
        log(f"agent key {got['key_id']} opened and stored")
    _write_private(mcp_file, json.dumps(mcp_config(a, key, key_file)))

    code, adm = call("/v1/miners/admission", {})
    log(f"admission requested: {code} {adm.get('job') if isinstance(adm, dict) else adm}")

    busy, stop = threading.Event(), threading.Event()

    def heartbeat() -> None:
        while not stop.is_set():
            if busy.is_set():
                call("/v1/jobs/work", {})  # keep alive while the agent runs
            stop.wait(15)

    threading.Thread(target=heartbeat, daemon=True).start()
    done = 0
    while not a.max_jobs or done < a.max_jobs:
        code, work = call("/v1/jobs/work", {})
        if code != 200 or not work.get("job"):
            if code not in (0, 200):
                log(f"work: {code} {work}")
            time.sleep(5)
            continue
        job, ws, brief = work["job"], work["workspace"], work.get("brief") or ""
        if not work.get("accepted"):
            code, _ = call("/v1/jobs/accept", {"job": job})
            log(f"accepted {job} ({work.get('effort')}) in {ws}: {code}")
            code, work = call("/v1/jobs/work", {})  # the deadline starts at accept
        deadline_ms = int(work.get("deadline_ms") or 0) if isinstance(work, dict) else 0
        minutes = max(10, int((deadline_ms / 1000 - time.time()) / 60) - 3) if deadline_ms else 25
        # the job's own deadline for `time_left` (none written: it says no deadline is set)
        deadline = deadline_file(a)
        if deadline is not None:
            _write_private(deadline, str(deadline_ms) if deadline_ms else "")
        deadline_utc = (
            time.strftime("%H:%M", time.gmtime(deadline_ms / 1000))
            if deadline_ms
            else "the job's deadline"
        )
        prompt = template.format(
            ws=ws,
            job=job,
            brief=brief,
            minutes=minutes,
            deadline_utc=deadline_utc,
            server=MCP_SERVER,
            questions=questions_rule(work if isinstance(work, dict) else {}, ws),
            no_questions="yes" if isinstance(work, dict) and work.get("no_questions") else "no",
        )
        prompt_file = a.state / "prompt.txt"
        _write_private(prompt_file, prompt)
        cmd = agent_argv(
            a, prompt=prompt, prompt_file=prompt_file, mcp_file=mcp_file, ws=ws, job=job
        )
        busy.set()
        t0 = time.time()
        out_file = logs / f"{job}.txt"
        try:
            proc = subprocess.run(  # noqa: S603 -- the operator's own configured agent CLI
                cmd,
                input=prompt if a.agent_command else None,
                capture_output=True,
                text=True,
                timeout=minutes * 60,
                cwd=work_dir,
                check=False,
            )
            text = proc.stdout
            out_file.write_text(text + "\n--- stderr ---\n" + proc.stderr)
        except subprocess.TimeoutExpired as e:
            raw = e.stdout or ""
            text = raw.decode() if isinstance(raw, bytes) else raw
            out_file.write_text(text + "\n--- TIMEOUT ---\n")
            log(f"{job}: agent timed out after {minutes} min")
        finally:
            busy.clear()
        log(f"{job}: agent finished in {(time.time() - t0) / 60:.1f} min")
        rej = re.search(r"^REJECT (not-allowed|not-capable):\s*(.+)$", text, re.MULTILINE)
        fin = re.search(r"^DONE\s+(\S+)\s*$", text, re.MULTILINE)
        if rej and not job.startswith("admission-"):
            code, out = call(
                "/v1/jobs/reject", {"job": job, "category": rej.group(1), "message": rej.group(2)}
            )
            log(f"rejected {job}: {code} {out}")
        else:
            digest = fin.group(1) if fin else ""
            code, out = call("/v1/jobs/submit", {"job": job, "workspace": ws, "digest": digest})
            log(f"submitted {job} digest={digest[:16] or '(none)'}: {code} {out}")
        done += 1
    stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
