"""OSTIARIUS — the ``portunus`` engine tool agents call.

Registry management, policy (gate/approve/grant), the audit chain, and the
boundary-only resolver. No subcommand ever prints a secret value to stdout;
``resolve`` either execs a command with the value in argv, or writes a 0600
temp file and prints its *path*.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from . import __version__
from .audit import AuditChain
from .auth import AuthError, EnvOIDCTokenSource, GCPWorkloadIdentityAuth
from .backend import AWSSecretsManagerBackend, BackendError, GcloudBackend, MockBackend, load_gcp_bindings
from .discover import DiscoverError, list_gcp_secrets, register_discovered
from .localvault import LocalEncryptedBackend, SessionExpired
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
        backend = GcloudBackend(
            project=project or os.environ.get("PORTUNUS_GCP_PROJECT", ""),
            bindings=load_gcp_bindings(),
            audit=audit,
        )
    elif backend_kind == "aws":
        # Stub: fails clearly rather than silently falling through to the
        # local-encrypted default (grill V1 -- the real pre-epic gap).
        backend = AWSSecretsManagerBackend()
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


def _cmd_ask_list(tag_set, registry, broker) -> int:
    """Agent-facing "what secrets exist for project X" query. Metadata only,
    zero-to-many, never a value -- routes through Registry.list_by_project(),
    which has no path to a backend at all. Requires a project in the
    inferred tags (a list without a project has nothing to scope the
    browse to); fails closed with a clear message otherwise, same
    no-guessing discipline as fetch/rotate."""
    project = tag_set.get("project", "")
    if not project:
        broker.audit.append("semantic_op", "-", "list-no-project")
        return _err(
            "a 'list' request needs a recognizable project "
            "(e.g. \"what secrets are available for mdostal.com\")"
        )
    refs = registry.list_by_project(
        project, provider=tag_set.get("provider") or None, env=tag_set.get("env") or None,
    )
    broker.audit.append("semantic_op", f"project:{project}", f"listed:{len(refs)}")
    if not refs:
        print(f"no references found for project={project}")
        return 0
    _print_reference_list(refs)
    return 0


def _print_reference_list(refs) -> None:
    """Metadata only, never a value."""
    for ref in sorted(refs, key=lambda r: r.name):
        print(
            f"  {{{{secret:{ref.name}}}}}  ->  {ref.sm_name}  "
            f"(provider={ref.provider}, env={ref.env}, state={ref.state})"
        )
        if ref.description:
            print(f"    description: {ref.description}")
        if ref.purpose:
            print(f"    purpose:     {ref.purpose}")
        if ref.injected_as:
            print(f"    injected_as: {ref.injected_as}")


def cmd_list(args) -> int:
    """portunus list --project <id> -- direct CLI access to list_by_project(),
    the same metadata-only method the LLM-facing `ask` list intent uses."""
    registry, *_ = _build()
    refs = registry.list_by_project(args.project, provider=args.provider or None, env=args.env or None)
    if args.json:
        print(json.dumps([r.to_dict() for r in sorted(refs, key=lambda r: r.name)]))
        return 0
    if not refs:
        print(f"no references found for project={args.project}")
        return 0
    _print_reference_list(refs)
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

    if intent_kind == "list":
        return _cmd_ask_list(tag_set, registry, broker)

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


def cmd_retag(args) -> int:
    """Update a reference's provider/project/env/tags in place. Metadata
    only -- never touches a value. Delegates entirely to Registry.retag()
    for the collision check (no hand-rolled CLI-level tag merge)."""
    registry, _audit, broker, _resolver = _build()
    try:
        tags = _parse_tags(args.tags) if args.tags else None
    except ValueError as exc:
        return _err(str(exc))

    kwargs = {}
    if args.provider:
        kwargs["provider"] = args.provider
    if args.project:
        kwargs["project"] = args.project
    if args.env:
        kwargs["env"] = args.env
    if tags is not None:
        kwargs["tags"] = tags

    try:
        ref = registry.retag(args.name, **kwargs)
    except AmbiguousMatch as exc:
        _err(f"retagging {args.name!r} would collide with: {', '.join(exc.candidates)}")
        return EXIT_AMBIGUOUS
    except KeyError:
        _err(f"unknown reference: {args.name}")
        return EXIT_NO_MATCH

    broker.audit.append("retag", ref.sm_name, "ok")
    print(f"  {{{{secret:{ref.name}}}}}  ->  {ref.sm_name}  "
          f"(provider={ref.provider}, project={ref.project}, env={ref.env}, tags={ref.tags})")
    return 0


def _session_backend(resolver):
    """Sessions are a LocalEncryptedBackend-only capability -- not part of
    the generic SecretBackend protocol GcloudBackend/MockBackend implement.
    Mirrors drop's exact same hasattr check."""
    backend = resolver.backend
    if not hasattr(backend, "store_session"):
        return None
    return backend


