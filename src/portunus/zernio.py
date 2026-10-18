"""Zernio rotation -- roll a Zernio credential and push it everywhere it is
used, in one ordered step (PANT-1146, GH #174; umbrella GH #141).

Two credentials, both told apart by `liveness.credential_kind` (the
`liveness_kind` tag, else the reference name's suffix):

``webhook-secret`` -- the HMAC-SHA256 signing secret of one Zernio webhook.
  Zernio never generates one: the secret is whatever the caller sets on
  ``PUT /v1/webhooks/settings``. Portunus generates it. Order:
  1. preflight  -- the admin API key can read the webhook; every target is
                   reachable (read-only, nothing written)
  2. stash      -- the current secret is copied to ``<sm_name>.previous`` in
                   the local vault (the rollback point)
  3. store      -- the new secret goes into the local vault
  4. push       -- every target env var gets the new secret; a Vercel
                   production target is redeployed and waited on (READY)
  5. switch     -- ``PUT /v1/webhooks/settings`` sets the new secret
  6. verify     -- ``POST /v1/webhooks/test``, then the delivery log must
                   show a 2xx that the receiver did not reject
  7. replay     -- deliveries the receivers rejected while the two sides
                   disagreed (from step 4 to step 5) are redelivered with
                   ``POST /v1/webhooks/logs/redeliver``
  8. retire     -- opt-in (`retire_old`): drop ``<sm_name>.previous``
  A receiver that answers a bad signature with 2xx (flayr's
  ``/api/webhooks/zernio`` does, so a bad signature never counts toward
  Zernio's auto-disable) is still caught: a delivery whose response body
  carries one of `REJECT_MARKERS` counts as rejected. `rollback` runs the
  same switch with the stashed previous secret.

``api-key`` -- a Zernio API key. Zernio has an API-key API
  (``/v1/api-keys``), so this is create -> verify -> store -> push ->
  retire: the new key copies the old key's scope/profiles/permission, is
  verified with a read-only ``GET /v1/profiles``, stored, pushed to every
  target (redeploying Vercel production), and only then -- opt-in -- the
  old key is DELETED (Zernio has no disable). A key that can't manage keys
  (``zrk_...``) gets the guided path: re-issue in the dashboard, store it,
  then `push`.

`push` writes the vault's current value to every target without minting
anything -- the "roll it everywhere" step on its own.

Targets come from the reference's ``zernio_targets`` tag (space-separated
specs, see envtargets.py) or the caller. The admin API key used for the
Zernio calls is the reference named by ``zernio_admin_ref`` (an api-key
reference rotates itself by default).

Secret-boundary invariant: admin credentials are only handled inside
``Resolver.resolve_call()``; the new value only ever reaches the local
vault, an outbound request body, or the boundary of a target writer. No
step, report, audit entry or error carries a value or a response body --
`PUT /webhooks/settings` and the delivery log return secrets and payloads,
so only named metadata fields are read from them.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from .envtargets import TargetError, TargetWriter, VercelEnvTarget, parse_targets
from .rotation import (
    RotationAdapterError,
    RotationResult,
    _add_superseded,
    _drop_superseded,
    _json_http,
    update_rotation_bindings,
)

ZERNIO_API = "https://zernio.com/api/v1"
KINDS = ("webhook-secret", "api-key")
TARGETS_TAG = "zernio_targets"
ADMIN_TAG = "zernio_admin_ref"
WEBHOOK_TAG = "zernio_webhook_id"
KEY_ID_TAG = "zernio_api_key_id"
PREVIOUS_SUFFIX = ".previous"
# Local-vault marker present while a webhook switch is in flight (from the
# stash until Zernio accepted the new secret). A rotation refuses to start
# over it: re-stashing would overwrite the only copy of the secret Zernio
# may still be signing with.
SWITCHING_SUFFIX = ".switching"
# Receiver response markers that mean "signature rejected" even on a 2xx.
REJECT_MARKERS = ("invalid_signature", "missing_signature", "Invalid webhook signature")
_SECRET_BYTES = 32
_LOG_PAGE = 100
_LOG_MAX_PAGES = 10
# Seconds before the flow started that still count for replay: absorbs clock
# skew between this host and Zernio's `createdAt`. Only events whose every
# delivery was rejected are replayed, so a wider window can't double-process.
_REPLAY_MARGIN = 60

ZernioTransport = Callable[[str, str, Dict[str, str], Optional[dict], float], Tuple[int, dict]]


class ZernioRotationError(RotationAdapterError):
    """A Zernio rotation step failed. Never carries a value."""


@dataclass(frozen=True)
class Step:
    """One step of a flow -- metadata only."""

    name: str
    detail: str


@dataclass(frozen=True)
class ZernioReport:
    """Outcome of a flow. Structurally incapable of carrying a value."""

    ref_name: str
    kind: str
    dry_run: bool
    steps: Tuple[Step, ...] = ()
    key_id: str = ""
    replayed: Tuple[str, ...] = ()
    retired_old: bool = False
    timestamp: int = field(default_factory=lambda: int(time.time()))


def _default_transport(method, url, headers, body, timeout):
    return _json_http(method, url, headers, body, timeout, "Zernio API")


def zernio_kind(ref) -> str:
    from .liveness import credential_kind
    return credential_kind(ref, KINDS)


def targets_for(ref, specs=None) -> List:
    """Targets from `specs` (CLI) or the reference's `zernio_targets` tag."""
    if specs:
        return parse_targets(specs)
    return parse_targets((getattr(ref, "tags", None) or {}).get(TARGETS_TAG, ""))


