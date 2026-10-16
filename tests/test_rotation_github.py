"""GitHubRotationAdapter + GitHub liveness (PANT-1141 / portunus#188).

Every GitHub HTTP call goes through an injected fake -- no real GitHub API
is ever contacted. Token values are obviously fake."""
import base64
import json

import pytest

from portunus.cli import main
from portunus.liveness import DEAD, LIVE, UNKNOWN, UNSUPPORTED, check_reference, run_liveness
from portunus.rotation import (
    GitHubRotationAdapter,
    RotationAdapterError,
    github_token_subtype,
    rotation_adapter_for,
)

OLD = "gho_FAKEOLDTOKEN000000000000000000000000"
NEW = "gho_FAKENEWTOKEN000000000000000000000000"
CLIENT_ID = "Iv1.fakeclientid"
CLIENT_SECRET = "fake-client-secret"
APP_TAGS = {
    "github_client_id": CLIENT_ID,
    "github_client_secret_ref": "gh-app-secret",
}


class _FakeGitHub:
    """Records every call; answers like GitHub's /applications/{id}/token
    and /rate_limit endpoints for a configurable set of valid tokens."""

    def __init__(self, valid=(OLD,), reset_status=200):
        self.valid = set(valid)
        self.reset_status = reset_status
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        if url.endswith("/rate_limit"):
            token = headers["Authorization"].split(" ", 1)[1]
            return (200, {}) if token in self.valid else (401, {"message": "Bad credentials"})
        assert url.endswith(f"/applications/{CLIENT_ID}/token")
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        assert headers["Authorization"] == f"Basic {expected}"
        token = body["access_token"]
        if method == "POST":
            return (200, {"id": 42}) if token in self.valid else (404, {})
        if method == "PATCH":
            if self.reset_status != 200:
                return self.reset_status, {}
            self.valid.discard(token)
            self.valid.add(NEW)
            return 200, {"id": 42, "token": NEW}
        raise AssertionError(method)


class _Store:
    def __init__(self, fail=False):
        self.fail = fail
        self.stored = {}

    def store(self, sm_name, value):
        if self.fail:
            raise OSError("disk full")
        self.stored[sm_name] = value


@pytest.fixture
def gh(stack):
    stack["backend"].set("gh-app-secret-sm", CLIENT_SECRET)
    stack["registry"].add("gh-app-secret", "gh-app-secret-sm")
    return stack


def _add_token(stack, value, name="gh-token", tags=None, **kw):
    stack["backend"].set(f"{name}-sm", value)
    return stack["registry"].add(name, f"{name}-sm", provider="github", tags=tags or {}, **kw)


# --- subtype classification -------------------------------------------------

@pytest.mark.parametrize("value,subtype", [
    ("ghp_abc", "classic_pat"),
    ("github_pat_11ABC", "fine_grained_pat"),
    ("gho_abc", "oauth_app"),
    ("ghu_abc", "app_user"),
    ("ghs_abc", "app_installation"),
    ("ghr_abc", "app_refresh"),
    ("-----BEGIN RSA PRIVATE KEY-----\n...", "app_private_key"),
    ("not-a-github-token", ""),
])
def test_subtype_from_prefix(value, subtype):
    assert github_token_subtype(value=value) == subtype


def test_subtype_tag_wins_over_prefix():
    assert github_token_subtype(value="ghp_x", tags={"github_token_type": "oauth_app"}) == "oauth_app"


def test_registry_resolves_github_to_real_adapter():
    adapter = rotation_adapter_for("github")
    assert isinstance(adapter, GitHubRotationAdapter)
    assert adapter.capability() == "auto"


class _Ref:
    def __init__(self, tags):
        self.tags = tags


