"""The einstein validator's HTTP face (DESIGN.md). Stdlib ``http.server``.

Miners (``btauth/1``, signed by the miner's hotkey and addressed to the validator's; registered on
the metagraph):

- ``POST /v1/miners/agent_key {x25519_public}`` -- the hotkey-bound agent key, sealed to that key;
- ``POST /v1/jobs/work`` -- the miner's assignment, if any (poll every few seconds: it is also how
  the validator knows the miner is alive);
- ``POST /v1/jobs/accept {job}``, ``POST /v1/jobs/submit {job, workspace, digest}``;
- ``POST /v1/miners/admission`` -- its entry test, a job assigned to it alone;
- ``POST /v1/miners/receipt`` -- its jobs, their scores, its weight and quality, and its recent
  attempts that did not pass with why (``failures``: a gate failure says what the gate found).

The platform and the operator (Bearer): ``GET /v1/capacity`` (active and free admitted miners, the
median job time: the platform's single queue computes positions and ETAs), ``POST /v1/cancel {job}``
(a claimed job: not charged), ``POST /v1/signals {job, kind, stars?, trust}`` (the client's
download, stars or change request), ``GET /v1/jobs/{job}``, ``GET /v1/scores``,
``POST /v1/weights/close`` (the hour's report now: dry runs), ``GET /v1/chain`` (our stake
share, kappa, the weights path). Jobs are not posted here: the validator CLAIMS them from the
platform's queue when it has a free admitted miner (``platform.claim``).

Public (no auth): ``GET /v1/reports`` (every hourly report's name and SHA-256, and the block its
hash was committed at) and ``GET /v1/reports/hour-N.json`` (the report's exact bytes), for
``cadmind-recompute``.

**Exposure.** The server refuses before it reads a body: a missing, negative, non-numeric,
duplicated or oversized ``Content-Length`` (and any ``Transfer-Encoding``), an unsigned request on
a signed route, a request from an unregistered hotkey, and a Bearer route without the token. It
reads at most ``MAX_BODY`` bytes, gives a connection ``timeout_s`` per read and ``deadline_s`` per
request, and holds at most ``max_connections`` connections (``max_per_ip`` from one address); past
either it answers 503 and closes. ``/v1/miners/agent_key`` (a key minted on the platform each time)
is rate-limited per hotkey and per address. In production a reverse proxy (nginx) stands in front
anyway, with TLS, ``client_max_body_size``, ``client_body_timeout``/``client_header_timeout``,
``limit_conn`` and ``limit_req``; it is named in ``trusted_proxies`` so its ``X-Real-IP`` is the
address the per-address limits count (and the proxy itself is not capped per address).

**Signed answers.** Every answer on a miner's (signed) route is signed by the validator's hotkey:
``X-Einstein-Validator`` (its ss58) and ``X-Einstein-Signature`` over :func:`response_payload` --
the status, the request's method, path and ``btauth/1`` nonce, the miner's hotkey and the SHA-256
of the answer's exact bytes. The miner checks it with :func:`verify_response` against the
validator hotkey it already addresses its requests to, so an answer that was forged, altered,
replayed from another request or sent to another miner is refused, TLS or not. (A refusal
answered before the request's signature was checked is signed too: it carries the nonce the
request named.)
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import re
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .desk import Desk, DeskError
from .scoring import report_order

MAX_BODY = 4 * 1024 * 1024
REPORT = re.compile(r"^hour-(\d{1,12})(?:-\d{1,6})?\.(json|sha256)$")
SIGNED = ("/v1/miners/", "/v1/jobs/")
"""routes a miner signs (``btauth/1``)"""
BEARER = ("/v1/cancel", "/v1/signals", "/v1/weights/close")
"""POST routes for the platform and the operator"""
SIGNATURE_HEADERS = ("X-Bittensor-Hotkey", "X-Bittensor-Nonce", "X-Bittensor-Signature")
TIMEOUT_S = 20.0
"""one read or write on a connection (a request line, a header line, a body chunk)"""
DEADLINE_S = 60.0
"""one whole request, from its first byte to its answer"""
MAX_CONNECTIONS = 64
MAX_PER_IP = 16
AGENT_KEY_PER_HOTKEY = (6, 3600.0)
"""agent keys minted per hotkey per hour"""
AGENT_KEY_PER_IP = (60, 3600.0)
"""agent key requests per client address per hour"""
RESPONSE_PROTOCOL = "cadmind-einstein-response/1"
HEADER_VALIDATOR = "X-Einstein-Validator"
HEADER_RESPONSE_SIGNATURE = "X-Einstein-Signature"
_BUSY_BODY = b'{"error": "busy, retry"}'
_BUSY = (
    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
    + f"Content-Length: {len(_BUSY_BODY)}\r\n".encode()
    + b"Retry-After: 5\r\nConnection: close\r\n\r\n"
    + _BUSY_BODY
)


def response_payload(
    *, status: int, method: str, path: str, nonce: str, hotkey: str, validator: str, body: bytes
) -> bytes:
    """The exact bytes a validator signs for one answer (newline-joined UTF-8)."""
    return "\n".join(
        (
            RESPONSE_PROTOCOL,
            str(int(status)),
            method.upper(),
            path,
            str(nonce),
            hotkey,
            validator,
            hashlib.sha256(body).hexdigest(),
        )
    ).encode()


def verify_response(
    headers: Mapping[str, str],
    body: bytes,
    *,
    status: int,
    method: str,
    path: str,
    nonce: str,
    hotkey: str,
    validator: str,
) -> bool:
    """True when the answer is signed by ``validator`` for this request (its nonce) of ``hotkey``
    and these exact bytes."""
    low = {k.lower(): v for k, v in headers.items()}
    if low.get(HEADER_VALIDATOR.lower()) != validator:
        return False
    try:
        sig = bytes.fromhex(
            str(low.get(HEADER_RESPONSE_SIGNATURE.lower()) or "").removeprefix("0x")
        )
        from bittensor.sp_core import Keypair  # noqa: PLC0415

        payload = response_payload(
            status=status,
            method=method,
            path=path,
            nonce=nonce,
            hotkey=hotkey,
            validator=validator,
            body=body,
        )
        return bool(sig) and bool(Keypair(ss58_address=validator).verify(payload, sig))
    except Exception:  # noqa: BLE001 -- anything malformed is not a valid signature
        return False


class RateLimit:
    """At most ``count`` events per ``per_s`` seconds per key (a sliding window)."""

    def __init__(
        self, count: int, per_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.count, self.per_s, self.clock = int(count), float(per_s), clock
        self._seen: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self._seen.setdefault(key, deque())
            while q and now - q[0] >= self.per_s:
                q.popleft()
            if len(q) >= self.count:
                return False
            q.append(now)
            if len(self._seen) > 10_000:  # forget the keys whose window has passed
                for k in [k for k, v in self._seen.items() if not v or now - v[-1] >= self.per_s]:
                    del self._seen[k]
            return True


class Server(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` with a bound on connections (in all, and per client address) and a
    watchdog that cuts a request past its deadline (a client trickling its headers or its body)."""

    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        addr: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        max_connections: int = MAX_CONNECTIONS,
        max_per_ip: int = MAX_PER_IP,
        trusted_proxies: Iterable[str] = (),
    ) -> None:
        # Set before binding: a failed bind (the port is taken) calls `server_close`, which reads
        # `_closed`, and must surface the real OSError, not an AttributeError.
        self.max_connections, self.max_per_ip = max_connections, max_per_ip
        self.trusted_proxies = frozenset(trusted_proxies)
        self._conn_lock = threading.Lock()
        self._open = 0
        self._per_ip: dict[str, int] = {}
        self._deadline: dict[socket.socket, float] = {}
        self.refused = 0
        """connections answered 503 because a cap was reached"""
        self._closed = threading.Event()
        super().__init__(addr, handler)
        threading.Thread(target=self._watch, name="einstein-watchdog", daemon=True).start()

    def process_request(self, request: Any, client_address: Any) -> None:
        ip = str(client_address[0])
        with self._conn_lock:
            full = self._open >= self.max_connections or (
                ip not in self.trusted_proxies and self._per_ip.get(ip, 0) >= self.max_per_ip
            )
            if not full:
                self._open += 1
                self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            else:
                self.refused += 1
        if full:
            with contextlib.suppress(OSError):
                request.settimeout(1.0)
                request.sendall(_BUSY)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._leave(ip, request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._leave(str(client_address[0]), request)

    def _leave(self, ip: str, request: Any) -> None:
        with self._conn_lock:
            self._open -= 1
            left = self._per_ip.get(ip, 1) - 1
            if left > 0:
                self._per_ip[ip] = left
            else:
                self._per_ip.pop(ip, None)
            self._deadline.pop(request, None)

    def open_connections(self) -> int:
        with self._conn_lock:
            return self._open

    def arm(self, conn: socket.socket, seconds: float) -> None:
        with self._conn_lock:
            self._deadline[conn] = time.monotonic() + seconds

    def disarm(self, conn: socket.socket) -> None:
        with self._conn_lock:
            self._deadline.pop(conn, None)

    def server_close(self) -> None:
        self._closed.set()
        super().server_close()

    def _watch(self) -> None:
        while not self._closed.wait(0.5):
            now = time.monotonic()
            with self._conn_lock:
                late = [c for c, t in self._deadline.items() if t <= now]
                for c in late:
                    del self._deadline[c]
            for c in late:
                with contextlib.suppress(OSError):
                    c.shutdown(socket.SHUT_RDWR)


def report_index(
    reports_dir: Path, commits: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The published reports, oldest first: name, SHA-256 and (when committed) the block."""
    out: list[dict[str, Any]] = []
    if not reports_dir.is_dir():
        return out
    for p in sorted(reports_dir.glob("hour-*.sha256"), key=report_order):
        sha = p.read_text(encoding="utf-8").split()[0]
        row: dict[str, Any] = {"report": f"{p.stem}.json", "sha256": sha}
        if commits and sha in commits:
            row["committed_block"] = commits[sha].get("block")
        out.append(row)
    return out


def serve(
    desk: Desk,
    host: str,
    port: int,
    *,
    token: str,
    validator_hotkey: str,
    verify: Callable[..., Any],
    close_hour: Callable[[], Mapping[str, Any] | None],
    reports_dir: Path | None = None,
    chain_info: Callable[[], Mapping[str, Any]] | None = None,
    timeout_s: float = TIMEOUT_S,
    deadline_s: float = DEADLINE_S,
    max_connections: int = MAX_CONNECTIONS,
    max_per_ip: int = MAX_PER_IP,
    trusted_proxies: Iterable[str] = (),
    agent_key_per_hotkey: tuple[int, float] = AGENT_KEY_PER_HOTKEY,
    agent_key_per_ip: tuple[int, float] = AGENT_KEY_PER_IP,
    clock: Callable[[], float] = time.monotonic,
    signer: Any = None,
) -> Server:
    """``signer``: the validator's hotkey (a wallet or a keypair), which signs every answer on a
    miner's route; without one, answers are unsigned (tests, a local stack)."""
    proxies = frozenset(trusted_proxies)
    sign_with: Any = None
    if signer is not None:
        from bittensor.signing import resolve_signer  # noqa: PLC0415

        sign_with = resolve_signer(signer, role="hotkey")
        if str(sign_with.ss58_address) != validator_hotkey:
            raise ValueError("the signer is not the validator's hotkey")
    key_by_hotkey = RateLimit(*agent_key_per_hotkey, clock=clock)
    key_by_ip = RateLimit(*agent_key_per_ip, clock=clock)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = timeout_s
        server: Server

        def log_message(self, _fmt: str, *_a: object) -> None:
            return None

        def handle_one_request(self) -> None:
            self.server.arm(self.connection, deadline_s)
            try:
                super().handle_one_request()
            finally:
                self.server.disarm(self.connection)

        def _send(self, code: int, body: Mapping[str, Any], *, close: bool = False) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            nonce = str(self.headers.get("X-Bittensor-Nonce") or "").strip()
            if sign_with is not None and self.path.startswith(SIGNED) and nonce:
                payload = response_payload(
                    status=code,
                    method=self.command,
                    path=self.path,
                    nonce=nonce[:40],
                    hotkey=str(self.headers.get("X-Bittensor-Hotkey") or "").strip()[:100],
                    validator=validator_hotkey,
                    body=data,
                )
                self.send_header(HEADER_VALIDATOR, validator_hotkey)
                self.send_header(
                    HEADER_RESPONSE_SIGNATURE, "0x" + bytes(sign_with.sign(payload)).hex()
                )
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def _refuse(self, code: int, error: str) -> None:
            """Refused before the body is read: the connection closes (its unread body must not be
            taken for the next request)."""
            self._send(code, {"error": error}, close=True)

        def _client_ip(self) -> str:
            peer = str(self.client_address[0])
            if peer in proxies:
                real = (self.headers.get("X-Real-IP") or "").strip()
                if real:
                    return real[:64]
                fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
                if fwd:
                    return fwd[:64]
            return peer

        def _length(self) -> int | None:
            """The body's length, or None after a refusal: one decimal ``Content-Length`` within
            ``MAX_BODY``, no ``Transfer-Encoding``."""
            if self.headers.get("Transfer-Encoding") is not None:
                self._refuse(400, "Transfer-Encoding is not accepted: send a Content-Length")
                return None
            values = self.headers.get_all("Content-Length") or []
            if not values:
                self._refuse(411, "Content-Length required")
                return None
            text = {v.strip() for v in values}
            if len(text) != 1:
                self._refuse(400, "one Content-Length")
                return None
            raw = text.pop()
            if not raw.isascii() or not raw.isdigit() or len(raw) > 12:
                self._refuse(400, "Content-Length is a non-negative integer")
                return None
            n = int(raw)
            if n > MAX_BODY:
                self._refuse(413, "body too large")
                return None
            return n

        def _body(self, n: int) -> bytes | None:
            """Exactly ``n`` bytes (n <= MAX_BODY), read in chunks; a short body is refused."""
            parts: list[bytes] = []
            left = min(n, MAX_BODY)
            while left > 0:
                chunk = self.rfile.read(min(left, 65_536))
                if not chunk:
                    self._refuse(400, "body shorter than its Content-Length")
                    return None
                parts.append(chunk)
                left -= len(chunk)
            return b"".join(parts)

        def _bearer(self) -> bool:
            if hmac.compare_digest(
                (self.headers.get("Authorization") or "").encode(), f"Bearer {token}".encode()
            ):
                return True
            self._send(401, {"error": "token required"})
            return False

        def _public(self) -> bool:
            """The published reports: anyone may read them."""
            if reports_dir is None or not self.path.startswith("/v1/reports"):
                return False
            if self.path.rstrip("/") == "/v1/reports":
                commits = desk.store.items("commit") if desk.store is not None else {}
                self._send(200, {"reports": report_index(reports_dir, commits)})
                return True
            m = REPORT.match(self.path[len("/v1/reports/") :])
            path = reports_dir / self.path[len("/v1/reports/") :] if m else None
            if path is None or not path.is_file():
                self._send(404, {"error": "no such report"})
                return True
            data = path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/json" if m and m.group(2) == "json" else "text/plain"
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True

        def do_GET(self) -> None:
            if self._public():
                return
            if not self._bearer():
                return
            try:
                if self.path == "/v1/chain" and chain_info is not None:
                    self._send(200, dict(chain_info()))
                elif self.path == "/v1/capacity":
                    self._send(200, desk.capacity())
                elif self.path.startswith("/v1/jobs/"):
                    self._send(200, desk.status(self.path.split("/", 3)[3]))
                elif self.path == "/v1/scores":
                    jobs, weight, quality = desk.ledger.snapshot(desk.now_ms())
                    self._send(
                        200,
                        {
                            "weight": weight,
                            "quality": quality,
                            "jobs": [asdict(o) for o in jobs[-200:]],
                            "miners": [asdict(m) for m in desk.miners.values()],
                            "rejections": desk.telemetry(),
                        },
                    )
                else:
                    self._send(404, {"error": "no such route"})
            except DeskError as e:
                self._send(e.status, {"error": str(e)})

        def _gate(self, signed: bool) -> bool:
            """Everything decided from the request line and the headers, before a byte of the body
            is read: the route, the caller's credentials (present, and for a miner a registered
            hotkey), the agent key's per-address rate."""
            if signed:
                if any(not self.headers.get(h) for h in SIGNATURE_HEADERS):
                    self._refuse(401, "btauth/1 signature required")
                    return False
                hk = str(self.headers.get("X-Bittensor-Hotkey") or "").strip()
                if hk not in set(desk.registered()):
                    self._refuse(403, "hotkey is not registered on the subnet")
                    return False
                if self.path == "/v1/miners/agent_key" and not key_by_ip.allow(self._client_ip()):
                    self._refuse(429, "too many agent key requests from this address")
                    return False
                return True
            if self.path not in BEARER:
                self._refuse(404, "no such route")
                return False
            if not hmac.compare_digest(
                (self.headers.get("Authorization") or "").encode(), f"Bearer {token}".encode()
            ):
                self._refuse(401, "token required")
                return False
            return True

        def do_POST(self) -> None:
            signed = self.path.startswith(SIGNED)
            if not self._gate(signed):
                return
            n = self._length()
            if n is None:
                return
            raw = self._body(n)
            if raw is None:
                return
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                self._send(400, {"error": "body is not JSON"})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": "body is a JSON object"})
                return
            try:
                if signed:
                    try:
                        caller = verify(
                            dict(self.headers.items()),
                            raw,
                            method="POST",
                            path=self.path,
                            self_hotkey_ss58=validator_hotkey,
                        )
                    except Exception:  # noqa: BLE001 -- one refusal for every auth failure, echoing nothing
                        self._send(401, {"error": "btauth/1 signature required"})
                        return
                    hk = str(caller.hotkey_ss58)
                    if self.path == "/v1/miners/agent_key":
                        if not key_by_hotkey.allow(hk):
                            self._send(429, {"error": "too many agent key requests: retry later"})
                            return
                        pub = base64.b64decode(str(body.get("x25519_public") or ""))
                        self._send(200, desk.agent_key(hk, pub))
                    elif self.path == "/v1/jobs/work":
                        self._send(200, desk.work(hk))
                    elif self.path == "/v1/jobs/accept":
                        self._send(200, desk.accept(hk, str(body.get("job") or "")))
                    elif self.path == "/v1/jobs/submit":
                        self._send(
                            200,
                            desk.submit(
                                hk,
                                str(body.get("job") or ""),
                                str(body.get("workspace") or ""),
                                str(body.get("digest") or ""),
                            ),
                        )
                    elif self.path == "/v1/jobs/reject":
                        self._send(
                            200,
                            desk.reject(
                                hk,
                                str(body.get("job") or ""),
                                str(body.get("category") or ""),
                                str(body.get("message") or ""),
                            ),
                        )
                    elif self.path == "/v1/miners/admission":
                        self._send(200, desk.admission(hk))
                    elif self.path == "/v1/miners/receipt":
                        jobs, weight, quality = desk.ledger.snapshot(desk.now_ms())
                        mine = [asdict(o) for o in jobs if o.hotkey == hk][-100:]
                        self._send(
                            200,
                            {
                                "hotkey": hk,
                                "weight": weight.get(hk, 0.0),
                                "quality": quality.get(hk, 0.0),
                                "jobs": mine,
                                # its recent attempts that did not pass, and why
                                "failures": desk.failures(hk),
                            },
                        )
                    else:
                        self._send(404, {"error": "no such route"})
                    return
                if self.path == "/v1/cancel":
                    self._send(200, desk.cancel(str(body.get("job") or "")))
                elif self.path == "/v1/signals":
                    self._send(200, desk.signal(str(body.get("job") or ""), body))
                elif self.path == "/v1/weights/close":
                    self._send(200, dict(close_hour() or {"closed": False}))
                else:
                    self._send(404, {"error": "no such route"})
            except DeskError as e:
                self._send(e.status, {"error": str(e)})
            except ValueError as e:
                self._send(400, {"error": str(e)[:300]})

    return Server(
        (host, port),
        Handler,
        max_connections=max_connections,
        max_per_ip=max_per_ip,
        trusted_proxies=proxies,
    )