def cmd_session_store(args) -> int:
    """Store a browser/login session. Mirrors drop's stdin-only-in
    discipline exactly: the session JSON blob comes from stdin or a local
    file, never an inline argv flag."""
    _registry, _audit, broker, resolver = _build()
    backend = _session_backend(resolver)
    if backend is None:
        return _err("session commands require the local-encrypted backend "
                     "(unset PORTUNUS_BACKEND or set it to unset/local)")

    if args.stdin:
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(args.value_file).read_text()
        except OSError as exc:
            return _err(f"cannot read --value-file: {exc}")
    try:
        session_obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _err(f"invalid session JSON: {exc}")

    try:
        backend.store_session(
            args.site, args.account, session_obj,
            ttl_seconds=args.ttl_seconds,
            rotation_interval_seconds=args.rotation_interval_seconds,
        )
    except ValueError as exc:
        return _err(str(exc))

    key = backend.session_key(args.site, args.account)
    broker.audit.append("session_store", key, "stored")
    print(f"stored session for {args.site} / {args.account} (ttl={args.ttl_seconds}s)")
    return 0


def cmd_session_load(args) -> int:
    """Load a session's full record (including real cookies/tokens) --
    exactly as sensitive as a secret value, so it gets the identical
    0600-tempfile, path-only-printed treatment as `resolve`."""
    _registry, _audit, broker, resolver = _build()
    backend = _session_backend(resolver)
    if backend is None:
        return _err("session commands require the local-encrypted backend "
                     "(unset PORTUNUS_BACKEND or set it to unset/local)")

    key = backend.session_key(args.site, args.account)
    try:
        record = backend.load_session(args.site, args.account, allow_expired=args.allow_expired)
    except SessionExpired as exc:
        broker.audit.append("session_load", key, "denied-expired")
        return _err(f"{exc} -- pass --allow-expired to load it anyway")
    except BackendError as exc:
        return _err(str(exc))

    fd, path = tempfile.mkstemp(prefix="portunus-session-")
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(record))
    except BaseException:
        os.unlink(path)
        raise
    broker.audit.append("session_load", key, "ok")
    print(path)  # path only, never the record
    return 0


def cmd_session_inspect(args) -> int:
    """Metadata only -- namespace/ttl/rotation/expired, never a payload."""
    _registry, _audit, _broker, resolver = _build()
    backend = _session_backend(resolver)
    if backend is None:
        return _err("session commands require the local-encrypted backend "
                     "(unset PORTUNUS_BACKEND or set it to unset/local)")
    try:
        view = backend.inspect_session(args.site, args.account)
    except BackendError as exc:
        return _err(str(exc))
    if args.json:
        print(json.dumps(view))
    else:
        print(f"  {args.site} / {args.account}  expired={view['expired']}  "
              f"ttl={view['ttl']}  rotation={view['rotation']}")
    return 0


def cmd_session_list(args) -> int:
    """Metadata for every stored session -- never a payload."""
    _registry, _audit, _broker, resolver = _build()
    backend = _session_backend(resolver)
    if backend is None:
        return _err("session commands require the local-encrypted backend "
                     "(unset PORTUNUS_BACKEND or set it to unset/local)")
    sessions = backend.list_sessions()
    if args.json:
        print(json.dumps(sessions))
    else:
        if not sessions:
            print("(no sessions stored)")
        for view in sessions:
            ns = view["namespace"]
            print(f"  {ns['site']} / {ns['account']}  expired={view['expired']}")
    return 0


def cmd_session_remove(args) -> int:
    """Remove a stored session. Confirms by namespace only."""
    _registry, _audit, broker, resolver = _build()
    backend = _session_backend(resolver)
    if backend is None:
        return _err("session commands require the local-encrypted backend "
                     "(unset PORTUNUS_BACKEND or set it to unset/local)")
    existed = backend.remove_session(args.site, args.account)
    if not existed:
        return _err(f"no such session: {args.site} / {args.account}")
    key = backend.session_key(args.site, args.account)
    broker.audit.append("session_remove", key, "removed")
    print(f"removed session for {args.site} / {args.account}")
    return 0


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


def cmd_auth_gcp(args) -> int:
    """Mint a GCP WIF access token and report identity/scope/expiry -- never the token."""
    audit = AuditChain()
    project = args.project or os.environ.get("PORTUNUS_GCP_PROJECT", "")
    bindings = load_gcp_bindings()
    audience = args.audience
    if not audience and project in bindings:
        audience = bindings[project].wif_audience
    if not audience:
        audience = os.environ.get("PORTUNUS_GCP_WIF_AUDIENCE", "")
    try:
        auth = GCPWorkloadIdentityAuth(
            audience=audience, token_source=EnvOIDCTokenSource(), audit=audit,
        )
        minted = auth.mint()
    except AuthError as exc:
        return _err(str(exc))
    print(
        "gcp:wif ok "
        f"identity={minted.identity} scope={minted.scope} expires_at={minted.expires_at}"
    )
    return 0


