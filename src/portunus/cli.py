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
from .adapters import AdapterError, EnvVarAdapter, FileAdapter
from .intent import AmbiguousIntent, classify_intent_kind, parse_intent
from .registry import AmbiguousMatch, NoMatch, Registry
from .resolver import Resolver, UnknownReference

# Distinct exit codes so scripts can branch on the failure mode without
# parsing stderr text. 1 is the pre-existing generic-error code (_err()).
EXIT_NO_MATCH = 3
EXIT_AMBIGUOUS = 4


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


def _parse_tags(raw: str) -> dict:
    """Parse "k=v,k2=v2" into {"k": "v", "k2": "v2"}."""
    tags = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"invalid --tags entry (want k=v): {pair!r}")
        k, v = pair.split("=", 1)
        tags[k.strip()] = v.strip()
    return tags


def cmd_find(args) -> int:
    """Find a reference by tags. Metadata-only -- never builds a Resolver or
    touches a backend, since no value is ever fetched here."""
    try:
        partial_tags = _parse_tags(args.tags)
    except ValueError as exc:
        return _err(str(exc))
    registry = Registry()
    try:
        ref = registry.resolve_by_tags(**partial_tags)
    except AmbiguousMatch as exc:
        _err(f"ambiguous match for {partial_tags!r}: {', '.join(exc.candidates)}")
        return EXIT_AMBIGUOUS
    except NoMatch:
        _err(f"no reference matches tags: {partial_tags!r}")
        return EXIT_NO_MATCH
    print(f"  {{{{secret:{ref.name}}}}}  ->  {ref.sm_name}  "
          f"(provider={ref.provider}, project={ref.project}, env={ref.env}, "
          f"scope={ref.scope}, kind={ref.kind}, tags={ref.tags}, state={ref.state})")
    return 0


def _adapter_from_args(args):
    """Build (adapter, adapter_kwargs, target_desc) from --target/--var/--path/
    --format/--key. Adapters validate their own params (empty var_name/path/
    key) and raise AdapterError -- no redundant pre-check here."""
    if args.target == "env":
        return EnvVarAdapter(), {"var_name": args.var}, f"env:{args.var}"
    return FileAdapter(), {"path": args.path, "fmt": args.format, "key": args.key}, f"file:{args.path}"


def _inject_resolved_ref(resolver, broker, ref, args, audit_action: str) -> int:
    """Shared boundary-injection dispatch for cmd_inject and cmd_ask: resolve
    an adapter from args, inject via Resolver.resolve_call's boundary-callable
    sink (value never returned/printed/logged), and write one audit_action
    entry (ref, target descriptor -- never the value) on success or failure."""
    adapter, adapter_kwargs, target_desc = _adapter_from_args(args)
    template = f"{{{{secret:{ref.name}}}}}"
    try:
        resolver.resolve_call(template, boundary=lambda v: adapter.inject(v, **adapter_kwargs))
    except (UnknownReference, NotInjectable, ApprovalRequired, BackendError, AdapterError) as exc:
        broker.audit.append(audit_action, ref.sm_name, f"error:{target_desc}")
        return _err(str(exc))
    broker.audit.append(audit_action, ref.sm_name, f"ok:{target_desc}")
    print(f"injected {{{{secret:{ref.name}}}}} -> {target_desc}")
    return 0


def cmd_inject(args) -> int:
    """Resolve a reference by tags and inject its value at a boundary target.

    The value only ever flows resolver -> adapter -> target via
    Resolver.resolve_call's boundary-callable sink -- it is never returned,
    printed, or logged here, on either the success or failure path.
    """
    try:
        partial_tags = _parse_tags(args.tags)
    except ValueError as exc:
        return _err(str(exc))

    registry, _audit, broker, resolver = _build()
    try:
        ref = registry.resolve_by_tags(**partial_tags)
    except AmbiguousMatch as exc:
        _err(f"ambiguous match for {partial_tags!r}: {', '.join(exc.candidates)}")
        return EXIT_AMBIGUOUS
    except NoMatch:
        _err(f"no reference matches tags: {partial_tags!r}")
        return EXIT_NO_MATCH

    return _inject_resolved_ref(resolver, broker, ref, args, "adapter_resolution")


