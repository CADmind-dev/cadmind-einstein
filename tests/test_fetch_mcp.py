"""The local fetch tool (:mod:`cadmind_einstein.fetch_mcp`): a fenced download on the miner's own
machine -- public http(s) only, every redirect checked, size and time capped -- uploaded to the
platform's upload route with the miner's agent key, served as a stdio MCP server. No real network:
a local site and a local platform, with the public-address check replaced where a test needs the
loopback site."""

from __future__ import annotations

import base64
import io
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from cadmind_einstein import agent_miner
from cadmind_einstein.fetch_mcp import (
    TOOL,
    FetchError,
    Server,
    download,
    public_address,
    server_config,
)

PDF = b"%PDF-1.7\n" + b"datasheet " * 100


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a: object) -> None:
        return

    def _send(self, code: int, body: bytes, ctype: str = "application/pdf", **headers: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        p = self.path
        if p == "/ds.pdf":
            return self._send(200, PDF)
        if p == "/moved":
            return self._send(302, b"", Location="/ds.pdf")
        if p == "/to-private":
            return self._send(302, b"", Location="http://10.0.0.7/admin")
        if p == "/to-metadata":
            return self._send(302, b"", Location="http://169.254.169.254/latest/meta-data/")
        if p == "/loop":
            return self._send(302, b"", Location="/loop")
        if p == "/big-declared":
            return self._big(True)
        if p == "/big-streamed":
            return self._big(False)
        if p == "/hang":
            time.sleep(2)
            return None
        if p == "/trickle":  # answers, then streams slower than the whole-download limit
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            for _ in range(20):
                self.wfile.write(b"%PDF" if _ == 0 else b"x" * 10)
                self.wfile.flush()
                time.sleep(0.1)
            return None
        if p == "/403":
            return self._send(403, b"<html>bot check</html>", "text/html")
        return self._send(404, b"no", "text/plain")

    def _big(self, declared: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        if declared:
            self.send_header("Content-Length", str(10_000))
        self.end_headers()
        self.wfile.write(b"%PDF" + b"x" * 9_996)


def loopback_only(host: str, port: int) -> str:
    """The test site is on loopback: allow 127.0.0.1 alone, check every other host as the tool
    does."""
    return "127.0.0.1" if host == "127.0.0.1" else public_address(host, port)


class Platform:
    """The platform's upload route, recording what it was sent."""

    def __init__(self, status: int = 200) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                return

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                fake.calls.append((self.path, dict(self.headers), body))
                if status != 200:
                    out = {"error": "the fetch budget is spent", "code": "x"}
                else:
                    out = {
                        "path": f"assets/{body['save_as']}",
                        "sha256": "ab" * 32,
                        "via": "builder",
                    }
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv, self.url = _serve(H)


class TestDownloadFences(unittest.TestCase):
    def setUp(self) -> None:
        self.site, self.base = _serve(Site)
        self.addCleanup(self.site.shutdown)

    def get(self, path: str, **kw: Any):
        return download(self.base + path, resolver=loopback_only, **kw)

    def test_a_document_and_a_checked_redirect(self) -> None:
        got = self.get("/ds.pdf")
        self.assertEqual((got.status, got.content_type, got.data), (200, "application/pdf", PDF))
        moved = self.get("/moved")
        self.assertEqual((moved.final_url, moved.data), (self.base + "/ds.pdf", PDF))

    def test_the_miners_own_network_is_out_of_reach(self) -> None:
        for url in (
            "http://127.0.0.1/x",
            "http://localhost/x",
            "http://10.1.2.3/x",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/x",
            "http://[::1]/x",
            "http://[::ffff:127.0.0.1]/x",
            "http://0.0.0.0/x",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(FetchError, "not a public address"):
                download(url)

    def test_a_name_resolving_to_a_private_address_is_refused(self) -> None:
        def private(*a: object, **k: object) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.10", 80))]

        with (
            mock.patch("socket.getaddrinfo", private),
            self.assertRaisesRegex(FetchError, "not a public"),
        ):
            download("http://intranet.example/x")

    def test_a_redirect_into_the_private_network_is_refused(self) -> None:
        for path in ("/to-private", "/to-metadata"):
            with self.subTest(path=path), self.assertRaisesRegex(FetchError, "not a public"):
                self.get(path)
        with self.assertRaisesRegex(FetchError, "redirects"):
            self.get("/loop")

    def test_only_http_and_https_without_credentials(self) -> None:
        for url in ("ftp://example.com/a", "file:///etc/passwd", "gopher://x/", "not a url"):
            with self.subTest(url=url), self.assertRaisesRegex(FetchError, "only http and https"):
                download(url)
        with self.assertRaisesRegex(FetchError, "credentials"):
            download("https://user:pw@example.com/a")

    def test_size_is_capped_declared_or_streamed(self) -> None:
        for path in ("/big-declared", "/big-streamed"):
            with self.subTest(path=path), self.assertRaisesRegex(FetchError, "cap"):
                self.get(path, max_bytes=5_000)
        self.assertEqual(len(self.get("/big-streamed", max_bytes=20_000).data), 10_000)

    def test_time_is_capped(self) -> None:
        t = time.monotonic()
        with self.assertRaisesRegex(FetchError, "did not answer in time"):
            self.get("/hang", timeout_s=0.5)
        with self.assertRaisesRegex(FetchError, "within"):
            self.get("/trickle", timeout_s=0.8)
        self.assertLess(time.monotonic() - t, 3)

    def test_a_connection_that_never_opens_fails_at_the_connect_limit(self) -> None:
        seen: list[float] = []

        def stuck(addr: Any, timeout: float = 0, *a: Any) -> socket.socket:
            seen.append(timeout)
            raise TimeoutError("timed out")

        with (
            mock.patch("socket.create_connection", stuck),
            self.assertRaisesRegex(FetchError, "did not accept a connection"),
        ):
            download("http://203.0.113.9/x", resolver=lambda h, p: h, connect_s=1.5)
        self.assertEqual(seen, [1.5])

    def test_an_http_error_says_so(self) -> None:
        with self.assertRaisesRegex(FetchError, "HTTP 403"):
            self.get("/403")


class TestTheTool(unittest.TestCase):
    def setUp(self) -> None:
        self.site, self.base = _serve(Site)
        self.addCleanup(self.site.shutdown)
        self.platform = Platform()
        self.addCleanup(self.platform.srv.shutdown)
        self.server = Server(self.platform.url, "cma_secret", resolver=loopback_only)

    def rpc(self, server: Server, *msgs: dict[str, Any]) -> list[dict[str, Any]]:
        out = io.StringIO()
        server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs) + "\n"), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def call(self, server: Server, **args: Any) -> dict[str, Any]:
        msg = {"name": TOOL, "arguments": args}
        return self.rpc(server, {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": msg})[
            0
        ]["result"]

    def test_the_upload_call_shape(self) -> None:
        got = self.call(self.server, workspace="ag-0123", url=self.base + "/moved", save_as="ds")
        self.assertNotIn("isError", got)
        answer = json.loads(got["content"][0]["text"])
        self.assertEqual((answer["path"], answer["via"]), ("assets/ds", "builder"))
        self.assertEqual(answer["asked"], self.base + "/moved")
        path, headers, body = self.platform.calls[0]
        self.assertEqual(path, "/api/agent/workspaces/ag-0123/upload")
        self.assertEqual(headers["Authorization"], "Bearer cma_secret")
        self.assertEqual(base64.b64decode(body["data_base64"]), PDF)
        self.assertEqual(body["url"], self.base + "/ds.pdf")  # the final url, after the redirect
        self.assertEqual((body["save_as"], body["content_type"]), ("ds", "application/pdf"))
        self.assertRegex(body["retrieved"], r"^\d{4}-\d{2}-\d{2}$")

    def test_refusals_are_tool_errors_and_nothing_is_uploaded(self) -> None:
        for args in (
            {"workspace": "ag-1", "url": "http://10.0.0.1/x", "save_as": "x.pdf"},
            {"workspace": "ag-1", "url": self.base + "/to-metadata", "save_as": "x.pdf"},
            {"workspace": "ag-1", "url": "file:///etc/passwd", "save_as": "x"},
            {"workspace": "ag-1", "url": self.base + "/ds.pdf"},
        ):
            with self.subTest(args=args):
                got = self.call(self.server, **args)
                self.assertTrue(got["isError"])
        self.assertEqual(self.platform.calls, [])

    def test_a_platform_refusal_reaches_the_agent(self) -> None:
        refusing = Platform(status=400)
        self.addCleanup(refusing.srv.shutdown)
        server = Server(refusing.url, "cma_secret", resolver=loopback_only)
        got = self.call(server, workspace="ag-1", url=self.base + "/ds.pdf", save_as="x.pdf")
        self.assertTrue(got["isError"])
        self.assertIn("budget is spent", got["content"][0]["text"])

    def test_the_mcp_session(self) -> None:
        out = self.rpc(
            self.server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "shell"}},
        )
        self.assertEqual(
            [m["id"] for m in out], [1, 2, 3, 4, 5]
        )  # the notification is not answered
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([t["name"] for t in out[1]["result"]["tools"]], [TOOL])
        self.assertEqual(out[3]["error"]["code"], -32601)
        self.assertEqual(out[4]["error"]["code"], -32602)