def _ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _rejected(log: dict) -> bool:
    code = log.get("statusCode")
    if not isinstance(code, int) or not 200 <= code < 300:
        return True
    body = log.get("responseBody")
    return isinstance(body, str) and any(m in body for m in REJECT_MARKERS)


class ZernioRotation:
    """Drives the Zernio flows. Every HTTP call goes through `transport`
    (Zernio) or `target_writer` (env targets), so tests never touch a real
    API. `confirm(question) -> bool` is asked when a target needs a manual
    redeploy; without it such a flow refuses before writing anything."""

    def __init__(self, resolver, registry=None, local_backend=None, audit=None,
                 transport: Optional[ZernioTransport] = None, target_writer=None,
                 confirm: Optional[Callable[[str], bool]] = None, redeploy: bool = True,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 30.0, verify_wait: float = 60.0, poll_interval: float = 5.0):
        self._resolver = resolver
        self._registry = registry if registry is not None else getattr(resolver, "registry", None)
        if local_backend is None:
            from .localvault import LocalEncryptedBackend
            local_backend = LocalEncryptedBackend()
        self._local = local_backend
        self._audit = audit
        self._transport = transport or _default_transport
        self._writer = target_writer or TargetWriter(resolver, registry=self._registry)
        self._confirm = confirm
        self._redeploy = redeploy
        self._clock = clock
        self._sleep = sleep
        self._timeout = timeout
        self._verify_wait = verify_wait
        self._poll = poll_interval

    # --- webhook secret ---------------------------------------------------------

    def rotate_webhook_secret(self, ref, targets, dry_run: bool = False,
                              retire_old: bool = False) -> ZernioReport:
        return self._switch_webhook(ref, targets, dry_run, retire_old, rollback=False)

    def rollback_webhook_secret(self, ref, targets, dry_run: bool = False) -> ZernioReport:
        """Put the stashed previous secret back everywhere (and stash the
        current one in its place, so a rollback can itself be undone)."""
        return self._switch_webhook(ref, targets, dry_run, False, rollback=True)

    def _switch_webhook(self, ref, targets, dry_run, retire_old, rollback) -> ZernioReport:
        name, sm_name, tags = ref.name, ref.sm_name, dict(ref.tags or {})
        self._require_kind(ref, "webhook-secret")
        webhook_id = tags.get(WEBHOOK_TAG, "")
        if not webhook_id:
            raise ZernioRotationError(f"{name!r} has no {WEBHOOK_TAG!r} tag -- the Zernio webhook "
                                      "whose secret this is")
        admin = self._admin_template(ref, self_ok=False)
        prev_name = sm_name + PREVIOUS_SUFFIX
        marker = sm_name + SWITCHING_SUFFIX
        if rollback and not self._local_has(prev_name):
            raise ZernioRotationError(f"no stashed previous secret for {name!r} in the local vault "
                                      "-- nothing to roll back to")
        if not rollback and self._local_has(marker):
            raise ZernioRotationError(
                f"an earlier switch of {name!r} did not finish -- Zernio may still sign with the "
                f"secret stashed in {prev_name}. Run `portunus rotate zernio --ref {name} --rollback` "
                "first; rotating again now would overwrite that stash")
        steps = [Step("preflight", self._describe_webhook(admin, webhook_id))]
        steps += self._preflight_targets(targets)
        steps += self._redeploy_plan(targets)
        if dry_run:
            return ZernioReport(name, "webhook-secret", True, tuple(steps))

        action = "rollback:zernio-webhook-secret" if rollback else "rotate:zernio-webhook-secret"
        start = self._clock()
        # 2-3. stash the current secret, store the new one.
        if rollback:
            new_value = self._local.access(prev_name)
        else:
            new_value = secrets.token_hex(_SECRET_BYTES)
        try:
            self._local.store(marker, webhook_id)
            self._stash_current(ref, prev_name)
            steps.append(Step("stash", f"current secret kept as {prev_name} (local vault)"))
            self._local.store(sm_name, new_value)
            steps.append(Step("store", f"{'previous' if rollback else 'new'} secret stored as "
                                       f"{sm_name} (local vault)"))
            # 4. push to every receiver, redeploying where needed.
            try:
                steps += self._push_value(new_value, targets, sm_name, action)
            except ZernioRotationError as exc:
                raise ZernioRotationError(
                    f"{exc} -- Zernio still signs with the old secret; run `portunus rotate zernio "
                    f"--ref {name} --rollback` to put it back on the targets written") from None
            # 5. switch Zernio over.
            status, resp = self._zernio(admin, "PUT", "/webhooks/settings",
                                        {"webhookId": webhook_id, "secret": new_value})
        finally:
            del new_value
        if status != 200:
            self._audit_append(action, sm_name, f"err:switch:{status}")
            raise ZernioRotationError(
                f"Zernio webhook update failed for {name!r} ({self._describe(status, resp)}) -- every "
                f"target already holds the new secret but Zernio still signs with {prev_name}; run "
                f"`portunus rotate zernio --ref {name} --rollback`")
        del resp
        self._local.remove(marker)
        steps.append(Step("switch", f"Zernio webhook {webhook_id} now signs with the new secret"))

        # 6. verify end to end.
        verdict = self._verify_delivery(admin, webhook_id)
        if verdict:
            self._audit_append(action, sm_name, "warn:verify-failed")
            raise ZernioRotationError(
                f"webhook {webhook_id} test delivery was not accepted after the switch ({verdict}); "
                f"the secret is switched everywhere Portunus writes. Check the receivers, or run "
                f"`portunus rotate zernio --ref {name} --rollback`")
        steps.append(Step("verify", f"test delivery to webhook {webhook_id} accepted"))

        # 7. replay what the receivers rejected while the two sides disagreed.
        replayed = self._replay_rejected(admin, webhook_id, since=start - _REPLAY_MARGIN)
        steps.append(Step("replay", f"{len(replayed)} rejected deliveries redelivered"
                          + (f": {', '.join(replayed)}" if replayed else "")))

        retired = False
        if retire_old:
            self._local.remove(prev_name)
            retired = True
            steps.append(Step("retire", f"{prev_name} removed from the local vault"))
        self._audit_append(action, sm_name, f"ok:webhook={webhook_id}:replayed={len(replayed)}")
        return ZernioReport(name, "webhook-secret", False, tuple(steps),
                            replayed=tuple(replayed), retired_old=retired)

    def _describe_webhook(self, admin: str, webhook_id: str) -> str:
        status, resp = self._zernio(admin, "GET", "/webhooks/settings", None)
        if status != 200:
            raise ZernioRotationError(f"Zernio webhook lookup failed ({self._describe(status, resp)})")
        hooks = resp.get("webhooks") or []
        del resp
        for hook in hooks:
            if isinstance(hook, dict) and hook.get("_id") == webhook_id:
                # Named metadata fields only: the record also carries `secret`.
                return (f"webhook {webhook_id} {hook.get('name', '')!r} -> {hook.get('url', '')} "
                        f"active={bool(hook.get('isActive'))} failures={hook.get('failureCount', 0)}")
        raise ZernioRotationError(f"Zernio has no webhook {webhook_id!r} for this API key")

    def _stash_current(self, ref, prev_name: str) -> None:
        """Copy the reference's current value to `prev_name` -- the value
        goes from the resolver boundary straight into the local vault."""
        if self._local_has(ref.sm_name):
            current = self._local.access(ref.sm_name)
            try:
                self._local.store(prev_name, current)
            finally:
                del current
            return
        self._resolver.resolve_call("{{secret:%s}}" % ref.name,
                                    lambda value: self._local.store(prev_name, value))

    def _verify_delivery(self, admin: str, webhook_id: str) -> str:
        """"" when the test delivery sent now was accepted, else why not. The
        delivery is told apart from earlier tests by its event id, not by
        time, so clock skew can't pass an old test off as this one."""
        before = {log.get("eventId") for log in
                  self._logs(admin, webhook_id, event="webhook.test", pages=1)}
        status, resp = self._zernio(admin, "POST", "/webhooks/test", {"webhookId": webhook_id})
        if status != 200 or resp.get("success") is not True:
            return f"test send {self._describe(status, resp)}"
        deadline = self._clock() + self._verify_wait
        while True:
            logs = self._logs(admin, webhook_id, event="webhook.test", pages=1)
            fresh = [log for log in logs if log.get("eventId") not in before]
            if fresh:
                if any(not _rejected(log) for log in fresh):
                    return ""
                return (f"receiver answered HTTP {fresh[0].get('statusCode')} or rejected the "
                        "signature")
            if self._clock() >= deadline:
                return f"no test delivery in the log after {int(self._verify_wait)}s"
            self._sleep(self._poll)

    def _replay_rejected(self, admin: str, webhook_id: str, since: float) -> List[str]:
        """Redeliver every non-test event delivered since `since` whose
        deliveries were all rejected. An event that was later accepted (a
        Zernio retry after the switch) is skipped."""
        by_event: Dict[str, bool] = {}
        for log in self._logs(admin, webhook_id):
            event_id = log.get("eventId")
            if not event_id or log.get("test") or _ts(log.get("createdAt")) < since - 1:
                continue
            by_event[event_id] = by_event.get(event_id, False) or not _rejected(log)
        replayed = []
        for event_id in sorted(e for e, accepted in by_event.items() if not accepted):
            status, resp = self._zernio(admin, "POST", "/webhooks/logs/redeliver",
                                        {"webhookId": webhook_id, "eventId": event_id})
            if status == 200:
                replayed.append(event_id)
            elif self._audit:
                self._audit.append("rotate:zernio-replay", event_id, f"warn:{status}")
            del resp
        return replayed

    def _logs(self, admin: str, webhook_id: str, event: str = "", pages: int = _LOG_MAX_PAGES) -> List[dict]:
        out: List[dict] = []
        for page in range(pages):
            query = {"webhookId": webhook_id, "limit": str(_LOG_PAGE), "skip": str(page * _LOG_PAGE)}
            if event:
                query["event"] = event
            status, resp = self._zernio(admin, "GET", "/webhooks/logs?" + urlencode(query), None)
            if status != 200:
                raise ZernioRotationError(f"Zernio delivery log read failed ({self._describe(status, resp)})")
            for log in resp.get("logs") or []:
                if isinstance(log, dict):
                    # Named fields only: the log also carries the request payload.
                    out.append({k: log.get(k) for k in
                                ("eventId", "event", "test", "statusCode", "responseBody", "createdAt")})
            more = (resp.get("pagination") or {}).get("hasMore")
            del resp
            if not more:
                break
        return out

    # --- API key -----------------------------------------------------------------

    def rotate_api_key(self, ref, targets, dry_run: bool = False,
                       retire_old: bool = False) -> ZernioReport:
        name, sm_name, tags = ref.name, ref.sm_name, dict(ref.tags or {})
        self._require_kind(ref, "api-key")
        old_id = tags.get(KEY_ID_TAG, "")
        if retire_old and not old_id:
            raise ZernioRotationError(f"--retire-old needs the {KEY_ID_TAG!r} tag on {name!r} -- the "
                                      "id of the key currently stored (see `GET /v1/api-keys`)")
        admin = self._admin_template(ref, self_ok=True)
        status, resp = self._zernio(admin, "GET", "/api-keys", None)
        if status != 200:
            raise ZernioRotationError(
                f"the admin key can't list Zernio API keys ({self._describe(status, resp)}). "
                "zrk_ keys can't manage keys: re-issue the key in the Zernio dashboard, store it "
                f"(`portunus drop {name} ...`), then `portunus rotate zernio --ref {name} --push-only`")
        keys = [k for k in resp.get("apiKeys") or [] if isinstance(k, dict)]
        del resp
        old = next((k for k in keys if k.get("id") == old_id), None) if old_id else None
        if old_id and old is None:
            raise ZernioRotationError(f"Zernio lists no API key with id {old_id!r} ({KEY_ID_TAG} on {name!r})")
        steps = [Step("preflight", f"admin key lists {len(keys)} Zernio API keys"
                      + (f"; old key {old_id} {old.get('name', '')!r} ({old.get('keyPreview', '')})"
                         if old else "; old key id unknown (no retire)"))]
        steps += self._preflight_targets(targets)
        steps += self._redeploy_plan(targets)
        if dry_run:
            return ZernioReport(name, "api-key", True, tuple(steps))

        action = "rotate:zernio-api-key"
        body = {"name": f"portunus-{name}-{int(self._clock())}"[:64]}
        if old:
            for k in ("scope", "permission", "disabledResourceGroups"):
                if old.get(k):
                    body[k] = old[k]
            ids = [p.get("_id") for p in old.get("profileIds") or [] if isinstance(p, dict)]
            if ids:
                body["profileIds"] = ids
        status, created = self._zernio(admin, "POST", "/api-keys", body)
        api_key = created.get("apiKey") if status in (200, 201) else None
        new_id = api_key.get("id", "") if isinstance(api_key, dict) else ""
        new_key = api_key.get("key", "") if isinstance(api_key, dict) else ""
        del api_key
        if not new_id or not new_key:
            raise ZernioRotationError(f"Zernio create key failed for {name!r}: {self._describe(status, created)}")
        del created
        try:
            if not self._verify_key(new_key):
                self._audit_append(action, sm_name, "warn:verify-failed")
                try:
                    self._zernio(admin, "DELETE", f"/api-keys/{quote(new_id, safe='')}", None)
                except Exception:
                    pass  # best-effort cleanup of the never-stored key
                raise ZernioRotationError(f"new Zernio key failed verification for {name!r} -- rotation "
                                          "aborted; old key left intact")
            self._local.store(sm_name, new_key)
            steps.append(Step("store", f"new key {new_id} stored as {sm_name} (local vault)"))
            self._record_key_id(ref, tags, new_id)
            if old_id:
                update_rotation_bindings(lambda b: _add_superseded(b, "zernio", old_id))
            try:
                steps += self._push_value(new_key, targets, sm_name, action)
            except ZernioRotationError as exc:
                raise ZernioRotationError(
                    f"{exc} -- the new key is stored and the old key is still valid; fix the target "
                    f"and run `portunus rotate zernio --ref {name} --push-only`") from None
        finally:
            del new_key

        retired = False
        if retire_old:
            status, resp = self._zernio(admin, "DELETE", f"/api-keys/{quote(old_id, safe='')}", None)
            if status not in (200, 204, 404):
                self._audit_append(action, sm_name, f"warn:retire-failed:new={new_id}")
                raise ZernioRotationError(
                    f"Zernio delete of old key {old_id} failed ({self._describe(status, resp)}) -- the new "
                    "key is stored and pushed; the old one stays listed as superseded")
            update_rotation_bindings(lambda b: _drop_superseded(b, "zernio", old_id))
            retired = True
            steps.append(Step("retire", f"old key {old_id} deleted"))
        self._audit_append(action, sm_name, f"ok:new={new_id}")
        return ZernioReport(name, "api-key", False, tuple(steps), key_id=new_id, retired_old=retired)

    def _verify_key(self, key: str) -> bool:
        try:
            status, resp = self._transport("GET", ZERNIO_API + "/profiles", self._headers(key), None,
                                           self._timeout)
        except RotationAdapterError:
            return False
        del resp
        # 401 = invalid/revoked; 403 = valid key whose `accounts` group is off.
        return status in (200, 403)

    def _record_key_id(self, ref, tags: dict, new_id: str) -> None:
        reg = self._registry
        if reg is None or not hasattr(reg, "retag") or ref.name not in reg:
            return
        reg.retag(ref.name, tags={**tags, KEY_ID_TAG: new_id})

    # --- push ----------------------------------------------------------------------

    def push(self, ref, targets, dry_run: bool = False) -> ZernioReport:
        """Write the local vault's current value to every target."""
        kind = zernio_kind(ref)
        if not self._local_has(ref.sm_name):
            raise ZernioRotationError(f"{ref.sm_name!r} is not in the local vault -- nothing to push")
        steps = self._preflight_targets(targets) + self._redeploy_plan(targets)
        if dry_run:
            return ZernioReport(ref.name, kind, True, tuple(steps))
        value = self._local.access(ref.sm_name)
        try:
            steps += self._push_value(value, targets, ref.sm_name, "push:zernio")
        finally:
            del value
        self._audit_append("push:zernio", ref.sm_name, f"ok:targets={len(targets)}")
        return ZernioReport(ref.name, kind, False, tuple(steps))

    # --- shared --------------------------------------------------------------------

    def _preflight_targets(self, targets) -> List[Step]:
        if not targets:
            raise ZernioRotationError(f"no targets -- set the {TARGETS_TAG!r} tag or pass --target "
                                      "(a value only in the vault is not rotated anywhere)")
        steps = []
        for target in targets:
            try:
                steps.append(Step("target", f"{target.label}: {self._writer.preflight(target)}"))
            except TargetError as exc:
                raise ZernioRotationError(f"preflight: {exc}") from None
        return steps

    def _manual_redeploys(self, targets) -> List[VercelEnvTarget]:
        return [t for t in targets if isinstance(t, VercelEnvTarget)
                and not (self._redeploy and t.auto_redeploy)]

    def _redeploy_plan(self, targets) -> List[Step]:
        manual = self._manual_redeploys(targets)
        steps = [Step("redeploy", f"{t.label}: " + ("needs a MANUAL redeploy (you'll be asked to "
                                                     "confirm)" if t in manual else
                                                     "redeployed by Portunus"))
                 for t in targets if isinstance(t, VercelEnvTarget)]
        if manual and self._confirm is None:
            raise ZernioRotationError(
                f"a Vercel target needs a manual redeploy ({', '.join(t.label for t in manual)}) "
                "and nobody can confirm it here -- run `portunus rotate zernio` interactively")
        return steps

    def _push_value(self, value: str, targets, sm_name: str, action: str) -> List[Step]:
        steps, written = [], []
        for target in targets:
            try:
                self._writer.write(target, value)
            except TargetError as exc:
                self._audit_append(action, sm_name, "err:push")
                raise ZernioRotationError(
                    f"push: {exc} (written so far: {', '.join(written) or 'none'})") from None
            written.append(target.label)
            steps.append(Step("push", f"{target.label}: written"))
        for target in targets:
            if not isinstance(target, VercelEnvTarget):
                continue
            if target in self._manual_redeploys(targets):
                if not self._confirm(f"redeploy {target.project} ({'+'.join(target.targets)}) on Vercel "
                                     "now and wait for it to be Ready -- done?"):
                    raise ZernioRotationError(f"redeploy of {target.label} not confirmed -- stopped "
                                              "before switching anything else")
                steps.append(Step("redeploy", f"{target.label}: confirmed by operator"))
                continue
            try:
                deployment = self._writer.redeploy(target)
            except TargetError as exc:
                self._audit_append(action, sm_name, "err:redeploy")
                raise ZernioRotationError(f"redeploy: {exc}") from None
            steps.append(Step("redeploy", f"{target.label}: {deployment} READY"))
        return steps

    def _require_kind(self, ref, kind: str) -> None:
        found = zernio_kind(ref)
        if found != kind:
            raise ZernioRotationError(
                f"{ref.name!r} is a {found or 'unrecognised'} credential, not a Zernio {kind} -- set "
                f"the 'liveness_kind' tag to one of: {', '.join(KINDS)}")

    def _admin_template(self, ref, self_ok: bool) -> str:
        admin = (ref.tags or {}).get(ADMIN_TAG, "")
        if not admin:
            if not self_ok:
                raise ZernioRotationError(f"{ref.name!r} has no {ADMIN_TAG!r} tag -- the reference "
                                          "holding the Zernio API key used to update the webhook")
            admin = ref.name
        return "{{secret:%s}}" % admin

    def _local_has(self, sm_name: str) -> bool:
        try:
            self._local.access(sm_name)
        except Exception:
            return False
        return True

    def _zernio(self, admin_template: str, method: str, path: str, body) -> Tuple[int, dict]:
        def boundary(key: str):
            return self._transport(method, ZERNIO_API + path, self._headers(key), body, self._timeout)

        return self._resolver.resolve_call(admin_template, boundary)

    @staticmethod
    def _headers(key: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "User-Agent": "portunus-rotation"}

    @staticmethod
    def _describe(status: int, resp: dict) -> str:
        # Only Zernio's error code -- never a body (which can carry a secret).
        code = str((resp or {}).get("code", ""))[:60]
        return f"HTTP {status}" + (f" {code}" if code else "")

    def _audit_append(self, action: str, sm_name: str, result: str) -> None:
        if self._audit:
            self._audit.append(action, sm_name, result)


