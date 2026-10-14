"""A local fetch tool for a miner's agent: a small MCP server over stdio, standard library only.

Many sites refuse automated fetches from the platform's address but answer a miner's own home or
office connection. This server gives the agent one tool, ``fetch_to_workspace(workspace, url,
save_as)``: it downloads the document **on the miner's machine** and uploads it into the job's
Watt workspace through the platform's upload route (``POST /api/agent/workspaces/{ws}/upload``),
where it is stored exactly as the hosted ``fetch`` stores a document (``assets/<name>`` with its
sha256, one fetch of the workspace's budget, ``via: "builder"``).

The agent gets no shell: this tool only downloads public http(s) documents and uploads them to the
platform with the miner's agent key. Briefs come from clients, so the download is fenced as a
server-side fetch would be (the agent may be steered to ask for anything):

- ``http`` and ``https`` only, no credentials in the url;
- every address the host resolves to must be public (no loopback, private, link-local, CGNAT,
  multicast or reserved address: the miner's own network stays out of reach), and the connection
  goes to the address that was checked (no second lookup a rebinding DNS could answer differently);
- every redirect (at most 5) is checked again;
- 5 s to connect, 20 s for the whole download, and the platform's 8 MB document cap.

    python -m cadmind_einstein.fetch_mcp --platform https://cadmind.ai --key-file STATE/agent.key

(``cadmind-einstein-fetch-mcp`` is the same program.) The reference miner writes this server into
its MCP config as ``cadmind-fetch`` beside the platform's ``cadmind-watt`` server.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import ipaddress
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote, urljoin, urlsplit

from . import __version__

SERVER_NAME = "cadmind-fetch"
TOOL = "fetch_to_workspace"
MAX_BYTES = 8_000_000
"""The platform's document cap (the hosted fetch's): larger is refused before it is uploaded."""
CONNECT_S = 5.0
TIMEOUT_S = 20.0
MAX_REDIRECTS = 5
UPLOAD_TIMEOUT_S = 120.0
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
USER_AGENT = (
    f"cadmind-einstein-miner/{__version__} "
    "(+https://github.com/cadmindai/cadmind-einstein; datasheet fetch for a client's device)"
)

Resolver = Callable[[str, int], str]


class FetchError(Exception):
    """A download or upload refused; the message is the agent's to read."""


@dataclass
class Download:
    url: str
    final_url: str
    status: int
    content_type: str
    data: bytes


# -- the download ---------------------------------------------------------------------------------


def public_address(host: str, port: int) -> str:
    """The first address ``host`` resolves to, when every address it resolves to is public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"{host} does not resolve ({e})") from None
    addrs = [str(i[4][0]) for i in infos]
    if not addrs:
        raise FetchError(f"{host} has no address")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise FetchError(f"{host} resolves to {a}, which is not a public address")
    return addrs[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


def download(
    url: str,
    *,
    max_bytes: int = MAX_BYTES,
    connect_s: float = CONNECT_S,
    timeout_s: float = TIMEOUT_S,
    resolver: Resolver | None = None,
    user_agent: str = USER_AGENT,
) -> Download:
    """GET ``url`` within the fences above; ``resolver(host, port) -> ip`` replaces the
    public-address check (tests only)."""
    check = resolver or public_address
    deadline = time.monotonic() + timeout_s
    current = url.strip()
    for _ in range(MAX_REDIRECTS + 1):
        try:
            parts = urlsplit(current)
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise FetchError(f"{current[:200]!r} is not a url") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError(f"{current[:200]!r}: only http and https urls")
        if parts.username or parts.password:
            raise FetchError("a url with credentials is refused")
        ip = check(parts.hostname, port)
        left = deadline - time.monotonic()
        if left <= 0:
            raise FetchError(f"{url}: not downloaded within {timeout_s:g} s")
        cls = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
        conn = cls(parts.hostname, ip, port, min(left, connect_s))
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        phase = "did not accept a connection in time"
        try:
            conn.connect()
            phase = "did not answer in time"
            sock = conn.sock
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            conn.request(
                "GET",
                path,
                headers={"User-Agent": user_agent, "Accept": "*/*", "Accept-Encoding": "identity"},
            )
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location") or ""
                if not loc:
                    raise FetchError(f"{current}: a redirect with no Location")
                current = urljoin(current, loc)
                continue
            if resp.status != 200:
                raise FetchError(f"{current} answered HTTP {resp.status}")
            phase = f"not downloaded within {timeout_s:g} s"
            declared = int(resp.getheader("Content-Length") or 0)
            if declared > max_bytes:
                raise FetchError(f"{current} is {declared} bytes, over the {max_bytes}-byte cap")
            chunks, total = [], 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise FetchError(f"{current}: not downloaded within {timeout_s:g} s")
                if sock.fileno() != -1:
                    sock.settimeout(max(0.05, left))
                chunk = resp.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(f"{current} is over the {max_bytes}-byte cap")
                chunks.append(chunk)
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            return Download(url, current, resp.status, ctype, b"".join(chunks))
        except TimeoutError:
            raise FetchError(f"{current}: {phase}") from None
        except ssl.SSLError as e:
            raise FetchError(f"{current}: TLS failed ({type(e).__name__})") from None
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise FetchError(f"{current}: {type(e).__name__}: {e}") from None
        finally:
            conn.close()
    raise FetchError(f"{url}: more than {MAX_REDIRECTS} redirects")


# -- the upload -----------------------------------------------------------------------------------


def upload(platform: str, key: str, workspace: str, got: Download, save_as: str) -> dict[str, Any]:
    """Store ``got`` in the workspace through the platform's upload route; its answer."""
    body = {
        "url": got.final_url,
        "save_as": save_as,
        "data_base64": base64.b64encode(got.data).decode("ascii"),
        "retrieved": time.strftime("%Y-%m-%d", time.gmtime()),
        "content_type": got.content_type,
    }
    req = urllib.request.Request(  # noqa: S310 -- the operator's own platform URL (http/https checked)
        f"{platform.rstrip('/')}/api/agent/workspaces/{quote(workspace, safe='')}/upload",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_S) as r:  # noqa: S310
            out = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error") or ""
        except ValueError:
            msg = ""
        raise FetchError(f"the platform refused the upload (HTTP {e.code}): {msg}") from None
    except (OSError, ValueError) as e:
        raise FetchError(f"the platform did not take the upload: {type(e).__name__}") from None
    return out if isinstance(out, dict) else {"answer": out}


