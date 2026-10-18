"""Zernio rotation tests (PANT-1146): webhook signing secret switch, API key
rotation, push-only, rollback, the rotation-run adapter and the liveness
probe.

A stateful fake Zernio API and a fake target writer share one timeline, so
the tests can assert the ORDER of every write -- the property that keeps
webhooks from failing signature checks mid-rotation. Nothing contacts a
real API; every value is obviously fake."""
import ast
import inspect
import json

import pytest

from portunus import zernio as zernio_mod
from portunus.audit import AuditChain
from portunus.envtargets import ConvexEnvTarget, TargetError, VercelEnvTarget
from portunus.liveness import UNKNOWN, LIVE, DEAD, ZernioLivenessAdapter, checkable_references
from portunus.registry import Registry
from portunus.rotation import RotationAdapterError, load_rotation_bindings, rotation_adapter_for
from portunus.zernio import (
    ZernioRotation,
    ZernioRotationAdapter,
    ZernioRotationError,
    targets_for,
    zernio_kind,
)

ADMIN_KEY = "sk_FAKE_ADMIN_KEY_aaaaaaaaaaaaaaaaaaaaaaaa"
OLD_KEY = "sk_FAKE_OLD_KEY_bbbbbbbbbbbbbbbbbbbbbbbbbb"
NEW_KEY = "sk_FAKE_NEW_KEY_cccccccccccccccccccccccccc"
OLD_SECRET = "FAKE_OLD_WEBHOOK_SECRET_dddddddddddddddd"
WEBHOOK_ID = "6a73653a0ce2dee3dc80b152"
SECRET_REF = "social-engine-prod-zernio-webhook-secret"
KEY_REF = "social-engine-prod-zernio-api-key"

CONVEX = ConvexEnvTarget("convex-prod-deploy-key", "ZERNIO_WEBHOOK_SECRET")
VERCEL = VercelEnvTarget("vercel-token", "social-engine", "ZERNIO_WEBHOOK_SECRET", "firefly-events")


class _Ref:
    def __init__(self, name, tags, env="prod", provider="zernio"):
        self.name = name
        self.sm_name = name
        self.tags = dict(tags)
        self.env = env
        self.provider = provider
        self.project = ""
        self.state = "enabled"


class _Resolver:
    def __init__(self, values, registry=None):
        self.values = values
        self.registry = registry
        self.templates = []

    def resolve_call(self, template, boundary):
        self.templates.append(template)
        return boundary(self.values[template[len("{{secret:"):-2]])


class _Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def iso(self, offset=0.0):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.t + offset, timezone.utc).isoformat().replace("+00:00", "Z")


class _Receivers:
    """The receivers' view: which secret each one verifies with."""

    def __init__(self, clock, timeline, initial):
        self.clock = clock
        self.timeline = timeline
        self.secrets = {CONVEX.label: initial, VERCEL.label: initial}
        self.pending = {}   # vercel: written but not deployed yet

    def accepts(self, signed_with):
        return all(s == signed_with for s in self.secrets.values())


class _FakeWriter:
    def __init__(self, receivers, fail_on=None, redeploy_fails=False):
        self.r = receivers
        self.fail_on = fail_on
        self.redeploy_fails = redeploy_fails
        self.preflights = []

    def preflight(self, target):
        self.preflights.append(target.label)
        return "reachable; variable exists"

    def write(self, target, value):
        if target.label == self.fail_on:
            raise TargetError(f"{target.label}: env write -> HTTP 500")
        self.r.timeline.append(("write", target.label))
        if isinstance(target, VercelEnvTarget):
            self.r.pending[target.label] = value
        else:
            self.r.secrets[target.label] = value

    def redeploy(self, target):
        if self.redeploy_fails:
            raise TargetError(f"{target.label}: redeploy dpl_x ended ERROR")
        self.r.timeline.append(("redeploy", target.label))
        self.r.secrets[target.label] = self.r.pending.pop(target.label)
        return "dpl_new"


