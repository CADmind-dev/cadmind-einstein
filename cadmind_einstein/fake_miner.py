"""A scripted miner for local end-to-end runs (DESIGN.md): no model, the miner's side of the
protocol for real.

It asks the validator for its agent key (signed; the secret sealed to an ephemeral X25519 key),
polls ``work``, accepts, and builds a reference Watt project in the job workspace **through the
platform's MCP server** -- each file written with ``write_file``, its assets fetched with ``fetch``,
then ``watt check``, ``build``, ``test`` and ``test --mutate`` -- reads the digest with ``watt
status`` and submits.

    python -m cadmind_einstein.fake_miner --uri //Alice --validator http://127.0.0.1:8793 \
        --validator-hotkey 5V... --platform http://127.0.0.1:PORT --project wall-hook [--jobs 1]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .sealing import new_keypair, open_sealed


def signed(
    keypair: Any,
    base: str,
    path: str,
    body: Mapping[str, Any],
    receiver: str,
    *,
    require_signed: bool = True,
) -> tuple[int, Any]:
    """POST a ``btauth/1``-signed request to the validator ``receiver``; its answer must carry the
    validator's signature over this request (:func:`.service.verify_response`), or it is taken as
    no answer (status 0) -- unless ``require_signed`` is off."""
    import bittensor.http_auth as btauth  # noqa: PLC0415

    from .service import verify_response  # noqa: PLC0415

    raw = json.dumps(dict(body)).encode()
    auth = btauth.sign(keypair, method="POST", path=path, body=raw, receiver_ss58=receiver)
    headers = {"Content-Type": "application/json", **auth}
    req = urllib.request.Request(base.rstrip("/") + path, data=raw, headers=headers)  # noqa: S310 -- configured
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            code, answer, got = r.status, r.read(), dict(r.headers.items())
    except urllib.error.HTTPError as e:
        code, answer, got = e.code, e.read(), dict(e.headers.items())
    except OSError as e:  # the validator restarting: said as no answer, and the loop polls again
        return 0, {"error": f"validator unreachable: {type(e).__name__}"}
    if require_signed and not verify_response(
        got,
        answer,
        status=code,
        method="POST",
        path=path,
        nonce=auth["X-Bittensor-Nonce"],
        hotkey=auth["X-Bittensor-Hotkey"],
        validator=receiver,
    ):
        return 0, {"error": f"the validator's answer ({code}) is not signed by {receiver}"}
    try:
        return code, json.loads(answer or b"{}")
    except ValueError:
        return code, {"error": "the validator's answer is not JSON"}


class Mcp:
    """The platform's MCP server, as any MCP client reaches it: JSON-RPC over HTTP with the agent
    key."""

    def __init__(self, platform: str, key: str) -> None:
        self.url, self.key, self.n = platform.rstrip("/") + "/mcp", key, 0

    def _rpc(self, method: str, params: Mapping[str, Any]) -> Any:
        self.n += 1
        raw = json.dumps(
            {"jsonrpc": "2.0", "id": self.n, "method": method, "params": dict(params)}
        ).encode()
        req = urllib.request.Request(  # noqa: S310 -- a configured http(s) URL
            self.url,
            data=raw,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        for wait in (2, 5, 10, 20, 30, 0):  # the platform's per-key request rate: wait it out
            try:
                with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310
                    out = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or not wait:
                    raise
                time.sleep(min(60.0, float(e.headers.get("Retry-After") or wait)))
        if "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return out["result"]

    def init(self) -> None:
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fake-miner", "version": "1"},
            },
        )

    def call(self, tool: str, **args: Any) -> Any:
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        text = "".join(
            c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"
        )
        try:
            out = json.loads(text)
        except ValueError:
            out = {"text": text}
        if res.get("isError"):
            raise RuntimeError(f"{tool}: {text[:300]}")
        return out

    def watt(self, ws: str, command: str, **args: Any) -> Any:
        out = self.call("watt", workspace=ws, command=command, wait_s=100, **args)
        while out.get("state") == "running":
            out = self.call("watt_wait", run=out["run"], wait_s=100)
        return out


def build(mcp: Mcp, ws: str, project: Path, log: Any) -> str:
    """Rebuild the reference project in the workspace; answer the digest Watt reports."""
    sources = tomllib.loads((project / "spec" / "sources.toml").read_text()).get("source", {})
    for s in sources.values():
        if s.get("asset"):
            mcp.call("fetch", workspace=ws, url=s["url"], save_as=Path(s["asset"]).name)
    files = (
        sorted(project.glob("spec/*.toml"))
        + sorted(project.rglob("src/**/*.py"))
        + sorted(project.glob("tests/test_*.py"))
    )
    for f in files:
        mcp.call(
            "write_file",
            workspace=ws,
            path=f.relative_to(project).as_posix(),
            content=f.read_text(),
        )
    log(f"wrote {len(files)} files")
    for command, args in (("check", {}), ("build", {}), ("test", {}), ("test", {"mutate": True})):
        out = mcp.watt(ws, command, **args)
        log(f"watt {command}{' --mutate' if args else ''}: {'ok' if out.get('ok') else 'errors'}")
    status = mcp.watt(ws, "status")
    st = ((status.get("data") or {}).get("status")) or {}
    log(f"status: stage {st.get('stage')}, deliverable {st.get('deliverable')}")
    return str(st.get("digest") or "")


def main(argv: list[str] | None = None) -> int:
    from bittensor.sp_core import Keypair  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="fake-miner")
    ap.add_argument("--uri", required=True)
    ap.add_argument("--validator", required=True)
    ap.add_argument("--validator-hotkey", required=True)
    ap.add_argument("--platform", required=True)
    ap.add_argument("--project", type=Path, required=True, help="the reference project to rebuild")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--idle-s", type=float, default=900.0)
    ap.add_argument("--fail", action="store_true", help="accept, then submit without building")
    ap.add_argument("--reject", default="", help="reject every job with this message (not-allowed)")
    a = ap.parse_args(argv)
    kp = Keypair.create_from_uri(a.uri)
    me = kp.ss58_address

    def log(msg: str) -> None:
        print(f"[fake-miner {me[:8]}] {msg}", file=sys.stderr, flush=True)  # noqa: T201

    priv, pub = new_keypair()
    code, got = signed(
        kp,
        a.validator,
        "/v1/miners/agent_key",
        {"x25519_public": base64.b64encode(pub).decode()},
        a.validator_hotkey,
    )
    if code != 200:
        log(f"agent_key refused: {code} {got}")
        return 1
    key = open_sealed(got["sealed"], priv, me)
    log(f"agent key {got['key_id']} opened ({key[:6]}...)")
    mcp = Mcp(a.platform, key)
    mcp.init()
    code, adm = signed(kp, a.validator, "/v1/miners/admission", {}, a.validator_hotkey)
    log(f"admission requested: {code} {adm.get('job') or adm}")
    done, idle_until = 0, time.monotonic() + a.idle_s
    target = a.jobs + 1  # the admission test, then the client jobs
    while done < target and time.monotonic() < idle_until:
        code, work = signed(kp, a.validator, "/v1/jobs/work", {}, a.validator_hotkey)
        if code != 200 or not work.get("job"):
            time.sleep(2)
            continue
        job, ws = work["job"], work["workspace"]
        if a.reject and not job.startswith("admission-"):
            code, out = signed(
                kp,
                a.validator,
                "/v1/jobs/reject",
                {"job": job, "category": "not-allowed", "message": a.reject},
                a.validator_hotkey,
            )
            log(f"rejected {job}: {code} {out}")
            done += 1
            continue
        signed(kp, a.validator, "/v1/jobs/accept", {"job": job}, a.validator_hotkey)
        log(f"accepted {job} in {ws}")
        digest = (
            "" if a.fail and not job.startswith("admission-") else build(mcp, ws, a.project, log)
        )
        code, out = signed(
            kp,
            a.validator,
            "/v1/jobs/submit",
            {"job": job, "workspace": ws, "digest": digest},
            a.validator_hotkey,
        )
        log(f"submitted {job}: {code} {out}")
        done += 1
        idle_until = time.monotonic() + a.idle_s
    return 0


if __name__ == "__main__":
    sys.exit(main())