@pytest.mark.parametrize("tags,cap", [
    ({"github_token_type": "classic_pat"}, "manual"),
    ({"github_token_type": "fine_grained_pat"}, "manual"),
    ({"github_token_type": "app_private_key"}, "manual"),
    ({"github_token_type": "app_installation"}, "not_applicable"),
    ({"github_token_type": "app_user"}, "not_applicable"),
    ({"github_token_type": "oauth_app", **APP_TAGS}, "auto"),
    ({"github_token_type": "oauth_app"}, "manual"),  # e.g. a `gh auth login` token
    ({}, "unknown"),
])
def test_capability_for_is_per_subtype(tags, cap):
    assert GitHubRotationAdapter().capability_for(_Ref(tags)) == cap


# --- rotate: refusals -------------------------------------------------------

@pytest.mark.parametrize("value,needle", [
    ("ghp_FAKE", "classic personal access tokens"),
    ("github_pat_FAKE", "fine-grained"),
    ("ghs_FAKE", "1 hour"),
    ("ghu_FAKE", "portunus oauth store"),
    ("something-else", "unrecognized"),
])
def test_rotate_refuses_non_resettable_subtypes_without_any_http(gh, value, needle):
    ref = _add_token(gh, value)
    http = _FakeGitHub()
    with pytest.raises(RotationAdapterError, match=needle):
        GitHubRotationAdapter(http=http, local_backend=_Store()).rotate(
            ref, resolver=gh["resolver"], retire_old=True
        )
    assert http.calls == []


def test_rotate_refuses_gh_cli_token_without_owning_app(gh):
    ref = _add_token(gh, OLD)
    http = _FakeGitHub()
    with pytest.raises(RotationAdapterError, match="gh auth login"):
        GitHubRotationAdapter(http=http, local_backend=_Store()).rotate(
            ref, resolver=gh["resolver"], retire_old=True
        )
    assert http.calls == []


def test_rotate_requires_retire_old_acknowledgement(gh):
    ref = _add_token(gh, OLD, tags=APP_TAGS)
    http = _FakeGitHub()
    with pytest.raises(RotationAdapterError, match="--retire-old"):
        GitHubRotationAdapter(http=http, local_backend=_Store()).rotate(ref, resolver=gh["resolver"])
    assert http.calls == []


def test_rotate_refuses_non_local_backend_before_reset(gh):
    ref = _add_token(gh, OLD, tags=APP_TAGS, backend="gcp")
    http = _FakeGitHub()
    with pytest.raises(RotationAdapterError, match="refusing before reset"):
        GitHubRotationAdapter(http=http, local_backend=_Store()).rotate(
            ref, resolver=gh["resolver"], retire_old=True
        )
    assert http.calls == []


def test_rotate_dead_old_token_never_resets(gh):
    ref = _add_token(gh, OLD, tags=APP_TAGS)
    http = _FakeGitHub(valid=())
    store = _Store()
    with pytest.raises(RotationAdapterError, match="already invalid"):
        GitHubRotationAdapter(http=http, local_backend=store, audit=gh["audit"]).rotate(
            ref, resolver=gh["resolver"], retire_old=True
        )
    assert [c[0] for c in http.calls] == ["POST"]
    assert store.stored == {}


# --- rotate: happy path and failures after reset ----------------------------

def test_rotate_resets_stores_then_verifies(gh):
    ref = _add_token(gh, OLD, tags=APP_TAGS)
    http = _FakeGitHub()
    store = _Store()
    result = GitHubRotationAdapter(http=http, local_backend=store, audit=gh["audit"]).rotate(
        ref, resolver=gh["resolver"], retire_old=True
    )
    assert [c[0] for c in http.calls] == ["POST", "PATCH", "POST"]
    assert http.calls[2][3] == {"access_token": NEW}  # verified the NEW token
    assert store.stored == {"gh-token-sm": NEW}
    assert result.provider == "github"
    assert result.key_id == "42"
    assert result.retired_old is True
    assert OLD not in repr(result) and NEW not in repr(result)
    log = gh["audit"].path.read_text()
    assert "rotate:github-oauth-app" in log and "ok:auth=42" in log
    assert NEW not in log and OLD not in log and CLIENT_SECRET not in log