class _FakeZernio:
    """Enough of the Zernio API for the flows: webhook settings/test/logs/
    redeliver and api-keys/profiles."""

    def __init__(self, clock, receivers, timeline):
        self.clock = clock
        self.r = receivers
        self.timeline = timeline
        self.secret = OLD_SECRET
        self.logs = []
        self.keys = {"key_old": {"id": "key_old", "name": "prod", "keyPreview": "sk_FAKE...bbbb",
                                 "scope": "profiles", "permission": "read-write",
                                 "profileIds": [{"_id": "prof_1", "name": "P"}],
                                 "disabledResourceGroups": ["billing"]}}
        self.valid_keys = {ADMIN_KEY, OLD_KEY}
        self.calls = []
        self.put_status = 200
        self.test_status = 200
        self.create_status = 201
        self.verify_status = 200
        self.list_keys_status = 200
        self.redeliver_status = 200
        self._n = 0

    def deliver(self, event, test=False, event_id=None):
        self._n += 1
        ok = self.r.accepts(self.secret)
        self.logs.insert(0, {
            "eventId": event_id or f"evt_{self._n}", "event": event, "test": test,
            "statusCode": 200,
            "responseBody": '{"status":"ok"}' if ok else '{"status":"invalid","reason":"invalid_signature"}',
            "createdAt": self.clock.iso(), "requestPayload": {"secret_like": OLD_SECRET},
        })

    def __call__(self, method, url, headers, body, timeout):
        path = url.replace("https://zernio.com/api/v1", "")
        self.calls.append({"method": method, "path": path, "headers": dict(headers), "body": body})
        token = headers["Authorization"].split(" ", 1)[1]
        if token not in self.valid_keys:
            return 401, {"error": "Invalid, revoked or expired API key.", "code": "invalid_credentials"}
        if path == "/webhooks/settings" and method == "GET":
            return 200, {"webhooks": [{"_id": WEBHOOK_ID, "name": "flayr-prod-comments",
                                       "url": "https://flayr.social/api/webhooks/zernio",
                                       "secret": self.secret, "isActive": True, "failureCount": 0}]}
        if path == "/webhooks/settings" and method == "PUT":
            if self.put_status != 200:
                return self.put_status, {"code": "server_error"}
            self.timeline.append(("zernio-put", body["webhookId"]))
            self.secret = body["secret"]
            # An event fired right at the switch, before Zernio's own retry.
            return 200, {"success": True, "webhook": {"_id": WEBHOOK_ID, "secret": self.secret}}
        if path == "/webhooks/test":
            if self.test_status != 200:
                return self.test_status, {"success": False}
            self.deliver("webhook.test", test=True)
            return 200, {"success": True, "message": "Test webhook sent successfully"}
        if path.startswith("/webhooks/logs?"):
            from urllib.parse import parse_qs
            q = parse_qs(path.split("?", 1)[1])
            logs = [l for l in self.logs if not q.get("event") or l["event"] == q["event"][0]]
            skip, limit = int(q["skip"][0]), int(q["limit"][0])
            page = logs[skip:skip + limit]
            return 200, {"logs": page, "pagination": {"hasMore": skip + limit < len(logs)}}
        if path == "/webhooks/logs/redeliver":
            if self.redeliver_status == 200:
                self.timeline.append(("redeliver", body["eventId"]))
            return self.redeliver_status, {}
        if path == "/api-keys" and method == "GET":
            if self.list_keys_status != 200:
                return self.list_keys_status, {"code": "forbidden"}
            return 200, {"apiKeys": list(self.keys.values())}
        if path == "/api-keys" and method == "POST":
            if self.create_status != 201:
                return self.create_status, {"code": "validation_error"}
            self.keys["key_new"] = {"id": "key_new", **body}
            self.valid_keys.add(NEW_KEY)
            return 201, {"message": "created", "apiKey": {"id": "key_new", "key": NEW_KEY, **body}}
        if path.startswith("/api-keys/") and method == "DELETE":
            kid = path.rsplit("/", 1)[1]
            self.timeline.append(("delete-key", kid))
            self.keys.pop(kid, None)
            return 200, {}
        if path == "/profiles":
            return self.verify_status, {"profiles": []}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def world(home):
    from portunus.localvault import LocalEncryptedBackend
    clock = _Clock()
    timeline = []
    receivers = _Receivers(clock, timeline, OLD_SECRET)
    api = _FakeZernio(clock, receivers, timeline)
    local = LocalEncryptedBackend()
    local.store(SECRET_REF, OLD_SECRET)
    local.store(KEY_REF, OLD_KEY)
    secret_ref = _Ref(SECRET_REF, {"zernio_webhook_id": WEBHOOK_ID, "zernio_admin_ref": "zernio-admin",
                                   "liveness_kind": "webhook-secret"})
    key_ref = _Ref(KEY_REF, {"zernio_api_key_id": "key_old"})
    resolver = _Resolver({"zernio-admin": ADMIN_KEY, KEY_REF: OLD_KEY, SECRET_REF: OLD_SECRET})
    audit = AuditChain()
    writer = _FakeWriter(receivers)

    def flow(**kw):
        params = dict(local_backend=local, audit=audit, transport=api, target_writer=writer,
                      clock=clock, sleep=clock.sleep, registry=None)
        params.update(kw)
        return ZernioRotation(resolver, **params)

    return {"clock": clock, "timeline": timeline, "receivers": receivers, "api": api, "local": local,
            "secret_ref": secret_ref, "key_ref": key_ref, "resolver": resolver, "audit": audit,
            "writer": writer, "flow": flow}


