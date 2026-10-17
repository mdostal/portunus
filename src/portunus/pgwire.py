"""Minimal PostgreSQL wire-protocol client -- just enough to prove a stored
connection string still authenticates (`SELECT 1`), with no driver
dependency (PANT-1120, Neon liveness + rotation verify).

Speaks protocol 3.0: TLS via SSLRequest (with SNI, which Neon's proxy routes
on), then SCRAM-SHA-256 / SCRAM-SHA-256-PLUS (tls-server-end-point channel
binding), MD5 or cleartext password auth, then one simple query. Nothing
else -- no result decoding, no prepared statements, no COPY.

Boundary rules, same as liveness.py's HTTP transport:
- The password only ever goes into the authentication exchange.
- A server ErrorResponse is reduced to its SQLSTATE code. The message text
  is never read into an exception: it can name the role, the database or
  the endpoint, and a future server could echo more.
- Errors raised here never carry the connection string or any part of it.

Cleartext and MD5 passwords are refused over a non-TLS socket unless the
connection string says `sslmode=disable` explicitly, and TLS always
verifies the server certificate and hostname (stricter than libpq's
`sslmode=require`, which skips verification): a probe that hands the
password to whoever answers would be a leak, not a check.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import socket
import ssl
import struct
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

_PROTOCOL_3_0 = 196608
_SSL_REQUEST = 80877103
_DEFAULT_PORT = 5432
_MAX_MESSAGE = 1 << 20

# SQLSTATE class 28: invalid_authorization_specification (28000) and
# invalid_password (28P01). The server rejected the credential itself.
AUTH_FAILED_CLASS = "28"


class PgError(Exception):
    """Connection failed. `sqlstate` is set when the server answered with an
    ErrorResponse; otherwise it is "" (network, TLS, protocol). The message
    never contains a credential, a connection string or server text."""

    def __init__(self, message: str, sqlstate: str = ""):
        super().__init__(message)
        self.sqlstate = sqlstate

    @property
    def auth_failed(self) -> bool:
        return self.sqlstate[:2] == AUTH_FAILED_CLASS


@dataclass(frozen=True)
class ConnInfo:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str
    sslmode: str = "require"
    channel_binding: str = "prefer"
    options: str = ""
    query: Tuple[Tuple[str, str], ...] = ()


def parse_url(url: str) -> ConnInfo:
    """Parse a `postgres://` / `postgresql://` URL. Raises ValueError without
    echoing the input."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError("not a postgres:// or postgresql:// connection string")
    if "," in parts.netloc:
        raise ValueError("multi-host connection strings are not supported")
    try:
        port = parts.port or _DEFAULT_PORT
    except ValueError:
        raise ValueError("connection string has an invalid port") from None
    if not parts.hostname:
        raise ValueError("connection string has no host")
    if not parts.username:
        raise ValueError("connection string has no user")
    query = tuple(parse_qsl(parts.query, keep_blank_values=True))
    params = dict(query)
    database = unquote(parts.path.lstrip("/")) or unquote(parts.username)
    return ConnInfo(
        host=parts.hostname,
        port=port,
        user=unquote(parts.username),
        password=unquote(parts.password or ""),
        database=database,
        sslmode=params.get("sslmode", "require"),
        channel_binding=params.get("channel_binding", "prefer"),
        options=params.get("options", ""),
        query=query,
    )


def replace_password(url: str, password: str) -> str:
    """Return `url` with its password replaced, everything else (host,
    pooler suffix, database, query parameters) kept byte-for-byte."""
    parts = urlsplit(url.strip())
    netloc = parts.netloc
    userinfo, _, hostport = netloc.rpartition("@")
    if not userinfo:
        raise ValueError("connection string has no user")
    user = userinfo.split(":", 1)[0]
    netloc = f"{user}:{quote(password, safe='')}@{hostport}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ---------------------------------------------------------------------------
# SCRAM-SHA-256 (RFC 5802 / RFC 7677)
# ---------------------------------------------------------------------------


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