def test_rotate_tagged_subtype_skips_value_sniffing(gh):
    ref = _add_token(gh, OLD, tags={"github_token_type": "oauth_app", **APP_TAGS})
    GitHubRotationAdapter(http=_FakeGitHub(), local_backend=_Store()).rotate(
        ref, resolver=gh["resolver"], retire_old=True
    )


def test_rotate_store_failure_after_reset_is_loud(gh):
    ref = _add_token(gh, OLD, tags=APP_TAGS)
    with pytest.raises(RotationAdapterError, match="old token is invalid") as exc_info:
        GitHubRotationAdapter(http=_FakeGitHub(), local_backend=_Store(fail=True)).rotate(
            ref, resolver=gh["resolver"], retire_old=True
        )
    assert NEW not in str(exc_info.value)


def test_rotate_reset_failure_stores_nothing(gh):
    ref = _add_token(gh, OLD, tags=APP_TAGS)
    store = _Store()
    with pytest.raises(RotationAdapterError, match="HTTP 422"):
        GitHubRotationAdapter(http=_FakeGitHub(reset_status=422), local_backend=store).rotate(
            ref, resolver=gh["resolver"], retire_old=True
        )
    assert store.stored == {}


# --- liveness ---------------------------------------------------------------

def test_liveness_live_and_dead(gh):
    _add_token(gh, OLD, name="alive")
    _add_token(gh, "ghp_FAKEDEAD", name="dead")
    results = {r.ref_name: r for r in run_liveness(
        gh["registry"], gh["resolver"], http=_FakeGitHub(valid=(OLD,)), audit=gh["audit"]
    )}
    assert set(results) == {"alive", "dead"}  # non-github refs are skipped
    assert results["alive"].status == LIVE
    assert results["dead"].status == DEAD
    log = gh["audit"].path.read_text()
    assert "ok:live" in log and "err:dead" in log
    assert OLD not in log and "ghp_FAKEDEAD" not in log


def test_liveness_ambiguous_status_is_unknown(gh):
    ref = _add_token(gh, OLD)
    result = check_reference(ref, gh["resolver"], http=lambda *a: (503, {}))
    assert result.status == UNKNOWN


def test_liveness_network_error_is_unknown(gh):
    ref = _add_token(gh, OLD)

    def boom(*a):
        raise OSError("unreachable")

    assert check_reference(ref, gh["resolver"], http=boom).status == UNKNOWN


def test_liveness_private_key_is_unsupported(gh):
    ref = _add_token(gh, "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----")
    http = _FakeGitHub()
    assert check_reference(ref, gh["resolver"], http=http).status == UNSUPPORTED
    assert http.calls == []


def test_liveness_requested_placeholder_is_unsupported(gh):
    # e.g. `demo-github-ci` -- state=requested, no value ever stored
    ref = gh["registry"].add("demo-github-ci", "demo-github-ci", provider="github", state="requested")
    http = _FakeGitHub()
    assert check_reference(ref, gh["resolver"], http=http).status == UNSUPPORTED
    assert http.calls == []


def test_liveness_unknown_provider_is_unsupported(gh):
    ref = gh["registry"].add("x", "x-sm", provider="stripe")
    assert check_reference(ref, gh["resolver"]).status == UNSUPPORTED


# --- CLI --------------------------------------------------------------------

def test_cli_liveness_exits_2_on_dead(home, monkeypatch, capsys):
    import portunus.liveness as liveness

    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_GH_DEAD", "ghp_FAKEDEAD")
    monkeypatch.setattr(liveness, "_github_http", _FakeGitHub(valid=()))
    from portunus import Registry

    Registry().add("gh-dead", "gh-dead", provider="github")
    rc = main(["rotation", "liveness", "--json"])
    out, err = capsys.readouterr()
    assert rc == 2
    payload = json.loads(out)
    assert payload["dead"] == 1
    assert payload["results"][0]["status"] == DEAD
    assert "DEAD credential: gh-dead" in err
    assert "ghp_FAKEDEAD" not in out + err


def test_cli_liveness_empty_registry(home, monkeypatch, capsys):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    assert main(["rotation", "liveness"]) == 0
    assert "supported providers: github" in capsys.readouterr().out