# --- kind / targets ---------------------------------------------------------------

def test_kind_from_tag_or_name_suffix():
    assert zernio_kind(_Ref("x", {"liveness_kind": "api-key"})) == "api-key"
    assert zernio_kind(_Ref("social-engine-dev-zernio-webhook-secret", {})) == "webhook-secret"
    assert zernio_kind(_Ref("social-engine-zernio-api-key", {})) == "api-key"
    assert zernio_kind(_Ref("zernio-thing", {})) == ""


def test_targets_from_tag_and_cli_override():
    ref = _Ref("r", {"zernio_targets": f"{CONVEX.label},{VERCEL.label}"})
    assert targets_for(ref) == [CONVEX, VERCEL]
    assert targets_for(ref, [CONVEX.label]) == [CONVEX]


# --- webhook secret ------------------------------------------------------------------

def test_dry_run_is_read_only(world):
    report = world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL], dry_run=True)
    assert report.dry_run
    assert world["timeline"] == []
    assert [c["method"] for c in world["api"].calls] == ["GET"]
    assert world["local"].access(SECRET_REF) == OLD_SECRET
    assert world["writer"].preflights == [CONVEX.label, VERCEL.label]
    names = [s.name for s in report.steps]
    assert names == ["preflight", "target", "target", "redeploy"]
    assert "flayr-prod-comments" in report.steps[0].detail


def test_webhook_rotation_order_and_end_state(world):
    report = world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    # Receivers first (Vercel redeployed), Zernio last.
    assert world["timeline"] == [("write", CONVEX.label), ("write", VERCEL.label),
                                 ("redeploy", VERCEL.label), ("zernio-put", WEBHOOK_ID)]
    new = world["local"].access(SECRET_REF)
    assert new != OLD_SECRET and len(new) == 64
    assert world["api"].secret == new
    assert set(world["receivers"].secrets.values()) == {new}
    assert world["local"].access(SECRET_REF + ".previous") == OLD_SECRET
    assert [s.name for s in report.steps][-4:] == ["redeploy", "switch", "verify", "replay"]
    assert not report.retired_old


def test_webhook_rotation_replays_deliveries_rejected_mid_switch(world):
    api, clock, receivers = world["api"], world["clock"], world["receivers"]
    original_redeploy = world["writer"].redeploy

    def redeploy_with_traffic(target):
        # Convex already switched, Vercel not yet: this event is rejected.
        api.deliver("comment.received", event_id="evt_mid")
        # This one is rejected at first but a later retry succeeds -> no replay.
        api.deliver("comment.received", event_id="evt_retried")
        out = original_redeploy(target)
        return out

    world["writer"].redeploy = redeploy_with_traffic
    original_call = api.__call__

    class _Wrap:
        def __call__(self, method, url, headers, body, timeout):
            status, resp = original_call(method, url, headers, body, timeout)
            if method == "PUT":
                api.deliver("comment.received", event_id="evt_retried")
            return status, resp

    flow = world["flow"](transport=_Wrap())
    report = flow.rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    assert report.replayed == ("evt_mid",)
    assert ("redeliver", "evt_mid") in world["timeline"]
    assert ("redeliver", "evt_retried") not in world["timeline"]