class TestMinerWiring(unittest.TestCase):
    def ns(self, **kw: Any) -> Any:
        import argparse

        base: dict[str, Any] = {
            "agent_command": "",
            "agent_cli": "claude",
            "model": "",
            "agent_arg": [],
            "platform": "https://cadmind.example/",
            "local_fetch": True,
        }
        return argparse.Namespace(**(base | kw))

    def test_the_config_names_both_servers_and_the_key_file_not_the_key(self) -> None:
        cfg = agent_miner.mcp_config(self.ns(), "cma_k", Path("/s/agent.key"))["mcpServers"]
        self.assertEqual(set(cfg), {"cadmind-watt", "cadmind-fetch"})
        local = cfg["cadmind-fetch"]
        self.assertEqual(local["type"], "stdio")
        self.assertEqual(local["args"][:2], ["-m", "cadmind_einstein.fetch_mcp"])
        self.assertIn("/s/agent.key", local["args"])
        self.assertNotIn("cma_k", json.dumps(local))
        self.assertEqual(server_config("https://p", Path("/k"))["args"][3], "https://p")
        rel = agent_miner.mcp_config(self.ns(), "k", Path("state/agent.key"))["mcpServers"]
        self.assertIn(str(Path("state/agent.key").resolve()), rel["cadmind-fetch"]["args"])
        off = agent_miner.mcp_config(self.ns(local_fetch=False), "k", Path("/k"))["mcpServers"]
        self.assertEqual(set(off), {"cadmind-watt"})

    def test_claude_is_allowed_the_watt_tools_and_the_one_local_tool_only(self) -> None:
        argv = agent_miner.agent_argv(
            self.ns(),
            prompt="P",
            prompt_file=Path("/p"),
            mcp_file=Path("/m"),
            ws="w",
            job="j",
        )
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertEqual(allowed, "mcp__cadmind-watt,mcp__cadmind-fetch__fetch_to_workspace")
        self.assertIn("--strict-mcp-config", argv)
        argv = agent_miner.agent_argv(
            self.ns(local_fetch=False),
            prompt="P",
            prompt_file=Path("/p"),
            mcp_file=Path("/m"),
            ws="w",
            job="j",
        )
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__cadmind-watt")
        self.assertFalse(
            agent_miner.parser().parse_args([*self._required, "--no-local-fetch"]).local_fetch
        )
        self.assertTrue(agent_miner.parser().parse_args(self._required).local_fetch)

    _required: ClassVar[list[str]] = [
        "--validator",
        "https://v",
        "--validator-hotkey",
        "5X",
        "--platform",
        "https://p",
        "--state",
        "/tmp/s",  # noqa: S108 -- parsed, never created
    ]

    def test_the_prompt_prefers_the_local_fetch(self) -> None:
        text = agent_miner.PROMPT.format(ws="w", job="j", brief="b", minutes=30, server="s")
        self.assertIn("fetch_to_workspace", text)
        self.assertIn('fetched_by = "builder"', text)


if __name__ == "__main__":
    unittest.main()


class TestTheProgram(unittest.TestCase):
    def test_it_speaks_mcp_over_stdio(self) -> None:
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "agent.key"
            key.write_text("cma_k\n")
            msgs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
            proc = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "cadmind_einstein.fetch_mcp",
                    "--platform",
                    "https://p",
                    "--key-file",
                    str(key),
                ],
                input="".join(json.dumps(m) + "\n" for m in msgs),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        out = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "cadmind-fetch")
        self.assertEqual(out[1]["result"]["tools"][0]["name"], TOOL)
