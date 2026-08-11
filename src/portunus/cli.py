"""OSTIARIUS — the ``portunus`` engine tool agents call.

Registry management, policy (gate/approve/grant), the audit chain, and the
boundary-only resolver. No subcommand ever prints a secret value to stdout;
``resolve`` either execs a command with the value in argv, or writes a 0600
temp file and prints its *path*.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .audit import AuditChain
from .backend import BackendError, GcloudBackend, MockBackend
from .localvault import LocalEncryptedBackend
from .broker import ApprovalRequired, Broker, NotInjectable
from .registry import Registry
from .resolver import Resolver, UnknownReference


def _err(msg: str) -> int:
    print(f"portunus: {msg}", file=sys.stderr)
    return 1


def _build(project: str = ""):
    registry = Registry()
    audit = AuditChain()
    broker = Broker(registry, audit)
    backend_kind = os.environ.get("PORTUNUS_BACKEND", "local")
    if backend_kind == "mock":
        # For local dry-runs only; values come from PORTUNUS_MOCK_<SM_NAME>.
        values = {}
        for k, v in os.environ.items():
            if k.startswith("PORTUNUS_MOCK_"):
                values[k[len("PORTUNUS_MOCK_"):].lower().replace("_", "-")] = v
        backend = MockBackend(values)
    elif backend_kind == "gcloud":
        backend = GcloudBackend(project=project or os.environ.get("PORTUNUS_GCP_PROJECT", ""))
    else:
        # Stage 1 default: the local-encrypted ARCA tier. No plaintext ever
        # leaves this machine, let alone an LLM context.
        backend = LocalEncryptedBackend()
    return registry, audit, broker, Resolver(registry, backend, broker)


# --- subcommand handlers -------------------------------------------------
def cmd_reg(args) -> int:
    registry, *_ = _build()
    if args.action == "show":
        if not len(registry):
            print("(empty registry)")
            return 0
        for ref in registry:
            gate = "  [gated]" if ref.approval == "required" else ""
            print(f"  {{{{secret:{ref.name}}}}}  ->  {ref.sm_name}  "
                  f"(scope={ref.scope}, kind={ref.kind}, state={ref.state}){gate}")
        return 0
    if args.action == "add":
        ref = registry.add(args.name, args.sm_name, scope=args.scope,
                           kind=args.kind, project=args.project or "")
        print(f"registered {{{{secret:{ref.name}}}}} -> {ref.sm_name}")
        return 0
    if args.action == "rm":
        print("removed" if registry.remove(args.name) else "no such reference")
        return 0
    if args.action == "json":
        import json
        print(json.dumps({r.name: r.to_dict() for r in registry}, indent=2))
        return 0
    return _err(f"unknown reg action: {args.action}")


def cmd_drop(args) -> int:
    """Put a secret INTO Arca. Harness-side only: the value never enters the
    LLM chat, ~/.claude, or a provider — it comes from stdin or a local file
    the human/harness prepared out-of-band, never from an inline argv flag.

    Lands in state=dropped (fail-closed); `portunus state <name> enabled` is
    the separate, explicit step that makes it injectable.
    """
    registry, _, broker, resolver = _build()
    backend = resolver.backend
    if not hasattr(backend, "store"):
        return _err(
            "drop requires the local-encrypted backend "
            "(unset PORTUNUS_BACKEND or set it to unset/local)"
        )
    if args.stdin:
        value = sys.stdin.readline().rstrip("\n")
    else:
        try:
            value = Path(args.value_file).read_text().rstrip("\n")
        except OSError as exc:
            return _err(f"cannot read --value-file: {exc}")
    if not value:
        return _err("empty secret value; nothing dropped")
    ref = registry.add(
        args.name, args.sm_name, scope=args.scope, kind=args.kind, state="dropped",
    )
    backend.store(ref.sm_name, value)
    del value  # scrub our local reference promptly
    broker.audit.append("drop", ref.sm_name, "stored")
    print(
        f"dropped {{{{secret:{ref.name}}}}} -> {ref.sm_name} (state=dropped; "
        f"run `portunus state {ref.name} enabled` to allow injection)"
    )
    return 0


def cmd_resolve(args) -> int:
    _, _, _, resolver = _build()
    try:
        if args.exec_argv:
            resolver.resolve_exec(args.exec_argv)  # does not return on success
            return 0
        text = sys.stdin.read() if args.stdin else (args.text or "")
        path = resolver.resolve_to_tempfile(text)
        print(path)  # path only, never the value
        return 0
    except UnknownReference as exc:
        return _err(f"unknown reference {{{{secret:{exc.args[0]}}}}}")
    except (NotInjectable, ApprovalRequired) as exc:
        return _err(str(exc))
    except BackendError as exc:
        return _err(str(exc))


def cmd_gate(args) -> int:
    _, _, broker, _ = _build()
    try:
        ref = broker.gate(args.name, on=not args.off)
    except KeyError:
        return _err(f"unknown reference: {args.name}")
    print(f"gate {'OFF' if args.off else 'ON'}: {ref.sm_name}")
    return 0


def cmd_approve(args) -> int:
    _, _, broker, _ = _build()
    try:
        broker.approve(args.name, ttl=args.ttl)
    except KeyError:
        return _err(f"unknown reference: {args.name}")
    print(f"approved {args.name} for the next {args.ttl} access(es)")
    return 0


def cmd_grant(args) -> int:
    _, _, broker, _ = _build()
    try:
        ref = broker.grant(args.name, args.member)
    except KeyError:
        return _err(f"unknown reference: {args.name}")
    print(f"granted {args.member} -> {ref.sm_name} (audited)")
    return 0


def cmd_state(args) -> int:
    registry, *_ = _build()
    try:
        registry.set_state(args.name, args.state)
    except KeyError:
        return _err(f"unknown reference: {args.name}")
    print(f"{args.name}: state={args.state}")
    return 0


def cmd_status(args) -> int:
    registry, *_ = _build()
    ref = registry.get(args.name)
    if ref is None:
        return _err(f"unknown reference: {args.name}")
    print(f"reference:     {ref.name}")
    print(f"sm_name:       {ref.sm_name}")
    print(f"state:         {ref.state}")
    print(f"approval-gate: {'yes' if ref.approval == 'required' else 'no'}")
    return 0


def cmd_audit(args) -> int:
    audit = AuditChain()
    entries = audit.entries()[-args.n:]
    print(f"{'seq':<4} {'actor':<14} {'action':<10} {'secret':<28} result")
    for e in entries:
        print(f"{e['seq']:<4} {e['actor'][:14]:<14} {e['action']:<10} "
              f"{e['secret'][:28]:<28} {e['result']}")
    return 0


def cmd_verify(args) -> int:
    audit = AuditChain()
    ok = audit.verify()
    print(f"audit chain: {'INTACT' if ok else 'BROKEN'} ({len(audit.entries())} entries)")
    return 0 if ok else 2


def cmd_session(args) -> int:
    import json
    _, _, broker, _ = _build()
    
    if args.action == "save":
        # read session from stdin or file
        if args.value_file:
            val = Path(args.value_file).read_text()
        else:
            val = sys.stdin.read()
        try:
            session_obj = json.loads(val)
        except json.JSONDecodeError as exc:
            return _err(f"invalid JSON session: {exc}")
        
        try:
            res = broker.session_save(args.site, args.account, session_obj, ttl_seconds=args.ttl)
            print(json.dumps(res, sort_keys=True))
            return 0
        except Exception as exc:
            return _err(str(exc))
            
    elif args.action == "load":
        try:
            res = broker.session_load(args.site, args.account)
            print(json.dumps(res, sort_keys=True))
            return 0
        except Exception as exc:
            return _err(str(exc))
            
    elif args.action == "list":
        try:
            res = broker.session_list()
            print(json.dumps(res, sort_keys=True))
            return 0
        except Exception as exc:
            return _err(str(exc))
            
    elif args.action == "revoke":
        try:
            ok = broker.session_revoke(args.site, args.account)
            if not ok:
                return _err(f"session not found: {args.site} {args.account}")
            print(f"revoked session for {args.site} {args.account}")
            return 0
        except Exception as exc:
            return _err(str(exc))
            
    return _err(f"unknown action {args.action}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="portunus", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=f"portunus {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("reg", help="manage the reference registry")
    rs = r.add_subparsers(dest="action", required=True)
    rs.add_parser("show", help="list references")
    a = rs.add_parser("add", help="register a reference (name -> SM name)")
    a.add_argument("name")
    a.add_argument("sm_name")
    a.add_argument("--scope", default="")
    a.add_argument("--kind", default="")
    a.add_argument("--project", default="")
    rm = rs.add_parser("rm", help="remove a reference")
    rm.add_argument("name")
    rs.add_parser("json", help="dump the registry as JSON")
    r.set_defaults(func=cmd_reg)

    dr = sub.add_parser(
        "drop",
        help="put a secret INTO Arca (local-encrypted); lands state=dropped",
    )
    dr.add_argument("name", help="reference name, e.g. shared-anthropic")
    dr.add_argument("sm_name", help="vault key, e.g. dostal-shared-anthropic")
    dr.add_argument("--scope", default="")
    dr.add_argument("--kind", default="")
    src = dr.add_mutually_exclusive_group(required=True)
    src.add_argument("--stdin", action="store_true", help="read the value from stdin")
    src.add_argument("--value-file", help="read the value from this local file")
    dr.set_defaults(func=cmd_drop)

    rv = sub.add_parser("resolve", help="resolve {{secret:NAME}} at the boundary")
    rv.add_argument("text", nargs="?", help="template text (or use --stdin)")
    rv.add_argument("--stdin", action="store_true", help="read template from stdin")
    rv.add_argument("--exec", dest="exec_argv", nargs=argparse.REMAINDER,
                    help="resolve in argv and exec: --exec cmd args...")
    rv.set_defaults(func=cmd_resolve)

    g = sub.add_parser("gate", help="require approval before a reference resolves")
    g.add_argument("name")
    g.add_argument("--off", action="store_true", help="clear the gate")
    g.set_defaults(func=cmd_gate)

    ap = sub.add_parser("approve", help="grant a time-boxed approval for a gated reference")
    ap.add_argument("name")
    ap.add_argument("--ttl", type=int, default=3)
    ap.set_defaults(func=cmd_approve)

    gr = sub.add_parser("grant", help="record an audited access widening")
    gr.add_argument("name")
    gr.add_argument("member")
    gr.set_defaults(func=cmd_grant)

    st = sub.add_parser("state", help="set lifecycle state (enabled/locked/dropped/revoked)")
    st.add_argument("name")
    st.add_argument("state", choices=("enabled", "locked", "dropped", "revoked"))
    st.set_defaults(func=cmd_state)

    stat = sub.add_parser("status", help="show a reference's state + gate")
    stat.add_argument("name")
    stat.set_defaults(func=cmd_status)

    au = sub.add_parser("audit", help="view the tamper-evident access log")
    au.add_argument("n", nargs="?", type=int, default=25)
    au.set_defaults(func=cmd_audit)

    ve = sub.add_parser("verify", help="verify the audit hash chain")
    ve.set_defaults(func=cmd_verify)

    sess = sub.add_parser("session", help="manage browser/login sessions")
    sess_sub = sess.add_subparsers(dest="action", required=True)
    
    ss = sess_sub.add_parser("save", help="save a session")
    ss.add_argument("site")
    ss.add_argument("account")
    ss.add_argument("--ttl", type=int, default=86400, help="session TTL in seconds")
    ss_src = ss.add_mutually_exclusive_group(required=True)
    ss_src.add_argument("--stdin", action="store_true", help="read session JSON from stdin")
    ss_src.add_argument("--value-file", help="read session JSON from a file")
    
    sl = sess_sub.add_parser("load", help="load a session")
    sl.add_argument("site")
    sl.add_argument("account")
    
    sess_sub.add_parser("list", help="list saved sessions")
    
    sr = sess_sub.add_parser("revoke", help="revoke (remove) a session")
    sr.add_argument("site")
    sr.add_argument("account")
    
    sess.set_defaults(func=cmd_session)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