def test_events_rejected_before_the_window_are_not_replayed(world):
    world["receivers"].secrets[CONVEX.label] = "something-else"
    world["api"].deliver("comment.received", event_id="evt_ancient")
    world["receivers"].secrets[CONVEX.label] = OLD_SECRET
    world["clock"].t += 3600
    report = world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    assert report.replayed == ()


def test_verify_failure_when_receiver_rejects_test_delivery(world):
    # Vercel's redeploy "succeeds" but the deployment still verifies with the old secret.
    world["writer"].redeploy = lambda target: "dpl_stale"
    with pytest.raises(ZernioRotationError) as exc:
        world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    assert "--rollback" in str(exc.value)
    assert "warn:verify-failed" in json.dumps(list(world["audit"].entries()))


def test_verify_ignores_an_older_accepted_test_delivery(world):
    world["api"].deliver("webhook.test", test=True, event_id="evt_old_test")  # accepted, pre-switch
    world["writer"].redeploy = lambda target: "dpl_stale"
    with pytest.raises(ZernioRotationError, match="rejected the signature"):
        world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])


def test_verify_times_out_without_a_logged_test(world):
    api = world["api"]
    real_deliver = api.deliver
    api.deliver = lambda *a, **k: None
    with pytest.raises(ZernioRotationError, match="no test delivery"):
        world["flow"](verify_wait=20).rotate_webhook_secret(world["secret_ref"], [CONVEX])
    api.deliver = real_deliver


def test_rollback_restores_previous_everywhere(world):
    world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    rotated = world["local"].access(SECRET_REF)
    world["flow"]().rollback_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    assert world["local"].access(SECRET_REF) == OLD_SECRET
    assert world["api"].secret == OLD_SECRET
    assert set(world["receivers"].secrets.values()) == {OLD_SECRET}
    # The rolled-back-from secret is now the stash, so the rollback can be undone.
    assert world["local"].access(SECRET_REF + ".previous") == rotated


def test_rollback_without_stash_refuses(world):
    with pytest.raises(ZernioRotationError, match="nothing to roll back"):
        world["flow"]().rollback_webhook_secret(world["secret_ref"], [CONVEX])


def test_retire_old_drops_the_stash(world):
    report = world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL], retire_old=True)
    assert report.retired_old
    with pytest.raises(Exception):
        world["local"].access(SECRET_REF + ".previous")


def test_push_failure_stops_before_zernio_and_names_rollback(world):
    world["writer"].fail_on = VERCEL.label
    with pytest.raises(ZernioRotationError) as exc:
        world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    msg = str(exc.value)
    assert "written so far: " + CONVEX.label in msg and "--rollback" in msg
    assert ("zernio-put", WEBHOOK_ID) not in world["timeline"]
    assert world["api"].secret == OLD_SECRET


def test_redeploy_failure_stops_before_zernio(world):
    world["writer"].redeploy_fails = True
    with pytest.raises(ZernioRotationError, match="redeploy"):
        world["flow"]().rotate_webhook_secret(world["secret_ref"], [VERCEL])
    assert world["api"].secret == OLD_SECRET


def test_zernio_put_failure_is_reported(world):
    world["api"].put_status = 500
    with pytest.raises(ZernioRotationError, match="webhook update failed"):
        world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX])


def test_manual_redeploy_needs_confirm(world):
    preview = VercelEnvTarget("vercel-token", "social-engine", "X", targets=("preview",))
    with pytest.raises(ZernioRotationError, match="manual redeploy"):
        world["flow"]().rotate_webhook_secret(world["secret_ref"], [preview], dry_run=True)
    with pytest.raises(ZernioRotationError, match="manual redeploy"):
        world["flow"](redeploy=False).rotate_webhook_secret(world["secret_ref"], [VERCEL], dry_run=True)


