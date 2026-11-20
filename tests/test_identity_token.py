"""identity_token: mint OIDC identity tokens into a 0600 file, refresh, never leak."""
import base64
import io
import json
import stat
import subprocess
import time

import pytest

from portunus.auth import AuthError
from portunus import identity_token as it


def make_jwt(claims, sig="SIGNATURE-SECRET-PART"):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'RS256', 'kid': 'k1'})}.{b64(claims)}.{sig}"


def good_claims(**over):
    now = int(time.time())
    c = {"iss": "https://accounts.google.com", "aud": "https://api.anthropic.com",
         "sub": "1234567890", "email": "claude@p.iam.gserviceaccount.com",
         "iat": now, "exp": now + 3600}
    c.update(over)
    return c


class FakeRunner:
    def __init__(self, stdout="", stderr="", rc=0):
        self.stdout, self.stderr, self.rc = stdout, stderr, rc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.rc, self.stdout, self.stderr)


def test_decode_claims_reads_payload():
    claims = it.decode_claims(make_jwt({"aud": "x", "exp": 5}))
    assert claims == {"aud": "x", "exp": 5}


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.!!!.c", f"a.{base64.urlsafe_b64encode(b'[1]').decode()}.c"])
def test_decode_claims_rejects_non_jwt(bad):
    with pytest.raises(AuthError):
        it.decode_claims(bad)


def test_gcloud_source_builds_impersonation_command(home):
    jwt = make_jwt(good_claims())
    runner = FakeRunner(stdout=jwt + "\n", stderr="WARNING: This command is using service account impersonation.")
    m = it.IdentityTokenMinter("https://api.anthropic.com", "gcloud",
                               service_account="claude@p.iam.gserviceaccount.com",
                               account="me@example.com", runner=runner)
    minted = m.mint()
    assert runner.calls == [[
        "gcloud", "auth", "print-identity-token", "--audiences=https://api.anthropic.com",
        "--include-email", "--impersonate-service-account=claude@p.iam.gserviceaccount.com",
        "--account=me@example.com",
    ]]
    assert minted.token == jwt
    assert minted.email == "claude@p.iam.gserviceaccount.com"
    assert minted.source == "gcloud"
    assert jwt not in repr(minted)
    assert jwt not in minted.describe()


def test_gcloud_failure_reports_stderr_not_stdout(home):
    runner = FakeRunner(stdout="PARTIAL.TOKEN.VALUE", stderr="ERROR: permission denied", rc=1)
    m = it.IdentityTokenMinter("aud", "gcloud", runner=runner)
    with pytest.raises(AuthError) as exc:
        m.mint()
    assert "permission denied" in str(exc.value)
    assert "PARTIAL.TOKEN.VALUE" not in str(exc.value)


def test_expired_token_is_rejected(home):
    jwt = make_jwt(good_claims(exp=int(time.time()) - 10))
    m = it.IdentityTokenMinter("aud", "gcloud", runner=FakeRunner(stdout=jwt))
    with pytest.raises(AuthError, match="expired"):
        m.mint()


def test_metadata_source_requests_full_format(home):
    jwt = make_jwt(good_claims())
    seen = {}

    def http_get(url, headers, timeout):
        seen["url"], seen["headers"] = url, headers
        return jwt

    minted = it.IdentityTokenMinter("https://api.anthropic.com", "metadata", http_get=http_get).mint()
    assert "audience=https%3A%2F%2Fapi.anthropic.com" in seen["url"]
    assert "format=full" in seen["url"]
    assert seen["headers"] == {"Metadata-Flavor": "Google"}
    assert minted.token == jwt


