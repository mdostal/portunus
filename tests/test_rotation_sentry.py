"""Sentry rotation (PANT-1139). Sentry's token endpoints (user, org and
internal-integration auth tokens) accept session auth only and reject any
bearer token, so no admin credential can mint or revoke a token. The adapter
is registered as `manual`: it never makes a network call, and `rotation run`
prints the human re-issue path rather than calling it a stub."""
import pytest

from portunus.cli import main
from portunus.rotation import (
    RotationAdapterError,
    SentryRotationAdapter,
    _ADAPTERS,
    rotation_adapter_for,
)


def test_sentry_is_registered():
    assert _ADAPTERS["sentry"] is SentryRotationAdapter
    assert isinstance(rotation_adapter_for("sentry"), SentryRotationAdapter)


def test_sentry_capability_is_manual():
    assert SentryRotationAdapter().capability() == "manual"


@pytest.mark.parametrize("retire_old", [False, True])
def test_sentry_rotate_raises_with_reissue_path(retire_old):
    with pytest.raises(RotationAdapterError, match="session auth only") as exc:
        SentryRotationAdapter().rotate("sentry-ref", retire_old=retire_old)
    assert "Re-issue by hand" in str(exc.value)


def test_cli_rotation_run_reports_manual_not_stub(home, capsys):
    from portunus.registry import Registry
    Registry().add(
        name="orchestration-dev-sentry-auth-token",
        sm_name="orchestration-dev-sentry-auth-token",
        provider="sentry",
    )
    rc = main(["rotation", "run", "orchestration-dev-sentry-auth-token"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "session auth only" in err
    assert "stub" not in err