def test_manual_redeploy_confirmed_and_declined(world):
    asked = []

    def yes(q):
        asked.append(q)
        world["receivers"].secrets[VERCEL.label] = world["receivers"].pending.pop(VERCEL.label)
        return True

    world["flow"](redeploy=False, confirm=yes).rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    assert asked and "social-engine" in asked[0]
    with pytest.raises(ZernioRotationError, match="not confirmed"):
        world["flow"](redeploy=False, confirm=lambda q: False).rotate_webhook_secret(
            world["secret_ref"], [VERCEL])


@pytest.mark.parametrize("tags,match", [
    ({"zernio_admin_ref": "zernio-admin", "liveness_kind": "webhook-secret"}, "zernio_webhook_id"),
    ({"zernio_webhook_id": WEBHOOK_ID, "liveness_kind": "webhook-secret"}, "zernio_admin_ref"),
    ({"zernio_webhook_id": WEBHOOK_ID, "zernio_admin_ref": "zernio-admin", "liveness_kind": "api-key"},
     "not a Zernio webhook-secret"),
])
def test_webhook_refusals_before_any_call(world, tags, match):
    with pytest.raises(ZernioRotationError, match=match):
        world["flow"]().rotate_webhook_secret(_Ref(SECRET_REF, tags), [CONVEX])
    assert world["api"].calls == []


def test_no_targets_refuses(world):
    with pytest.raises(ZernioRotationError, match="no targets"):
        world["flow"]().rotate_webhook_secret(world["secret_ref"], [], dry_run=True)


def test_unknown_webhook_refuses(world):
    ref = _Ref(SECRET_REF, {**world["secret_ref"].tags, "zernio_webhook_id": "nope"})
    with pytest.raises(ZernioRotationError, match="no webhook 'nope'"):
        world["flow"]().rotate_webhook_secret(ref, [CONVEX], dry_run=True)


# --- API key -----------------------------------------------------------------------------

def test_api_key_rotation_order_and_settings_copied(world):
    report = world["flow"]().rotate_api_key(world["key_ref"], [CONVEX, VERCEL], retire_old=True)
    create = next(c for c in world["api"].calls if c["method"] == "POST" and c["path"] == "/api-keys")
    assert create["body"]["scope"] == "profiles" and create["body"]["profileIds"] == ["prof_1"]
    assert create["body"]["permission"] == "read-write"
    assert create["body"]["disabledResourceGroups"] == ["billing"]
    verify = next(c for c in world["api"].calls if c["path"] == "/profiles")
    assert verify["headers"]["Authorization"] == f"Bearer {NEW_KEY}"
    # Retire strictly after every target got the new key.
    assert world["timeline"][-1] == ("delete-key", "key_old")
    assert world["local"].access(KEY_REF) == NEW_KEY
    assert (report.key_id, report.retired_old) == ("key_new", True)


def test_api_key_default_keeps_old_key_and_records_superseded(world):
    world["flow"]().rotate_api_key(world["key_ref"], [CONVEX])
    assert "key_old" in world["api"].keys
    assert load_rotation_bindings()["zernio"].superseded_key_ids == ("key_old",)


def test_api_key_id_written_back_to_registry(home, world):
    reg = Registry()
    reg.add(KEY_REF, KEY_REF, provider="zernio", tags={"zernio_api_key_id": "key_old"})
    world["flow"](registry=reg).rotate_api_key(reg.get(KEY_REF), [CONVEX])
    assert Registry().get(KEY_REF).tags["zernio_api_key_id"] == "key_new"


def test_api_key_verify_failure_deletes_new_and_stores_nothing(world):
    world["api"].verify_status = 401
    with pytest.raises(ZernioRotationError, match="failed verification"):
        world["flow"]().rotate_api_key(world["key_ref"], [CONVEX])
    assert world["local"].access(KEY_REF) == OLD_KEY
    assert ("delete-key", "key_new") in world["timeline"]
    assert not any(t[0] == "write" for t in world["timeline"])