# -- the MCP server -------------------------------------------------------------------------------

TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL,
    "title": "Fetch a document from this builder's own connection",
    "description": (
        "Download a document (a maker's product page, a datasheet, a drawing, a standard's "
        "page) from THIS builder's own internet connection and store it in the Watt workspace's "
        "assets/<save_as>, exactly as `fetch` stores one. Many sites that block the hosted "
        "`fetch` answer this one. "
        "Answers the stored path, sha256, size, type and date for the `source` record (add "
        'fetched_by = "builder"), and the text of an HTML or text page; a stored PDF is read with '
        "read_file (by pages). Public http(s) addresses only, 8 MB at most, 20 s. One fetch of the "
        "workspace's budget, as `fetch`."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workspace": {"type": "string", "description": "the job's workspace id (ag-...)"},
            "url": {"type": "string", "description": "the http(s) address of the document"},
            "save_as": {"type": "string", "description": "a file name, e.g. ds_magnet.pdf"},
        },
        "required": ["workspace", "url", "save_as"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
}


class Server:
    """One stdio session: JSON-RPC 2.0 messages, one per line."""

    def __init__(
        self,
        platform: str,
        key: str,
        *,
        resolver: Resolver | None = None,
        connect_s: float = CONNECT_S,
        timeout_s: float = TIMEOUT_S,
        max_bytes: int = MAX_BYTES,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.platform, self.key = platform, key
        self.fences: dict[str, Any] = {
            "resolver": resolver,
            "connect_s": connect_s,
            "timeout_s": timeout_s,
            "max_bytes": max_bytes,
            "user_agent": user_agent,
        }

    def fetch_to_workspace(self, args: dict[str, Any]) -> dict[str, Any]:
        ws, url, save_as = args.get("workspace"), args.get("url"), args.get("save_as")
        if not all(isinstance(x, str) and x for x in (ws, url, save_as)):
            raise FetchError(f"{TOOL} takes workspace, url and save_as (strings)")
        got = download(str(url), **self.fences)
        out = upload(self.platform, self.key, str(ws), got, str(save_as))
        if got.final_url != got.url:
            out.setdefault("asked", got.url)
        return out

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """The answer to one message; None for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "not a JSON-RPC 2.0 request")
        ident, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if ident is None:
            return None  # notifications (initialized, cancelled) need no answer
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            return _result(
                ident,
                {
                    "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                },
            )
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": [TOOL_SCHEMA]})
        if method == "tools/call":
            if not isinstance(params, dict) or params.get("name") != TOOL:
                return _error(ident, -32602, f"no tool {params.get('name')!r}; the tool is {TOOL}")
            args = params.get("arguments") or {}
            try:
                out = self.fetch_to_workspace(args if isinstance(args, dict) else {})
            except FetchError as e:
                return _result(ident, {"content": [_text(str(e))], "isError": True})
            return _result(
                ident, {"content": [_text(json.dumps(out, ensure_ascii=False, indent=1))]}
            )
        return _error(ident, -32601, f"no method {method!r}")

    def serve(self, stdin: IO[str], stdout: IO[str]) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                answer: dict[str, Any] | None = _error(None, -32700, "not JSON")
            else:
                answer = self.handle(msg)
            if answer is not None:
                stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
                stdout.flush()


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _result(ident: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def server_config(platform: str, key_file: Path) -> dict[str, Any]:
    """This server as an ``mcpServers`` entry (stdio): the same Python that runs the miner."""
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": [
            "-m",
            "cadmind_einstein.fetch_mcp",
            "--platform",
            platform,
            "--key-file",
            str(key_file),
        ],
    }


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cadmind-einstein-fetch-mcp", description=__doc__.split("\n")[0]
    )
    ap.add_argument("--platform", required=True, help="the CADmind platform's base URL")
    ap.add_argument("--key-file", type=Path, required=True, help="the miner's agent key (0600)")
    ap.add_argument("--connect-s", type=float, default=CONNECT_S, help="seconds to connect")
    ap.add_argument("--timeout-s", type=float, default=TIMEOUT_S, help="seconds for a download")
    ap.add_argument("--user-agent", default=USER_AGENT, help="the User-Agent the download sends")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    if urlsplit(a.platform).scheme not in ("http", "https"):
        print("--platform: an http(s) URL", file=sys.stderr)  # noqa: T201
        return 2
    key = a.key_file.read_text(encoding="utf-8").strip()
    if not key:
        print(f"{a.key_file}: no agent key", file=sys.stderr)  # noqa: T201
        return 2
    Server(
        a.platform,
        key,
        connect_s=a.connect_s,
        timeout_s=a.timeout_s,
        user_agent=a.user_agent,
    ).serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
