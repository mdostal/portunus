"""TOTP seeds held by Portunus -- hand out the current code, never the seed.

A ``totp`` reference stores an ``otpauth://totp/...`` URI in the vault (seed,
algorithm, digits, period, issuer, account). Whoever holds the seed can mint
codes forever, so the seed IS the credential: no CLI command or MCP tool ever
returns it or the URI. The only thing that leaves this module is the current
code and how many seconds it stays valid (RFC 6238 over RFC 4226 HOTP).

``Resolver`` refuses to substitute a totp reference into any boundary sink
(``resolve``/``inject``/``resolve_exec``/``resolve_to_tempfile``), so the
seed can't be smuggled out through ``{{secret:NAME}}`` either -- see
``Resolver._fetch`` and ``Resolver.otp``.

Recovery codes are separate, one-time ``recovery_code`` references: gated
by default when created, and moved to state ``used`` after the one resolve
that hands them out.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

# Reference.secret_type values. "" is an ordinary opaque secret.
SECRET_TYPE_TOTP = "totp"
SECRET_TYPE_RECOVERY_CODE = "recovery_code"
SECRET_TYPES = ("", SECRET_TYPE_TOTP, SECRET_TYPE_RECOVERY_CODE)

_ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}
_BASE32_RE = re.compile(r"^[A-Z2-7]+=*$")


class TotpError(ValueError):
    """The value is not a usable TOTP seed/URI. The message never contains
    the seed."""


@dataclass(frozen=True)
class TotpParams:
    secret: bytes
    algorithm: str = "SHA1"
    digits: int = 6
    period: int = 30
    issuer: str = ""
    account: str = ""

    def __repr__(self) -> str:  # never print the seed, even in a traceback
        return (f"TotpParams(secret=<redacted>, algorithm={self.algorithm!r}, "
                f"digits={self.digits}, period={self.period}, issuer={self.issuer!r}, "
                f"account={self.account!r})")

    __str__ = __repr__


def _decode_base32(raw: str) -> bytes:
    cleaned = re.sub(r"[\s-]", "", raw).upper().rstrip("=")
    if not cleaned or not _BASE32_RE.match(cleaned):
        raise TotpError("seed is not valid base32")
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        secret = base64.b32decode(padded)
    except (binascii.Error, ValueError):
        raise TotpError("seed is not valid base32") from None
    if len(secret) < 10:
        # RFC 4226 §4 requires at least 128 bits; 80 is the floor real
        # issuers use. Anything shorter is almost certainly a typo.
        raise TotpError("seed is too short (need at least 80 bits)")
    return secret


def parse(value: str) -> TotpParams:
    """Parse an ``otpauth://totp/...`` URI, or a bare base32 seed (defaults:
    SHA1, 6 digits, 30 s). Raises TotpError without echoing the input."""
    value = (value or "").strip()
    if not value:
        raise TotpError("empty TOTP value")
    if not value.lower().startswith("otpauth://"):
        return TotpParams(secret=_decode_base32(value))
    parsed = urlparse(value)
    if parsed.netloc.lower() != "totp":
        raise TotpError(f"only otpauth://totp URIs are supported (got {parsed.netloc.lower() or 'none'})")
    query = {k.lower(): v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
    if "secret" not in query:
        raise TotpError("otpauth URI has no secret parameter")
    secret = _decode_base32(query["secret"])
    algorithm = (query.get("algorithm") or "SHA1").upper().replace("-", "")
    if algorithm not in _ALGORITHMS:
        raise TotpError(f"unsupported algorithm {algorithm!r} (want SHA1, SHA256 or SHA512)")
    try:
        digits = int(query.get("digits") or 6)
        period = int(query.get("period") or 30)
    except ValueError:
        raise TotpError("digits and period must be integers") from None
    if not 6 <= digits <= 10:
        raise TotpError("digits must be between 6 and 10")
    if not 1 <= period <= 3600:
        raise TotpError("period must be between 1 and 3600 seconds")
    label = unquote(parsed.path.lstrip("/"))
    label_issuer, _, account = label.rpartition(":")
    issuer = query.get("issuer") or label_issuer.strip()
    return TotpParams(secret=secret, algorithm=algorithm, digits=digits, period=period,
                      issuer=issuer, account=account.strip())


def to_uri(params: TotpParams) -> str:
    """The canonical otpauth URI stored in the vault for a TOTP reference."""
    seed = base64.b32encode(params.secret).decode("ascii").rstrip("=")
    label = quote(f"{params.issuer}:{params.account}" if params.issuer else params.account, safe=":@")
    query = f"secret={seed}&algorithm={params.algorithm}&digits={params.digits}&period={params.period}"
    if params.issuer:
        query += f"&issuer={quote(params.issuer, safe='')}"
    return f"otpauth://totp/{label}?{query}"


def normalize(value: str) -> str:
    """Validate a URI/bare seed and return the canonical URI to store."""
    return to_uri(parse(value))


def hotp(secret: bytes, counter: int, digits: int = 6, algorithm: str = "SHA1") -> str:
    """RFC 4226 HOTP value for `counter`."""
    digest = hmac.new(secret, struct.pack(">Q", counter), _ALGORITHMS[algorithm]).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def code_at(params: TotpParams, at: float) -> str:
    """RFC 6238 TOTP code at unix time `at` (T0 = 0)."""
    return hotp(params.secret, int(at) // params.period, params.digits, params.algorithm)


def current(value: str, now: Optional[float] = None) -> dict:
    """The one public way out of a stored TOTP value: the code and its
    remaining validity. Never the seed, never the URI."""
    params = parse(value)
    at = time.time() if now is None else now
    return {
        "code": code_at(params, at),
        "seconds_left": params.period - (int(at) % params.period),
        "period": params.period,
        "digits": params.digits,
    }


def describe(value: str) -> dict:
    """Non-secret facts about a stored seed (for prompt/drop confirmations)."""
    params = parse(value)
    return {"issuer": params.issuer, "account": params.account,
            "algorithm": params.algorithm, "digits": params.digits, "period": params.period}


# --- QR import ----------------------------------------------------------------

def decode_qr(image_path: str, runner: Optional[Callable] = None,
              which: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """Decode an authenticator-setup QR image with `zbarimg` and return the
    otpauth URI it encodes. The URI is captured from the subprocess's own
    pipe and handed straight back to the caller to store -- never printed.
    Raises TotpError (no seed in the message) when the image has no
    otpauth QR code."""
    which = which or shutil.which
    runner = runner or subprocess.run
    if not which("zbarimg"):
        raise TotpError("QR import needs `zbarimg` (zbar-tools / brew install zbar) on PATH")
    try:
        proc = runner(["zbarimg", "--quiet", "--raw", image_path],
                      capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise TotpError("zbarimg timed out") from None
    except OSError as exc:
        raise TotpError(f"could not run zbarimg: {exc.strerror}") from None
    for line in (proc.stdout or "").splitlines():
        if line.strip().lower().startswith("otpauth://"):
            return line.strip()
    raise TotpError("no otpauth:// QR code found in the image")
