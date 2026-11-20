"""ARCA -- Bitwarden personal/organization vault, via the official `bw` CLI.

Two halves with deliberately different blast radii (same split as
chrome_discovery vs. chrome_crypto):

* ``list_items`` / ``register_items`` -- **metadata-only discovery**. `bw list
  items` returns full item JSON *including* passwords, so every entry is
  projected down to a safe ``BitwardenItem`` (id, name, username, URIs,
  folder/org ids, custom-field *names*) the moment it is parsed; nothing
  value-bearing is kept, returned or printed.
* ``get_field`` -- the one value-returning call, used only by
  ``BitwardenBackend.access()`` at the resolver boundary.

Authentication: Portunus never holds the Bitwarden master password or API
key. The human unlocks their own vault (``bw login`` / ``bw unlock``) and
exports ``BW_SESSION``; ``bw`` reads it from the inherited environment, so the
session token never appears on argv. A locked/unauthenticated vault fails
closed with a remediation message.

Scope: this is the personal/org *password-manager* vault (``bw``). Bitwarden
**Secrets Manager** (``bws``, machine-account tokens) is a separate product and
not covered here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional, Tuple

SM_PREFIX = "bitwarden:"
DEFAULT_FIELD = "password"
# Standard login fields addressable as `bitwarden:<id>:<field>`. Anything else
# is looked up as a custom field name on the item.
LOGIN_FIELDS = ("password", "username", "totp")
LOCKED_REMEDIATION = (
    "run `bw login` (once) and `export BW_SESSION=$(bw unlock --raw)` in the shell "
    "that runs Portunus, then retry"
)

Runner = Callable[..., subprocess.CompletedProcess]


class BitwardenError(RuntimeError):
    """`bw` missing, vault locked, item not found, or malformed output."""


@dataclass
class BitwardenItem:
    """Metadata for one vault item -- structurally never a secret value."""

    id: str
    name: str
    type: str = "login"
    username: str = ""
    uris: List[str] = field(default_factory=list)
    folder_id: str = ""
    organization_id: str = ""
    custom_fields: List[str] = field(default_factory=list)  # names only
    revision_date: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_TYPE_NAMES = {1: "login", 2: "secure_note", 3: "card", 4: "identity", 5: "ssh_key"}


def _default_runner(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs)


def _run_bw(args: List[str], runner: Optional[Runner], timeout: int) -> str:
    if runner is None and shutil.which("bw") is None:
        raise BitwardenError(
            "bitwarden: `bw` CLI not found on PATH -- install it "
            "(https://bitwarden.com/help/cli/) and unlock your vault"
        )
    run = runner or _default_runner
    try:
        proc = run(
            ["bw", *args, "--nointeraction"],
            capture_output=True, text=True, timeout=timeout, env=dict(os.environ),
        )
    except subprocess.TimeoutExpired as exc:
        raise BitwardenError(f"bitwarden: `bw {args[0]}` timed out") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        low = err.lower()
        if "locked" in low or "not logged in" in low or "session" in low or "master password" in low:
            raise BitwardenError(f"bitwarden: vault is locked or not logged in -- {LOCKED_REMEDIATION}")
        if "not found" in low:
            raise BitwardenError("bitwarden: item not found")
        # stderr from `bw` never echoes a value, but cap it anyway.
        raise BitwardenError(f"bitwarden: `bw {args[0]}` failed: {err[:200]}")
    return proc.stdout or ""


def _project(entry: dict) -> Optional[BitwardenItem]:
    item_id = entry.get("id") or ""
    if not item_id:
        return None
    login = entry.get("login") or {}
    uris = [u.get("uri", "") for u in (login.get("uris") or []) if u.get("uri")]
    custom = [f.get("name", "") for f in (entry.get("fields") or []) if f.get("name")]
    return BitwardenItem(
        id=item_id,
        name=entry.get("name") or "",
        type=_TYPE_NAMES.get(entry.get("type"), str(entry.get("type", ""))),
        username=login.get("username") or "",
        uris=uris,
        folder_id=entry.get("folderId") or "",
        organization_id=entry.get("organizationId") or "",
        custom_fields=custom,
        revision_date=entry.get("revisionDate") or "",
    )


def status(runner: Optional[Runner] = None, timeout: int = 30) -> dict:
    """`bw status` -> {"status": "unlocked"|"locked"|"unauthenticated", ...}.
    Metadata only (server URL, user email, lock state)."""
    out = _run_bw(["status"], runner, timeout)
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise BitwardenError("bitwarden: `bw status` returned malformed JSON") from exc
    return {k: data.get(k, "") for k in ("status", "serverUrl", "userEmail", "lastSync")}


def list_items(search: str = "", runner: Optional[Runner] = None, timeout: int = 60) -> List[BitwardenItem]:
    """Enumerate vault items (optionally `bw list items --search`), metadata only."""
    args = ["list", "items"]
    if search:
        args += ["--search", search]
    out = _run_bw(args, runner, timeout)
    try:
        raw = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise BitwardenError("bitwarden: `bw list items` returned malformed JSON") from exc
    items: List[BitwardenItem] = []
    for entry in raw if isinstance(raw, list) else []:
        projected = _project(entry)
        if projected is not None:
            items.append(projected)
    del raw  # drop the value-bearing JSON as early as possible
    return items


def parse_sm_name(sm_name: str) -> Tuple[str, str]:
    """`bitwarden:<item-id>[:<field>]` -> (item_id, field). Field defaults to
    "password"; anything after the first colon past the id is the field name
    (custom field names may themselves contain colons)."""
    if not sm_name.startswith(SM_PREFIX):
        raise BitwardenError(
            f"bitwarden backend: sm_name must be 'bitwarden:<item-id>[:<field>]', got {sm_name!r}"
        )
    item_id, _, fld = sm_name[len(SM_PREFIX):].partition(":")
    if not item_id:
        raise BitwardenError(
            f"bitwarden backend: sm_name must be 'bitwarden:<item-id>[:<field>]', got {sm_name!r}"
        )
    return item_id, fld or DEFAULT_FIELD


def make_sm_name(item_id: str, fld: str = DEFAULT_FIELD) -> str:
    return f"{SM_PREFIX}{item_id}" if fld == DEFAULT_FIELD else f"{SM_PREFIX}{item_id}:{fld}"


def get_field(item_id: str, fld: str = DEFAULT_FIELD, runner: Optional[Runner] = None, timeout: int = 60) -> str:
    """Return ONE field's plaintext for one item. The only value-returning call
    in this module; callers outside the resolver boundary must not use it."""
    if fld in ("password", "username", "totp", "notes"):
        # `bw get <field> <id>` prints just that value (totp: the current code).
        value = _run_bw(["get", fld, item_id], runner, timeout)
        return value.rstrip("\n")
    out = _run_bw(["get", "item", item_id], runner, timeout)
    try:
        entry = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise BitwardenError("bitwarden: `bw get item` returned malformed JSON") from exc
    for f in entry.get("fields") or []:
        if f.get("name") == fld:
            return f.get("value") or ""
    raise BitwardenError(f"bitwarden: item {item_id!r} has no field named {fld!r}")


def derive_local_name(item: BitwardenItem) -> str:
    """`bw-<slugified item name>-<first 8 of id>` -- the id suffix keeps two
    items with the same display name (common: several "Instagram" logins)
    from colliding."""
    slug = "".join(c if c.isalnum() else "-" for c in item.name.lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"bw-{slug or 'item'}-{item.id[:8]}"


@dataclass
class BitwardenRegisterReport:
    registered: List[str] = field(default_factory=list)
    already_registered: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)


def register_items(registry, items: List[BitwardenItem]) -> BitwardenRegisterReport:
    """Write discovered items as `state=requested`, `backend=bitwarden`
    placeholders so they show up in `portunus search` / `portunus_search`.
    Only login items with a password are worth a reference; requested state
    fails closed (never injectable until a human enables it). Never
    overwrites a reference whose derived name collides with a different one.
    """
    report = BitwardenRegisterReport()
    for item in items:
        if item.type != "login":
            continue
        local_name = derive_local_name(item)
        sm_name = make_sm_name(item.id)
        existing = registry.get(local_name)
        if existing is not None:
            if existing.sm_name == sm_name:
                report.already_registered.append(local_name)
            else:
                report.conflicts.append(local_name)
            continue
        tags = {"source": "bitwarden"}
        if item.username:
            tags["username"] = item.username
        if item.uris:
            tags["uri"] = item.uris[0]
        registry.add(
            local_name, sm_name, state="requested", provider="bitwarden",
            backend="bitwarden", kind="password",
            description=f"Bitwarden login: {item.name}", tags=tags,
        )
        report.registered.append(local_name)
    return report
