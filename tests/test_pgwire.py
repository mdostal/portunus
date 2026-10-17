"""portunus.pgwire -- the driverless Postgres probe behind the Neon liveness
check and rotation verify (PANT-1120).

The wire client runs against an in-process fake server on 127.0.0.1 that
speaks just enough protocol 3.0 (SCRAM-SHA-256 / MD5 / cleartext / trust);
SCRAM itself is pinned to the RFC 7677 test vector. No real database is
contacted. Credentials below are obviously fake."""
import base64
import hashlib
import hmac
import socket
import struct
import threading

import pytest

from portunus import pgwire
from portunus.pgwire import PgError, ScramClient, parse_url, replace_password

PASSWORD = "FAKE-pw/with:odd@chars"


# --- SCRAM vector -----------------------------------------------------------

def test_scram_matches_rfc7677_vector():
    c = ScramClient("pencil", user="user", nonce="rOprNGfwEbeRWgbNEkqO")
    assert c.first_message() == b"n,,n=user,r=rOprNGfwEbeRWgbNEkqO"
    final = c.final_message(
        b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
        b"s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096")
    assert final == (b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
                     b"p=dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    c.verify_server(b"v=6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=")


def test_scram_rejects_wrong_server_signature():
    c = ScramClient("pencil", user="user", nonce="rOprNGfwEbeRWgbNEkqO")
    c.final_message(b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
                    b"s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096")
    with pytest.raises(PgError, match="signature"):
        c.verify_server(b"v=" + base64.b64encode(b"x" * 32))


def test_scram_rejects_server_nonce_not_extending_ours():
    c = ScramClient("pencil", nonce="abc")
    with pytest.raises(PgError, match="malformed"):
        c.final_message(b"r=zzz,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096")


def test_channel_binding_header():
    c = ScramClient("pw", nonce="n1", cbind_type="tls-server-end-point", cbind_data=b"\x01\x02")
    assert c.first_message() == b"p=tls-server-end-point,,n=,r=n1"
    final = c.final_message(b"r=n1srv,s=" + base64.b64encode(b"salt") + b",i=1")
    expected_c = base64.b64encode(b"p=tls-server-end-point,,\x01\x02")
    assert final.startswith(b"c=" + expected_c + b",r=n1srv,p=")


# --- URL handling -----------------------------------------------------------

NEON_URL = ("postgresql://neondb_owner:" + "FAKE%2Fpw" +
            "@ep-cool-name-a1b2c3-pooler.us-east-2.aws.neon.tech/neondb"
            "?sslmode=require&channel_binding=require")


def test_parse_neon_url():
    info = parse_url(NEON_URL)
    assert (info.host, info.port, info.user, info.password, info.database) == (
        "ep-cool-name-a1b2c3-pooler.us-east-2.aws.neon.tech", 5432, "neondb_owner",
        "FAKE/pw", "neondb")
    assert (info.sslmode, info.channel_binding) == ("require", "require")
    assert "FAKE" not in repr(info)


@pytest.mark.parametrize("bad", [
    "mysql://u:p@h/db", "not a url", "postgres://h/db", "postgres://u:p@h1,h2/db",
    "postgres://u:p@h:notaport/db",
])
def test_parse_rejects_without_echoing(bad):
    with pytest.raises(ValueError) as exc:
        parse_url(bad)
    assert "p@" not in str(exc.value) and bad not in str(exc.value)


def test_replace_password_keeps_everything_else():
    new = replace_password(NEON_URL, "n3w/pa:ss@word")
    assert new == NEON_URL.replace("FAKE%2Fpw", "n3w%2Fpa%3Ass%40word")
    assert parse_url(new).password == "n3w/pa:ss@word"


# --- fake server ------------------------------------------------------------

def _msg(type_: bytes, payload: bytes = b"") -> bytes:
    return type_ + struct.pack("!I", len(payload) + 4) + payload


def _error(code: str) -> bytes:
    return _msg(b"E", b"SFATAL\x00C" + code.encode() +
                b"\x00Mpassword authentication failed for user 'neondb_owner'\x00\x00")


class FakePg:
    """One-connection-at-a-time protocol 3.0 server for tests."""

    def __init__(self, auth="scram", password=PASSWORD, ssl_answer=b"N", startup_error=None,
                 tls_context=None, plus=False):
        self.auth, self.password, self.ssl_answer = auth, password, ssl_answer
        self.tls_context, self.plus, self.cbind = tls_context, plus, None
        self.startup_error = startup_error
        self.startups, self.queries, self.received_cleartext = [], [], []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def url(self, password=PASSWORD, sslmode="disable", extra=""):
        from urllib.parse import quote
        return (f"postgres://neondb_owner:{quote(password, safe='')}@127.0.0.1:{self.port}/neondb"
                f"?sslmode={sslmode}{extra}")

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                self._handle(conn)
            except (OSError, AssertionError, struct.error):
                pass
            finally:
                conn.close()

    @staticmethod
    def _read(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise OSError("eof")
            buf += chunk
        return buf

    def _recv_typed(self, conn):
        head = self._read(conn, 5)
        return head[:1], self._read(conn, struct.unpack("!I", head[1:])[0] - 4)

    def _handle(self, conn):
        length = struct.unpack("!I", self._read(conn, 4))[0]
        body = self._read(conn, length - 4)
        if struct.unpack("!I", body[:4])[0] == 80877103:
            if self.tls_context is None:
                conn.sendall(self.ssl_answer)
                return
            conn.sendall(b"S")
            conn = self.tls_context.wrap_socket(conn, server_side=True)
            self.cbind = hashlib.sha256(conn.getpeercert(True) or self._own_cert_der).digest()
            length = struct.unpack("!I", self._read(conn, 4))[0]
            body = self._read(conn, length - 4)
        fields = body[4:].split(b"\x00")
        self.startups.append(dict(zip(fields[0::2], fields[1::2])))
        if self.startup_error:
            conn.sendall(_error(self.startup_error))
            return
        if not getattr(self, "_auth_" + self.auth)(conn):
            conn.sendall(_error("28P01"))
            return
        conn.sendall(_msg(b"R", struct.pack("!I", 0)) + _msg(b"S", b"server_version\x0016\x00")
                     + _msg(b"K", struct.pack("!II", 1, 2)) + _msg(b"Z", b"I"))
        while True:
            type_, payload = self._recv_typed(conn)
            if type_ == b"X":
                return
            assert type_ == b"Q"
            self.queries.append(payload.rstrip(b"\x00").decode())
            conn.sendall(_msg(b"T", b"\x00\x01?column?\x00" + b"\x00" * 18)
                         + _msg(b"D", b"\x00\x01\x00\x00\x00\x011")
                         + _msg(b"C", b"SELECT 1\x00") + _msg(b"Z", b"I"))

    def _auth_trust(self, conn):
        return True

    def _auth_cleartext(self, conn):
        conn.sendall(_msg(b"R", struct.pack("!I", 3)))
        _, payload = self._recv_typed(conn)
        got = payload.rstrip(b"\x00").decode()
        self.received_cleartext.append(got)
        return got == self.password

    def _auth_md5(self, conn):
        salt = b"\x01\x02\x03\x04"
        conn.sendall(_msg(b"R", struct.pack("!I", 5) + salt))
        _, payload = self._recv_typed(conn)
        inner = hashlib.md5(self.password.encode() + b"neondb_owner").hexdigest()
        want = b"md5" + hashlib.md5(inner.encode() + salt).hexdigest().encode()
        return payload.rstrip(b"\x00") == want

    def _auth_scram(self, conn):
        mechs = b"SCRAM-SHA-256-PLUS\x00SCRAM-SHA-256\x00\x00" if self.plus else b"SCRAM-SHA-256\x00\x00"
        conn.sendall(_msg(b"R", struct.pack("!I", 10) + mechs))
        _, payload = self._recv_typed(conn)
        mech, rest = payload.split(b"\x00", 1)
        self.mechanism = mech.decode()
        client_first = rest[4:]
        gs2 = b",".join(client_first.split(b",", 2)[:2]) + b","
        bare = client_first.split(b",", 2)[2]
        nonce = dict(kv.split(b"=", 1) for kv in bare.split(b","))[b"r"]
        salt = b"fake-salt-123456"
        server_first = b"r=" + nonce + b"SRV,s=" + base64.b64encode(salt) + b",i=4096"
        conn.sendall(_msg(b"R", struct.pack("!I", 11) + server_first))
        _, final = self._recv_typed(conn)
        without_proof, proof = final.rsplit(b",p=", 1)
        want_c = gs2 + (self.cbind if mech == b"SCRAM-SHA-256-PLUS" else b"")
        if without_proof.split(b",")[0] != b"c=" + base64.b64encode(want_c):
            return False
        salted = hashlib.pbkdf2_hmac("sha256", self.password.encode(), salt, 4096)
        client_key = hmac.new(salted, b"Client Key", "sha256").digest()
        stored = hashlib.sha256(client_key).digest()
        auth_msg = bare + b"," + server_first + b"," + without_proof
        sig = hmac.new(stored, auth_msg, "sha256").digest()
        got_key = bytes(a ^ b for a, b in zip(base64.b64decode(proof), sig))
        if hashlib.sha256(got_key).digest() != stored:
            return False
        server_sig = hmac.new(hmac.new(salted, b"Server Key", "sha256").digest(), auth_msg,
                              "sha256").digest()
        conn.sendall(_msg(b"R", struct.pack("!I", 12) + b"v=" + base64.b64encode(server_sig)))
        return True

    def close(self):
        self.sock.close()


@pytest.fixture
def fake_pg():
    servers = []

    def make(**kw):
        servers.append(FakePg(**kw))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


@pytest.mark.parametrize("auth", ["scram", "md5", "cleartext", "trust"])
def test_probe_live(fake_pg, auth):
    pg = fake_pg(auth=auth)
    pgwire.probe(parse_url(pg.url()), timeout=5)
    assert pg.queries == ["SELECT 1"]
    assert pg.startups[0][b"user"] == b"neondb_owner"
    assert pg.startups[0][b"database"] == b"neondb"
    assert pg.startups[0][b"application_name"] == b"portunus-liveness"


@pytest.mark.parametrize("auth", ["scram", "md5", "cleartext"])
def test_probe_wrong_password_is_auth_failure_without_server_text(fake_pg, auth):
    pg = fake_pg(auth=auth)
    with pytest.raises(PgError) as exc:
        pgwire.probe(parse_url(pg.url(password="stale-FAKE")), timeout=5)
    assert exc.value.sqlstate == "28P01" and exc.value.auth_failed
    assert "neondb_owner" not in str(exc.value) and "stale" not in str(exc.value)
    assert pg.queries == []


def test_non_auth_server_error_is_not_auth_failure(fake_pg):
    pg = fake_pg(startup_error="3D000")  # database does not exist
    with pytest.raises(PgError) as exc:
        pgwire.probe(parse_url(pg.url()), timeout=5)
    assert exc.value.sqlstate == "3D000" and not exc.value.auth_failed


def test_tls_required_but_refused_never_sends_password(fake_pg):
    pg = fake_pg(auth="cleartext", ssl_answer=b"N")
    with pytest.raises(PgError, match="TLS") as exc:
        pgwire.probe(parse_url(pg.url(sslmode="require")), timeout=5)
    assert exc.value.sqlstate == "" and not exc.value.auth_failed
    assert pg.startups == [] and pg.received_cleartext == []


def test_connection_refused_is_not_auth_failure():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(PgError) as exc:
        pgwire.probe(parse_url(f"postgres://u:FAKE@127.0.0.1:{port}/db?sslmode=disable"), timeout=2)
    assert not exc.value.auth_failed and "FAKE" not in str(exc.value)


def test_channel_binding_required_but_unavailable(fake_pg):
    pg = fake_pg(auth="scram")
    with pytest.raises(PgError, match="channel_binding"):
        pgwire.probe(parse_url(pg.url(extra="&channel_binding=require")), timeout=5)


# --- TLS (SNI, cert verification, SCRAM-SHA-256-PLUS) -----------------------

@pytest.fixture(scope="module")
def tls_material(tmp_path_factory):
    import datetime
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    d = tmp_path_factory.mktemp("tls")
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(d / "cert.pem", d / "key.pem")
    return server, str(d / "cert.pem"), cert.public_bytes(serialization.Encoding.DER)


@pytest.fixture
def trust_test_ca(monkeypatch, tls_material):
    import ssl
    real = ssl.create_default_context
    monkeypatch.setattr(pgwire.ssl, "create_default_context",
                        lambda *a, **kw: real(cafile=tls_material[1]))


def _tls_pg(fake_pg, tls_material, **kw):
    pg = fake_pg(tls_context=tls_material[0], **kw)
    pg._own_cert_der = tls_material[2]
    return pg


def _tls_url(pg, extra=""):
    return pg.url(sslmode="require", extra=extra).replace("127.0.0.1", "localhost")


def test_tls_scram_plus_channel_binding(fake_pg, tls_material, trust_test_ca):
    pg = _tls_pg(fake_pg, tls_material, plus=True)
    pgwire.probe(parse_url(_tls_url(pg, "&channel_binding=require")), timeout=5)
    assert pg.mechanism == "SCRAM-SHA-256-PLUS" and pg.queries == ["SELECT 1"]


def test_tls_plain_scram_when_binding_disabled(fake_pg, tls_material, trust_test_ca):
    pg = _tls_pg(fake_pg, tls_material, plus=True)
    pgwire.probe(parse_url(_tls_url(pg, "&channel_binding=disable")), timeout=5)
    assert pg.mechanism == "SCRAM-SHA-256"


def test_tls_cleartext_password_allowed(fake_pg, tls_material, trust_test_ca):
    pg = _tls_pg(fake_pg, tls_material, auth="cleartext")
    pgwire.probe(parse_url(_tls_url(pg)), timeout=5)
    assert pg.received_cleartext == [PASSWORD]


def test_tls_untrusted_certificate_fails_before_auth(fake_pg, tls_material):
    pg = _tls_pg(fake_pg, tls_material, auth="cleartext")
    with pytest.raises(PgError, match="TLS handshake") as exc:
        pgwire.probe(parse_url(_tls_url(pg)), timeout=5)
    assert not exc.value.auth_failed and pg.received_cleartext == []
