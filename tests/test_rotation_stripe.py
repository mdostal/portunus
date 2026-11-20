"""Stripe rotation (PANT-1134). Stripe's public API has no endpoint for
creating, rolling or expiring an API key of any type -- standard secret
(`sk_`), publishable (`pk_`) and restricted (`rk_`) keys are all rotated on
the Dashboard only. The adapter is `manual`: it never makes a network call,
and `rotation run` prints the Dashboard path instead of calling it a stub."""
import pytest

from portunus.cli import main
from portunus.rotation import (
    RotationAdapterError,
    StripeRotationAdapter,
    _ADAPTERS,
    rotation_adapter_for,
)


def test_stripe_is_registered():
    assert _ADAPTERS["stripe"] is StripeRotationAdapter
    assert isinstance(rotation_adapter_for("stripe"), StripeRotationAdapter)


def test_stripe_capability_is_manual():
    assert StripeRotationAdapter().capability() == "manual"


@pytest.mark.parametrize("retire_old", [False, True])
def test_stripe_rotate_raises_with_dashboard_path(retire_old):
    with pytest.raises(RotationAdapterError) as exc:
        StripeRotationAdapter().rotate("stripe-ref", retire_old=retire_old)
    message = str(exc.value)
    assert "no API" in message
    assert "dashboard.stripe.com/apikeys" in message
    # An honest manual, not a perpetual "not yet implemented" stub.
    assert "not yet implemented" not in message
    assert "adapter-request" not in message


def test_cli_rotation_run_reports_manual_not_stub(home, capsys):
    from portunus.registry import Registry
    Registry().add(name="stripe-prod-secret-key", sm_name="stripe-prod-secret-key",
                   provider="stripe")
    rc = main(["rotation", "run", "stripe-prod-secret-key"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot be rotated programmatically" in err
    assert "stub" not in err
