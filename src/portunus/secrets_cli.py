"""``secrets`` — the local-first broker CLI (drop-in for dostal-swarm's bin/secrets).

Backed by the local encrypted vault (``LocalEncryptedBackend``): every value is
encrypted at rest under a local master key, and every read goes through the same
broker/lifecycle/audit chokepoints as the cloud tier.

Zero-leak rules (enforced here, tested in tests/test_secrets_cli.py):
  * no subcommand except ``get`` (the explicit, lifecycle-guarded human path)
    ever writes a secret value to stdout/stderr
  * ``inject``/``env`` write values only into a 0600 env file and print the
    *path*; ``exec`` puts values only into the child process environment
  * ``resolve`` substitutes ``{{secret:NAME}}`` only into an exec'd argv or a
    0600 temp file (path printed, never the value)
  * audit entries carry names/handles only — value absent by construction

Naming matches the swarm convention: ``secrets set <scope> <kind>`` stores
``dostal-<scope>-<kind>`` and registers the ``{{secret:<scope>-<kind>}}``
reference; ``inject``/``env``/``exec`` map kind -> env var(s) via the same
table as dostal-swarm's ``bin/secrets:env_names``.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__
from .audit import AuditChain
from .backend import BackendError
from .broker import ApprovalRequired, Broker, NotInjectable
from .localvault import LocalEncryptedBackend
from .paths import home
from .registry import Registry, Reference
from .resolver import Resolver, UnknownReference

SM_PREFIX = os.environ.get("PORTUNUS_SM_PREFIX", "dostal")
DEFAULT_TTL = int(os.environ.get("PORTUNUS_BUILD_TTL", "3600"))

# kind -> env var name(s); mirrors dostal-swarm bin/secrets:env_names exactly.
ENV_MAP: Dict[str, Tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "codex": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY",),
    "linear": ("LINEAR_API_KEY",),
    "slack": ("SLACK_BOT_TOKEN",),
    "github": ("GH_TOKEN", "GITHUB_TOKEN"),
}


def env_names(kind: str) -> List[str]:
    return list(ENV_MAP.get(kind, (kind.upper().replace("-", "_") + "_KEY",)))


def sm_name(scope: str, kind: str) -> str:
    return f"{SM_PREFIX}-{scope}-{kind}"


def ref_name(scope: str, kind: str) -> str:
    return f"{scope}-{kind}"


def _err(msg: str) -> int:
    print(f"secrets: {msg}", file=sys.stderr)
    return 1


class _SecretStore:
    """Small secrets-CLI adapter over LocalEncryptedBackend.

    The encrypted vault stores values only. Version counters and metadata live
    in a separate 0600 JSON sidecar because they are non-secret mount/status
    data and should stay queryable without decrypting values.
    """

    def __init__(self):
        self.backend = LocalEncryptedBackend()
        self.meta_path = home() / "secrets-meta.json"

    def _load_meta(self) -> Dict[str, Dict[str, object]]:
        if not self.meta_path.exists():
            return {}
        try:
            return json.loads(self.meta_path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return {}

    def _flush_meta(self, data: Dict[str, Dict[str, object]]) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.meta_path)
        os.chmod(self.meta_path, 0o600)

    def add_version(self, sm_name: str, value: str, meta: Optional[Dict[str, str]] = None) -> int:
        data = self._load_meta()
        current = data.get(sm_name, {})
        version = int(current.get("version", 0)) + 1
        self.backend.store(sm_name, value)
        stored_meta = dict(current.get("meta", {}))
        if meta:
            stored_meta.update({k: v for k, v in meta.items() if v})
        data[sm_name] = {"version": version, "meta": stored_meta}
        self._flush_meta(data)
        return version

    def access(self, sm_name: str) -> str:
        return self.backend.access(sm_name)

    def delete(self, sm_name: str) -> bool:
        removed = self.backend.remove(sm_name)
        data = self._load_meta()
        if sm_name in data:
            del data[sm_name]
            self._flush_meta(data)
        return removed

    def latest_version(self, sm_name: str) -> int:
        if sm_name not in self.backend._load():
            raise BackendError(f"unknown secret: {sm_name}")
        return int(self._load_meta().get(sm_name, {}).get("version", 1))

    def meta(self, sm_name: str) -> Dict[str, str]:
        meta = self._load_meta().get(sm_name, {}).get("meta", {})
        return dict(meta) if isinstance(meta, dict) else {}


class _Stack:
    def __init__(self):
        self.registry = Registry()
        self.audit = AuditChain()
        self.broker = Broker(self.registry, self.audit)
        self.vault = _SecretStore()
        self.resolver = Resolver(self.registry, self.vault, self.broker)


# exec seam — tests monkeypatch this to capture the environment instead of exec'ing.
def _exec(argv: Sequence[str], env: Dict[str, str]):  # pragma: no cover - replaced in tests
    os.execvpe(argv[0], list(argv), env)


# --- value input (never argv, never echoed) --------------------------------
def _read_value(args) -> str:
    if getattr(args, "file", None):
        data = Path(args.file).read_text()
    elif sys.stdin.isatty():
        data = getpass.getpass(f"value for {sm_name(args.scope, args.kind)} (input hidden): ")
    else:
        data = sys.stdin.read()
    value = data.rstrip("\n")
    if not value:
        raise ValueError("empty value — refusing to store")
    return value


def _store(stack: _Stack, args, state: str, action: str) -> int:
    try:
        value = _read_value(args)
    except (OSError, ValueError) as exc:
        return _err(str(exc))
    name = args.ref or ref_name(args.scope, args.kind)
    sm = sm_name(args.scope, args.kind)
    meta = {
        "description": getattr(args, "description", "") or "",
        "project": getattr(args, "project", "") or "",
        "environment": getattr(args, "env", "") or "",
    }
    try:
        version = stack.vault.add_version(sm, value, meta=meta)
    except BackendError as exc:
        return _err(str(exc))
    del value
    existing = stack.registry.get(name)
    stack.registry.add(
        name, sm, scope=args.scope, kind=args.kind, state=state,
        approval=existing.approval if existing else "",
    )
    stack.audit.append(action, sm, f"version={version} state={state}")
    print(f"{sm}: stored version {version} (encrypted at rest, state={state})")
    return 0


# --- reference collection for inject/env/exec -------------------------------
def _collect(registry: Registry, scope: str, keys: Optional[List[str]] = None) -> Dict[str, Reference]:
    """kind -> Reference for `scope`, with shared included and scope overriding."""
    chosen: Dict[str, Reference] = {}
    for ref in registry:
        if ref.scope == "shared":
            chosen.setdefault(ref.kind, ref)
    if scope != "shared":
        for ref in registry:
            if ref.scope == scope:
                chosen[ref.kind] = ref
    if keys:
        missing = [k for k in keys if k not in chosen]
        if missing:
            raise KeyError(
                f"unknown key: scope={scope} kind={','.join(missing)} "
                f"(secret not found: {', '.join(sm_name(scope, k) for k in missing)})"
            )
        chosen = {k: chosen[k] for k in keys}
    if not chosen:
        raise KeyError(f"no secrets registered for scope={scope}")
    return chosen


def _resolve_pairs(stack: _Stack, chosen: Dict[str, Reference]) -> Tuple[Dict[str, str], List[str]]:
    """Lifecycle-gate then decrypt each reference. Returns (env pairs, handles).

    The returned dict holds plaintext: callers must sink it ONLY into a 0600
    file or a child process environment, never stdout/stderr/logs.
    """
    pairs: Dict[str, str] = {}
    handles: List[str] = []
    for kind in sorted(chosen):
        ref = chosen[kind]
        stack.broker.check_injectable(ref.name)     # dropped/revoked fail closed
        value = stack.vault.access(ref.sm_name)     # decrypt at the boundary
        for env_var in env_names(kind):
            pairs[env_var] = value
        handles.append(f"portunus:{ref.scope}:{ref.kind}:{stack.vault.latest_version(ref.sm_name)}")
    return pairs, handles


def _write_env_file(path: Path, pairs: Dict[str, str], scope: str, ttl: int, handles: List[str]) -> None:
    lines = [f"{k}={v}" for k, v in sorted(pairs.items())]
    lines.append(f"PORTUNUS_SCOPE={scope}")
    lines.append(f"PORTUNUS_EXPIRES_AT={int(time.time()) + ttl}")
    lines.append(f"PORTUNUS_HANDLES={','.join(handles)}")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


# --- subcommands -------------------------------------------------------------
def cmd_set(args) -> int:
    return _store(_Stack(), args, state="enabled", action="set")


def cmd_drop(args) -> int:
    return _store(_Stack(), args, state="dropped", action="dropped")


def _transition(args, state: str) -> int:
    stack = _Stack()
    name = ref_name(args.scope, args.kind)
    try:
        ref = stack.registry.set_state(name, state)
    except KeyError:
        return _err(f"unknown secret: {sm_name(args.scope, args.kind)}")
    stack.audit.append(state, ref.sm_name, "ok")
    print(f"{ref.sm_name}: state={state}")
    return 0


def cmd_enable(args) -> int:
    return _transition(args, "enabled")


def cmd_lock(args) -> int:
    return _transition(args, "locked")


def cmd_revoke(args) -> int:
    return _transition(args, "revoked")


def cmd_get(args) -> int:
    stack = _Stack()
    name = ref_name(args.scope, args.kind)
    ref = stack.registry.get(name)
    if ref is None:
        return _err(f"unknown secret: {sm_name(args.scope, args.kind)}")
    state = ref.state or "enabled"
    if state != "enabled":
        stack.audit.append("get", ref.sm_name, f"denied-{state}")
        if state == "locked":
            return _err(f"{ref.sm_name} is locked; inject-only (state=locked)")
        return _err(f"{ref.sm_name} is {state} — plaintext read blocked")
    try:
        value = stack.vault.access(ref.sm_name)
    except BackendError as exc:
        return _err(str(exc))
    stack.audit.append("get", ref.sm_name, "ok")
    # The one explicit, lifecycle-guarded human read path.
    print(value)
    return 0


def cmd_rm(args) -> int:
    stack = _Stack()
    name = ref_name(args.scope, args.kind)
    sm = sm_name(args.scope, args.kind)
    removed_vault = stack.vault.delete(sm)
    removed_reg = stack.registry.remove(name)
    if not (removed_vault or removed_reg):
        return _err(f"unknown secret: {sm}")
    stack.audit.append("rm", sm, "removed")
    print(f"{sm}: removed (vault={'yes' if removed_vault else 'no'}, registry={'yes' if removed_reg else 'no'})")
    return 0


def cmd_list(args) -> int:
    stack = _Stack()
    rows = [r for r in stack.registry if not args.scope or r.scope == args.scope]
    if not rows:
        print("(no secrets registered)")
        return 0
    for ref in sorted(rows, key=lambda r: (r.scope, r.kind)):
        gate = "  [gated]" if ref.approval == "required" else ""
        print(f"  {ref.sm_name:<36} scope={ref.scope:<10} kind={ref.kind:<12} state={ref.state}{gate}")
    return 0


def cmd_discover(args) -> int:
    stack = _Stack()
    out = []
    for ref in stack.registry:
        meta = stack.vault.meta(ref.sm_name)
        if args.kind and ref.kind != args.kind:
            continue
        if args.project and meta.get("project") != args.project:
            continue
        if args.env and meta.get("environment") != args.env:
            continue
        out.append({
            "name": ref.name, "sm_name": ref.sm_name, "scope": ref.scope,
            "kind": ref.kind, "state": ref.state,
            "description": meta.get("description", ""),
            "project": meta.get("project", ""),
            "environment": meta.get("environment", ""),
        })
    if args.output == "json":
        print(json.dumps(out, indent=2))
    elif not out:
        print("(no matching secrets)")
    else:
        for row in sorted(out, key=lambda r: r["sm_name"]):
            extras = " ".join(f"{k}={row[k]}" for k in ("project", "environment") if row[k])
            desc = f'  "{row["description"]}"' if row["description"] else ""
            print(f"  {row['sm_name']:<36} kind={row['kind']:<12} state={row['state']} {extras}{desc}")
    return 0


def _do_inject(args, deprecated_env: bool = False) -> int:
    stack = _Stack()
    if deprecated_env:
        print("secrets: note: `env` is the legacy alias — prefer `secrets inject`", file=sys.stderr)
    keys = [k.strip() for k in (args.keys or "").split(",") if k.strip()] or None
    try:
        chosen = _collect(stack.registry, args.scope, keys)
        pairs, handles = _resolve_pairs(stack, chosen)
    except KeyError as exc:
        return _err(str(exc.args[0]))
    except (NotInjectable, ApprovalRequired, BackendError) as exc:
        return _err(str(exc))
    if args.out:
        path = Path(args.out)
    else:
        base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
        path = base / f"agent-{os.getpid()}.env"
    _write_env_file(path, pairs, args.scope, args.ttl, handles)
    pairs.clear()
    stack.audit.append("inject", f"scope={args.scope}", f"handles={','.join(handles)}")
    print(path)  # the path only — never a value
    return 0


def cmd_inject(args) -> int:
    return _do_inject(args)


def cmd_env(args) -> int:
    return _do_inject(args, deprecated_env=True)


def cmd_exec(args) -> int:
    if not args.cmd_argv:
        return _err("exec requires a command: secrets exec <scope> -- cmd args...")
    stack = _Stack()
    keys = [k.strip() for k in (args.keys or "").split(",") if k.strip()] or None
    try:
        chosen = _collect(stack.registry, args.scope, keys)
        pairs, handles = _resolve_pairs(stack, chosen)
    except KeyError as exc:
        return _err(str(exc.args[0]))
    except (NotInjectable, ApprovalRequired, BackendError) as exc:
        return _err(str(exc))
    stack.audit.append("exec", f"scope={args.scope}",
                       f"handles={','.join(handles)} argv0={args.cmd_argv[0]}")
    child_env = dict(os.environ)
    child_env.update(pairs)
    # Values flow ONLY into the child process environment (exec boundary).
    _exec(args.cmd_argv, child_env)
    return 0  # only reached when _exec is a test double


def cmd_resolve(args) -> int:
    stack = _Stack()
    try:
        if args.cmd_argv:
            stack.resolver.resolve_exec(args.cmd_argv)  # does not return on success
            return 0
        if args.file:
            text = Path(args.file).read_text()
        elif args.stdin:
            text = sys.stdin.read()
        else:
            text = args.text or ""
        path = stack.resolver.resolve_to_tempfile(text)
        print(path)  # path only, never the value
        return 0
    except UnknownReference as exc:
        return _err(f"unknown reference {{{{secret:{exc.args[0]}}}}}")
    except (NotInjectable, ApprovalRequired, BackendError) as exc:
        return _err(str(exc))


def cmd_handle(args) -> int:
    stack = _Stack()
    name = ref_name(args.scope, args.kind)
    ref = stack.registry.get(name)
    if ref is None:
        return _err(f"unknown secret: {sm_name(args.scope, args.kind)}")
    try:
        version = stack.vault.latest_version(ref.sm_name)
    except BackendError as exc:
        return _err(str(exc))
    print(f"portunus:{ref.scope}:{ref.kind}:{version}")
    return 0


def cmd_status(args) -> int:
    stack = _Stack()
    name = ref_name(args.scope, args.kind)
    ref = stack.registry.get(name)
    if ref is None:
        return _err(f"unknown secret: {sm_name(args.scope, args.kind)}")
    try:
        versions = stack.vault.latest_version(ref.sm_name)
    except BackendError:
        versions = 0
    meta = stack.vault.meta(ref.sm_name)
    print(f"secret:        {ref.sm_name}")
    print(f"reference:     {{{{secret:{ref.name}}}}}")
    print(f"state:         {ref.state}")
    print(f"versions:      {versions}")
    print(f"approval-gate: {'yes' if ref.approval == 'required' else 'no'}")
    print(f"env vars:      {','.join(env_names(ref.kind))}")
    for field in ("description", "project", "environment"):
        if meta.get(field):
            print(f"{field + ':':<14} {meta[field]}")
    return 0


def cmd_expire_check(args) -> int:
    path = Path(args.envfile)
    if not path.exists():
        print(f"secrets: no such env file: {path}", file=sys.stderr)
        return 2
    expires = None
    for line in path.read_text().splitlines():
        if line.startswith("PORTUNUS_EXPIRES_AT="):
            try:
                expires = int(line.split("=", 1)[1])
            except ValueError:
                expires = None
    if expires is None:
        print("secrets: env file has no PORTUNUS_EXPIRES_AT stamp", file=sys.stderr)
        return 2
    remaining = expires - int(time.time())
    if remaining < 0:
        print(f"secrets: env file expired {-remaining}s ago (epoch {expires})", file=sys.stderr)
        return 2
    print(f"env file valid ({remaining}s remaining)")
    return 0


def cmd_audit(args) -> int:
    audit = AuditChain()
    entries = audit.entries()[-args.n:]
    if args.output == "json":
        print(json.dumps(entries, indent=2))
        return 0
    print(f"{'seq':<4} {'actor':<14} {'action':<10} {'secret':<32} result")
    for e in entries:
        print(f"{e['seq']:<4} {e['actor'][:14]:<14} {e['action']:<10} "
              f"{e['secret'][:32]:<32} {e['result']}")
    return 0


def cmd_verify(args) -> int:
    audit = AuditChain()
    ok = audit.verify()
    print(f"audit chain: {'INTACT' if ok else 'BROKEN'} ({len(audit.entries())} entries)")
    return 0 if ok else 2


def cmd_mount(args) -> int:
    """The Pantheon mount contract: values-free data sources for a Vault tab."""
    contract = {
        "plugin": "portunus",
        "version": __version__,
        "tab": "Vault",
        "kind": "cli-json",
        "backend": "local-encrypted",
        "sources": {
            "references": {"argv": ["secrets", "discover", "--output", "json"],
                           "returns": "registered secrets: names/scope/kind/state/metadata"},
            "status": {"argv": ["secrets", "status", "<scope>", "<kind>"],
                       "returns": "lifecycle state, version count, env mapping"},
            "audit": {"argv": ["secrets", "audit", "--output", "json"],
                      "returns": "hash-chain access log entries"},
            "verify": {"argv": ["secrets", "verify"],
                       "returns": "audit chain integrity (exit 0 intact / 2 broken)"},
        },
        "guarantee": "every mount source is values-free by construction; "
                     "plaintext only ever flows to inject/exec/resolve boundaries",
    }
    print(json.dumps(contract, indent=2))
    return 0


# --- parser ------------------------------------------------------------------
def _add_scope_kind(p: argparse.ArgumentParser) -> None:
    p.add_argument("scope", help='"shared" or a client slug (e.g. att)')
    p.add_argument("kind", help="gemini | anthropic | linear | slack | github | ...")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="secrets",
        description="Local-first secret broker (Portunus local encrypted tier).",
    )
    p.add_argument("--version", action="version", version=f"secrets (portunus {__version__})")
    sub = p.add_subparsers(dest="cmd", required=True)

    for verb, func, help_text, state_note in (
        ("set", cmd_set, "store/rotate a secret (stdin, prompt, or --file; never argv)", "enabled"),
        ("drop", cmd_drop, "lifecycle-aware store: state=dropped until enabled", "dropped"),
    ):
        sp = sub.add_parser(verb, help=help_text)
        _add_scope_kind(sp)
        sp.add_argument("--file", help="read the value from a file")
        sp.add_argument("--ref", help="custom {{secret:NAME}} reference name")
        sp.add_argument("--description", default="")
        sp.add_argument("--project", default="")
        sp.add_argument("--env", default="", help="dev | prod | shared")
        sp.set_defaults(func=func)

    for verb, func in (("enable", cmd_enable), ("lock", cmd_lock), ("revoke", cmd_revoke)):
        sp = sub.add_parser(verb, help=f"set lifecycle state to {verb}d")
        _add_scope_kind(sp)
        sp.set_defaults(func=func)

    sp = sub.add_parser("get", help="read latest plaintext (blocked when locked/dropped/revoked)")
    _add_scope_kind(sp)
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("rm", help="remove a secret from the vault + registry")
    _add_scope_kind(sp)
    sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser("list", help="list registered secrets (names/states only)")
    sp.add_argument("scope", nargs="?", default="")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("discover", help="query the registry by intent (never values)")
    sp.add_argument("--kind", default="")
    sp.add_argument("--project", default="")
    sp.add_argument("--env", default="")
    sp.add_argument("--output", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_discover)

    for verb, func, help_text in (
        ("inject", cmd_inject, "write scope secrets to a 0600 env file; prints the PATH"),
        ("env", cmd_env, "legacy alias of inject"),
    ):
        sp = sub.add_parser(verb, help=help_text)
        sp.add_argument("scope")
        sp.add_argument("--keys", default="", help="comma-separated kinds to include")
        sp.add_argument("--out", default="", help="env file destination (default: $RUNNER_TEMP)")
        sp.add_argument("--ttl", type=int, default=DEFAULT_TTL)
        sp.set_defaults(func=func)

    sp = sub.add_parser("exec", help="run a command with secrets ONLY in its env: exec <scope> -- cmd ...")
    sp.add_argument("scope")
    sp.add_argument("--keys", default="")
    sp.set_defaults(func=cmd_exec)

    sp = sub.add_parser("resolve", help="resolve {{secret:NAME}} at the boundary (text or -- cmd)")
    sp.add_argument("text", nargs="?")
    sp.add_argument("--stdin", action="store_true")
    sp.add_argument("--file", default="")
    sp.set_defaults(func=cmd_resolve)

    sp = sub.add_parser("handle", help="print the opaque portunus:<scope>:<kind>:<version> handle")
    _add_scope_kind(sp)
    sp.set_defaults(func=cmd_handle)

    sp = sub.add_parser("status", help="show lifecycle state + versions + env mapping")
    _add_scope_kind(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("expire-check", help="check an injected env file's TTL stamp")
    sp.add_argument("envfile")
    sp.set_defaults(func=cmd_expire_check)

    sp = sub.add_parser("audit", help="view the tamper-evident access log")
    sp.add_argument("n", nargs="?", type=int, default=25)
    sp.add_argument("--output", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("verify", help="verify the audit hash chain")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("mount", help="print the Pantheon Vault-tab mount contract (JSON)")
    sp.set_defaults(func=cmd_mount)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd_argv: List[str] = []
    if "--" in argv:
        split = argv.index("--")
        cmd_argv = argv[split + 1:]
        argv = argv[:split]
    parser = build_parser()
    args = parser.parse_args(argv)
    args.cmd_argv = cmd_argv
    try:
        return args.func(args)
    except BackendError as exc:
        return _err(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
