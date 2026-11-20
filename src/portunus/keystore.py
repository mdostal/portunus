"""OS keystore backend -- macOS Keychain, Linux Secret Service, Windows
Credential Manager -- behind one ``keychain:`` reference scheme (GH #254).

Read-only. Portunus never creates, updates or deletes keystore items.

sm_name scheme::

    keychain:<service>[:<account>]

``<service>`` and ``<account>`` are percent-decoded, so a literal colon in a
service name is written ``%3A``. Everything after the first unescaped colon is
the account.

Platform mapping (same addressing everywhere, so one reference is portable):

* **macOS** -- ``security find-generic-password -s <service> [-a <account>] -w``.
  The value comes back on stdout, never argv. A locked keychain (or one that
  would need a GUI prompt) fails closed.
* **Linux** -- ``secret-tool lookup service <service> [username <account>]``
  over the freedesktop Secret Service D-Bus API, which GNOME Keyring and
  KWallet (5.97+) both implement. The ``service``/``username`` attribute pair
  is the convention python-keyring uses, so items it stored resolve as-is.
* **Windows** -- ``CredReadW`` on a generic credential whose target name is
  ``<service>``. If ``<account>`` is given the credential's user name must
  match, otherwise it fails closed rather than return someone else's secret.

Locked or unavailable stores raise ``KeystoreError``; there is no fallback to
any other backend.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Callable, Optional, Tuple
from urllib.parse import quote, unquote

SM_PREFIX = "keychain:"
SCHEME_HINT = "keychain:<service>[:<account>]"

Runner = Callable[..., subprocess.CompletedProcess]
# (target_name) -> (user_name, blob_bytes) or None when not found.
WinReader = Callable[[str], Optional[Tuple[str, bytes]]]

# macOS `security` exit codes (errSec* & 0xff is not reliable, so match text too).
_MAC_NOT_FOUND_EXIT = 44
_MAC_LOCKED_MARKERS = ("interaction is not allowed", "keychain is locked",
                       "user canceled", "user cancelled", "authorization")
_LINUX_UNAVAILABLE_MARKERS = ("d-bus", "dbus", "org.freedesktop.secrets",
                              "cannot autolaunch", "no such interface", "was not provided by any")

_ERROR_NOT_FOUND = 1168  # Windows ERROR_NOT_FOUND
_CRED_TYPE_GENERIC = 1


class KeystoreError(RuntimeError):
    """Tool missing, store locked/unavailable, item not found, bad sm_name."""


def parse_sm_name(sm_name: str) -> Tuple[str, str]:
    """`keychain:<service>[:<account>]` -> (service, account). Account may be ''."""
    if not sm_name.startswith(SM_PREFIX):
        raise KeystoreError(f"keychain backend: sm_name must be {SCHEME_HINT!r}, got {sm_name!r}")
    raw_service, _, raw_account = sm_name[len(SM_PREFIX):].partition(":")
    service, account = unquote(raw_service), unquote(raw_account)
    if not service:
        raise KeystoreError(f"keychain backend: sm_name must be {SCHEME_HINT!r}, got {sm_name!r}")
    return service, account


def make_sm_name(service: str, account: str = "") -> str:
    s = quote(service, safe=" ")
    return f"{SM_PREFIX}{s}:{quote(account, safe=' @')}" if account else f"{SM_PREFIX}{s}"


def platform_store(platform: Optional[str] = None) -> str:
    """Human name of the store this platform reads from."""
    p = platform or sys.platform
    if p == "darwin":
        return "macOS Keychain"
    if p.startswith("linux"):
        return "Secret Service (GNOME Keyring / KWallet)"
    if p == "win32":
        return "Windows Credential Manager"
    return f"unsupported platform {p!r}"


def tool_status(platform: Optional[str] = None) -> Tuple[bool, str]:
    """(available, detail). Probes only that the access tool exists -- never
    reads an item, so it cannot trigger an unlock prompt."""
    p = platform or sys.platform
    store = platform_store(p)
    if p == "darwin":
        ok = shutil.which("security") is not None
        return ok, f"{store} via `security`" + ("" if ok else " -- `security` not found on PATH")
    if p.startswith("linux"):
        ok = shutil.which("secret-tool") is not None
        return ok, (f"{store} via `secret-tool`" if ok else
                    f"{store} -- `secret-tool` not found on PATH (install libsecret-tools / libsecret)")
    if p == "win32":
        return True, f"{store} via CredReadW"
    return False, store


def _default_runner(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs)


def _run(cmd, runner: Optional[Runner], timeout: int, tool: str) -> subprocess.CompletedProcess:
    if runner is None and shutil.which(cmd[0]) is None:
        raise KeystoreError(f"keychain backend: `{cmd[0]}` not found on PATH ({tool})")
    try:
        return (runner or _default_runner)(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # A hung unlock prompt lands here: treat it as locked, never retry elsewhere.
        raise KeystoreError(
            f"keychain backend: `{cmd[0]}` timed out after {timeout}s -- the keystore is "
            "probably locked and waiting on an unlock prompt"
        ) from exc


def _read_macos(service: str, account: str, runner: Optional[Runner], timeout: int) -> str:
    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd += ["-a", account]
    cmd.append("-w")
    proc = _run(cmd, runner, timeout, "macOS Keychain")
    if proc.returncode == 0:
        out = proc.stdout or ""
        return out[:-1] if out.endswith("\n") else out
    err = (proc.stderr or "").strip()
    low = err.lower()
    if proc.returncode == _MAC_NOT_FOUND_EXIT or "could not be found" in low:
        raise KeystoreError(f"keychain backend: no Keychain item for {_label(service, account)}")
    if any(m in low for m in _MAC_LOCKED_MARKERS):
        raise KeystoreError(
            "keychain backend: the macOS keychain is locked or access was denied -- "
            "unlock it (`security unlock-keychain`) and allow access, then retry"
        )
    raise KeystoreError(f"keychain backend: `security` failed (exit {proc.returncode}): {err[:200]}")


def _read_linux(service: str, account: str, runner: Optional[Runner], timeout: int) -> str:
    cmd = ["secret-tool", "lookup", "service", service]
    if account:
        cmd += ["username", account]
    proc = _run(cmd, runner, timeout, "Secret Service")
    err = (proc.stderr or "").strip()
    low = err.lower()
    if proc.returncode == 0:
        return proc.stdout or ""
    if any(m in low for m in _LINUX_UNAVAILABLE_MARKERS):
        raise KeystoreError(
            "keychain backend: no Secret Service is reachable on the session bus -- start "
            "GNOME Keyring or KWallet in this session (DBUS_SESSION_BUS_ADDRESS must be set)"
        )
    if "locked" in low or "dismissed" in low or "prompt" in low:
        raise KeystoreError(
            "keychain backend: the Secret Service collection is locked -- unlock your "
            "keyring, then retry"
        )
    if proc.returncode == 1 and not err:
        # secret-tool prints nothing and exits 1 both for "no such item" and for
        # an unlock prompt the user dismissed; say both, fall back to neither.
        raise KeystoreError(
            f"keychain backend: no Secret Service item for {_label(service, account)} "
            "(or the collection is locked and the unlock prompt was dismissed)"
        )
    raise KeystoreError(f"keychain backend: `secret-tool` failed (exit {proc.returncode}): {err[:200]}")


def _win_cred_read(target: str) -> Optional[Tuple[str, bytes]]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    pcred = ctypes.POINTER(CREDENTIAL)()
    if not advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            return None
        raise KeystoreError(f"keychain backend: CredReadW failed (Windows error {code})")
    try:
        cred = pcred.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return cred.UserName or "", blob
    finally:
        advapi32.CredFree(pcred)


def _decode_win_blob(blob: bytes) -> str:
    # Credential Manager's UI and python-keyring store UTF-16-LE; some tools
    # (cmdkey-adjacent scripts, Git Credential Manager) store UTF-8.
    if len(blob) % 2 == 0:
        try:
            text = blob.decode("utf-16-le")
            if "\x00" not in text:
                return text
        except UnicodeDecodeError:
            pass
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KeystoreError("keychain backend: credential blob is neither UTF-16 nor UTF-8 text") from exc


def _read_windows(service: str, account: str, reader: Optional[WinReader]) -> str:
    found = (reader or _win_cred_read)(service)
    if found is None:
        raise KeystoreError(f"keychain backend: no Credential Manager entry for {_label(service, account)}")
    user, blob = found
    if account and user != account:
        raise KeystoreError(
            f"keychain backend: Credential Manager entry {service!r} belongs to a different "
            f"user name than {account!r}"
        )
    return _decode_win_blob(blob)


def _label(service: str, account: str) -> str:
    return f"service={service!r} account={account!r}" if account else f"service={service!r}"


def read(service: str, account: str = "", *, platform: Optional[str] = None,
         runner: Optional[Runner] = None, win_reader: Optional[WinReader] = None,
         timeout: int = 30) -> str:
    """Return one item's plaintext. The only value-returning call in this
    module; callers outside the resolver boundary must not use it."""
    p = platform or sys.platform
    if p == "darwin":
        return _read_macos(service, account, runner, timeout)
    if p.startswith("linux"):
        return _read_linux(service, account, runner, timeout)
    if p == "win32":
        return _read_windows(service, account, win_reader)
    raise KeystoreError(f"keychain backend: {p!r} has no supported OS keystore")
