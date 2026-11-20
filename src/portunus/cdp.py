"""A small Chrome DevTools Protocol client on the standard library (PANT-1400).

Just enough of RFC 6455 (client side: masked frames out, fragmented and
control frames in) and of CDP's JSON-RPC (flattened sessions, events kept
aside while waiting for a reply) for `portunus browser fill` to type a value
into a page. No third-party websocket package, so the fill path adds no
dependency to Portunus.

The endpoint must be on loopback. A CDP server receives the text it is asked
to type, and it is also the only source of truth for which origin a page is
on: a remote endpoint (or a local page posing as one) could claim any origin
and collect the value. `check_loopback` is applied to both the HTTP discovery
URL and the websocket URL it hands back.

Nothing in this module logs or formats a message parameter: callers pass
secret text in `params`, and errors carry only the method name and CDP's own
error message (which never echoes parameters).
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

DEFAULT_ENDPOINT = "http://127.0.0.1:9222"
ENDPOINT_ENV = "PORTUNUS_BROWSER_CDP"


class CDPError(RuntimeError):
    """The browser could not be reached, or a CDP call failed. Never carries
    the parameters of the call (they may hold the value being typed)."""


def check_loopback(url: str) -> Tuple[str, int, str]:
    """Return (host, port, scheme) for `url`, or raise unless its host is a
    loopback address or `localhost`."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "ws"):
        raise CDPError(f"CDP endpoint must be http:// or ws:// on loopback, got {parsed.scheme or 'no'} scheme")
    host = parsed.hostname or ""
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise CDPError(
            f"CDP endpoint {host or url!r} is not on loopback -- Portunus only types secrets "
            f"into a browser on this machine (127.0.0.1, ::1 or localhost)"
        )
    return host, parsed.port or 80, parsed.scheme


def endpoint_from(explicit: str = "") -> str:
    return explicit or os.environ.get(ENDPOINT_ENV, "") or DEFAULT_ENDPOINT


def browser_ws_url(endpoint: str, timeout: float = 5.0) -> str:
    """Resolve an http:// DevTools endpoint to its browser websocket URL
    (GET /json/version). A ws:// endpoint is returned as is."""
    endpoint = endpoint.rstrip("/")
    check_loopback(endpoint)
    if endpoint.startswith("ws://"):
        return endpoint
    try:
        with urllib.request.urlopen(endpoint + "/json/version", timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # URLError, timeout, bad JSON
        raise CDPError(
            f"no DevTools endpoint at {endpoint} ({type(exc).__name__}) -- start Chrome with "
            f"--remote-debugging-port=9222, or pass --cdp / set {ENDPOINT_ENV}"
        ) from None
    ws = info.get("webSocketDebuggerUrl", "")
    if not ws:
        raise CDPError(f"{endpoint}/json/version has no webSocketDebuggerUrl")
    check_loopback(ws)
    return ws


class CDPConnection:
    """One websocket to the browser target, with flattened sessions."""

    def __init__(self, ws_url: str, timeout: float = 10.0):
        host, port, _ = check_loopback(ws_url)
        parsed = urllib.parse.urlsplit(ws_url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self.timeout = timeout
        self._next_id = 0
        self.events: List[dict] = []
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise CDPError(f"cannot connect to {host}:{port} ({type(exc).__name__})") from None
        self._buf = b""
        self._handshake(host, port, path)

    # --- websocket ---------------------------------------------------------
    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        hosthdr = f"[{host}]" if ":" in host else host
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {hosthdr}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CDPError("browser closed the connection during the websocket handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise CDPError(f"websocket handshake refused: {status.decode('latin-1', 'replace')}")

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise CDPError("browser closed the DevTools connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def _recv_message(self) -> str:
        parts: List[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b1 & 0x80 else b""
            data = self._recv_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise CDPError("browser closed the DevTools connection")
            if opcode == 0x9:
                self._send_frame(data, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8")

    # --- CDP ---------------------------------------------------------------
    def send(self, method: str, params: Optional[dict] = None, session_id: str = "") -> dict:
        """Call `method` and return its result. Events that arrive meanwhile
        are appended to `self.events`."""
        self._next_id += 1
        msg_id = self._next_id
        msg: Dict[str, object] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self._send_frame(json.dumps(msg).encode("utf-8"))
        del msg
        deadline = time.monotonic() + self.timeout
        while True:
            if time.monotonic() > deadline:
                raise CDPError(f"{method}: no reply from the browser within {self.timeout:.0f}s")
            try:
                reply = json.loads(self._recv_message())
            except socket.timeout:
                raise CDPError(f"{method}: no reply from the browser within {self.timeout:.0f}s") from None
            if reply.get("id") == msg_id:
                if "error" in reply:
                    err = reply["error"]
                    raise CDPError(f"{method}: {err.get('message', 'error')}")
                return reply.get("result", {})
            if "method" in reply:
                self.events.append(reply)

    def drain(self, quiet: float = 0.3, limit: float = 3.0) -> None:
        """Collect events until none arrive for `quiet` seconds (at most `limit`)."""
        end = time.monotonic() + limit
        self._sock.settimeout(quiet)
        try:
            while time.monotonic() < end:
                try:
                    msg = json.loads(self._recv_message())
                except socket.timeout:
                    break
                if "method" in msg:
                    self.events.append(msg)
        finally:
            self._sock.settimeout(self.timeout)

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "CDPConnection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
