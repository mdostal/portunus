"""PANT-1403 / GH #254: Infisical adapter -- universal auth, full-address and
project-mapped addressing, fail-closed errors, CLI map/status, health. No
network: every test injects a fake HTTP transport."""
import json
import urllib.parse
from types import SimpleNamespace

import pytest

from portunus import infisical
from portunus.backend import BackendError, InfisicalBackend, MockBackend

SECRET = "sk_live_SUPER-SECRET"
CLIENT_SECRET = "client-SECRET-xyz"
PID = "6f1c0000-aaaa-bbbb-cccc-000000000001"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("INFISICAL_TOKEN", "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
                "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET", "INFISICAL_SITE_URL", "INFISICAL_API_URL"):
        monkeypatch.delenv(var, raising=False)


class FakeInfisical:
    def __init__(self, secrets=None, login_status=200, expires_in=3600, secret_status=None):
        self.secrets = {("/", "STRIPE_KEY"): SECRET} if secrets is None else secrets
        self.login_status = login_status
        self.expires_in = expires_in
        self.secret_status = list(secret_status or [])
        self.calls = []
        self.logins = 0

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/api/v1/auth/universal-auth/login":
            self.logins += 1
            if self.login_status != 200:
                return self.login_status, b'{"message":"invalid credentials"}'
            return 200, json.dumps({"accessToken": f"tok-{self.logins}", "expiresIn": self.expires_in,
                                    "tokenType": "Bearer"}).encode()
        assert parts.path.startswith("/api/v3/secrets/raw/")
        if self.secret_status:
            status = self.secret_status.pop(0)
            if status != 200:
                return status, b'{"message":"nope"}'
        q = dict(urllib.parse.parse_qsl(parts.query))
        name = urllib.parse.unquote(parts.path.rsplit("/", 1)[1])
        value = self.secrets.get((q["secretPath"], name))
        if value is None:
            return 404, json.dumps({"message": f"Secret with name '{name}' not found"}).encode()
        return 200, json.dumps({"secret": {"secretKey": name, "secretValue": value}}).encode()


def _env_creds(monkeypatch):
    monkeypatch.setenv("INFISICAL_UNIVERSAL_AUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET", CLIENT_SECRET)


# --- addressing ------------------------------------------------------------

@pytest.mark.parametrize("sm_name,folder,name", [
    (f"infisical:{PID}:prod:STRIPE_KEY", "/", "STRIPE_KEY"),
    (f"infisical:{PID}:prod:/STRIPE_KEY", "/", "STRIPE_KEY"),
    (f"infisical:{PID}:prod:/payments/eu/STRIPE_KEY", "/payments/eu", "STRIPE_KEY"),
])
def test_parse_full_address(sm_name, folder, name):
    a = infisical.parse_address(sm_name)
    assert (a.project_id, a.environment, a.secret_path, a.name) == (PID, "prod", folder, name)
    assert a.site_url == infisical.DEFAULT_SITE_URL


@pytest.mark.parametrize("bad", ["infisical:", f"infisical:{PID}:prod", f"infisical:{PID}::X", f"infisical:{PID}:prod:/"])
def test_parse_full_address_rejects(bad):
    with pytest.raises(infisical.InfisicalError):
        infisical.parse_address(bad)


def test_parse_mapped_address_joins_base_folder():
    m = {"shop": infisical.InfisicalMapping("shop", PID, "dev", secret_path="/backend",
                                            site_url="https://infisical.example.com/api/")}
    a = infisical.parse_address("payments/STRIPE_KEY", "shop", m)
    assert (a.secret_path, a.name, a.environment) == ("/backend/payments", "STRIPE_KEY", "dev")
    assert a.site_url == "https://infisical.example.com"
    assert infisical.parse_address("/abs/K", "shop", m).secret_path == "/abs"


def test_plain_name_without_mapping_fails_with_remediation():
    with pytest.raises(infisical.InfisicalError, match="infisical map shop"):
        infisical.parse_address("STRIPE_KEY", "shop", {})


def test_site_url_env(monkeypatch):
    monkeypatch.setenv("INFISICAL_API_URL", "https://self.example.com/api")
    assert infisical.parse_address(f"infisical:{PID}:prod:K").site_url == "https://self.example.com"


# --- resolve ---------------------------------------------------------------

def test_universal_auth_login_then_read(monkeypatch):
    _env_creds(monkeypatch)
    http = FakeInfisical()
    be = InfisicalBackend(http=http)
    assert be.access(f"infisical:{PID}:prod:STRIPE_KEY") == SECRET
    login, read = http.calls
    assert login[0] == "POST" and json.loads(login[3]) == {"clientId": "cid", "clientSecret": CLIENT_SECRET}
    assert CLIENT_SECRET not in login[1]  # never in the URL
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(read[1]).query))
    assert q == {"workspaceId": PID, "environment": "prod", "secretPath": "/", "include_imports": "true"}
    assert read[2]["Authorization"] == "Bearer tok-1"


def test_token_is_cached_until_expiry(monkeypatch):
    _env_creds(monkeypatch)
    http = FakeInfisical()
    be = InfisicalBackend(http=http)
    be.access(f"infisical:{PID}:prod:STRIPE_KEY")
    be.access(f"infisical:{PID}:prod:STRIPE_KEY")
    assert http.logins == 1


def test_expired_token_relogs_once(monkeypatch):
    _env_creds(monkeypatch)
    http = FakeInfisical(secret_status=[401, 200])
    assert InfisicalBackend(http=http).access(f"infisical:{PID}:prod:STRIPE_KEY") == SECRET
    assert http.logins == 2


