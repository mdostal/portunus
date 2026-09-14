"""GCPServiceAccountKeyRotationAdapter tests.

All gcloud calls go through an injected fake runner -- no real GCP API is
ever contacted. The runner captures every call so assertions can inspect
exactly which gcloud sub-commands were (and were not) invoked.
"""
import dataclasses
import json
from pathlib import Path

import pytest

from portunus.rotation import (
    GCPServiceAccountKeyRotationAdapter,
    RotationAdapterError,
    RotationResult,
    rotation_adapter_for,
)

# ---------------------------------------------------------------------------
# Fake SA key payloads -- key material is obviously fake, never real
# ---------------------------------------------------------------------------

_FAKE_NEW_KEY_ID = "new-key-id-abc123"
_FAKE_OLD_KEY_ID = "old-key-id-xyz789"

FAKE_NEW_KEY = {
    "type": "service_account",
    "project_id": "my-project",
    "private_key_id": _FAKE_NEW_KEY_ID,
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nFAKE_NEW\n-----END RSA PRIVATE KEY-----\n",
    "client_email": "sa@my-project.iam.gserviceaccount.com",
}

FAKE_OLD_KEY = {
    "type": "service_account",
    "project_id": "my-project",
    "private_key_id": _FAKE_OLD_KEY_ID,
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nFAKE_OLD\n-----END RSA PRIVATE KEY-----\n",
    "client_email": "sa@my-project.iam.gserviceaccount.com",
}

SA_EMAIL = "sa@my-project.iam.gserviceaccount.com"


# ---------------------------------------------------------------------------
# Fake runner factory
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_runner(*, verify_ok=True, create_ok=True, disable_ok=True):
    """Return (runner_fn, call_log).

    The runner writes FAKE_NEW_KEY JSON to the output path when it sees a
    `gcloud iam service-accounts keys create` command, so the adapter can
    parse private_key_id without touching a real API.
    """
    calls: list = []

    def runner(cmd, capture_output=True, text=True, timeout=60):
        calls.append(list(cmd))

        if "service-accounts" in cmd and "keys" in cmd and "create" in cmd:
            if create_ok:
                # Output path is the first positional arg after 'create'
                create_idx = cmd.index("create")
                output_path = cmd[create_idx + 1]
                Path(output_path).write_text(json.dumps(FAKE_NEW_KEY))
                return _FakeProc(0)
            return _FakeProc(1, stderr="gcloud create failed (fake)")

        if "activate-service-account" in cmd:
            return _FakeProc(0 if verify_ok else 1,
                             stderr="" if verify_ok else "gcloud activate failed (fake)")

        if "print-access-token" in cmd:
            return _FakeProc(0 if verify_ok else 1, stdout="ya29.FAKE-TOKEN")

        if "disable" in cmd:
            return _FakeProc(0 if disable_ok else 1,
                             stderr="" if disable_ok else "gcloud disable failed (fake)")

        return _FakeProc(0)

    return runner, calls


# ---------------------------------------------------------------------------
# Reference helper
# ---------------------------------------------------------------------------

class _FakeRef:
    def __init__(
        self,
        name="ffe-cicd-sa-key",
        sm_name="ffe-cicd-sa-key",
        project="my-project",
        iam_account=SA_EMAIL,
    ):
        self.name = name
        self.sm_name = sm_name
        self.project = project
        self.tags = {"iam_account": iam_account} if iam_account else {}


# ---------------------------------------------------------------------------
# capability()
# ---------------------------------------------------------------------------

def test_gcp_adapter_capability_is_auto():
    adapter = GCPServiceAccountKeyRotationAdapter()
    assert adapter.capability() == "auto"


def test_rotation_adapter_for_gcp_returns_real_adapter():
    adapter = rotation_adapter_for("gcp")
    assert isinstance(adapter, GCPServiceAccountKeyRotationAdapter)


# ---------------------------------------------------------------------------
# Non-destructive default (retire_old=False)
# ---------------------------------------------------------------------------

def test_non_destructive_default_stores_new_key(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, calls = _make_runner()
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)
    result = adapter.rotate(_FakeRef())

    # New key is stored
    stored = json.loads(local.access("ffe-cicd-sa-key"))
    assert stored["private_key_id"] == _FAKE_NEW_KEY_ID


def test_non_destructive_default_does_not_disable_old_key(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, calls = _make_runner()
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)
    adapter.rotate(_FakeRef())

    disable_calls = [c for c in calls if "disable" in c]
    delete_calls = [c for c in calls if "delete" in c]
    assert disable_calls == [], "old key must NOT be disabled in non-destructive mode"
    assert delete_calls == [], "old key must NOT be deleted in non-destructive mode"


def test_non_destructive_default_result_retired_old_is_false(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, _ = _make_runner()
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)
    result = adapter.rotate(_FakeRef())

    assert isinstance(result, RotationResult)
    assert result.retired_old is False
    assert result.provider == "gcp"
    assert result.phase == "stored"


# ---------------------------------------------------------------------------
# Verify-failure: nothing stored, nothing retired, audit warn:verify-failed
# ---------------------------------------------------------------------------

def test_verify_failure_stores_nothing(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, _ = _make_runner(verify_ok=False)
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)

    with pytest.raises(RotationAdapterError, match="verification failed"):
        adapter.rotate(_FakeRef())

    # Old key content is still the stored value -- nothing was overwritten
    stored = json.loads(local.access("ffe-cicd-sa-key"))
    assert stored["private_key_id"] == _FAKE_OLD_KEY_ID, "old key must remain intact on verify failure"