def _cmd_ask_add(args, registry, broker) -> int:
    """Agent-requested add: free text alone can't safely name/tag a
    brand-new secret (there's no existing vocabulary to match it against),
    so this requires explicit --name/--tags rather than guessing from the
    request text. Creates a value-less state=requested placeholder --
    fulfillment (the actual value) still requires a human running
    `portunus drop`."""
    if not args.name or not args.tags:
        return _err(
            "an 'add' request needs --name and --tags "
            "(e.g. --name vercel-mdostal --tags provider=vercel,project=mdostal.com) -- "
            "free text alone can't safely name a brand-new secret"
        )
    try:
        partial_tags = _parse_tags(args.tags)
    except ValueError as exc:
        return _err(str(exc))

    structured = {k: partial_tags.pop(k) for k in ("provider", "project", "env") if k in partial_tags}
    ref = registry.request(args.name, tags=partial_tags, **structured)
    broker.audit.append("semantic_op", ref.sm_name or args.name, "requested:add")
    print(
        f"requested {{{{secret:{ref.name}}}}} (state=requested) -- "
        f"a human can fulfill it via `portunus drop {ref.name} <sm_name> --stdin`"
    )
    return 0


def _cmd_ask_rotate(tag_set, registry, broker) -> int:
    """Agent-requested rotate: flags an EXISTING reference for rotation via
    a tags marker -- never touches its value or lifecycle state. Only
    applies to a reference resolve_by_tags can already find; there's
    nothing to rotate otherwise, so this fails closed exactly like fetch."""
    try:
        ref = registry.resolve_by_tags(**tag_set)
    except AmbiguousMatch as exc:
        broker.audit.append("semantic_op", "-", f"ambiguous-resolve:{','.join(exc.candidates)}")
        _err(f"request resolves to more than one reference ({', '.join(exc.candidates)}); "
             f"please specify more (e.g. env)")
        return EXIT_AMBIGUOUS
    except NoMatch:
        broker.audit.append("semantic_op", "-", f"no-match:{tag_set!r}")
        _err(f"no reference matches the inferred tags: {tag_set!r} -- nothing to rotate")
        return EXIT_NO_MATCH

    registry.retag(ref.name, tags={**ref.tags, "rotation_requested": "true"})
    broker.audit.append("semantic_op", ref.sm_name, "requested:rotate")
    print(
        f"flagged {{{{secret:{ref.name}}}}} for rotation -- "
        f"a human can rotate it via the UI or `portunus drop` a new value"
    )
    return 0