class ScramClient:
    """Client side of one SCRAM-SHA-256 exchange. Postgres ignores the SCRAM
    user name (it uses the startup packet's), so `user` defaults to ""."""

    def __init__(self, password: str, user: str = "", nonce: Optional[str] = None,
                 cbind_type: str = "", cbind_data: bytes = b""):
        self._password = password.encode()
        self._nonce = nonce or base64.b64encode(secrets.token_bytes(18)).decode()
        self._cbind_data = cbind_data
        self._gs2 = (f"p={cbind_type},," if cbind_type else "n,,").encode()
        self._first_bare = f"n={user},r={self._nonce}".encode()
        self._server_signature = b""

    def first_message(self) -> bytes:
        return self._gs2 + self._first_bare

    def final_message(self, server_first: bytes) -> bytes:
        attrs = dict(kv.split("=", 1) for kv in server_first.decode().split(",") if "=" in kv)
        nonce, salt, iterations = attrs.get("r", ""), attrs.get("s", ""), attrs.get("i", "")
        if not nonce.startswith(self._nonce) or not salt or not iterations.isdigit():
            raise PgError("SCRAM: malformed server-first-message")
        salted = hashlib.pbkdf2_hmac("sha256", self._password, base64.b64decode(salt), int(iterations))
        client_key = _hmac(salted, b"Client Key")
        stored_key = hashlib.sha256(client_key).digest()
        channel = base64.b64encode(self._gs2 + self._cbind_data).decode()
        without_proof = f"c={channel},r={nonce}".encode()
        auth_message = self._first_bare + b"," + server_first + b"," + without_proof
        signature = _hmac(stored_key, auth_message)
        proof = bytes(a ^ b for a, b in zip(client_key, signature))
        self._server_signature = _hmac(_hmac(salted, b"Server Key"), auth_message)
        return without_proof + b",p=" + base64.b64encode(proof)

    def verify_server(self, server_final: bytes) -> None:
        attrs = dict(kv.split("=", 1) for kv in server_final.decode().split(",") if "=" in kv)
        got = base64.b64decode(attrs.get("v", ""))
        if not self._server_signature or not hmac.compare_digest(got, self._server_signature):
            raise PgError("SCRAM: server signature mismatch")


def _tls_server_end_point(sock) -> bytes:
    """RFC 5929 tls-server-end-point: hash of the server certificate with its
    signature hash algorithm, SHA-256 when that is MD5/SHA-1/unknown."""
    der = sock.getpeercert(binary_form=True)
    algo = "sha256"
    try:
        from cryptography import x509
        name = x509.load_der_x509_certificate(der).signature_hash_algorithm.name
        if name not in ("md5", "sha1"):
            algo = name
    except Exception:
        pass
    return hashlib.new(algo, der).digest()


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, sock):
        self.sock = sock

    def send(self, type_: bytes, payload: bytes = b"") -> None:
        self.sock.sendall(type_ + struct.pack("!I", len(payload) + 4) + payload)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise PgError("server closed the connection")
            buf += chunk
        return buf

    def recv(self) -> Tuple[bytes, bytes]:
        header = self._recv_exact(5)
        type_, length = header[:1], struct.unpack("!I", header[1:])[0]
        if length < 4 or length > _MAX_MESSAGE:
            raise PgError("protocol error: bad message length")
        payload = self._recv_exact(length - 4)
        if type_ == b"E":
            raise PgError("server rejected the connection", _sqlstate(payload))
        return type_, payload


def _sqlstate(payload: bytes) -> str:
    """Pull only the SQLSTATE ('C') field out of an ErrorResponse."""
    for field_ in payload.split(b"\x00"):
        if field_[:1] == b"C":
            return field_[1:6].decode("ascii", "replace")
    return "?????"