def test_api_key_verify_accepts_403_restricted_group(world):
    world["api"].verify_status = 403
    world["flow"]().rotate_api_key(world["key_ref"], [CONVEX])
    assert world["local"].access(KEY_REF) == NEW_KEY


def test_api_key_push_failure_never_retires(world):
    world["writer"].fail_on = VERCEL.label
    with pytest.raises(ZernioRotationError) as exc:
        world["flow"]().rotate_api_key(world["key_ref"], [CONVEX, VERCEL], retire_old=True)
    assert "--push-only" in str(exc.value)
    assert "key_old" in world["api"].keys
    assert world["local"].access(KEY_REF) == NEW_KEY


def test_api_key_zrk_admin_gets_guided_path(world):
    world["api"].list_keys_status = 403
    with pytest.raises(ZernioRotationError) as exc:
        world["flow"]().rotate_api_key(world["key_ref"], [CONVEX])
    assert "dashboard" in str(exc.value) and "--push-only" in str(exc.value)


def test_api_key_retire_needs_key_id(world):
    with pytest.raises(ZernioRotationError, match="zernio_api_key_id"):
        world["flow"]().rotate_api_key(_Ref(KEY_REF, {}), [CONVEX], retire_old=True)
    assert world["api"].calls == []


def test_api_key_unknown_old_id_refuses(world):
    with pytest.raises(ZernioRotationError, match="no API key with id"):
        world["flow"]().rotate_api_key(_Ref(KEY_REF, {"zernio_api_key_id": "gone"}), [CONVEX])


def test_api_key_create_failure(world):
    world["api"].create_status = 400
    with pytest.raises(ZernioRotationError, match="create key failed"):
        world["flow"]().rotate_api_key(world["key_ref"], [CONVEX])
    assert world["local"].access(KEY_REF) == OLD_KEY


# --- push-only ------------------------------------------------------------------------------

def test_push_only_writes_vault_value_without_minting(world):
    report = world["flow"]().push(world["key_ref"], [CONVEX, VERCEL])
    assert world["receivers"].secrets[CONVEX.label] == OLD_KEY
    assert world["api"].calls == []
    assert not report.dry_run


# --- rotation-run adapter ---------------------------------------------------------------------

def test_registered_for_rotation_run():
    adapter = rotation_adapter_for("zernio")
    assert isinstance(adapter, ZernioRotationAdapter) and adapter.capability() == "auto"


def test_adapter_runs_flow_with_tagged_targets(world):
    ref = _Ref(KEY_REF, {"zernio_api_key_id": "key_old", "zernio_targets": CONVEX.label})
    adapter = ZernioRotationAdapter(transport=world["api"], local_backend=world["local"],
                                    target_writer=world["writer"])
    result = adapter.rotate(ref, resolver=world["resolver"])
    assert (result.provider, result.key_id, result.phase) == ("zernio", "key_new", "pushed")


def test_adapter_rejects_unknown_kind_and_bad_spec(world):
    adapter = ZernioRotationAdapter(transport=world["api"], local_backend=world["local"],
                                    target_writer=world["writer"])
    with pytest.raises(RotationAdapterError, match="liveness_kind"):
        adapter.rotate(_Ref("zernio-thing", {"zernio_targets": CONVEX.label}), resolver=world["resolver"])
    with pytest.raises(RotationAdapterError):
        adapter.rotate(_Ref(KEY_REF, {"zernio_targets": "bogus:x"}), resolver=world["resolver"])


# --- liveness ----------------------------------------------------------------------------------

def test_liveness_probes_api_key_only():
    seen = []

    def transport(method, url, headers, timeout):
        seen.append((url, headers["Authorization"]))
        return 401

    adapter = ZernioLivenessAdapter(transport=transport)
    resolver = _Resolver({KEY_REF: OLD_KEY})
    assert adapter.check(_Ref(KEY_REF, {}), resolver).status == DEAD
    assert seen == [("https://zernio.com/api/v1/profiles", f"Bearer {OLD_KEY}")]
    result = adapter.check(_Ref(SECRET_REF, {}), resolver)
    assert result.status == UNKNOWN and "test delivery" in result.detail
    assert len(seen) == 1


