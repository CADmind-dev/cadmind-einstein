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
``mcpServers`` JSON file naming the ``cadmind-watt`` server, mode 0600), ``{model}``,
``{workspace}`` and ``{job}``. The agent's last line must be ``DONE <digest>`` or
``REJECT <not-allowed|not-capable>: <message for the client>``.

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
from .sealing import new_keypair, open_sealed
from .version import check_latest

MCP_SERVER = "cadmind-watt"

PROMPT = """You are a CADmind einstein miner. Build this client's device in the Watt workspace \
`{ws}` (made from the brief), using ONLY the {server} MCP tools.

Start with `watt_guide` (brief, then primer, then scope). Work fast and carefully:
1. First write DESIGN-NOTES.md: how the device is made, installed, used and serviced, what can go \
wrong at each step and the requirement/test proving it does not; every critical number sourced \
(`fetch` into the project's assets/), never invented. Fetch only what the first sketch needs, then \
build.
2. Iterate with check / build / test; edit files, never rewrite them whole; run the full `test` \
with mutate once at the end (`watt_wait`), only after a passing scoped guard (mutate with several \
`only`, one run) covering every must requirement, never as a first check.
3. Self-review: load paths, retention, clearances, every clause tested; fix the weakest points.
Never weaken a test to pass. What only the client can decide is an open decision, designed around \
your best sourced assumption.
A `watt` run far longer than usual: stop it with `watt_cancel` and change the design.

If the scope guide says we may not or cannot build it, end with exactly one line:
REJECT <not-allowed|not-capable>: <message for the client>
Otherwise work until `watt` status says deliverable, then end with exactly one line:
DONE <digest>
(the digest `watt` status reports). You have about {minutes} minutes.

Job: {job}
Brief:
{brief}
"""


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
        f"mcp__{MCP_SERVER}",
        "--output-format",
        "text",
    ]
    if a.model:
        argv += ["--model", a.model]
    return argv + list(a.agent_arg)


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
    cfg = {
        "mcpServers": {
            MCP_SERVER: {
                "type": "http",
                "url": a.platform.rstrip("/") + "/mcp",
                "headers": {"Authorization": f"Bearer {key}"},
            }
        }
    }
    _write_private(mcp_file, json.dumps(cfg))

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
        prompt = template.format(ws=ws, job=job, brief=brief, minutes=minutes, server=MCP_SERVER)
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
