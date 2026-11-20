"""Stripe API key rotation (PANT-1409, portunus#245).

Stripe's v2 IAM API (``/v2/iam/api_keys``) can rotate a standard or
restricted secret key, or a publishable key, without anyone visiting the
Dashboard. Endpoints used (contract taken from Stripe's generated SDK,
stripe-python ``private-preview`` branch, ``stripe/v2/iam/_api_key_service.py``,
and docs.stripe.com/stripe-apps/api-authentication/managed-api-keys):

- ``POST /v2/iam/api_keys/{id}/rotate`` -- "A new key with the same
  properties is created and returned. The existing key is expired
  immediately, unless an expiry time is specified"
  (``expire_current_key_in_minutes``, max 10080 = 7 days). Rotating a
  secret key REQUIRES a caller-supplied RSA public key (PEM, >= 2048 bits):
  live secret tokens come back only as a JWE (``secret_key.encrypted_secret``,
  ``jwe_compact``) encrypted to that key. Publishable keys come back as
  plaintext ``publishable_key.token`` and take no public key.
- ``GET /v2/iam/api_keys/{id}`` -- metadata (status, type, livemode); never
  a live secret token.
- ``POST /v2/iam/api_keys/{id}/expire`` -- "The specified key becomes
  invalid immediately."

AVAILABILITY: as of 2026-11 this API is a Stripe *preview*
(``Stripe-Version: 2026-11-18.preview``). Stripe documents it for approved
Stripe Apps holding the ``api_key_write`` permission, acting on a user's
account through the ``Stripe-Context`` header. Whether a merchant's own
secret key may call it for its own account depends on Stripe enabling the
preview for that account. When Stripe refuses, the adapter fails before
anything changes and says so; see docs/rotation.md (Stripe API keys).

Non-destructive default: Portunus never asks Stripe for an immediate
expiry. Every rotate call carries ``expire_current_key_in_minutes`` (the
grace window), so a failure in verify or store still leaves the old key
working. ``--retire-old`` expires the old key with an explicit ``/expire``
call, and only after the new key is verified and stored.

Boundary guarantee: the admin key is handled only inside
``Resolver.resolve_call()``. The new secret is decrypted in-process with an
ephemeral RSA private key that never leaves this function, written straight
to the local backend, and never returned, logged, or put in an error
message. Every HTTP call goes through the injected ``transport`` seam.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Callable, Dict, Optional, Tuple

from .rotation import (
    RotationAdapterError,
    RotationResult,
    _add_superseded,
    _drop_superseded,
    _json_http,
    update_rotation_bindings,
)

STRIPE_API = "https://api.stripe.com"
# The v2 IAM API keys endpoints are preview-only; override per ref with
# tags["stripe_api_version"] once Stripe ships a GA version.
STRIPE_PREVIEW_VERSION = "2026-11-18.preview"
# Stripe's documented maximum for expire_current_key_in_minutes (7 days).
STRIPE_MAX_GRACE_MINUTES = 10080
# Default grace when --retire-old is not passed: Stripe's maximum, matching
# the Dashboard's "both keys work for up to 7 days" rotation window.
STRIPE_DEFAULT_GRACE_MINUTES = STRIPE_MAX_GRACE_MINUTES
KEY_TYPES = ("secret_key", "publishable_key")

# (method, url, headers, json_body_or_None, timeout) -> (status, parsed_json_or_{})
StripeTransport = Callable[[str, str, Dict[str, str], Optional[dict], float], Tuple[int, dict]]
# () -> (private_key_object, public_key_pem_str)
KeypairFactory = Callable[[], Tuple[object, str]]


def _default_stripe_transport(
    method: str, url: str, headers: Dict[str, str], body: Optional[dict], timeout: float
) -> Tuple[int, dict]:
    return _json_http(method, url, headers, body, timeout, "Stripe API")


def _ephemeral_rsa_keypair() -> Tuple[object, str]:
    """A throwaway RSA-3072 keypair for one rotation. The private key stays in
    memory for the length of the rotate() call and is never serialized."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private_key, pem


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


_AES_GCM_KEY_BYTES = {"A128GCM": 16, "A192GCM": 24, "A256GCM": 32}


