"""Story 03 (portunus-chrome-credential-broker): Linux key fetch (Secret
Service / KWallet / no-keyring) and the key=None plaintext-fallback path
through the shared decrypt_blob(). Not live-tested on a real Linux desktop in
this session (implementing machine is macOS) -- these are fixture/mock-only;
Story 05's closeout must disclose that honestly rather than claim a live
proof that didn't happen.

Secret-boundary invariant: same no-log/no-print discipline as Story 02,
extended to the plaintext-fallback path -- a password Chrome never "really"
encrypted is still never allowed to appear in Portunus's own output.
"""
import subprocess
from pathlib import Path

import pytest

from portunus.chrome_crypto import ChromeCryptoError, decrypt_blob, linux_key


def test_linux_key_uses_secret_service_when_available(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        assert cmd[0] == "secret-tool"
        return subprocess.CompletedProcess(cmd, 0, stdout="fixture-secret-service-password\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    key = linux_key(Path("/home/alice/.config/google-chrome/Default"))

    assert key is not None
    assert len(key) == 16


def test_linux_key_falls_back_to_kwallet_when_secret_service_unavailable(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "secret-tool":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no result")
        if cmd[0] == "kwallet-query":
            return subprocess.CompletedProcess(cmd, 0, stdout="fixture-kwallet-password\n", stderr="")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    key = linux_key(Path("/home/alice/.config/google-chrome/Default"))

    assert key is not None
    assert len(key) == 16


def test_linux_key_returns_none_when_neither_keyring_available(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no result")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert linux_key(Path("/home/alice/.config/google-chrome/Default")) is None


def test_linux_key_returns_none_when_tools_are_missing_entirely(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert linux_key(Path("/home/alice/.config/google-chrome/Default")) is None


def test_decrypt_blob_with_none_key_returns_plaintext_for_v10_blob():
    blob = b"v10" + b"fixture-plaintext-value"

    assert decrypt_blob(None, blob) == "fixture-plaintext-value"


def test_decrypt_blob_with_none_key_rejects_v11_blob():
    """v11 means Chrome actually encrypted it -- key=None (no keyring found)
    must never be silently treated as 'good enough' to decrypt a v11 blob."""
    blob = b"v11" + b"\x00" * 16

    with pytest.raises(ChromeCryptoError):
        decrypt_blob(None, blob)


def test_no_raw_value_ever_reaches_stdout_or_stderr(monkeypatch, capsys):
    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="fixture-secret-service-password\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    key = linux_key(Path("/home/alice/.config/google-chrome/Default"))
    decrypt_blob(None, b"v10" + b"fixture-plaintext-value")  # the no-keyring plaintext path

    captured = capsys.readouterr()
    assert "fixture-secret-service-password" not in captured.out
    assert "fixture-secret-service-password" not in captured.err
    assert "fixture-plaintext-value" not in captured.out
    assert "fixture-plaintext-value" not in captured.err
