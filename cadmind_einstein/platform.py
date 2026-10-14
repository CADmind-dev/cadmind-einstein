"""The validator's client of the platform's agent API (DESIGN.md): its own agent key, which the
platform recognises as the subnet operator's. Operator routes mint
miners' keys and grant, freeze and fork job workspaces; the ordinary agent routes run ``watt`` in
the validator's own forks and export them.

Stdlib only. Every failure is a :class:`PlatformError` carrying the HTTP status and the platform's
code.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote


class PlatformError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status, self.code = status, code


class OperatorClient:
    def __init__(
        self, base_url: str, key: str, *, timeout: float = 160.0, queue_url: str = ""
    ) -> None:
        self.base, self.key, self.timeout = base_url.rstrip("/"), key, timeout
        self.queue = (queue_url or base_url).rstrip("/")

    def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        raw: bool = False,
        timeout: float | None = None,
        base: str = "",
    ) -> Any:
        data = json.dumps(dict(body)).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 -- a configured http(s) URL
            (base or self.base) + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:  # noqa: S310 -- configured
                out = r.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            raise PlatformError(
                str(err.get("error") or e.reason), e.code, str(err.get("code") or "")
            ) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise PlatformError(f"platform unreachable: {e}") from None
        return out if raw else json.loads(out or b"{}")

    @staticmethod
    def _ws(ws: str) -> str:
        return quote(ws, safe="")

    # -- operator routes ---------------------------------------------------------------------------

    def mint_key(self, hotkey: str) -> dict[str, Any]:
        return dict(self._call("POST", "/api/agent/subnet/keys", {"hotkey": hotkey}))

    def disable_key(self, hotkey: str) -> dict[str, Any]:
        return dict(self._call("POST", "/api/agent/subnet/keys/disable", {"hotkey": hotkey}))

    def grant(self, hotkey: str, *, brief: str = "", seed: str = "", name: str = "job") -> str:
        body: dict[str, Any] = {"hotkey": hotkey, "name": name}
        body.update({"seed": seed} if seed else {"brief": brief})
        return str(
            self._call("POST", "/api/agent/subnet/workspaces", body, timeout=320)["workspace"]
        )

    def state(self, ws: str) -> dict[str, Any]:
        return dict(self._call("GET", f"/api/agent/subnet/workspaces/{self._ws(ws)}"))

    def freeze(self, ws: str) -> dict[str, Any]:
        return dict(self._call("POST", f"/api/agent/subnet/workspaces/{self._ws(ws)}/freeze", {}))

    def fork(self, ws: str) -> str:
        return str(
            self._call(
                "POST", f"/api/agent/subnet/workspaces/{self._ws(ws)}/fork", {}, timeout=320
            )["workspace"]
        )

    # -- the platform's single job queue (engines pull) --------------------------------------------

    def claim(self) -> dict[str, Any] | None:
        """One einstein job from the platform's queue, now leased to this validator, or None:
        ``{job, brief, effort, privacy, seed?, rerun_of?, trust}``."""
        out = self._call("POST", "/api/engine/einstein/claim", {}, base=self.queue)
        job = out.get("job")
        return dict(job) if isinstance(job, dict) else None

    def lease(self, jobs: list[str]) -> dict[str, Any]:
        """Renew the lease on the jobs we hold; ``lost`` lists those the platform no longer gives us
        (cancelled, or re-queued after a lapsed lease)."""
        return dict(
            self._call("POST", "/api/engine/einstein/lease", {"jobs": list(jobs)}, base=self.queue)
        )

    def claimed(self) -> list[dict[str, Any]]:
        """The jobs the platform holds leased to this validator (reconciled after a restart)."""
        out = self._call("GET", "/api/engine/einstein/claimed", base=self.queue)
        return [dict(j) for j in out.get("jobs") or []]

    def complete(self, job: str, result: Mapping[str, Any]) -> None:
        """A job's end: delivered (charged once) or declined / out of reach / didn't finish
        (not)."""
        self._call(
            "POST",
            f"/api/engine/einstein/jobs/{quote(job, safe='')}/result",
            {"result": dict(result)},
            base=self.queue,
        )

    # -- the validator's own workspaces ------------------------------------------------------------

    def run(
        self,
        ws: str,
        command: str,
        args: Mapping[str, Any] | None = None,
        *,
        budget_s: float = 1800.0,
    ) -> dict[str, Any]:
        """A `watt` command in one of the validator's workspaces, waited to its end."""
        out = dict(
            self._call(
                "POST",
                f"/api/agent/workspaces/{self._ws(ws)}/run",
                {"command": command, "args": dict(args or {}), "wait_s": 100},
            )
        )
        end = time.monotonic() + budget_s
        while out.get("state") == "running" and time.monotonic() < end:
            out = dict(
                self._call("GET", f"/api/agent/runs/{quote(str(out['run']), safe='')}?wait_s=100")
            )
        return out

    def export(self, ws: str) -> bytes:
        return bytes(
            self._call("GET", f"/api/agent/workspaces/{self._ws(ws)}/export", raw=True, timeout=320)
        )

    def delete(self, ws: str) -> None:
        try:
            self._call("DELETE", f"/api/agent/workspaces/{self._ws(ws)}")
        except PlatformError as e:
            if e.status != 404:
                raise
