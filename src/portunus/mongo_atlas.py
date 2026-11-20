"""MongoDB Atlas database-user rotation (PANT-1131, portunus#198).

The stored credential is a whole MongoDB connection URI
(``mongodb+srv://user:password@cluster0.abcde.mongodb.net/db?...``); the
database user embedded in it is what rotates. The Atlas Administration API
v2 manages database users programmatically. Checked 2026-11-20 against the
published OpenAPI spec (https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2.json):

- ``GET    /api/atlas/v2/groups/{groupId}/databaseUsers/{databaseName}/{username}``
- ``POST   /api/atlas/v2/groups/{groupId}/databaseUsers``                  (201)
- ``PATCH  /api/atlas/v2/groups/{groupId}/databaseUsers/{databaseName}/{username}``
- ``DELETE /api/atlas/v2/groups/{groupId}/databaseUsers/{databaseName}/{username}`` (204)
- ``GET    /api/atlas/v2/groups/{groupId}/clusters/{clusterName}/status``
  -> ``changeStatus`` ``PENDING`` | ``APPLIED`` ("When status is Pending,
  new users can't log in").

All of them take the versioned media type ``application/vnd.atlas.2023-01-01+json``
and need the *Project Database Access Admin* role (or Project Owner). The
API authenticates with either
- a **service account** (recommended by Atlas): OAuth 2.0 client
  credentials at ``POST https://cloud.mongodb.com/api/oauth/token``
  (HTTP Basic client_id:client_secret, ``grant_type=client_credentials``),
  giving a 1-hour Bearer token, or
- a legacy **programmatic API key** (public/private key), which uses HTTP
  Digest auth.

Two modes, chosen by the ``atlas_rotation_mode`` tag:

``new-user`` (default, non-destructive). Same posture as the add-then-
retire adapters (Resend, Vercel):
  1. read     -- GET the current user; copy its roles, scopes, labels.
  2. create   -- POST a sibling user ``<base>-rot<unix-ts>`` with a fresh
                 password.
  3. wait     -- optional: poll the cluster's changeStatus until APPLIED.
  4. verify   -- a real connection (``ping``) with the new URI. On failure
                 the new user is deleted and nothing is stored.
  5. store    -- the URI with only its user:password replaced, plus the
                 optional companion username/password refs.
  6. retire   -- only with ``--retire-old``: DELETE the old user.

``password`` (in place, destructive, so ``--retire-old`` is required):
PATCH the user's password, STORE FIRST (the old password is gone the moment
Atlas applies it), then wait and verify. Same posture as Neon.

Verification needs a MongoDB driver: the optional ``pymongo`` dependency
(``pip install 'pantheon-portunus[mongodb]'``). Without it the adapter
refuses BEFORE changing anything rather than rotating blind.

Boundary rules: the admin credential, the passwords and the URIs never go
into a RotationResult, an audit entry, or an error message. Atlas error
bodies are reduced to their ``errorCode``/``detail`` fields, and driver
exceptions to their class name and numeric code.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import string
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from .rotation import RotationAdapterError, RotationResult

ATLAS_BASE = "https://cloud.mongodb.com"
ATLAS_API = ATLAS_BASE + "/api/atlas/v2"
ATLAS_TOKEN_URL = ATLAS_BASE + "/api/oauth/token"
ATLAS_MEDIA_TYPE = "application/vnd.atlas.2023-01-01+json"

_GROUP_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_ROT_SUFFIX_RE = re.compile(r"-rot\d+$")
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_PASSWORD_LENGTH = 32
_MODES = ("new-user", "password")
_AUTH_TYPES = ("service-account", "api-key")

# Fields of the existing user copied onto the new one in new-user mode.
_COPIED_FIELDS = ("roles", "scopes", "labels", "description")

# (method, url, headers, body_bytes_or_None, timeout, digest_creds_or_None)
#   -> (status, parsed_json_or_{})
AtlasTransport = Callable[
    [str, str, Dict[str, str], Optional[bytes], float, Optional[Tuple[str, str]]],
    Tuple[int, dict],
]
# (uri, timeout) -> None; raises on failure.
MongoConnector = Callable[[str, float], None]


class MongoVerifyError(Exception):
    """A verification connect failed. Carries only a short reason."""


def _default_transport(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: float,
    digest: Optional[Tuple[str, str]],
) -> Tuple[int, dict]:
    import urllib.error
    import urllib.request

    handlers = []
    if digest is not None:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, ATLAS_BASE, digest[0], digest[1])
        handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise RotationAdapterError(f"Atlas API unreachable: {exc.reason}") from None
    try:
        parsed = json.loads(raw.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def pymongo_available() -> bool:
    try:
        import pymongo  # noqa: F401
    except ImportError:
        return False
    return True


def _pymongo_connect(uri: str, timeout: float) -> None:
    import pymongo
    from pymongo.errors import OperationFailure, PyMongoError

    ms = int(timeout * 1000)
    client = None
    try:
        # The constructor already resolves a mongodb+srv:// host (SRV/TXT
        # lookups) and validates the URI, so it raises too.
        client = pymongo.MongoClient(
            uri, serverSelectionTimeoutMS=ms, connectTimeoutMS=ms, socketTimeoutMS=ms,
            appname="portunus-rotation-verify",
        )
        client.admin.command("ping")
    except OperationFailure as exc:
        # Code only: the message can name the user or the database.
        raise MongoVerifyError(f"{type(exc).__name__} code {exc.code}") from None
    except (PyMongoError, ValueError, TypeError) as exc:
        raise MongoVerifyError(type(exc).__name__) from None
    finally:
        if client is not None:
            client.close()


# --- connection-URI helpers ----------------------------------------------------

def parse_uri_user(uri: str) -> Tuple[str, str]:
    """(username, authSource-or-"") from a mongodb:// or mongodb+srv:// URI.
    Raises ValueError with a message that never quotes the URI."""
    parts = urlsplit(uri.strip())
    if parts.scheme not in ("mongodb", "mongodb+srv"):
        raise ValueError("not a mongodb:// or mongodb+srv:// connection URI")
    netloc = parts.netloc
    if "@" not in netloc:
        raise ValueError("the connection URI has no user:password part")
    userinfo = netloc.rsplit("@", 1)[0]
    if ":" not in userinfo:
        raise ValueError("the connection URI has a user but no password")
    user = unquote(userinfo.split(":", 1)[0])
    if not user:
        raise ValueError("the connection URI has an empty user")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    mechanism = query.get("authMechanism", "")
    if mechanism and not mechanism.upper().startswith("SCRAM-"):
        raise ValueError(f"authMechanism {mechanism!r} is not password (SCRAM) auth")
    return user, query.get("authSource", "")


def uri_host(uri: str) -> str:
    """The host part (no credentials) -- safe to log."""
    return urlsplit(uri.strip()).netloc.rsplit("@", 1)[-1]


def replace_credentials(uri: str, user: str, password: str) -> str:
    """The same URI with only user:password replaced (host list, database,
    options all kept)."""
    parts = urlsplit(uri.strip())
    host = parts.netloc.rsplit("@", 1)[-1]
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def generate_password() -> str:
    # Alphanumeric: the Atlas spec describes the field as an "alphanumeric
    # string", and it needs no URI escaping.
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(_PASSWORD_LENGTH))


def next_username(current: str, now: int) -> str:
    return f"{_ROT_SUFFIX_RE.sub('', current)}-rot{now}"


# --- the adapter -----------------------------------------------------------------

class MongoDBAtlasRotationAdapter:
    """Real MongoDB Atlas database-user rotation (provider ``mongodb-atlas``).

    The ref's tags drive the adapter:
    - ``atlas_group_id`` -- required; the Atlas project id (24 hex chars;
      Atlas UI: Project Settings -> Project ID).
    - ``atlas_admin_ref`` -- required; name of another reference holding the
      Atlas admin credential as ``<id>:<secret>``: a service account's
      ``client_id:client_secret`` (default) or a programmatic API key's
      ``public_key:private_key`` (set ``atlas_auth=api-key``). It needs the
      Project Database Access Admin role on that project.
    - ``atlas_auth`` -- optional; ``service-account`` (default) or ``api-key``.
    - ``atlas_rotation_mode`` -- optional; ``new-user`` (default) or ``password``.
    - ``atlas_auth_db`` -- optional; the user's authentication database.
      Defaults to the URI's ``authSource``, else ``admin``.
    - ``atlas_cluster_name`` -- optional; when set, wait for the cluster's
      changeStatus to reach APPLIED before verifying.
    - ``atlas_username_ref`` / ``atlas_password_ref`` -- optional; companion
      references that hold the same user's name/password separately (e.g.
      ``mercatus-liber-mongodb-username`` / ``-password``). They are updated
      together with the URI so the three never drift apart.

    The ref and every companion must live in the local backend.
    """

    def __init__(
        self,
        transport: Optional[AtlasTransport] = None,
        connector: Optional[MongoConnector] = None,
        local_backend=None,
        audit=None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        password_factory: Callable[[], str] = generate_password,
        timeout: float = 30.0,
        apply_timeout: float = 300.0,
        verify_attempts: int = 6,
    ):
        self._transport = transport or _default_transport
        self._connector = connector
        self._local_backend = local_backend
        self._audit = audit
        self._sleep = sleep
        self._clock = clock
        self._password_factory = password_factory
        self._timeout = timeout
        self._apply_timeout = apply_timeout
        self._verify_attempts = max(1, verify_attempts)
        self._bearer = ""

    def capability(self) -> str:
        return "auto"

    # --- entry point -----------------------------------------------------------

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> RotationResult:
        if ref is None or not hasattr(ref, "sm_name"):
            raise RotationAdapterError(
                "MongoDB Atlas rotation needs a registry reference (with tags), not a bare name"
            )
        if resolver is None:
            raise RotationAdapterError(
                "MongoDB Atlas rotation needs a resolver to read the stored connection URI"
            )
        ref_name = getattr(ref, "name", ref.sm_name)
        tags = getattr(ref, "tags", None) or {}

        group_id = tags.get("atlas_group_id", "")
        admin_ref = tags.get("atlas_admin_ref", "")
        missing = [t for t, v in (("atlas_group_id", group_id), ("atlas_admin_ref", admin_ref))
                   if not v]
        if missing:
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r} needs tags {', '.join(missing)} -- see "
                "docs/rotation.md (MongoDB Atlas connection URIs)"
            )
        if not _GROUP_ID_RE.match(group_id):
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: tags['atlas_group_id'] must be the "
                "24-hex-digit Atlas project id"
            )
        mode = tags.get("atlas_rotation_mode", "new-user")
        if mode not in _MODES:
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: tags['atlas_rotation_mode'] must be "
                f"one of {', '.join(_MODES)}"
            )
        auth_type = tags.get("atlas_auth", "service-account")
        if auth_type not in _AUTH_TYPES:
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: tags['atlas_auth'] must be one of "
                f"{', '.join(_AUTH_TYPES)}"
            )
        if mode == "password" and not retire_old:
            raise RotationAdapterError(
                f"MongoDB Atlas password rotation for {ref_name!r} replaces the user's password "
                "in place -- the old one stops working as soon as Atlas applies it. Re-run with "
                "--retire-old to acknowledge, or use atlas_rotation_mode=new-user (default) for "
                "a non-destructive rotation."
            )
        companions = {k: tags.get(k, "") for k in ("atlas_username_ref", "atlas_password_ref")}
        if getattr(ref, "backend", "") not in ("", "local"):
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: reference lives in backend "
                f"{ref.backend!r}, but this adapter can only store to the local vault -- refusing "
                "before touching Atlas."
            )
        connector = self._connector
        if connector is None:
            if not pymongo_available():
                raise RotationAdapterError(
                    f"MongoDB Atlas rotation for {ref_name!r} verifies the new credential with a "
                    "real connection, which needs pymongo -- install it with "
                    "`pip install 'pantheon-portunus[mongodb]'`. Nothing was changed."
                )
            connector = _pymongo_connect

        local_backend = self._local_backend
        if local_backend is None:
            from .localvault import LocalEncryptedBackend
            local_backend = LocalEncryptedBackend()

        ctx = {
            "ref": ref, "ref_name": ref_name, "tags": tags, "group_id": group_id,
            "mode": mode, "auth_type": auth_type, "companions": companions,
            "retire_old": retire_old, "local": local_backend, "connector": connector,
        }
        outcome = resolver.resolve_call(
            "{{secret:%s}}" % admin_ref,
            lambda admin_cred: resolver.resolve_call(
                "{{secret:%s}}" % ref_name,
                lambda old_uri: self._run(ctx, admin_cred, old_uri),
            ),
        )
        new_user, retired = outcome
        return RotationResult(
            provider="mongodb-atlas",
            ref_name=ref_name,
            key_id=f"{group_id}/{new_user}",
            phase="verified",
            retired_old=retired,
        )

    # --- flow ----------------------------------------------------------------

    def _run(self, ctx, admin_cred: str, old_uri: str) -> Tuple[str, bool]:
        ref_name = ctx["ref_name"]
        try:
            old_user, auth_source = parse_uri_user(old_uri)
        except ValueError as exc:
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: the stored value is not usable "
                f"({exc}) -- nothing was changed"
            ) from None
        auth_db = ctx["tags"].get("atlas_auth_db") or auth_source or "admin"
        ident, _, secret = admin_cred.strip().partition(":")
        if not ident or not secret:
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: the admin credential reference must "
                "hold '<id>:<secret>' (service account client_id:client_secret, or API key "
                "public_key:private_key) -- nothing was changed"
            )
        self._bearer = ""
        auth = (ctx["auth_type"], ident, secret)
        try:
            # --- 1. READ the current user --------------------------------------
            status, user_doc = self._api(auth, "GET", self._user_path(ctx, auth_db, old_user))
            if status != 200:
                self._audit_append(ctx, f"err:read-user-{status}")
                raise RotationAdapterError(
                    f"MongoDB Atlas rotation for {ref_name!r}: reading the database user failed: "
                    f"{self._describe(status, user_doc)} -- nothing was changed"
                )
            for field_name in ("x509Type", "awsIAMType", "ldapAuthType", "oidcAuthType"):
                value = str(user_doc.get(field_name, "NONE") or "NONE")
                if value != "NONE":
                    raise RotationAdapterError(
                        f"MongoDB Atlas rotation for {ref_name!r}: the user authenticates with "
                        f"{field_name}={value}, not a password -- nothing to rotate"
                    )
            if ctx["mode"] == "password":
                return self._rotate_password(ctx, auth, auth_db, old_user, old_uri), True
            return self._rotate_new_user(ctx, auth, auth_db, old_user, old_uri, user_doc)
        finally:
            self._bearer = ""
            del secret

    def _rotate_new_user(self, ctx, auth, auth_db, old_user, old_uri, user_doc):
        ref_name = ctx["ref_name"]
        new_user = next_username(old_user, int(self._clock()))
        password = self._password_factory()
        body = {k: user_doc[k] for k in _COPIED_FIELDS if user_doc.get(k)}
        body.update({"groupId": ctx["group_id"], "databaseName": auth_db,
                     "username": new_user, "password": password})
        if not body.get("roles"):
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: the current user has no roles to copy "
                "-- nothing was changed"
            )

        # --- 2. CREATE ----------------------------------------------------------
        status, resp = self._api(auth, "POST",
                                 f"/groups/{quote(ctx['group_id'], safe='')}/databaseUsers", body)
        if status not in (200, 201):
            self._audit_append(ctx, f"err:create-{status}")
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: creating the new database user "
                f"failed: {self._describe(status, resp)} -- nothing was changed"
            )
        new_uri = replace_credentials(old_uri, new_user, password)

        # --- 3. WAIT + 4. VERIFY; undo the new user on failure ------------------
        try:
            self._wait_applied(ctx, auth)
            reason = self._verify(ctx["connector"], new_uri)
        except RotationAdapterError as exc:
            reason = str(exc)
        if reason is not None:
            cleanup = self._delete_user(ctx, auth, auth_db, new_user)
            self._audit_append(ctx, "err:verify-failed")
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: the new user {new_user!r} could not "
                f"connect ({reason}); "
                + ("it was deleted again" if cleanup else
                   f"deleting it failed too -- remove {new_user!r} in Atlas (Database Access)")
                + ". The stored credential was not changed and is still valid."
            )

        # --- 5. STORE -----------------------------------------------------------
        try:
            self._store_all(ctx, new_uri, new_user, password)
        except Exception as exc:
            self._audit_append(ctx, "err:store-failed")
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: the new user {new_user!r} works but "
                f"storing it failed ({type(exc).__name__}). The old user was NOT retired, so the "
                "previously stored credential still works; delete the orphaned new user in Atlas "
                "or re-run."
            ) from None
        finally:
            del password, new_uri

        # --- 6. RETIRE (opt-in) -------------------------------------------------
        retired = False
        if ctx["retire_old"]:
            retired = self._delete_user(ctx, auth, auth_db, old_user)
            if not retired:
                self._audit_append(ctx, f"warn:retire-failed:new={new_user}")
                raise RotationAdapterError(
                    f"MongoDB Atlas rotation for {ref_name!r}: the new user {new_user!r} is "
                    f"stored and verified, but deleting the old user {old_user!r} failed -- "
                    "remove it in Atlas (Database Access)."
                )
        self._audit_append(ctx, f"ok:user={new_user}" + (f",retired={old_user}" if retired else ""))
        return new_user, retired

    def _rotate_password(self, ctx, auth, auth_db, user, old_uri) -> str:
        ref_name = ctx["ref_name"]
        password = self._password_factory()

        # --- 2. PATCH the password -----------------------------------------
        status, resp = self._api(auth, "PATCH", self._user_path(ctx, auth_db, user),
                                 {"password": password})
        if status != 200:
            self._audit_append(ctx, f"err:patch-{status}")
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: changing the password failed: "
                f"{self._describe(status, resp)} -- nothing was changed"
            )

        # --- 3. STORE first: the old password is going away ----------------
        new_uri = replace_credentials(old_uri, user, password)
        try:
            self._store_all(ctx, new_uri, user, password)
        except Exception as exc:
            self._audit_append(ctx, "err:store-failed-after-patch")
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: Atlas changed the password but storing "
                f"it failed ({type(exc).__name__}) -- the old password stops working once Atlas "
                "applies the change; set a new one in Atlas (Database Access -> Edit) and re-store."
            ) from None
        finally:
            del password

        # --- 4. WAIT + 5. VERIFY -------------------------------------------
        try:
            self._wait_applied(ctx, auth)
            reason = self._verify(ctx["connector"], new_uri)
        except RotationAdapterError as exc:
            reason = str(exc)
        finally:
            del new_uri
        if reason is not None:
            self._audit_append(ctx, "warn:verify-failed")
            raise RotationAdapterError(
                f"MongoDB Atlas rotation for {ref_name!r}: new password stored, but connecting "
                f"with it failed ({reason}) -- investigate before relying on it."
            )
        self._audit_append(ctx, f"ok:user={user}")
        return user

    # --- phases -----------------------------------------------------------------

    def _store_all(self, ctx, new_uri: str, user: str, password: str) -> None:
        local = ctx["local"]
        local.store(ctx["ref"].sm_name, new_uri)
        if ctx["companions"]["atlas_username_ref"]:
            local.store(ctx["companions"]["atlas_username_ref"], user)
        if ctx["companions"]["atlas_password_ref"]:
            local.store(ctx["companions"]["atlas_password_ref"], password)

    def _wait_applied(self, ctx, auth) -> None:
        cluster = ctx["tags"].get("atlas_cluster_name", "")
        if not cluster:
            return
        path = (f"/groups/{quote(ctx['group_id'], safe='')}/clusters/"
                f"{quote(cluster, safe='')}/status")
        deadline = time.monotonic() + self._apply_timeout
        while True:
            status, body = self._api(auth, "GET", path)
            change = str(body.get("changeStatus", "")) if status == 200 else ""
            if change == "APPLIED":
                return
            if status != 200 and status != 429 and status < 500:
                raise RotationAdapterError(
                    f"polling cluster status failed: {self._describe(status, body)}"
                )
            if time.monotonic() >= deadline:
                raise RotationAdapterError(
                    f"cluster changes still {change or 'unknown'!r} after "
                    f"{int(self._apply_timeout)}s"
                )
            self._sleep(3.0)

    def _verify(self, connector, uri: str) -> Optional[str]:
        """None when the URI connects, else a short reason -- never server text."""
        reason = ""
        for attempt in range(self._verify_attempts):
            if attempt:
                self._sleep(5.0)
            try:
                connector(uri, self._timeout)
                return None
            except MongoVerifyError as exc:
                reason = str(exc)
            except Exception as exc:  # a custom connector; class name only
                reason = type(exc).__name__
        return reason

    def _delete_user(self, ctx, auth, auth_db, user) -> bool:
        try:
            status, _ = self._api(auth, "DELETE", self._user_path(ctx, auth_db, user))
        except RotationAdapterError:
            return False
        return status in (200, 202, 204)

    # --- HTTP -------------------------------------------------------------------

    @staticmethod
    def _user_path(ctx, auth_db: str, user: str) -> str:
        return (f"/groups/{quote(ctx['group_id'], safe='')}/databaseUsers/"
                f"{quote(auth_db, safe='')}/{quote(user, safe='')}")

    def _api(self, auth, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, dict]:
        auth_type, ident, secret = auth
        headers = {"Accept": ATLAS_MEDIA_TYPE, "User-Agent": "portunus-rotation"}
        data = None
        if body is not None:
            headers["Content-Type"] = ATLAS_MEDIA_TYPE
            data = json.dumps(body).encode()
        digest = None
        if auth_type == "api-key":
            digest = (ident, secret)
        else:
            headers["Authorization"] = f"Bearer {self._token(ident, secret)}"
        for attempt in range(3):
            if attempt:
                self._sleep(2.0 * attempt)
            status, resp = self._transport(method, ATLAS_API + path, headers, data,
                                           self._timeout, digest)
            if status not in (429, 503):
                return status, resp
        return status, resp

    def _token(self, client_id: str, client_secret: str) -> str:
        if self._bearer:
            return self._bearer
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "portunus-rotation",
        }
        status, body = self._transport("POST", ATLAS_TOKEN_URL, headers,
                                       b"grant_type=client_credentials", self._timeout, None)
        token = str(body.get("access_token") or "") if status == 200 else ""
        if not token:
            raise RotationAdapterError(
                f"Atlas service-account token request failed (HTTP {status}) -- check the "
                "client id/secret in the atlas_admin_ref reference; nothing was changed"
            )
        self._bearer = token
        return token

    def _audit_append(self, ctx, result: str) -> None:
        if self._audit:
            self._audit.append("rotate:mongodb-atlas-user", ctx["ref"].sm_name, result)

    @staticmethod
    def _describe(status: int, resp: dict) -> str:
        # Atlas ApiError fields only, never the whole body.
        code = str(resp.get("errorCode", ""))[:80]
        detail = str(resp.get("detail", ""))[:200]
        return f"HTTP {status}" + (f" {code}: {detail}" if code or detail else "")