def decrypt_jwe_compact(token: str, private_key) -> str:
    """Decrypt a compact-serialized JWE (RSA-OAEP / RSA-OAEP-256 key wrap,
    AES-GCM content encryption), the format Stripe uses for
    ``secret_key.encrypted_secret``. Raises RotationAdapterError on anything
    unexpected; the error never includes ciphertext or plaintext."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 5:
        raise RotationAdapterError("Stripe returned an encrypted secret that is not a compact JWE")
    try:
        header = json.loads(_b64url_decode(parts[0]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise RotationAdapterError("Stripe returned a JWE with an unreadable header") from exc
    alg, enc = header.get("alg"), header.get("enc")
    if header.get("zip"):
        raise RotationAdapterError("Stripe returned a compressed JWE, which Portunus does not accept")
    if alg == "RSA-OAEP-256":
        oaep_hash = hashes.SHA256()
    elif alg == "RSA-OAEP":
        oaep_hash = hashes.SHA1()  # noqa: S303 -- JWE RSA-OAEP is defined with SHA-1
    else:
        raise RotationAdapterError(f"Stripe returned a JWE with unsupported alg {alg!r}")
    if enc not in _AES_GCM_KEY_BYTES:
        raise RotationAdapterError(f"Stripe returned a JWE with unsupported enc {enc!r}")
    try:
        cek = private_key.decrypt(
            _b64url_decode(parts[1]),
            padding.OAEP(mgf=padding.MGF1(algorithm=oaep_hash), algorithm=oaep_hash, label=None),
        )
        if len(cek) != _AES_GCM_KEY_BYTES[enc]:
            raise ValueError("content key length does not match enc")
        plaintext = AESGCM(cek).decrypt(
            _b64url_decode(parts[2]),
            _b64url_decode(parts[3]) + _b64url_decode(parts[4]),
            parts[0].encode("ascii"),
        )
        return plaintext.decode("utf-8")
    except Exception as exc:  # InvalidTag, ValueError, UnicodeDecodeError ...
        raise RotationAdapterError("could not decrypt the new Stripe secret returned by the rotation") from exc


def classify_stripe_key(value: str) -> str:
    """``publishable_key`` for ``pk_...``, ``secret_key`` for ``sk_``/``rk_``,
    "" for anything else. Only the classification ever leaves a boundary."""
    if value.startswith("pk_"):
        return "publishable_key"
    if value.startswith(("sk_", "rk_")):
        return "secret_key"
    return ""


class StripeRotationAdapter:
    """Real Stripe API key rotation over the v2 IAM API (preview).

    Phases (in order):
    1. rotate  -- ``POST /v2/iam/api_keys/{old_id}/rotate`` with the admin
                  key, ``expire_current_key_in_minutes`` = grace window, and
                  (secret keys) an ephemeral RSA public key. The returned
                  JWE is decrypted in-process.
    2. verify  -- ``GET /v2/iam/api_keys/{new_id}`` with the admin key must
                  report ``status=active`` and the expected ``type``. A
                  secret key must also authenticate a read-only
                  ``GET /v1/balance`` (a restricted key without balance
                  access passes on Stripe's ``permission_error``: the key was
                  accepted, only the permission is missing).
    3. store   -- write the new value to the local backend, record
                  ``tags["stripe_key_id"]``, drop ``rotation_requested``,
                  and flip a ``locked`` reference back to ``enabled``.
    4. retire  -- OPT-IN ONLY via ``retire_old=True``:
                  ``POST /v2/iam/api_keys/{old_id}/expire``.

    A failed verify stores nothing and retires nothing. It records
    ``warn:verify-failed`` and makes a best-effort expire of the new,
    never-stored key, so the old key keeps working through the grace window.
    Without --retire-old the old key id is appended to the ``stripe``
    RotationBinding's ``superseded_key_ids``, and Stripe expires it when
    the grace window ends.

    Ref tags:
    - ``stripe_key_id`` (required) -- Stripe id of the API key object stored
      in this ref (shown on the Dashboard's API keys page, or returned by
      ``GET /v2/iam/api_keys``).
    - ``stripe_admin_ref`` -- reference holding the key that calls the IAM
      API. Defaults to the ref itself (an unrestricted ``sk_`` can rotate
      itself). Required for publishable keys, which cannot authenticate the
      IAM API.
    - ``stripe_key_type`` -- ``secret_key`` / ``publishable_key``. When
      absent, it is classified from the stored value's prefix inside a
      resolve boundary.
    - ``stripe_context`` -- optional ``Stripe-Context`` header (e.g. a
      connected account id when a platform/app key rotates on its behalf).
    - ``stripe_grace_minutes`` -- old-key grace window, 1..10080 (default
      10080 = 7 days).
    - ``stripe_api_version`` -- ``Stripe-Version`` override (default
      ``2026-11-18.preview``).
    """

    def __init__(
        self,
        transport: Optional[StripeTransport] = None,
        local_backend=None,
        audit=None,
        registry=None,
        admin_credential_ref: Optional[str] = None,
        keypair_factory: Optional[KeypairFactory] = None,
        timeout: float = 30.0,
    ):
        self._transport = transport or _default_stripe_transport
        self._local_backend = local_backend
        self._audit = audit
        self._registry = registry
        # A "{{secret:NAME}}" template; overrides tags["stripe_admin_ref"].
        self._admin_credential_ref = admin_credential_ref
        self._keypair_factory = keypair_factory or _ephemeral_rsa_keypair
        self._timeout = timeout

    def capability(self) -> str:
        return "auto"

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> RotationResult:
        if hasattr(ref, "sm_name"):
            sm_name = ref.sm_name
            ref_name = getattr(ref, "name", sm_name)
            tags = dict(getattr(ref, "tags", None) or {})
        else:
            sm_name = str(ref)
            ref_name = sm_name
            tags = {}

        if resolver is None:
            raise RotationAdapterError(
                f"StripeRotationAdapter: rotating {ref_name!r} needs a resolver to "
                "resolve the Stripe admin key"
            )
        old_id = tags.get("stripe_key_id", "").strip()
        if not old_id:
            raise RotationAdapterError(
                f"StripeRotationAdapter: {ref_name!r} has no tags['stripe_key_id'] -- set it to "
                "the Stripe API key object's id (Dashboard > API keys, or GET /v2/iam/api_keys) "
                "so Portunus knows which key to rotate; nothing was changed"
            )
        grace = self._grace_minutes(tags)
        key_type = self._key_type(ref_name, tags, resolver)
        if key_type == "publishable_key" and not (
            self._admin_credential_ref or tags.get("stripe_admin_ref")
        ):
            raise RotationAdapterError(
                f"StripeRotationAdapter: {ref_name!r} is a publishable key, which cannot "
                "authenticate the Stripe IAM API -- set tags['stripe_admin_ref'] to the "
                "reference holding the account's secret key; nothing was changed"
            )
        admin_template = (
            self._admin_credential_ref
            or "{{secret:%s}}" % tags.get("stripe_admin_ref", ref_name)
        )
        api_version = tags.get("stripe_api_version") or STRIPE_PREVIEW_VERSION
        context = tags.get("stripe_context", "")

        local_backend = self._local_backend
        if local_backend is None:
            from .localvault import LocalEncryptedBackend
            local_backend = LocalEncryptedBackend()

        # --- 1. ROTATE ---------------------------------------------------------
        body: dict = {"expire_current_key_in_minutes": grace}
        private_key = None
        if key_type == "secret_key":
            private_key, public_pem = self._keypair_factory()
            body["public_key"] = {
                "id": f"portunus-{int(time.time())}",
                "pem_key": {"algorithm": "RSA", "data": public_pem},
            }
        status, rotated = self._admin_call(
            resolver, admin_template, api_version, context,
            "POST", f"/v2/iam/api_keys/{old_id}/rotate", body,
        )
        new_id = str(rotated.get("id", "")) if status in (200, 201) else ""
        if not new_id:
            if self._audit:
                self._audit.append("rotate:stripe-key", sm_name, f"err:rotate-http-{status}")
            raise RotationAdapterError(
                f"Stripe rotate failed for {ref_name!r}: {self._describe(status, rotated)}"
                + self._hint(status)
                + " -- old key left intact"
            )

        new_value = ""
        try:
            try:
                new_value = self._extract_value(rotated, key_type, private_key)
            finally:
                private_key = None
            # --- 2. VERIFY -----------------------------------------------------
            if not new_value or not self._verify(
                resolver, admin_template, api_version, context, new_id, key_type, new_value
            ):
                self._abort_new_key(resolver, admin_template, api_version, context, new_id, sm_name)
                raise RotationAdapterError(
                    f"Stripe key verification failed for {ref_name!r} (new key {new_id}) -- "
                    "nothing stored; the new key was expired and the old key keeps working "
                    f"for its {grace}-minute grace window, so retry or roll back in the "
                    "Dashboard before it ends"
                )
            # --- 3. STORE ------------------------------------------------------
            local_backend.store(sm_name, new_value)
        finally:
            new_value = ""

        self._record(resolver, ref_name, tags, new_id)
        update_rotation_bindings(lambda b: _add_superseded(b, "stripe", old_id))

        # --- 4. RETIRE (opt-in) ------------------------------------------------
        retired = False
        if retire_old:
            status, expired = self._admin_call(
                resolver, admin_template, api_version, context,
                "POST", f"/v2/iam/api_keys/{old_id}/expire", {},
            )
            if status not in (200, 201, 404):
                if self._audit:
                    self._audit.append(
                        "rotate:stripe-key", sm_name, f"warn:retire-failed:new={new_id}"
                    )
                raise RotationAdapterError(
                    f"Stripe expire of old key {old_id} failed for {ref_name!r} "
                    f"({self._describe(status, expired)}) -- the new key IS stored; the old "
                    f"one stays listed as superseded and Stripe expires it in {grace} minutes"
                )
            # 404: already gone -- the goal state.
            update_rotation_bindings(lambda b: _drop_superseded(b, "stripe", old_id))
            retired = True

        if self._audit:
            self._audit.append(
                "rotate:stripe-key", sm_name,
                f"ok:new={new_id}" + (":retired-old" if retired else f":old-expires-in={grace}m"),
            )
        return RotationResult(
            provider="stripe", ref_name=ref_name, key_id=new_id, phase="stored",
            retired_old=retired,
        )

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _grace_minutes(tags: dict) -> int:
        raw = tags.get("stripe_grace_minutes", "")
        if raw == "":
            return STRIPE_DEFAULT_GRACE_MINUTES
        try:
            grace = int(raw)
        except ValueError:
            grace = 0
        if not 1 <= grace <= STRIPE_MAX_GRACE_MINUTES:
            raise RotationAdapterError(
                "StripeRotationAdapter: tags['stripe_grace_minutes'] must be an integer "
                f"between 1 and {STRIPE_MAX_GRACE_MINUTES}, got {raw!r}"
            )
        return grace

    @staticmethod
    def _key_type(ref_name: str, tags: dict, resolver) -> str:
        declared = tags.get("stripe_key_type", "")
        if declared:
            if declared not in KEY_TYPES:
                raise RotationAdapterError(
                    f"StripeRotationAdapter: tags['stripe_key_type'] must be one of "
                    f"{KEY_TYPES}, got {declared!r}"
                )
            return declared
        try:
            kind = resolver.resolve_call("{{secret:%s}}" % ref_name, classify_stripe_key)
        except Exception:
            kind = ""
        if kind not in KEY_TYPES:
            raise RotationAdapterError(
                f"StripeRotationAdapter: could not tell whether {ref_name!r} holds a secret or "
                "publishable key -- set tags['stripe_key_type'] to secret_key or "
                "publishable_key; nothing was changed"
            )
        return kind

    @staticmethod
    def _extract_value(rotated: dict, key_type: str, private_key) -> str:
        if rotated.get("type") and rotated.get("type") != key_type:
            raise RotationAdapterError(
                f"Stripe rotated a {rotated.get('type')!r}, expected {key_type!r}"
            )
        if key_type == "publishable_key":
            block = rotated.get("publishable_key") or {}
            return str(block.get("token", "")) if isinstance(block, dict) else ""
        block = rotated.get("secret_key") or {}
        if not isinstance(block, dict):
            return ""
        encrypted = block.get("encrypted_secret") or {}
        if isinstance(encrypted, dict) and encrypted.get("ciphertext"):
            if private_key is None:
                raise RotationAdapterError("Stripe returned an encrypted secret but no key to decrypt it")
            return decrypt_jwe_compact(encrypted["ciphertext"], private_key)
        # Sandbox/test-mode keys may come back in plaintext.
        return str(block.get("token") or "")

    def _verify(self, resolver, admin_template, api_version, context, new_id, key_type, value) -> bool:
        try:
            status, meta = self._admin_call(
                resolver, admin_template, api_version, context,
                "GET", f"/v2/iam/api_keys/{new_id}", None,
            )
        except RotationAdapterError:
            return False
        if status != 200 or meta.get("status") != "active" or meta.get("type", key_type) != key_type:
            return False
        if key_type == "publishable_key":
            published = meta.get("publishable_key") or {}
            token = published.get("token") if isinstance(published, dict) else None
            return token is None or token == value
        if classify_stripe_key(value) != "secret_key":
            return False
        try:
            status, resp = self._transport(
                "GET", STRIPE_API + "/v1/balance", self._v1_headers(value), None, self._timeout
            )
        except RotationAdapterError:
            return False
        if status == 200:
            return True
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        # The key authenticated; a restricted key just lacks balance:read.
        return status == 403 and err.get("type") == "permission_error"

    def _abort_new_key(self, resolver, admin_template, api_version, context, new_id, sm_name):
        if self._audit:
            self._audit.append("rotate:stripe-key", sm_name, f"warn:verify-failed:new={new_id}")
        try:
            self._admin_call(
                resolver, admin_template, api_version, context,
                "POST", f"/v2/iam/api_keys/{new_id}/expire", {},
            )
        except Exception:
            pass  # best-effort cleanup; the caller's error is what matters

    def _admin_call(self, resolver, admin_template, api_version, context, method, path, body):
        def _boundary(admin_key: str):
            return self._transport(
                method, STRIPE_API + path,
                self._v2_headers(admin_key, api_version, context), body, self._timeout,
            )

        return resolver.resolve_call(admin_template, _boundary)

    @staticmethod
    def _v2_headers(key: str, api_version: str, context: str) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Stripe-Version": api_version,
            "User-Agent": "portunus-rotation",
        }
        if context:
            headers["Stripe-Context"] = context
        return headers

    @staticmethod
    def _v1_headers(key: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {key}", "User-Agent": "portunus-rotation"}

    @staticmethod
    def _describe(status: int, resp: dict) -> str:
        # Only Stripe's error type/code/message -- never the body, which on
        # success carries the new key.
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        bits = [str(err.get(k, ""))[:80] for k in ("type", "code") if err.get(k)]
        message = str(err.get("message", ""))[:200]
        return f"HTTP {status}" + (f" {'/'.join(bits)}" if bits else "") + (
            f": {message}" if message else "")

    @staticmethod
    def _hint(status: int) -> str:
        if status in (401, 403, 404):
            return (
                " (Stripe's /v2/iam/api_keys API is a preview: the admin key's account must be "
                "enrolled, or the key must belong to a Stripe App with api_key_write; see "
                "docs/rotation.md, Stripe API keys)"
            )
        return ""

    def _record(self, resolver, ref_name: str, tags: dict, new_id: str) -> None:
        registry = self._registry or getattr(resolver, "registry", None)
        if registry is None or not hasattr(registry, "retag") or ref_name not in registry:
            return
        new_tags = {k: v for k, v in tags.items() if k != "rotation_requested"}
        new_tags["stripe_key_id"] = new_id
        registry.retag(ref_name, tags=new_tags)
        # The stored value no longer has the provenance that got it locked
        # (e.g. pasted into a chat): Stripe minted it straight into the vault.
        current = registry.get(ref_name) if hasattr(registry, "get") else None
        if current is not None and getattr(current, "state", "") == "locked":
            registry.set_state(ref_name, "enabled")