@pytest.mark.parametrize("code,status", [(200, LIVE), (401, DEAD), (403, UNKNOWN), (429, UNKNOWN)])
def test_liveness_status_mapping(code, status):
    adapter = ZernioLivenessAdapter(transport=lambda *a: code)
    assert adapter.check(_Ref(KEY_REF, {}), _Resolver({KEY_REF: OLD_KEY})).status == status


def test_scheduled_sweep_includes_api_key_not_secret(home):
    reg = Registry()
    reg.add(KEY_REF, KEY_REF, provider="zernio")
    reg.add(SECRET_REF, SECRET_REF, provider="zernio")
    assert [r.name for r in checkable_references(reg)] == [KEY_REF]


# --- secret boundary -----------------------------------------------------------------------------

def _all_values(world):
    vals = {ADMIN_KEY, OLD_KEY, NEW_KEY, OLD_SECRET}
    try:
        vals.add(world["local"].access(SECRET_REF))
    except Exception:
        pass
    return vals


def test_no_value_in_reports_audit_or_errors(world, home):
    world["api"].deliver("comment.received", event_id="evt_x")
    reports = [world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL]),
               world["flow"]().rotate_api_key(world["key_ref"], [CONVEX], retire_old=True),
               world["flow"]().push(world["key_ref"], [CONVEX], dry_run=True)]
    blob = json.dumps([r.__dict__ for r in reports], default=lambda o: o.__dict__)
    blob += json.dumps(list(world["audit"].entries()))
    blob += (home / "rotation-bindings.json").read_text()
    errors = []
    for setup in (lambda w: setattr(w["api"], "put_status", 500),
                  lambda w: setattr(w["writer"], "fail_on", CONVEX.label),
                  lambda w: setattr(w["api"], "verify_status", 401)):
        world["api"].put_status, world["writer"].fail_on, world["api"].verify_status = 200, None, 200
        setup(world)
        for run in (lambda: world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX]),
                    lambda: world["flow"]().rotate_api_key(_Ref(KEY_REF, {}), [CONVEX])):
            try:
                run()
            except ZernioRotationError as exc:
                errors.append(str(exc))
    blob += " ".join(errors)
    assert errors
    for value in _all_values(world):
        assert value not in blob


def test_module_has_no_print_or_logging_sink():
    """Structural: zernio.py and envtargets.py never print or log -- every
    value leaves only through a vault store, a request body or a boundary."""
    from portunus import envtargets
    for mod in (zernio_mod, envtargets):
        tree = ast.parse(inspect.getsource(mod))
        names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "print" not in names
        assert not attrs & {"debug", "info", "warning", "error", "exception", "write_text"}
        imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        assert "logging" not in imported


def test_report_and_step_have_no_value_field():
    fields = set(zernio_mod.ZernioReport.__dataclass_fields__) | set(zernio_mod.Step.__dataclass_fields__)
    assert not fields & {"value", "secret", "key", "token"}


def test_failed_switch_blocks_a_second_rotation_but_not_rollback(world):
    world["api"].put_status = 500
    with pytest.raises(ZernioRotationError, match="--rollback"):
        world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    world["api"].put_status = 200
    # Re-rotating would stash the never-switched secret over the real old one.
    with pytest.raises(ZernioRotationError, match="did not finish"):
        world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL], dry_run=True)
    assert world["local"].access(SECRET_REF + ".previous") == OLD_SECRET
    world["flow"]().rollback_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    assert world["local"].access(SECRET_REF) == OLD_SECRET
    assert world["api"].secret == OLD_SECRET
    # The marker is gone, so a fresh rotation is allowed again.
    world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])


def test_completed_rotation_leaves_no_marker(world):
    world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    world["flow"]().rotate_webhook_secret(world["secret_ref"], [CONVEX, VERCEL])
    assert world["api"].secret == world["local"].access(SECRET_REF)
