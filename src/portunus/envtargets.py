"""Env-var targets -- the places a rotated value is pushed to (PANT-1146).

Rotation adapters (rotation.py) mint a new credential and store it in the
local vault. A credential that is *injected* somewhere -- a Vercel project
env var, a Convex deployment env var -- is only rotated once every one of
those copies holds the new value too. This module writes them.

Targets are named by a spec string, so they can live in a reference's tags
(``zernio_targets``, space-separated) or be passed on the command line:

- ``convex:<deploy-key-ref>:<VAR>`` -- env var VAR on the Convex deployment
  the deploy key ``<deploy-key-ref>`` authenticates against (the deployment
  is read from the key, or the ``convex_url`` tag on that reference for a
  preview/project key). Convex applies a change without a redeploy.
- ``vercel:<token-ref>:<project>[@<team>]:<VAR>[:<target>[+<target>...]]``
  -- env var VAR of a Vercel project, for the given targets (default
  ``production``). ``<team>`` is a team id (``team_...``) or slug. Vercel
  only applies an env change to new deployments, so a ``production``
  target is redeployed afterwards (``redeploy``); other targets are left to
  the operator.

Secret-boundary invariant: every admin credential (deploy key, Vercel
token) is only handled inside a ``Resolver.resolve_call()`` boundary, and
the value being pushed is only ever placed in an outbound request body.
Nothing here returns, logs or raises with a value or a response body --
error messages carry the HTTP status and the provider's error code only.
All HTTP goes through the injected ``transport`` seam.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

# Shared with the Convex liveness probe / parity fetcher (PANT-1151).
from .liveness import CONVEX_URL_TAG, convex_deployment_url  # noqa: F401 (re-exported)

# (method, url, headers, json_body_or_None, timeout) -> (status, parsed_json_or_{})
JSONTransport = Callable[[str, str, Dict[str, str], Optional[dict], float], Tuple[int, dict]]

VERCEL_API = "https://api.vercel.com"
VERCEL_TARGETS = ("production", "preview", "development")
_VERCEL_DONE = "READY"
_VERCEL_FAILED = ("ERROR", "CANCELED")


class TargetError(RuntimeError):
    """A target could not be read or written. Never carries a value."""


class TargetSpecError(ValueError):
    """A target spec string is malformed."""


@dataclass(frozen=True)
class ConvexEnvTarget:
    deploy_key_ref: str
    var: str

    @property
    def label(self) -> str:
        return f"convex:{self.deploy_key_ref}:{self.var}"


@dataclass(frozen=True)
class VercelEnvTarget:
    token_ref: str
    project: str
    var: str
    team: str = ""
    targets: Tuple[str, ...] = ("production",)

    @property
    def label(self) -> str:
        team = f"@{self.team}" if self.team else ""
        return f"vercel:{self.token_ref}:{self.project}{team}:{self.var}:{'+'.join(self.targets)}"

    @property
    def auto_redeploy(self) -> bool:
        """Only a production target is redeployed by Portunus."""
        return "production" in self.targets


def parse_target(spec: str):
    """Parse one target spec. Raises TargetSpecError on anything malformed,
    so a typo is reported before any value moves."""
    kind, _, rest = spec.strip().partition(":")
    parts = rest.split(":") if rest else []
    if kind == "convex":
        if len(parts) != 2 or not all(parts):
            raise TargetSpecError(f"{spec!r}: expected convex:<deploy-key-ref>:<VAR>")
        return ConvexEnvTarget(parts[0], parts[1])
    if kind == "vercel":
        if len(parts) not in (3, 4) or not all(parts[:3]):
            raise TargetSpecError(
                f"{spec!r}: expected vercel:<token-ref>:<project>[@<team>]:<VAR>[:<target>+...]")
        project, _, team = parts[1].partition("@")
        targets = tuple(parts[3].split("+")) if len(parts) == 4 and parts[3] else ("production",)
        bad = [t for t in targets if t not in VERCEL_TARGETS]
        if bad or not project:
            raise TargetSpecError(
                f"{spec!r}: Vercel targets must be among {', '.join(VERCEL_TARGETS)}")
        return VercelEnvTarget(parts[0], project, parts[2], team, targets)
    raise TargetSpecError(f"{spec!r}: unknown target kind {kind!r} (convex, vercel)")


def parse_targets(specs) -> List:
    """Parse a string of specs separated by spaces, `;` or `,` -- spaces in
    a tag, since `--tags k=v,...` splits on commas -- or an iterable."""
    if isinstance(specs, str):
        specs = re.split(r"[\s,;]+", specs)
    return [parse_target(s) for s in specs if s and s.strip()]


def _default_transport(method, url, headers, body, timeout):
    from .rotation import _json_http
    return _json_http(method, url, headers, body, timeout, "env target API")


def _error_code(resp: dict) -> str:
    # Only the provider's error code -- never a message or a body, which on
    # some endpoints can echo the submitted value.
    err = resp.get("error") if isinstance(resp, dict) else None
    code = err.get("code", "") if isinstance(err, dict) else (resp.get("code", "") if isinstance(resp, dict) else "")
    return str(code)[:60]


def _describe(status: int, resp: dict) -> str:
    code = _error_code(resp)
    return f"HTTP {status}" + (f" {code}" if code else "")


class TargetWriter:
    """Reads (presence only) and writes env-var targets, and redeploys a
    Vercel production target. `resolver` resolves the admin credentials;
    `registry` (default: resolver.registry) supplies the `convex_url` tag."""

    def __init__(self, resolver, registry=None, transport: Optional[JSONTransport] = None,
                 timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, redeploy_timeout: float = 900.0,
                 poll_interval: float = 10.0):
        self._resolver = resolver
        self._registry = registry if registry is not None else getattr(resolver, "registry", None)
        self._transport = transport or _default_transport
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._redeploy_timeout = redeploy_timeout
        self._poll = poll_interval

    # --- public API ----------------------------------------------------------

    def preflight(self, target) -> str:
        """Read-only check that the target is reachable with its admin
        credential. Returns a metadata-only description; raises TargetError."""
        if isinstance(target, ConvexEnvTarget):
            present = self._convex_call(target, "GET", "/api/v1/list_environment_variables", None,
                                        lambda status, resp: self._convex_has(target, status, resp))
            return "reachable; " + ("variable exists" if present else "variable will be created")
        envs = self._vercel_envs(target)
        match = self._vercel_match(target, envs)
        if match is None:
            return "reachable; variable will be created"
        return f"reachable; variable exists (targets: {', '.join(_env_targets(match))})"

    def write(self, target, value: str) -> None:
        """Write `value` to the target. The value only ever goes into the
        outbound request body."""
        if isinstance(target, ConvexEnvTarget):
            body = {"changes": [{"name": target.var, "value": value}]}
            del value

            def check(status, resp):
                if status != 200:
                    raise TargetError(f"{target.label}: update_environment_variables -> "
                                      f"{_describe(status, resp)}")

            self._convex_call(target, "POST", "/api/v1/update_environment_variables", body, check)
            return
        match = self._vercel_match(target, self._vercel_envs(target))
        if match is not None:
            path = f"/v9/projects/{quote(target.project, safe='')}/env/{quote(str(match['id']), safe='')}"
            status, resp = self._vercel_call(target, "PATCH", path, {"value": value})
            ok = status == 200
        else:
            path = f"/v10/projects/{quote(target.project, safe='')}/env"
            body = {"key": target.var, "value": value, "type": "encrypted",
                    "target": list(target.targets)}
            status, resp = self._vercel_call(target, "POST", path, body, {"upsert": "true"})
            failed = resp.get("failed") if isinstance(resp, dict) else None
            ok = status in (200, 201) and not failed
            if failed and isinstance(failed, list) and isinstance(failed[0], dict):
                resp = failed[0]
        del value
        if not ok:
            raise TargetError(f"{target.label}: env write -> {_describe(status, resp)}")

    def redeploy(self, target: VercelEnvTarget) -> str:
        """Redeploy the project's current production deployment so it picks
        up the new env value, and wait until it is READY. Returns the new
        deployment id."""
        p = quote(target.project, safe="")
        status, project = self._vercel_call(target, "GET", f"/v9/projects/{p}", None)
        project_id = project.get("id", "") if status == 200 else ""
        if not project_id:
            raise TargetError(f"{target.label}: project lookup -> {_describe(status, project)}")
        status, listing = self._vercel_call(
            target, "GET", "/v7/deployments", None,
            {"projectId": project_id, "target": "production", "state": "READY", "limit": "1"})
        deployments = listing.get("deployments") if status == 200 else None
        if not deployments:
            raise TargetError(f"{target.label}: no READY production deployment to redeploy "
                              f"({_describe(status, listing)})")
        current = deployments[0].get("uid", "")
        status, created = self._vercel_call(
            target, "POST", "/v13/deployments",
            {"name": target.project, "deploymentId": current, "target": "production"},
            {"forceNew": "1"})
        new_id = created.get("id", "") if status in (200, 201) else ""
        if not new_id:
            raise TargetError(f"{target.label}: redeploy -> {_describe(status, created)}")
        deadline = self._clock() + self._redeploy_timeout
        while True:
            status, dep = self._vercel_call(target, "GET", f"/v13/deployments/{quote(new_id, safe='')}", None)
            state = dep.get("readyState", "") if status == 200 else ""
            if state == _VERCEL_DONE:
                return new_id
            if state in _VERCEL_FAILED:
                raise TargetError(f"{target.label}: redeploy {new_id} ended {state}")
            if self._clock() >= deadline:
                raise TargetError(f"{target.label}: redeploy {new_id} not READY after "
                                  f"{int(self._redeploy_timeout)}s (last state {state or status})")
            self._sleep(self._poll)

    # --- Convex ---------------------------------------------------------------

    def _convex_call(self, target: ConvexEnvTarget, method: str, path: str, body, handle):
        ref = self._registry.get(target.deploy_key_ref) if self._registry is not None else None
        if ref is None:
            raise TargetError(f"{target.label}: no deploy-key reference "
                              f"{target.deploy_key_ref!r} in registry")
        override = (getattr(ref, "tags", None) or {}).get(CONVEX_URL_TAG, "")

        def boundary(key: str):
            base = convex_deployment_url(key, override)
            if not base:
                raise TargetError(f"{target.label}: deploy key names no deployment and no "
                                  f"{CONVEX_URL_TAG!r} tag is set")
            status, resp = self._transport(method, base + path,
                                           {"Authorization": f"Convex {key.strip()}",
                                            "Content-Type": "application/json"},
                                           body, self._timeout)
            try:
                return handle(status, resp)
            finally:
                del resp

        return self._resolver.resolve_call("{{secret:%s}}" % target.deploy_key_ref, boundary)

    @staticmethod
    def _convex_has(target: ConvexEnvTarget, status: int, resp: dict) -> bool:
        if status != 200:
            raise TargetError(f"{target.label}: list_environment_variables -> {_describe(status, resp)}")
        env = resp.get("environmentVariables")
        if not isinstance(env, dict):
            raise TargetError(f"{target.label}: unexpected list_environment_variables response")
        return target.var in env

    # --- Vercel ---------------------------------------------------------------

    def _vercel_call(self, target: VercelEnvTarget, method: str, path: str, body,
                     query: Optional[Dict[str, str]] = None) -> Tuple[int, dict]:
        params = dict(query or {})
        if target.team:
            params["teamId" if target.team.startswith("team_") else "slug"] = target.team
        url = VERCEL_API + path + (f"?{urlencode(params)}" if params else "")

        def boundary(token: str):
            return self._transport(method, url, {"Authorization": f"Bearer {token}",
                                                 "Content-Type": "application/json"},
                                   body, self._timeout)

        return self._resolver.resolve_call("{{secret:%s}}" % target.token_ref, boundary)

    def _vercel_envs(self, target: VercelEnvTarget) -> List[dict]:
        status, resp = self._vercel_call(
            target, "GET", f"/v10/projects/{quote(target.project, safe='')}/env", None)
        if status != 200:
            raise TargetError(f"{target.label}: list env -> {_describe(status, resp)}")
        envs = resp.get("envs")
        if envs is None and "key" in resp:
            envs = [resp]
        # Keep only the metadata the match needs; drop any (decrypted) value.
        return [{"id": e.get("id"), "key": e.get("key"), "target": e.get("target")}
                for e in (envs or []) if isinstance(e, dict)]

    @staticmethod
    def _vercel_match(target: VercelEnvTarget, envs: List[dict]) -> Optional[dict]:
        """The existing variable that already covers every requested target
        (PATCHing it changes all of its targets). None: create via upsert."""
        want = set(target.targets)
        for env in envs:
            if env.get("key") == target.var and env.get("id") and want <= set(_env_targets(env)):
                return env
        return None


def _env_targets(env: dict) -> List[str]:
    t = env.get("target") or []
    return [t] if isinstance(t, str) else [str(x) for x in t]