class ZernioRotationAdapter:
    """`portunus rotation run` entry point for provider ``zernio``: runs the
    full flow for the reference's kind with its tagged targets,
    non-interactively (a target that needs a manual redeploy refuses)."""

    def __init__(self, transport: Optional[ZernioTransport] = None, local_backend=None, audit=None,
                 registry=None, target_writer=None):
        self._kw = dict(transport=transport, local_backend=local_backend, audit=audit,
                        registry=registry, target_writer=target_writer)

    def capability(self) -> str:
        return "auto"

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> RotationResult:
        if resolver is None:
            raise RotationAdapterError(f"rotating {ref.name!r} needs a resolver")
        flow = ZernioRotation(resolver, **self._kw)
        kind = zernio_kind(ref)
        try:
            targets = targets_for(ref)
        except ValueError as exc:
            raise RotationAdapterError(str(exc)) from None
        if kind == "webhook-secret":
            report = flow.rotate_webhook_secret(ref, targets, retire_old=retire_old)
        elif kind == "api-key":
            report = flow.rotate_api_key(ref, targets, retire_old=retire_old)
        else:
            raise RotationAdapterError(
                f"{ref.name!r} is not a recognised Zernio credential -- set the 'liveness_kind' "
                f"tag to one of: {', '.join(KINDS)}")
        return RotationResult(provider="zernio", ref_name=ref.name, key_id=report.key_id,
                              phase="pushed", retired_old=report.retired_old)