def test_wif_source_exchanges_then_generates_id_token(home, monkeypatch):
    from portunus.auth import GCPAccessToken
    monkeypatch.setattr(it.GCPWorkloadIdentityAuth, "mint",
                        lambda self: GCPAccessToken("FEDERATED.STS.TOKEN", 1, "i", "s"))
    jwt = make_jwt(good_claims())
    seen = {}

    def http_post(url, data, headers, timeout):
        seen.update(url=url, data=data, headers=headers)
        return {"token": jwt}

    m = it.IdentityTokenMinter("https://api.anthropic.com", "wif", service_account="sa@p.iam.gserviceaccount.com",
                               wif_audience="//iam.googleapis.com/projects/1/x", http_post=http_post)
    minted = m.mint()
    assert seen["url"].endswith("/serviceAccounts/sa@p.iam.gserviceaccount.com:generateIdToken")
    assert seen["data"] == {"audience": "https://api.anthropic.com", "includeEmail": True}
    assert seen["headers"]["Authorization"] == "Bearer FEDERATED.STS.TOKEN"
    assert minted.token == jwt


def test_wif_needs_audience_and_sa():
    with pytest.raises(AuthError):
        it.IdentityTokenMinter("aud", "wif")


def test_unknown_source_rejected():
    with pytest.raises(AuthError):
        it.IdentityTokenMinter("aud", "nope")


def test_mint_audit_never_contains_token(home):
    jwt = make_jwt(good_claims())
    it.IdentityTokenMinter("aud", "gcloud", runner=FakeRunner(stdout=jwt)).mint()
    audit = (home / "audit.log")
    text = "".join(p.read_text() for p in home.rglob("*") if p.is_file())
    assert "identity-token-mint" in text or not audit.exists()
    assert "SIGNATURE-SECRET-PART" not in text


def test_write_token_file_is_0600(tmp_path):
    jwt = make_jwt(good_claims())
    minted = it._wrap(jwt, "gcloud", "aud")
    out = tmp_path / "sub" / "tok.jwt"
    it.write_token_file(out, minted)
    assert out.read_text().strip() == jwt
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700


def _minted(exp_in, jti=False, now=1_000_000):
    return it.MintedIdentityToken(token="t", audience="a", expires_at=now + exp_in, has_jti=jti)


def test_refresh_delay_is_earlier_of_margin_and_half_life():
    # 1h token: half-life (1800) comes before exp-900 (2700).
    assert it.next_refresh_delay(_minted(3600), now=1_000_000, margin=900) == 1800
    # A big margin wins over half-life.
    assert it.next_refresh_delay(_minted(3600), now=1_000_000, margin=2400) == 1200
    assert it.next_refresh_delay(_minted(1200), now=1_000_000, margin=900) == 300


def test_refresh_delay_jti_token_rotates_often():
    assert it.next_refresh_delay(_minted(3600, jti=True), now=1_000_000, margin=900, jti_rotate_seconds=240) == 240


def test_refresh_delay_never_below_floor():
    assert it.next_refresh_delay(_minted(-100), now=1_000_000) == it.MIN_SLEEP


class SeqMinter:
    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    def mint(self):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_refresher_rewrites_file_and_survives_failure(tmp_path, monkeypatch):
    first = it._wrap(make_jwt(good_claims(email="one@x")), "gcloud", "aud")
    second = it._wrap(make_jwt(good_claims(email="two@x")), "gcloud", "aud")
    minter = SeqMinter([first, AuthError("transient"), second])
    log = io.StringIO()
    out = tmp_path / "tok.jwt"
    r = it.TokenRefresher(minter, out, log=log)
    r.run_once()
    assert out.read_text().strip() == first.token

    # Drive the loop with zero waits: fail once, then succeed, then stop.
    waits = []

    class FakeEvent:
        def wait(self, seconds):
            waits.append(seconds)
            return len(waits) > 2

        def set(self):
            pass

    r._stop = FakeEvent()
    r.loop()
    assert out.read_text().strip() == second.token
    assert waits[1] == it.RETRY_SECONDS
    text = log.getvalue()
    assert "refresh failed" in text and "refreshed" in text
    assert first.token not in text and second.token not in text