def cmd_ask(args) -> int:
    """Semantic front door: natural-language request -> parse_intent -> a tag
    set -> resolve_by_tags -> the same boundary-injection dispatch as inject.

    Fails closed at BOTH layers: parse_intent() on an unrecognizable/
    conflicting request, resolve_by_tags() on an under-specified-but-
    recognized request that still matches more than one reference. Neither
    layer ever guesses. The raw request text is never written to the audit
    log -- only the resolved tag set / outcome.

    add/rotate intents (see classify_intent_kind) never supply or see a
    value -- an agent can only REQUEST that a human fulfill an add (via
    Registry.request(), a value-less placeholder) or rotate (a metadata
    flag on the existing reference) -- the value still flows exclusively
    through the existing harness-side-only `drop` path.
    """
    registry, _audit, broker, resolver = _build()

    intent_kind = classify_intent_kind(args.request.lower())
    if intent_kind == "add":
        return _cmd_ask_add(args, registry, broker)

    try:
        tag_set = parse_intent(args.request, registry)
    except AmbiguousIntent as exc:
        broker.audit.append("semantic_op", "-", f"ambiguous-intent:{','.join(exc.candidates)}")
        return _err(exc.clarifying_question)

    if intent_kind == "rotate":
        return _cmd_ask_rotate(tag_set, registry, broker)

    try:
        ref = registry.resolve_by_tags(**tag_set)
    except AmbiguousMatch as exc:
        broker.audit.append("semantic_op", "-", f"ambiguous-resolve:{','.join(exc.candidates)}")
        _err(f"request resolves to more than one reference ({', '.join(exc.candidates)}); "
             f"please specify more (e.g. env)")
        return EXIT_AMBIGUOUS
    except NoMatch:
        broker.audit.append("semantic_op", "-", f"no-match:{tag_set!r}")
        _err(f"no reference matches the inferred tags: {tag_set!r}")
        return EXIT_NO_MATCH

    if not args.target:
        # Resolve-only: a legitimate success case, not a failure -- lets a
        # caller (e.g. the UI's Ask Bar) preview the match before committing
        # to an injection target. Metadata only, same as `find`.
        broker.audit.append("semantic_op", ref.sm_name, "resolved-only")
        if args.json:
            import json
            print(json.dumps(ref.to_dict()))
        else:
            print(f"  {{{{secret:{ref.name}}}}}  ->  {ref.sm_name}  "
                  f"(provider={ref.provider}, project={ref.project}, env={ref.env}, "
                  f"state={ref.state}) -- add --target to inject")
        return 0

    return _inject_resolved_ref(resolver, broker, ref, args, "semantic_op")


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
    try:
        extra_tags = _parse_tags(args.tags) if args.tags else {}
    except ValueError as exc:
        return _err(str(exc))
    ref = registry.add(
        args.name, args.sm_name, scope=args.scope, kind=args.kind, state="dropped",
        provider=args.provider, project=args.project, env=args.env, tags=extra_tags,
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
    entries = audit.entries()
    if args.secret:
        entries = [e for e in entries if e["secret"] == args.secret]
    entries = entries[-args.n:]
    if args.json:
        import json
        print(json.dumps(entries, indent=2))
        return 0
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


# --- parser --------------------------------------------------------------
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

    fd = sub.add_parser("find", help="find a reference by tags (metadata only, never a value)")
    fd.add_argument("--tags", required=True,
                     help="comma-separated k=v pairs, e.g. provider=vercel,project=mdostal.com")
    fd.set_defaults(func=cmd_find)

    inj = sub.add_parser("inject", help="resolve by tags and inject at a boundary target")
    inj.add_argument("--tags", required=True,
                      help="comma-separated k=v pairs, e.g. provider=vercel,project=mdostal.com")
    inj.add_argument("--target", required=True, choices=("env", "file"))
    inj.add_argument("--var", default="", help="target env var name (--target env)")
    inj.add_argument("--path", default="", help="target file path (--target file)")
    inj.add_argument("--format", dest="format", default="", choices=("", "env", "json", "yaml"),
                      help="target file format (--target file)")
    inj.add_argument("--key", default="", help="key to template the value under (--target file)")
    inj.set_defaults(func=cmd_inject)

    ask = sub.add_parser("ask", help="semantic front door: natural-language request -> injection")
    ask.add_argument("request", help='e.g. "the vercel secret for mdostal.com"')
    ask.add_argument("--target", choices=("env", "file"), default="",
                      help="required to actually inject; omit to only resolve+report")
    ask.add_argument("--var", default="", help="target env var name (--target env)")
    ask.add_argument("--path", default="", help="target file path (--target file)")
    ask.add_argument("--format", dest="format", default="", choices=("", "env", "json", "yaml"),
                      help="target file format (--target file)")
    ask.add_argument("--key", default="", help="key to template the value under (--target file)")
    ask.add_argument("--json", action="store_true", help="machine-readable output for resolve-only calls (UI consumer)")
    ask.add_argument("--name", default="", help="required for an 'add' request, e.g. --name vercel-mdostal")
    ask.add_argument("--tags", default="",
                      help="required for an 'add' request: comma-separated k=v pairs, e.g. provider=vercel,project=mdostal.com")
    ask.set_defaults(func=cmd_ask)

    dr = sub.add_parser(
        "drop",
        help="put a secret INTO Arca (local-encrypted); lands state=dropped",
    )
    dr.add_argument("name", help="reference name, e.g. shared-anthropic")
    dr.add_argument("sm_name", help="vault key, e.g. dostal-shared-anthropic")
    dr.add_argument("--scope", default="")
    dr.add_argument("--kind", default="")
    dr.add_argument("--provider", default="")
    dr.add_argument("--project", default="")
    dr.add_argument("--env", default="")
    dr.add_argument("--tags", default="", help="comma-separated k=v pairs, e.g. team=platform")
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
    au.add_argument("--json", action="store_true", help="machine-readable output (UI consumer)")
    au.add_argument("--secret", default="", help="filter to one SM name (metadata only, never a value)")
    au.set_defaults(func=cmd_audit)

    ve = sub.add_parser("verify", help="verify the audit hash chain")
    ve.set_defaults(func=cmd_verify)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