def cmd_discover(args) -> int:
    """Read-only: list what already exists in a live GCP project (names/labels
    only, never a value). --register writes not-yet-registered ones as
    state=requested placeholders. See discover.py -- this command never
    touches SecretBackend.access()."""
    registry = Registry()
    try:
        discovered = list_gcp_secrets(args.project)
    except DiscoverError as exc:
        return _err(str(exc))

    if args.register:
        report = register_discovered(registry, args.project, discovered)
        for name in report.registered:
            print(f"registered  {name} (state=requested)")
        for name in report.conflicts:
            print(f"conflict    {name} -- already points at a different secret, skipped")
        for name in report.already_registered:
            print(f"unchanged   {name} (already registered)")
        return 0

    from .discover import diff_against_registry
    already, not_yet = diff_against_registry(registry, args.project, discovered)
    for name in already:
        print(f"registered      {name}")
    for d in not_yet:
        label_note = f" labels={d.labels}" if d.labels else ""
        print(f"not-registered  {d.sm_name}{label_note}")
    return 0


# --- parser --------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="portunus", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=f"portunus {__version__}")
    p.add_argument(
        "--home", default="",
        help="explicit vault path for this invocation only, overrides PORTUNUS_HOME "
             "(cross-repo targeting -- not automatic multi-vault search)",
    )
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

    ls = sub.add_parser(
        "list",
        help="list every reference for a project (metadata only, never a value)",
    )
    ls.add_argument("--project", required=True)
    ls.add_argument("--provider", default="")
    ls.add_argument("--env", default="")
    ls.add_argument("--json", action="store_true", help="machine-readable output")
    ls.set_defaults(func=cmd_list)

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

    rt = sub.add_parser("retag", help="update a reference's provider/project/env/tags in place")
    rt.add_argument("name")
    rt.add_argument("--provider", default="")
    rt.add_argument("--project", default="")
    rt.add_argument("--env", default="")
    rt.add_argument("--tags", default="", help="comma-separated k=v pairs, replaces the open tags dict")
    rt.set_defaults(func=cmd_retag)

    ses = sub.add_parser("session", help="browser/login session storage (local-encrypted backend only)")
    ses_sub = ses.add_subparsers(dest="action", required=True)

    ses_store = ses_sub.add_parser("store", help="store a session (value via --stdin or --value-file, never inline)")
    ses_store.add_argument("site")
    ses_store.add_argument("account")
    ses_store.add_argument("--ttl-seconds", type=int, required=True)
    ses_store.add_argument("--rotation-interval-seconds", type=int, default=None)
    ses_src = ses_store.add_mutually_exclusive_group(required=True)
    ses_src.add_argument("--stdin", action="store_true", help="read the session JSON from stdin")
    ses_src.add_argument("--value-file", help="read the session JSON from this local file")
    ses_store.set_defaults(func=cmd_session_store)

    ses_load = ses_sub.add_parser("load", help="load a session -- writes a 0600 tempfile, prints only the path")
    ses_load.add_argument("site")
    ses_load.add_argument("account")
    ses_load.add_argument("--allow-expired", action="store_true")
    ses_load.set_defaults(func=cmd_session_load)

    ses_inspect = ses_sub.add_parser("inspect", help="show session metadata only (never the payload)")
    ses_inspect.add_argument("site")
    ses_inspect.add_argument("account")
    ses_inspect.add_argument("--json", action="store_true")
    ses_inspect.set_defaults(func=cmd_session_inspect)

    ses_list = ses_sub.add_parser("list", help="list every stored session's metadata (never a payload)")
    ses_list.add_argument("--json", action="store_true")
    ses_list.set_defaults(func=cmd_session_list)

    ses_remove = ses_sub.add_parser("remove", help="remove a stored session")
    ses_remove.add_argument("site")
    ses_remove.add_argument("account")
    ses_remove.set_defaults(func=cmd_session_remove)

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

    auth_p = sub.add_parser("auth", help="check keyless cloud credential minting")
    auth_sub = auth_p.add_subparsers(dest="provider", required=True)
    auth_gcp = auth_sub.add_parser("gcp", help="mint a GCP WIF access token without printing it")
    auth_gcp.add_argument("--project", default="")
    auth_gcp.add_argument("--audience", default="")
    auth_gcp.set_defaults(func=cmd_auth_gcp)

    disc = sub.add_parser(
        "discover",
        help="read-only: list what already exists in a live provider project (never a value)",
    )
    disc.add_argument("--provider", required=True, choices=("gcp",))
    disc.add_argument("--project", required=True)
    disc.add_argument("--register", action="store_true",
                       help="write not-yet-registered secrets as state=requested placeholders")
    disc.set_defaults(func=cmd_discover)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.home:
        return args.func(args)

    # --home overrides PORTUNUS_HOME for this invocation only. paths.home()
    # reads the env fresh on every call, so setting it here (and restoring
    # it afterward) is sufficient to route every Registry()/AuditChain()
    # construction site -- including any that don't go through _build() --
    # without threading an override parameter through each one by hand.
    prior = os.environ.get("PORTUNUS_HOME")
    os.environ["PORTUNUS_HOME"] = args.home
    try:
        return args.func(args)
    finally:
        if prior is None:
            os.environ.pop("PORTUNUS_HOME", None)
        else:
            os.environ["PORTUNUS_HOME"] = prior


if __name__ == "__main__":
    raise SystemExit(main())