def _open(info: ConnInfo, timeout: float):
    try:
        raw = socket.create_connection((info.host, info.port), timeout=timeout)
    except OSError as exc:
        raise PgError(f"connect failed: {type(exc).__name__}") from None
    if info.sslmode == "disable":
        return raw, False
    try:
        raw.sendall(struct.pack("!II", 8, _SSL_REQUEST))
        answer = raw.recv(1)
    except OSError as exc:
        raw.close()
        raise PgError(f"SSLRequest failed: {type(exc).__name__}") from None
    if answer != b"S":
        raw.close()
        raise PgError("server does not support TLS (set sslmode=disable only for local servers)")
    try:
        return ssl.create_default_context().wrap_socket(raw, server_hostname=info.host), True
    except (ssl.SSLError, OSError) as exc:
        raw.close()
        raise PgError(f"TLS handshake failed: {type(exc).__name__}") from None


def _authenticate(conn: _Conn, info: ConnInfo, tls: bool) -> None:
    while True:
        type_, payload = conn.recv()
        if type_ != b"R":
            raise PgError("protocol error: expected an authentication request")
        code = struct.unpack("!I", payload[:4])[0]
        if code == 0:
            return
        if code in (3, 5):
            if not tls and info.sslmode != "disable":
                raise PgError("refusing to send a password without TLS")
            if code == 3:
                secret = info.password.encode()
            else:
                salt = payload[4:8]
                inner = hashlib.md5(info.password.encode() + info.user.encode()).hexdigest()
                secret = b"md5" + hashlib.md5(inner.encode() + salt).hexdigest().encode()
            conn.send(b"p", secret + b"\x00")
        elif code == 10:
            mechanisms = [m.decode() for m in payload[4:].split(b"\x00") if m]
            plus = "SCRAM-SHA-256-PLUS" in mechanisms and tls and info.channel_binding != "disable"
            if info.channel_binding == "require" and not plus:
                raise PgError("channel_binding=require but the server offers no channel binding")
            if not plus and "SCRAM-SHA-256" not in mechanisms:
                raise PgError("server offers no supported SASL mechanism")
            if plus:
                mech = "SCRAM-SHA-256-PLUS"
                scram = ScramClient(info.password, cbind_type="tls-server-end-point",
                                    cbind_data=_tls_server_end_point(conn.sock))
            else:
                mech = "SCRAM-SHA-256"
                scram = ScramClient(info.password)
            first = scram.first_message()
            conn.send(b"p", mech.encode() + b"\x00" + struct.pack("!I", len(first)) + first)
            type_, payload = conn.recv()
            if type_ != b"R" or struct.unpack("!I", payload[:4])[0] != 11:
                raise PgError("SCRAM: expected SASLContinue")
            conn.send(b"p", scram.final_message(payload[4:]))
            type_, payload = conn.recv()
            if type_ != b"R" or struct.unpack("!I", payload[:4])[0] != 12:
                raise PgError("SCRAM: expected SASLFinal")
            scram.verify_server(payload[4:])
        else:
            raise PgError(f"unsupported authentication method {code}")


def _until_ready(conn: _Conn) -> None:
    while True:
        type_, _ = conn.recv()
        if type_ == b"Z":
            return


def probe(info: ConnInfo, timeout: float = 10.0) -> None:
    """Connect, authenticate, run `SELECT 1`, disconnect. Returns on success;
    raises PgError otherwise (check `.auth_failed`)."""
    sock, tls = _open(info, timeout)
    conn = _Conn(sock)
    try:
        params: Dict[str, str] = {
            "user": info.user,
            "database": info.database,
            "application_name": "portunus-liveness",
        }
        if info.options:
            params["options"] = info.options
        body = struct.pack("!I", _PROTOCOL_3_0)
        body += b"".join(k.encode() + b"\x00" + v.encode() + b"\x00" for k, v in params.items())
        body += b"\x00"
        sock.sendall(struct.pack("!I", len(body) + 4) + body)
        _authenticate(conn, info, tls)
        _until_ready(conn)
        conn.send(b"Q", b"SELECT 1\x00")
        _until_ready(conn)
        try:
            conn.send(b"X")
        except OSError:
            pass
    except (OSError, struct.error, UnicodeDecodeError, ValueError) as exc:
        raise PgError(f"connection failed: {type(exc).__name__}") from None
    finally:
        try:
            sock.close()
        except OSError:
            pass