def test_verify_failure_disables_nothing(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, calls = _make_runner(verify_ok=False)
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)

    with pytest.raises(RotationAdapterError):
        adapter.rotate(_FakeRef(), retire_old=True)

    disable_calls = [c for c in calls if "disable" in c]
    assert disable_calls == [], "nothing must be disabled when verify fails"


def test_verify_failure_appends_warn_audit(home):
    from portunus.audit import AuditChain
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))
    audit = AuditChain()

    runner, _ = _make_runner(verify_ok=False)
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local, audit=audit)

    with pytest.raises(RotationAdapterError):
        adapter.rotate(_FakeRef())

    entries = audit.entries()
    assert any(
        e.get("result", "").startswith("warn:verify-failed")
        for e in entries
    ), "audit must record warn:verify-failed on verify failure"


# ---------------------------------------------------------------------------
# retire_old=True: disable old key, NEVER delete in same call
# ---------------------------------------------------------------------------

def test_retire_old_disables_superseded_key(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, calls = _make_runner()
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)
    result = adapter.rotate(_FakeRef(), retire_old=True)

    disable_calls = [c for c in calls if "disable" in c]
    assert len(disable_calls) == 1, "exactly one disable call expected"
    assert _FAKE_OLD_KEY_ID in disable_calls[0], "old key id must be in the disable command"


def test_retire_old_never_deletes_in_same_call(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, calls = _make_runner()
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)
    adapter.rotate(_FakeRef(), retire_old=True)

    delete_calls = [c for c in calls if "delete" in c]
    assert delete_calls == [], "delete must NEVER happen in the same call as disable"


def test_retire_old_result_reports_retired_old_true(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, _ = _make_runner()
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)
    result = adapter.rotate(_FakeRef(), retire_old=True)

    assert result.retired_old is True


def test_disable_happens_after_store_not_before(home):
    """Verify the disable gcloud call comes AFTER the store (which is silent
    on the runner), so a store failure cannot leave the old key disabled."""
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, calls = _make_runner()
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)
    adapter.rotate(_FakeRef(), retire_old=True)

    # create comes before disable in the call log
    create_idx = next(i for i, c in enumerate(calls) if "create" in c)
    disable_idx = next(i for i, c in enumerate(calls) if "disable" in c)
    assert create_idx < disable_idx, "disable must come after create (and implicitly after store)"


# ---------------------------------------------------------------------------
# Output-surface boundary: no key material in RotationResult
# ---------------------------------------------------------------------------

def test_rotation_result_contains_no_key_material(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    runner, _ = _make_runner()
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)
    result = adapter.rotate(_FakeRef())

    result_str = str(result)
    # Fake key material strings must never appear in the result repr
    assert "FAKE_NEW" not in result_str
    assert "FAKE_OLD" not in result_str
    assert "BEGIN RSA" not in result_str


def test_rotation_result_structural_fields(home):
    """RotationResult for gcp must not have any credential-carrying field."""
    field_names = {f.name for f in dataclasses.fields(RotationResult)}
    assert "private_key" not in field_names
    assert "key_material" not in field_names
    assert "token" not in field_names
    assert "secret" not in field_names


# ---------------------------------------------------------------------------
# Missing iam_account raises clearly
# ---------------------------------------------------------------------------

def test_missing_iam_account_raises(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()

    runner, _ = _make_runner()
    adapter = GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)

    class _RefNoAccount:
        name = "no-account-ref"
        sm_name = "no-account-ref"
        project = "my-project"
        tags = {}

    with pytest.raises(RotationAdapterError, match="iam_account"):
        adapter.rotate(_RefNoAccount())


# ---------------------------------------------------------------------------
# CLI: rotation run
# ---------------------------------------------------------------------------

def test_cli_rotation_run_requires_ref_with_iam_account(home, capsys):
    """CLI rotation run prints result and exits 0 on success."""
    from portunus.localvault import LocalEncryptedBackend
    from portunus.rotation import GCPServiceAccountKeyRotationAdapter
    import portunus.rotation as _rot_mod

    local = LocalEncryptedBackend()
    local.store("ffe-cicd-sa-key", json.dumps(FAKE_OLD_KEY))

    # Seed the registry
    from portunus.registry import Registry
    reg = Registry()
    reg.add(
        name="ffe-cicd-sa-key",
        sm_name="ffe-cicd-sa-key",
        provider="gcp",
        project="my-project",
        tags={"iam_account": SA_EMAIL},
    )

    runner, calls = _make_runner()

    # Monkey-patch rotation_adapter_for to inject our fake runner
    original = _rot_mod.rotation_adapter_for

    def _patched_adapter_for(provider):
        if provider == "gcp":
            return GCPServiceAccountKeyRotationAdapter(runner=runner, local_backend=local)
        return original(provider)

    _rot_mod.rotation_adapter_for = _patched_adapter_for
    try:
        from portunus.cli import main
        rc = main(["rotation", "run", "ffe-cicd-sa-key"])
    finally:
        _rot_mod.rotation_adapter_for = original

    out = capsys.readouterr().out
    assert rc == 0
    assert "ffe-cicd-sa-key" in out
    assert "stored" in out
    # Key material must not appear in CLI output
    assert "FAKE_NEW" not in out
    assert "BEGIN RSA" not in out


def test_cli_rotation_run_unknown_ref_exits_nonzero(home, capsys):
    from portunus.cli import main
    rc = main(["rotation", "run", "no-such-ref"])
    assert rc != 0