def test_infisical_token_env_skips_login(monkeypatch):
    monkeypatch.setenv("INFISICAL_TOKEN", "pre-minted")
    http = FakeInfisical()
    assert InfisicalBackend(http=http).access(f"infisical:{PID}:prod:STRIPE_KEY") == SECRET
    assert http.logins == 0 and http.calls[0][2]["Authorization"] == "Bearer pre-minted"


def test_credentials_from_local_vault():
    local = MockBackend({"infisical-client-id": "local-cid", "infisical-client-secret": CLIENT_SECRET})
    http = FakeInfisical()
    assert InfisicalBackend(local_backend=local, http=http).access(f"infisical:{PID}:prod:STRIPE_KEY") == SECRET
    assert json.loads(http.calls[0][3])["clientId"] == "local-cid"


def test_mapped_reference_uses_mapping_and_custom_refs():
    infisical.save_mappings({"shop": infisical.InfisicalMapping(
        "shop", PID, "dev", secret_path="/backend", client_id_ref="shop-cid", client_secret_ref="shop-cs")})
    local = MockBackend({"shop-cid": "c", "shop-cs": "s"})
    http = FakeInfisical(secrets={("/backend", "DB_URL"): "postgres://SECRET"})
    assert InfisicalBackend(local_backend=local, http=http).access("DB_URL", project="shop") == "postgres://SECRET"


def test_no_credentials_fails_closed():
    http = FakeInfisical()
    with pytest.raises(BackendError, match="no machine-identity credentials"):
        InfisicalBackend(local_backend=MockBackend({}), http=http).access(f"infisical:{PID}:prod:STRIPE_KEY")
    assert http.calls == []


def test_rejected_login_fails_closed(monkeypatch):
    _env_creds(monkeypatch)
    with pytest.raises(BackendError, match="login was rejected"):
        InfisicalBackend(http=FakeInfisical(login_status=401)).access(f"infisical:{PID}:prod:STRIPE_KEY")


def test_not_found_carries_router_marker(monkeypatch):
    from portunus.backend import is_not_found
    _env_creds(monkeypatch)
    with pytest.raises(BackendError) as exc:
        InfisicalBackend(http=FakeInfisical()).access(f"infisical:{PID}:prod:MISSING")
    assert is_not_found(exc.value)


def test_denied_read_fails_closed(monkeypatch):
    _env_creds(monkeypatch)
    with pytest.raises(BackendError, match="denied"):
        InfisicalBackend(http=FakeInfisical(secret_status=[403])).access(f"infisical:{PID}:prod:STRIPE_KEY")


def test_unreachable_fails_closed(monkeypatch):
    _env_creds(monkeypatch)

    def down(*a, **k):
        raise infisical.InfisicalError("infisical: app.infisical.com is unreachable (dns)")

    with pytest.raises(BackendError, match="unreachable"):
        InfisicalBackend(http=down).access(f"infisical:{PID}:prod:STRIPE_KEY")


def test_errors_never_contain_values(monkeypatch):
    _env_creds(monkeypatch)
    for status in (401, 403, 404, 500):
        with pytest.raises(BackendError) as exc:
            InfisicalBackend(http=FakeInfisical(login_status=status)).access(f"infisical:{PID}:prod:STRIPE_KEY")
        assert CLIENT_SECRET not in str(exc.value) and SECRET not in str(exc.value)


# --- config / CLI / health / router -----------------------------------------

def test_mappings_file_is_0600(home):
    infisical.save_mappings({"shop": infisical.InfisicalMapping("shop", PID, "dev")})
    assert (home / "infisical.json").stat().st_mode & 0o777 == 0o600
    assert infisical.load_mappings()["shop"].project_id == PID


def test_cli_map_and_status(home, capsys):
    from portunus.cli import main
    assert main(["infisical", "map", "shop", "--project-id", PID, "--env", "dev", "--path", "/backend"]) == 0
    assert main(["infisical", "map", "shop", "--env", "prod"]) == 0  # partial update keeps the rest
    m = infisical.load_mappings()["shop"]
    assert (m.project_id, m.environment, m.secret_path) == (PID, "prod", "/backend")
    capsys.readouterr()
    assert main(["infisical", "status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mappings"][0]["project"] == "shop" and "local vault" in out["mappings"][0]["credentials"]
    assert main(["infisical", "map", "shop", "--remove"]) == 0
    assert infisical.load_mappings() == {}


def test_cli_map_requires_ids_for_new_mapping(home):
    from portunus.cli import main
    assert main(["infisical", "map", "shop", "--env", "dev"]) == 1


def test_health_reports_unmapped(home):
    from portunus.health import _check_infisical, _Report
    report = _Report()
    refs = [SimpleNamespace(name="db", sm_name="DB_URL", project="shop"),
            SimpleNamespace(name="full", sm_name=f"infisical:{PID}:prod:K", project="")]
    _check_infisical(report, refs)
    check = report.checks[-1]
    assert check["ok"] is False and "no mapping for: db" in check["detail"]
    infisical.save_mappings({"shop": infisical.InfisicalMapping("shop", PID, "dev")})
    report = _Report()
    _check_infisical(report, refs)
    assert report.checks[-1]["ok"] is True


def test_router_builds_real_infisical_backend(home):
    from portunus.audit import AuditChain
    from portunus.backend import LocalFallbackBackend
    from portunus.cli import _make_backend_router
    router = _make_backend_router({}, AuditChain(), fallback_backend=SimpleNamespace())
    inst = router(SimpleNamespace(backend="infisical", project=""))
    assert isinstance(inst, LocalFallbackBackend)
    assert isinstance(inst.remote, InfisicalBackend)
