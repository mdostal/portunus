"""OSTIARIUS — the ``portunus`` engine tool agents call.

Registry management, policy (gate/approve/grant), the audit chain, and the
boundary-only resolver. No subcommand ever prints a secret value to stdout;
``resolve`` either execs a command with the value in argv, or writes a 0600
temp file and prints its *path*.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from . import agent_setup
from . import update as update_mod
from .audit import AuditChain
from .auth import AuthError, EnvOIDCTokenSource, GCPWorkloadIdentityAuth
from .backend import (
    AWSSecretsManagerBackend, AzureKeyVaultBackend, BackendError, DopplerBackend,
    GcloudBackend, InfisicalBackend, MockBackend, OnePasswordConnectBackend, SyncingBackend,
    VaultBinding, VaultServerBackend, load_vault_bindings, save_vault_bindings,
)
from .backup import ExportError, export_archive, import_archive
from .paths import home
from .rotation import RotationBinding, load_rotation_bindings, save_rotation_bindings
from .views import ViewError, add_to_view, create_view, delete_view, load_views, remove_from_view
from .roles import (
    PolicyError,
    VALID_SCOPE_TYPES,
    delete_policy,
    enforcement_is_on,
    load_policies,
    set_enforcement,
    set_policy,
)
from .crawl import crawl_candidates, generate_report
from .leakscan import (
    add_scan_path,
    add_scan_repo,
    load_leak_status,
    load_scan_paths,
    load_scan_repos,
    mark_rotated,
    remove_scan_path,
    remove_scan_repo,
    run_scan,
    summarize,
)
from .discover import DiscoverError, list_gcp_secrets, register_discovered
from .localvault import LocalEncryptedBackend, SessionExpired
from .broker import ApprovalRequired, Broker, Identity, NotAuthorized, NotInjectable
from .adapters import AdapterError, EnvVarAdapter, FileAdapter
from .intent import AmbiguousIntent, classify_intent_kind, parse_intent
from .registry import SUGGESTIBLE_FIELDS, AmbiguousMatch, NoMatch, Registry
from .resolver import Resolver, UnknownReference
from .vault_transfer import build_bundle, import_bundle, verify_access, write_bundle

# Distinct exit codes so scripts can branch on the failure mode without
# parsing stderr text. 1 is the pre-existing generic-error code (_err()).
EXIT_NO_MATCH = 3
EXIT_AMBIGUOUS = 4


def _err(msg: str) -> int:
    print(f"portunus: {msg}", file=sys.stderr)
    return 1


def _make_backend_router(vault_bindings, audit, fallback_backend):
    """The actual per-project/per-reference router (portunus-vault-routing).
    3-level precedence: (1) ref.backend, if set, wins outright; (2) else the
    reference's project VaultBinding.backend (wrapped in a recency-aware
    SyncingBackend when that project's sync_mode="cached"); (3) else
    `fallback_backend` (today's global PORTUNUS_BACKEND-selected backend,
    unchanged). Backend instances are constructed once and cached for this
    router's lifetime, not reconstructed per call -- including the shared
    SyncingBackend, so its sync-state file sees every cached-mode access
    regardless of which project triggered it."""
    instances: dict = {}

    def _for_kind(kind: str):
        if kind in instances:
            return instances[kind]
        if kind == "local":
            inst = LocalEncryptedBackend()
        elif kind == "gcp":
            inst = GcloudBackend(bindings=vault_bindings, audit=audit)
        elif kind == "aws":
            inst = AWSSecretsManagerBackend()
        elif kind == "vault":
            inst = VaultServerBackend()
        elif kind == "infisical":
            inst = InfisicalBackend()
        elif kind == "doppler":
            inst = DopplerBackend()
        elif kind == "onepassword":
            inst = OnePasswordConnectBackend()
        elif kind == "azure":
            inst = AzureKeyVaultBackend()
        else:
            inst = fallback_backend
        instances[kind] = inst
        return inst

    def _synced_gcp():
        if "synced-gcp" not in instances:
            instances["synced-gcp"] = SyncingBackend(
                _for_kind("gcp"), _for_kind("local"), home() / "sync-state.json",
            )
        return instances["synced-gcp"]

    def router(ref):
        if ref.backend:
            return _for_kind(ref.backend)
        binding = vault_bindings.get(ref.project)
        if binding is not None:
            if binding.backend == "gcp" and binding.sync_mode == "cached":
                return _synced_gcp()
            return _for_kind(binding.backend)
        return fallback_backend

    return router


def _build(project: str = ""):
    registry = Registry()
    audit = AuditChain()
    broker = Broker(registry, audit)
    backend_kind = os.environ.get("PORTUNUS_BACKEND", "local")
    if backend_kind == "mock":
        # For local dry-runs only; values come from PORTUNUS_MOCK_<SM_NAME>.
        # Always wins outright -- never routed through vault-bindings.json,
        # a safety rail for tests/dry-runs (grill H2, portunus-vault-routing).
        values = {}
        for k, v in os.environ.items():
            if k.startswith("PORTUNUS_MOCK_"):
                values[k[len("PORTUNUS_MOCK_"):].lower().replace("_", "-")] = v
        backend = MockBackend(values)
        return registry, audit, broker, Resolver(registry, backend, broker)

    vault_bindings = load_vault_bindings()
    if backend_kind == "gcloud":
        backend = GcloudBackend(
            project=project or os.environ.get("PORTUNUS_GCP_PROJECT", ""),
            bindings=vault_bindings,
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

    backend_for = _make_backend_router(vault_bindings, audit, backend)
    return registry, audit, broker, Resolver(registry, backend, broker, backend_for=backend_for)


# --- subcommand handlers -------------------------------------------------
def cmd_reg(args) -> int:
    registry, audit, broker, resolver = _build()
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
        try:
            injected_as = _parse_tags(args.injected_as) if args.injected_as else None
            related = _parse_related(args.related) if args.related else None
        except ValueError as exc:
            return _err(str(exc))
        ref = registry.add(args.name, args.sm_name, scope=args.scope,
                           kind=args.kind, org=args.org or "", project=args.project or "",
                           description=args.description or "", purpose=args.purpose or "",
                           injected_as=injected_as, group=args.group or "", related=related,
                           repo=args.repo or "")
        print(f"registered {{{{secret:{ref.name}}}}} -> {ref.sm_name}")
        return 0
    if args.action == "rm":
        ref = registry.get(args.name)
        if ref is None:
            print("no such reference")
            return 0
        # Best-effort: also purge the underlying stored value from whichever
        # backend actually serves this reference -- registry.remove() alone
        # only drops the pointer, leaving the encrypted value (local
        # backend) or the cloud secret (GCP/other) orphaned forever. Not
        # every backend supports a programmatic remove (GCP Secret Manager
        # deletion is deliberately out of scope here -- a human should do
        # that explicitly in the console); those are skipped silently, the
        # registry entry is still removed either way.
        backend = resolver.backend_for(ref) if resolver.backend_for else resolver.backend
        purged_value = False
        if hasattr(backend, "remove"):
            try:
                purged_value = bool(backend.remove(ref.sm_name))
            except Exception:
                purged_value = False
        registry.remove(args.name)
        if purged_value:
            print(f"removed {{{{secret:{args.name}}}}} -- registry entry and stored value both purged")
        else:
            print(
                f"removed {{{{secret:{args.name}}}}} -- registry entry only "
                "(this backend does not support automatic value removal)"
            )
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


def _parse_related(raw: str) -> list:
    """Parse "name1,name2" into ["name1", "name2"] -- trims whitespace, drops
    blank entries. Deliberately not _parse_tags(): related is a bare name
    list, not k=v pairs."""
    return [name.strip() for name in raw.split(",") if name.strip()]


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
    except (UnknownReference, NotInjectable, ApprovalRequired, NotAuthorized, BackendError, AdapterError) as exc:
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
        injected_as = _parse_tags(args.injected_as) if args.injected_as else None
        related = _parse_related(args.related) if args.related else None
        source_files = _parse_related(args.source_files) if args.source_files else None
    except ValueError as exc:
        return _err(str(exc))

    kwargs = {}
    if args.org:
        kwargs["org"] = args.org
    if args.provider:
        kwargs["provider"] = args.provider
    if args.project:
        kwargs["project"] = args.project
    if args.env:
        kwargs["env"] = args.env
    if tags is not None:
        kwargs["tags"] = tags
    if args.description:
        kwargs["description"] = args.description
    if args.purpose:
        kwargs["purpose"] = args.purpose
    if injected_as is not None:
        kwargs["injected_as"] = injected_as
    if args.group:
        kwargs["group"] = args.group
    if related is not None:
        kwargs["related"] = related
    if args.repo:
        kwargs["repo"] = args.repo
    if source_files is not None:
        kwargs["source_files"] = source_files

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
          f"(org={ref.org}, provider={ref.provider}, project={ref.project}, env={ref.env}, tags={ref.tags})")
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
        injected_as = _parse_tags(args.injected_as) if args.injected_as else {}
        related = _parse_related(args.related) if args.related else []
        source_files = _parse_related(args.source_files) if args.source_files else []
    except ValueError as exc:
        return _err(str(exc))
    ref = registry.add(
        args.name, args.sm_name, scope=args.scope, kind=args.kind, state="dropped",
        org=args.org, provider=args.provider, project=args.project, env=args.env, tags=extra_tags,
        description=args.description, purpose=args.purpose, injected_as=injected_as,
        group=args.group, related=related, backend=args.backend, repo=args.repo,
        source_files=source_files,
    )
    backend.store(ref.sm_name, value)
    del value  # scrub our local reference promptly
    broker.audit.append("drop", ref.sm_name, "stored")
    print(
        f"dropped {{{{secret:{ref.name}}}}} -> {ref.sm_name} (state=dropped; "
        f"run `portunus state {ref.name} enabled` to allow injection)"
    )
    return 0


def cmd_drop_bulk(args) -> int:
    """Bulk counterpart to `drop` -- one JSON file of entries, each with the
    same fields `drop` accepts (name/sm_name/value required). The backend
    gate is checked once upfront; a malformed entry is reported under
    "failed" and does not abort the rest of the batch. Never prints a value
    on any path, including a failed entry's error message."""
    registry, _, broker, resolver = _build()
    backend = resolver.backend
    if not hasattr(backend, "store"):
        return _err(
            "drop requires the local-encrypted backend "
            "(unset PORTUNUS_BACKEND or set it to unset/local)"
        )
    try:
        entries = json.loads(Path(args.entries_file).read_text())
    except OSError as exc:
        return _err(f"cannot read entries file: {exc}")
    except json.JSONDecodeError as exc:
        return _err(f"malformed JSON in entries file: {exc}")

    created: list = []
    failed: list = []
    for entry in entries:
        entry_name = entry.get("name", "")
        try:
            value = entry.get("value", "")
            if not value:
                raise ValueError("empty secret value; nothing dropped")
            ref = registry.add(
                entry_name, entry.get("sm_name", ""),
                scope=entry.get("scope", ""), kind=entry.get("kind", ""), state="dropped",
                provider=entry.get("provider", ""), project=entry.get("project", ""),
                env=entry.get("env", ""), tags=entry.get("tags"),
                description=entry.get("description", ""), purpose=entry.get("purpose", ""),
                injected_as=entry.get("injected_as"), group=entry.get("group", ""),
                related=entry.get("related"), backend=entry.get("backend", ""),
            )
            backend.store(ref.sm_name, value)
            del value
            broker.audit.append("drop", ref.sm_name, "stored")
            created.append(ref.name)
        except (ValueError, KeyError) as exc:
            failed.append({"name": entry_name, "error": str(exc)})

    if args.json:
        print(json.dumps({"created": created, "failed": failed}))
        return 0
    for name in created:
        print(f"  dropped  {name}")
    for entry in failed:
        print(f"  failed   {entry['name']}: {entry['error']}")
    return 0


def cmd_retag_bulk(args) -> int:
    """Bulk counterpart to `retag` -- selects every reference whose `group`
    starts with --group-prefix (a plain string prefix, no query language)
    and applies the same Registry.retag() to each. One reference's
    collision failure is reported under "failed" and does not abort the
    rest of the batch, same precedent as drop_bulk. --dry-run reports what
    WOULD change and makes zero writes -- required given this can touch
    dozens of real references in one call."""
    registry, _audit, broker, _resolver = _build()
    try:
        source_files = _parse_related(args.source_files) if args.source_files else None
    except ValueError as exc:
        return _err(str(exc))

    kwargs = {}
    if args.org:
        kwargs["org"] = args.org
    if args.repo:
        kwargs["repo"] = args.repo
    if source_files is not None:
        kwargs["source_files"] = source_files

    matched = [ref.name for ref in registry if ref.group.startswith(args.group_prefix)]

    if args.dry_run:
        if args.json:
            print(json.dumps({"would_update": matched}))
        else:
            for name in matched:
                print(f"  would update  {name}")
        return 0

    updated: list = []
    failed: list = []
    for name in matched:
        try:
            registry.retag(name, **kwargs)
            updated.append(name)
        except AmbiguousMatch as exc:
            failed.append({"name": name, "error": f"would collide with: {', '.join(exc.candidates)}"})
        except KeyError as exc:
            failed.append({"name": name, "error": str(exc)})

    if args.json:
        print(json.dumps({"updated": updated, "failed": failed}))
        return 0
    for name in updated:
        print(f"  updated  {name}")
    for entry in failed:
        print(f"  failed   {entry['name']}: {entry['error']}")
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
    except (NotInjectable, ApprovalRequired, NotAuthorized) as exc:
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
    bindings = load_vault_bindings()
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


def cmd_auth_login(args) -> int:
    """Thin wrapper around `gcloud auth login <email>` -- still opens a real
    browser; Portunus doesn't remove that step, this is just the one command
    to remember. Never touches a secret value -- only the account email and
    gcloud's own status output."""
    if shutil.which("gcloud") is None:
        return _err("gcloud CLI not found on PATH")
    try:
        proc = subprocess.run(
            ["gcloud", "auth", "login", args.email], capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return _err(f"gcloud auth login timed out for {args.email}")
    if proc.returncode != 0:
        return _err(f"gcloud auth login failed for {args.email}: {proc.stderr.strip()[:200]}")
    print(f"authenticated: {args.email}")
    return 0


def cmd_auth_status(args) -> int:
    """Cross-reference every configured GCP project binding's account
    against gcloud's locally credentialed accounts (`gcloud auth list`) --
    reports which bindings are authenticated vs. missing, per-binding. Not
    automatic reauth -- a status report plus `auth login` above it. Account
    emails and gcloud's own credential list are not secret values."""
    bindings = load_vault_bindings()
    if not bindings:
        if args.json:
            print(json.dumps({}))
        else:
            print("no bindings configured")
        return 0

    credentialed = set()
    if shutil.which("gcloud") is not None:
        try:
            proc = subprocess.run(
                ["gcloud", "auth", "list", "--format=json"], capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                credentialed = {entry.get("account", "") for entry in json.loads(proc.stdout or "[]")}
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            credentialed = set()

    report = {
        project: {"account": binding.account, "authenticated": binding.account in credentialed}
        for project, binding in bindings.items()
    }
    if args.json:
        print(json.dumps(report))
        return 0
    for project, info in report.items():
        status = "authenticated" if info["authenticated"] else "MISSING"
        print(f"{project}: {info['account'] or '(no account set)'} -- {status}")
    return 0


def _wif_configured(project: str) -> bool:
    """True iff `project` has a vault-bindings.json entry with a non-empty
    wif_audience. Boolean only -- the audience string itself is never
    returned by this helper's callers (matches `portunus auth gcp`'s own
    restraint: identity/scope/expiry only, never the audience/token)."""
    bindings = load_vault_bindings()
    binding = bindings.get(project)
    return bool(binding and binding.wif_audience)


def _eager_sync_down(registry, resolver, names: List[str]) -> Dict[str, str]:
    """Warm the local encrypted cache immediately for freshly-registered
    references under a sync_mode=cached project (portunus-vault-backup story
    01) -- reuses SyncingBackend.access() exactly as `cmd_sync` does, purely
    for its cache-populating side effect. Deliberately bypasses
    Broker.check_injectable: the reference's `state` stays "requested"
    throughout, so every real resolve/inject/ask/MCP path remains fully
    fail-closed exactly as before -- only the local cache gets populated.
    Best-effort and per-reference: a fetch failure here never fails
    registration itself, and the fetched value is never captured into
    anything that escapes this function."""
    results: Dict[str, str] = {}
    for name in names:
        ref = registry.get(name)
        if ref is None:
            continue
        backend = resolver.backend_for(ref) if resolver.backend_for else resolver.backend
        if not isinstance(backend, SyncingBackend):
            continue  # not a cached-mode reference -- nothing to warm
        try:
            backend.access(ref.sm_name, project=ref.project)
        except BackendError as exc:
            results[name] = f"sync-failed: {exc}"
            continue
        results[name] = "synced"
    return results


def cmd_discover(args) -> int:
    """Read-only: list what already exists in a live GCP project (names/labels
    only, never a value). --register writes not-yet-registered ones as
    state=requested placeholders. See discover.py -- this command never
    touches SecretBackend.access() directly; --register additionally warms
    the local cache for sync_mode=cached projects via _eager_sync_down()."""
    registry, _audit, _broker, resolver = _build()
    account = ""
    binding = load_vault_bindings().get(args.project)
    if binding:
        account = binding.account
    try:
        discovered = list_gcp_secrets(args.project, account=account)
    except DiscoverError as exc:
        return _err(str(exc))

    if args.register:
        report = register_discovered(registry, args.project, discovered)
        sync_results = _eager_sync_down(registry, resolver, report.registered)
        if args.json:
            print(json.dumps({
                "registered": report.registered,
                "conflicts": report.conflicts,
                "already_registered": report.already_registered,
                "wif_configured": _wif_configured(args.project),
                "sync_results": sync_results,
            }))
            return 0
        for name in report.registered:
            note = ""
            status = sync_results.get(name)
            if status == "synced":
                note = " (cache warmed)"
            elif status is not None:
                note = f" ({status})"
            print(f"registered  {name} (state=requested){note}")
        for name in report.conflicts:
            print(f"conflict    {name} -- already points at a different secret, skipped")
        for name in report.already_registered:
            print(f"unchanged   {name} (already registered)")
        return 0

    from .discover import diff_against_registry
    already, not_yet = diff_against_registry(registry, args.project, discovered)
    if args.json:
        print(json.dumps({
            "already_registered": already,
            "not_yet_registered": [
                {"sm_name": d.sm_name, "labels": d.labels, "create_time": d.create_time}
                for d in not_yet
            ],
            "wif_configured": _wif_configured(args.project),
        }))
        return 0
    for name in already:
        print(f"registered      {name}")
    for d in not_yet:
        label_note = f" labels={d.labels}" if d.labels else ""
        print(f"not-registered  {d.sm_name}{label_note}")
    return 0


def cmd_bindings_set(args) -> int:
    """Upsert one project's vault binding -- only explicitly-passed fields
    change, preserving whichever field wasn't passed (mirrors Registry.
    retag()'s only-passed-fields-change pattern). Identity-selector/topology
    strings only (account email, WIF audience) -- never a credential."""
    bindings = load_vault_bindings()
    existing = bindings.get(args.project)
    account = args.account if args.account else (existing.account if existing else "")
    wif_audience = (
        args.wif_audience if args.wif_audience else (existing.wif_audience if existing else "")
    )
    backend = args.backend if args.backend else (existing.backend if existing else "gcp")
    sync_mode = args.sync_mode if args.sync_mode else (existing.sync_mode if existing else "direct")
    bindings[args.project] = VaultBinding(
        project=args.project, wif_audience=wif_audience, account=account,
        backend=backend, sync_mode=sync_mode,
    )
    save_vault_bindings(bindings)
    print(
        f"binding set: {args.project} (backend={backend}, sync_mode={sync_mode}, "
        f"account={account or '-'}, wif_audience={wif_audience or '-'})"
    )
    return 0


def cmd_rotation_bindings_set(args) -> int:
    """Upsert one provider's rotation binding -- only explicitly-passed
    fields change (mirrors cmd_bindings_set's own only-passed-fields-change
    pattern). `account` is a free-text context hint (e.g. a Vercel team
    slug) -- never a credential."""
    bindings = load_rotation_bindings()
    existing = bindings.get(args.provider)
    status = args.status if args.status else (existing.status if existing else "stub")
    account = args.account if args.account else (existing.account if existing else "")
    bindings[args.provider] = RotationBinding(provider=args.provider, status=status, account=account)
    save_rotation_bindings(bindings)
    print(f"rotation binding set: {args.provider} (status={status}, account={account or '-'})")
    return 0


def cmd_rotation_bindings_show(args) -> int:
    """Show one or all rotation bindings -- status/account only, never a
    credential (there is no credential to show; rotation adapters resolve
    their own admin token via the normal boundary-only resolver)."""
    bindings = load_rotation_bindings()
    if args.provider:
        b = bindings.get(args.provider)
        if b is None:
            if args.json:
                print(json.dumps({}))
            else:
                print(f"no rotation binding configured for {args.provider}")
            return 0
        bindings = {args.provider: b}
    if args.json:
        print(json.dumps({
            provider: {"status": b.status, "account": b.account}
            for provider, b in bindings.items()
        }))
        return 0
    if not bindings:
        print("(no rotation bindings configured)")
        return 0
    for provider, b in sorted(bindings.items()):
        print(f"  {provider}  status={b.status}  account={b.account or '-'}")
    return 0


def cmd_bindings_show(args) -> int:
    """Show one or all vault bindings -- real account/wif_audience values,
    not presence-only. A local CLI reading the operator's own 0600
    vault-bindings.json is the same trust boundary as `cat`ing it directly."""
    bindings = load_vault_bindings()
    if args.project:
        binding = bindings.get(args.project)
        if binding is None:
            if args.json:
                print(json.dumps({}))
            else:
                print(f"no binding configured for {args.project}")
            return 0
        bindings = {args.project: binding}
    if args.json:
        print(json.dumps({
            proj: {
                "account": b.account, "wif_audience": b.wif_audience,
                "backend": b.backend, "sync_mode": b.sync_mode,
            }
            for proj, b in bindings.items()
        }))
        return 0
    if not bindings:
        print("(no bindings configured)")
        return 0
    for proj, b in bindings.items():
        print(
            f"  {proj}  backend={b.backend}  sync_mode={b.sync_mode}  "
            f"account={b.account or '-'}  wif_audience={b.wif_audience or '-'}"
        )
    return 0


def cmd_sync(args) -> int:
    """Force a recency check (and re-fetch if stale) for every cached-mode
    reference in a project -- ahead of relying on incidental access timing
    (the deploy use case: materialize a fresh .env once, not a live SM
    round-trip per secret per instance). Metadata-only report: names and
    error strings, never a value."""
    registry, _, broker, resolver = _build()
    synced, fresh, failed = [], [], []
    for ref in registry.list_by_project(args.project):
        try:
            gated_ref = broker.check_injectable(ref.name, requester=Identity.from_env())
        except (NotInjectable, ApprovalRequired, NotAuthorized):
            continue  # not currently injectable/authorized -- nothing to sync, not a failure
        backend = resolver.backend_for(gated_ref) if resolver.backend_for else resolver.backend
        if not isinstance(backend, SyncingBackend):
            continue  # not a cached-mode reference -- nothing to report
        try:
            backend.access(gated_ref.sm_name, project=gated_ref.project)
        except BackendError as exc:
            failed.append({"name": ref.name, "error": str(exc)})
            continue
        (synced if backend.last_sync_result == "synced" else fresh).append(ref.name)

    if args.json:
        print(json.dumps({"synced": synced, "already_fresh": fresh, "failed": failed}))
        return 0
    for name in synced:
        print(f"  synced        {name}")
    for name in fresh:
        print(f"  already-fresh {name}")
    for entry in failed:
        print(f"  failed        {entry['name']}: {entry['error']}")
    if not (synced or fresh or failed):
        print("(no cached-mode references for this project)")
    return 0


_EXPORT_PASSPHRASE_ENV = "PORTUNUS_EXPORT_PASSPHRASE"


def _resolve_passphrase(prompt: str, confirm: bool = False) -> str:
    """Never accepted via an inline CLI flag -- matches `portunus drop`'s
    own boundary-only convention for sensitive input. Only
    PORTUNUS_EXPORT_PASSPHRASE (for scripted/automated backup jobs) or an
    interactive getpass prompt; either way it never touches argv."""
    env_val = os.environ.get(_EXPORT_PASSPHRASE_ENV)
    if env_val:
        return env_val
    value = getpass.getpass(prompt)
    if confirm:
        again = getpass.getpass("confirm passphrase: ")
        if value != again:
            raise ValueError("passphrases did not match")
    return value


def cmd_vault_status(args) -> int:
    """Report whether this PORTUNUS_HOME has ever been initialized --
    absence of BOTH registry.json and vault-bindings.json (design-
    discussion.md §5, portunus-vault-trust-and-access). A vault with
    either file present has been used before (even a single `portunus
    drop`/`bindings set` creates one) and must never be treated as
    uninitialized again, regardless of how empty it looks. Drives the
    Standalone UI's first-run setup wizard -- checked here, in Python,
    rather than duplicated as filesystem logic in TypeScript."""
    base = home()
    initialized = (base / "registry.json").exists() or (base / "vault-bindings.json").exists()
    if args.json:
        print(json.dumps({"initialized": initialized}))
        return 0
    print("initialized" if initialized else "not yet initialized (first run)")
    return 0


def cmd_vault_export(args) -> int:
    """Coordinated, passphrase-locked snapshot of the vault's critical-state
    surface (registry.json, master.key, vault.enc.json, vault-bindings.json,
    rotation-bindings.json/gcp-bindings.json if present, audit.log) -- see
    backup.py. CLI-only: no MCP tool, no UI surface (design-discussion.md
    §6) -- an archive containing every secret in the vault should never be
    triggerable by an LLM-facing tool without a human directly initiating
    it."""
    try:
        passphrase = _resolve_passphrase("export passphrase: ", confirm=True)
    except ValueError as exc:
        return _err(str(exc))
    out_path = Path(args.out) if args.out else Path.cwd() / "portunus-vault-export.pvault"
    try:
        path = export_archive(out_path, passphrase)
    except ExportError as exc:
        return _err(str(exc))
    # Recorded AFTER the archive is written -- the snapshot inside
    # export_archive() already captured audit.log's prior state, so this
    # entry becomes the vault's next real append, not part of what got
    # archived. Never the passphrase, never a secret value -- only the
    # archive path (informational, matches every other audit entry's
    # metadata-only discipline).
    _, audit, _, _ = _build()
    audit.append("vault_export", "-", f"exported -> {path}")
    print(f"exported vault -> {path}")
    return 0


def cmd_vault_import(args) -> int:
    """Reverse of `vault export` -- see backup.py::import_archive(). Fails
    closed on a wrong passphrase; refuses a target that already has vault
    state present unless --force (full replace, never a merge)."""
    try:
        passphrase = _resolve_passphrase("import passphrase: ")
    except ValueError as exc:
        return _err(str(exc))
    try:
        written = import_archive(Path(args.archive), passphrase, force=args.force)
    except ExportError as exc:
        return _err(str(exc))
    # Recorded AFTER the restore, against the now-restored audit.log/.clock
    # -- continues that chain forward as its next real append, the same way
    # `vault_export` continues the source vault's chain rather than being
    # folded into what was archived.
    _, audit, _, _ = _build()
    audit.append("vault_import", "-", f"imported {len(written)} file(s) from {args.archive}")
    print(f"imported {len(written)} file(s): {', '.join(written)}")
    return 0


def cmd_vault_access_export(args) -> int:
    """Scoped, plain-JSON, metadata-only bundle of registry+bindings info
    (never a secret value -- see vault_transfer.py) so a second Portunus
    instance can gain working access without a full-vault backup/restore.
    Distinct from `vault export` (backup.py): never passphrase-locked,
    because it structurally cannot contain a secret value."""
    registry, audit, _, _ = _build()
    vault_bindings = load_vault_bindings()
    rotation_bindings = load_rotation_bindings()
    try:
        bundle = build_bundle(
            registry, vault_bindings, rotation_bindings,
            project=args.project, org=args.org, tags=args.tags,
        )
    except ValueError as exc:
        return _err(str(exc))
    path = write_bundle(bundle, args.out)
    audit.append(
        "vault_access_export", "-",
        f"exported {len(bundle['references'])} reference(s) -> {path}",
    )
    print(f"exported {len(bundle['references'])} reference(s) -> {path}")
    return 0


def cmd_vault_access_import(args) -> int:
    """Reverse of `vault access export` -- see vault_transfer.py::import_bundle().
    A per-reference conflict never aborts the batch (matches drop_bulk's own
    precedent); pass --force to overwrite a conflicting entry."""
    try:
        raw = Path(args.bundle).read_text()
    except OSError as exc:
        return _err(f"could not read bundle: {exc}")
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _err(f"invalid bundle JSON: {exc}")

    registry, audit, _, _ = _build()
    vault_bindings = load_vault_bindings()
    rotation_bindings = load_rotation_bindings()
    report = import_bundle(bundle, registry, vault_bindings, rotation_bindings, force=args.force)
    save_vault_bindings(vault_bindings)
    save_rotation_bindings(rotation_bindings)

    audit.append(
        "vault_access_import", "-",
        f"created={len(report['created'])} updated={len(report['updated'])} "
        f"conflicted={len(report['conflicted'])} skipped={len(report['skipped'])} "
        f"from {args.bundle}",
    )
    print(
        f"created {len(report['created'])}, updated {len(report['updated'])}, "
        f"skipped {len(report['skipped'])}, conflicted {len(report['conflicted'])}"
    )
    for conflict in report["conflicted"]:
        print(
            f"  conflict: {conflict['name']} -- "
            f"existing sm_name={conflict['existing_sm_name']!r} backend={conflict['existing_backend']!r} "
            f"vs bundle sm_name={conflict['new_sm_name']!r} backend={conflict['new_backend']!r} "
            f"(use --force to overwrite)"
        )
    return 0


def cmd_vault_access_verify(args) -> int:
    """Real per-reference reachability check -- see vault_transfer.py::
    verify_access(). CLI-only, no MCP tool (design-discussion.md §4):
    triggers real backend API calls across potentially every reference in
    the registry on one invocation, a human-initiated batch operation the
    same way `vault export`/`import` already are."""
    registry, _, _, resolver = _build(project=args.project or "")
    vault_bindings = load_vault_bindings()
    report = verify_access(registry, resolver, vault_bindings, project=args.project)

    print(
        f"reachable {len(report['reachable'])}, "
        f"needs-drop {len(report['needs_drop'])}, "
        f"needs-auth {len(report['needs_auth'])}, "
        f"failed {len(report['failed'])}"
    )
    for entry in report["needs_drop"]:
        print(f"  {entry['name']}: {entry['hint']}")
    for entry in report["needs_auth"]:
        print(f"  {entry['name']}: {entry['hint']}")
    for entry in report["failed"]:
        print(f"  {entry['name']}: {entry['hint']}")
    return 0


def _view_to_dict(view) -> dict:
    return {"name": view.name, "description": view.description, "ref_names": view.ref_names}


def cmd_views_create(args) -> int:
    try:
        view = create_view(args.name, description=args.description or "")
    except ViewError as exc:
        return _err(str(exc))
    print(f"created view {view.name!r}")
    return 0


def cmd_views_add(args) -> int:
    try:
        view = add_to_view(args.name, args.ref_name)
    except ViewError as exc:
        return _err(str(exc))
    print(f"{args.ref_name} -> {view.name} ({len(view.ref_names)} reference(s))")
    return 0


def cmd_views_remove(args) -> int:
    try:
        view = remove_from_view(args.name, args.ref_name)
    except ViewError as exc:
        return _err(str(exc))
    print(f"{args.ref_name} removed from {view.name} ({len(view.ref_names)} reference(s))")
    return 0


def cmd_views_delete(args) -> int:
    print("deleted" if delete_view(args.name) else "no such view")
    return 0


def cmd_views_show(args) -> int:
    views = load_views()
    if args.name:
        view = views.get(args.name)
        if view is None:
            if args.json:
                print(json.dumps({}))
            else:
                print(f"no such view: {args.name}")
            return 0
        views = {args.name: view}
    if args.json:
        print(json.dumps({name: _view_to_dict(v) for name, v in views.items()}))
        return 0
    if not views:
        print("(no views configured)")
        return 0
    for name, v in views.items():
        print(f"  {name}  ({v.description or 'no description'})  -- {len(v.ref_names)} reference(s)")
        for ref_name in v.ref_names:
            print(f"      {{{{secret:{ref_name}}}}}")
    return 0


def _policy_to_dict(p) -> dict:
    return {
        "scope_type": p.scope_type, "scope_value": p.scope_value, "role": p.role,
        "actions": p.actions, "principal": p.principal,
    }


def cmd_roles_set(args) -> int:
    """Writes genuinely persist to roles.json and, as of portunus-petitio-
    rbac Story 02, feed an audit-only evaluation on every resolve -- but
    still never enforced (raised on) until Story 03's opt-in flag. See
    roles.py's own module docstring."""
    actions = [a.strip() for a in args.actions.split(",") if a.strip()] if args.actions else []
    try:
        record = set_policy(args.scope_type, args.scope_value, args.role, actions, principal=args.principal)
    except PolicyError as exc:
        return _err(str(exc))
    _, audit, _, _ = _build()
    audit.append("roles_config_changed", "-", f"set {record.key}")
    print(f"set policy {record.key}: actions={record.actions}")
    return 0


def cmd_roles_delete(args) -> int:
    existed = delete_policy(args.scope_type, args.scope_value, args.role, principal=args.principal)
    if existed:
        _, audit, _, _ = _build()
        audit.append("roles_config_changed", "-", f"deleted {args.scope_type}:{args.scope_value}:{args.role}:{args.principal or '*'}")
    print("deleted" if existed else "no such policy")
    return 0


def cmd_roles_show(args) -> int:
    policies = load_policies()
    if args.scope_type:
        policies = {k: p for k, p in policies.items() if p.scope_type == args.scope_type}
    if args.scope_value:
        policies = {k: p for k, p in policies.items() if p.scope_value == args.scope_value}
    if args.json:
        print(json.dumps({k: _policy_to_dict(p) for k, p in policies.items()}))
        return 0
    if not policies:
        print("(no policies configured -- roles are audit-only, not enforced yet)")
        return 0
    print("NOTE: roles are audit-only -- would-allow/would-deny is logged on every resolve, but never enforced (raised on) yet.")
    for k, p in policies.items():
        principal_note = f" principal={p.principal}" if p.principal else " principal=* (everyone)"
        print(f"  {k}  actions={p.actions}{principal_note}")
    return 0


def cmd_roles_enforce(args) -> int:
    """portunus-petitio-rbac Story 03. Default: off. A scope with zero
    configured policies always allows regardless of this setting --
    enforcement only ever narrows behavior for a scope that has at least
    one policy record (design-discussion.md §5, "permissive-if-
    unconfigured"). Scoped per PORTUNUS_HOME/--home automatically, same as
    roles.json itself."""
    if args.state == "status":
        print(f"enforcement: {'on' if enforcement_is_on() else 'off'}")
        return 0
    set_enforcement(args.state == "on")
    _, audit, _, _ = _build()
    audit.append("roles_config_changed", "-", f"enforce {args.state}")
    print(f"enforcement: {args.state}")
    return 0


def cmd_crawl(args) -> int:
    """Discovery only -- bundles known context for references missing
    metadata, for an LLM/human to read and call `portunus metadata
    confirm`/`portunus_suggest_metadata` against. Never writes a Reference
    field itself, never touches a value."""
    registry, *_ = _build()
    candidates = crawl_candidates(registry, org=args.org, project=args.project)
    if args.json:
        print(json.dumps(candidates))
        return 0
    if not candidates:
        print("(no candidates -- every matching reference already has description/purpose/org)")
        return 0
    for c in candidates:
        print(f"  {{{{secret:{c['name']}}}}}  sm_name={c['sm_name']}  group={c['group'] or '-'}")
    return 0


def cmd_report(args) -> int:
    """Render current vault state as Markdown -- a real 'deploy docs'
    starting point, independent of whether crawl ever found anything.
    Read-only, metadata-only -- never a value."""
    registry, *_ = _build()
    report = generate_report(registry, org=args.org, project=args.project)
    if args.out:
        Path(args.out).write_text(report)
        print(f"wrote report -> {args.out}")
        return 0
    print(report)
    return 0


def cmd_leak_scan(args) -> int:
    """Scan configured local paths for occurrences of managed secret
    values -- advisory only, never blocks check_injectable/resolve, never
    auto-rotates. Exits non-zero when new findings are found (useful for a
    CI/cron invocation)."""
    registry, audit, broker, resolver = _build()
    result = run_scan(registry, broker, resolver.backend, backend_for=resolver.backend_for)

    if not result.configured_paths and not result.configured_repos:
        if args.json:
            print(json.dumps({"configured": False, "findings": []}))
        else:
            print(
                "(no scan paths or repos configured -- `portunus leak-scan config add-path "
                "<glob>` or `add-repo <path>` first)"
            )
        return 0

    audit.append("leak-scan", "*", f"{len(result.findings)}-new-findings")
    for finding in result.findings:
        audit.append("leak-scan-finding", finding.ref_name, finding.path)

    if args.json:
        print(json.dumps([
            {"ref_name": f.ref_name, "path": f.path, "line_number": f.line_number}
            for f in result.findings
        ]))
    elif not result.findings:
        print("no new findings")
    else:
        for f in result.findings:
            print(f"  LEAK  {{{{secret:{f.ref_name}}}}}  {f.path}:{f.line_number}")
    return 1 if result.findings else 0


def cmd_leak_scan_config_add_path(args) -> int:
    add_scan_path(args.glob)
    print(f"added -> {args.glob}")
    return 0


def cmd_leak_scan_config_remove_path(args) -> int:
    remove_scan_path(args.glob)
    print(f"removed -> {args.glob}")
    return 0


def cmd_leak_scan_config_show(args) -> int:
    paths = load_scan_paths()
    if args.json:
        print(json.dumps(paths))
        return 0
    if not paths:
        print("(no scan paths configured)")
        return 0
    for p in paths:
        print(f"  {p}")
    return 0


def cmd_leak_scan_config_add_repo(args) -> int:
    add_scan_repo(args.repo_path)
    print(f"added repo -> {args.repo_path}")
    return 0


def cmd_leak_scan_config_remove_repo(args) -> int:
    remove_scan_repo(args.repo_path)
    print(f"removed repo -> {args.repo_path}")
    return 0


def cmd_leak_scan_config_show_repos(args) -> int:
    repos = load_scan_repos()
    if args.json:
        print(json.dumps(repos))
        return 0
    if not repos:
        print("(no repos configured)")
        return 0
    for r in repos:
        print(f"  {r}")
    return 0


def cmd_leak_status(args) -> int:
    detail = getattr(args, "detail", False)
    statuses = load_leak_status()
    if args.name:
        status = statuses.get(args.name)
        if status:
            summary = summarize(status, detail=detail)
        else:
            summary = {
                "ref_name": args.name, "severity": None, "finding_count": 0,
                "first_detected_at": None, "last_detected_at": None,
            }
            if detail:
                summary["findings"] = []
                summary["distinct_files"] = 0
        if args.json:
            print(json.dumps(summary))
            return 0
        if summary["severity"] is None:
            print(f"{args.name}: no active findings")
        else:
            print(f"{args.name}: {summary['severity']} ({summary['finding_count']} finding(s))")
        return 0

    active = {name: s for name, s in statuses.items() if s.findings}
    if args.json:
        print(json.dumps([summarize(s, detail=detail) for s in active.values()]))
        return 0
    if not active:
        print("(no references with active leak findings)")
        return 0
    for name in sorted(active):
        summary = summarize(active[name])
        print(f"  {name}: {summary['severity']} ({summary['finding_count']} finding(s))")
    return 0


def cmd_leak_mark_rotated(args) -> int:
    """A human's own assertion that `name` has been rotated at its
    provider -- Portunus cannot verify this independently. Clears active
    findings and resets the escalation clock."""
    _, audit, _, _ = _build()
    mark_rotated(args.name)
    audit.append("leak-mark-rotated", args.name, "ok")
    print(f"marked rotated -> {args.name}")
    return 0


def cmd_metadata_confirm(args) -> int:
    """Accept an agent-suggested field -- applies it via the SAME retag()
    a manual edit would use (no second write path to drift from), then
    clears the sidecar entry. Never a value; description/purpose/tags/group
    only (Registry.SUGGESTIBLE_FIELDS)."""
    registry, audit, _, _ = _build()
    try:
        ref = registry.require(args.name)
    except KeyError:
        return _err(f"unknown reference: {args.name}")
    suggestion = ref.suggested.get(args.field)
    if suggestion is None:
        return _err(f"no pending suggestion for {args.field!r} on {args.name}")
    try:
        registry.retag(args.name, **{args.field: suggestion["value"]})
    except AmbiguousMatch as exc:
        return _err(f"confirming {args.field!r} would collide with: {', '.join(exc.candidates)}")
    registry.clear_suggestion(args.name, args.field)
    audit.append("metadata_confirmed", ref.sm_name, f"{args.field} confirmed (suggested by {suggestion['by']})")
    print(f"confirmed {args.field} for {{{{secret:{args.name}}}}}")
    return 0


def cmd_metadata_reject(args) -> int:
    registry, audit, _, _ = _build()
    try:
        ref = registry.require(args.name)
    except KeyError:
        return _err(f"unknown reference: {args.name}")
    if args.field not in ref.suggested:
        return _err(f"no pending suggestion for {args.field!r} on {args.name}")
    registry.clear_suggestion(args.name, args.field)
    audit.append("metadata_rejected", ref.sm_name, f"{args.field} rejected")
    print(f"rejected {args.field} suggestion for {{{{secret:{args.name}}}}}")
    return 0


def cmd_metadata_pending(args) -> int:
    """List every reference with at least one pending suggestion --
    metadata only, no values (suggestions are never secret values anyway)."""
    registry, *_ = _build()
    result = {}
    for ref in registry:
        if ref.suggested:
            result[ref.name] = {k: v for k, v in ref.suggested.items()}
    if args.json:
        print(json.dumps(result))
        return 0
    if not result:
        print("(no pending suggestions)")
        return 0
    for name, fields in result.items():
        for field_name, info in fields.items():
            print(f"  {{{{secret:{name}}}}}  {field_name}: {info['value']!r} (suggested by {info['by']})")
    return 0


def _build_tree(refs, key_fn=None):
    """refs -> (ungrouped_names, nested_tree_dict, refs_meta_dict).

    `key_fn(ref) -> str` supplies the path to nest under -- defaults to
    `ref.group` (unchanged behavior for every existing caller). A reference
    whose key_fn returns "" lands in `ungrouped` -- never silently dropped
    (Grill H1), same guarantee regardless of which facet is active.
    `related` entries not present in `refs` (the already-filtered result
    set) are marked unresolved, never dropped or erroring -- metadata
    consistency is informational, not fail-closed.
    """
    if key_fn is None:
        key_fn = lambda r: r.group  # noqa: E731
    names = {r.name for r in refs}
    ungrouped = []
    tree: dict = {}
    refs_meta: dict = {}
    for r in refs:
        refs_meta[r.name] = {
            "sm_name": r.sm_name,
            "description": r.description,
            "related": [
                {"name": rel, "unresolved": rel not in names} for rel in r.related
            ],
        }
        path = key_fn(r) or ""
        segments = [s for s in path.split("/") if s] if path else []
        if not segments:
            ungrouped.append(r.name)
            continue
        node = tree
        for seg in segments:
            node = node.setdefault(seg, {})
        node.setdefault("_refs", []).append(r.name)
    return ungrouped, tree, refs_meta


def _related_suffix(name: str, refs_meta: dict) -> str:
    related = refs_meta.get(name, {}).get("related", [])
    if not related:
        return ""
    parts = [
        f"{r['name']}{' (unresolved)' if r['unresolved'] else ''}" for r in related
    ]
    return "  related: " + ", ".join(parts)


def _render_tree_text(ungrouped: list, tree: dict, refs_meta: dict, bucket_label="(ungrouped)") -> str:
    lines: list = []
    if ungrouped:
        lines.append(bucket_label)
        for name in sorted(ungrouped):
            lines.append(f"  {name}{_related_suffix(name, refs_meta)}")

    def walk(node: dict, indent: int) -> None:
        for name in sorted(node.get("_refs", [])):
            lines.append(f"{'  ' * indent}{name}{_related_suffix(name, refs_meta)}")
        for seg in sorted(k for k in node if k != "_refs"):
            lines.append(f"{'  ' * indent}{seg}/")
            walk(node[seg], indent + 1)

    walk(tree, 0)
    return "\n".join(lines)


_TREE_KEY_FNS = {
    "group": (lambda r: r.group, "(ungrouped)"),
    "repo": (lambda r: r.repo, "(no repo set)"),
}


def cmd_tree(args) -> int:
    """LLM-facing relationship/hierarchy query -- metadata only, never a
    value. Every reference with an empty key for the active facet (--by)
    renders under a bucket rather than being silently dropped (Grill H1).
    --by group (the default, unchanged from before this flag existed) nests
    by the free-text group path; --by repo nests by the structured repo
    field instead -- same builder/renderer, different key."""
    registry = Registry()
    refs = list(registry)
    if args.project:
        refs = [r for r in refs if r.project == args.project]
    key_fn, bucket_label = _TREE_KEY_FNS[args.by]
    if not refs:
        if args.json:
            print(json.dumps({"ungrouped": [], "tree": {}, "refs": {}}))
        else:
            print("no references to show")
        return 0
    ungrouped, tree, refs_meta = _build_tree(refs, key_fn=key_fn)
    if args.json:
        print(json.dumps({"ungrouped": sorted(ungrouped), "tree": tree, "refs": refs_meta}))
        return 0
    print(_render_tree_text(ungrouped, tree, refs_meta, bucket_label=bucket_label))
    return 0


def cmd_mcp(args) -> int:
    """Start the Portunus MCP stdio server -- a third OSTIARIUS entry point
    (alongside this CLI and the UI's API routes) so other agents/harnesses
    can query and inject secrets directly."""
    from .mcp_server import run_server
    run_server()
    return 0


def cmd_agent_init(args) -> int:
    """Wire the MCP server + usage skills into every detected (or
    explicitly --harness'd) agent CLI on this machine. Idempotent -- safe to
    re-run any time, e.g. after installing a new harness."""
    only = args.harness or None
    result = agent_setup.agent_init(only=only)
    if args.json:
        print(json.dumps(result))
        return 0
    for name, present in result["harnesses"].items():
        if only is not None and name not in only:
            continue
        if not present:
            print(f"{name}: not found on this machine, skipped")
            continue
        registered = result["mcp_registered"].get(name)
        print(f"{name}: MCP server {'registered' if registered else 'FAILED to register'}")
    if result["skills_installed"]:
        print(f"skills installed/updated: {', '.join(result['skills_installed'])}")
    elif "claude" in result["requested"]:
        print("skills: already up to date")
    return 0


def cmd_agent_status(args) -> int:
    """Report which agent CLIs are present, which have the MCP server
    registered, and which usage skills are installed -- never mutates
    anything (use `agent init` for that)."""
    status = agent_setup.agent_status()
    if args.json:
        print(json.dumps(status))
        return 0
    for name, present in status["harnesses"].items():
        if not present:
            print(f"{name}: not found on this machine")
            continue
        registered = status["mcp_registered"].get(name)
        print(f"{name}: present, MCP server {'registered' if registered else 'NOT registered'}")
    installed = [name for name, ok in status["skills"].items() if ok]
    missing = [name for name, ok in status["skills"].items() if not ok]
    print(f"skills installed: {', '.join(installed) if installed else '(none)'}")
    if missing:
        print(f"skills missing: {', '.join(missing)}")
    return 0


def cmd_update_check(args) -> int:
    """Live, read-only check against GitHub releases -- never installs
    anything. Always fresh; ignores any cached result from a background
    check."""
    result = update_mod.check_now()
    if args.json:
        print(json.dumps(result))
        return 0
    if result["error"]:
        print(f"couldn't check for updates: {result['error']}")
        return 1
    if result["update_available"]:
        print(f"portunus {result['latest']} is available (you have {result['current']})")
        print("run `portunus update run` to upgrade")
    else:
        print(f"up to date (v{result['current']})")
    return 0


def cmd_update_run(args) -> int:
    """The one mutating path in this whole feature. Always re-checks live
    first (never trusts a stale cache to decide whether to install
    something), refuses on a dev/editable checkout, and requires either an
    interactive confirmation or --yes before ever calling apply_update --
    never a silent unattended swap, same posture as the desktop app."""
    if update_mod.is_dev_checkout():
        print("this looks like a dev/editable install (inside a git checkout) -- "
              "run `git pull` instead, `update run` refuses to touch it")
        return 1
    result = update_mod.check_now()
    if result["error"]:
        print(f"couldn't check for updates: {result['error']}")
        return 1
    if not result["update_available"]:
        print(f"already up to date (v{result['current']})")
        return 0
    tag = result["latest"]
    if not args.yes:
        if not sys.stdin.isatty():
            print(f"{tag} is available (you have {result['current']}) -- re-run with --yes to install non-interactively")
            return 1
        answer = input(f"{tag} is available (you have {result['current']}). Install now? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("not updating")
            return 0
    ok = update_mod.apply_update(tag)
    if not ok:
        print(f"update to {tag} failed")
        return 1
    print(f"updated to {tag}")
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
    a.add_argument("--org", default="", help="organizational umbrella above project, e.g. firefly-events")
    a.add_argument("--project", default="")
    a.add_argument("--description", default="", help="what this secret is")
    a.add_argument("--purpose", default="", help="what this secret is for")
    a.add_argument("--injected-as", default="",
                    help="comma-separated env=target pairs, e.g. prod=env:STRIPE_KEY")
    a.add_argument("--group", default="", help="hierarchical path, e.g. project-y/supabase/auth")
    a.add_argument("--related", default="", help="comma-separated reference names")
    a.add_argument("--repo", default="", help="the git repo that consumes this secret")
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

    rt = sub.add_parser("retag", help="update a reference's org/provider/project/env/tags/metadata in place")
    rt.add_argument("name")
    rt.add_argument("--org", default="", help="organizational umbrella above project, e.g. firefly-events")
    rt.add_argument("--provider", default="")
    rt.add_argument("--project", default="")
    rt.add_argument("--env", default="")
    rt.add_argument("--tags", default="", help="comma-separated k=v pairs, replaces the open tags dict")
    rt.add_argument("--description", default="", help="what this secret is")
    rt.add_argument("--purpose", default="", help="what this secret is for")
    rt.add_argument("--injected-as", default="",
                     help="comma-separated env=target pairs, replaces the injected_as dict")
    rt.add_argument("--group", default="", help="hierarchical path, e.g. project-y/supabase/auth")
    rt.add_argument("--related", default="", help="comma-separated reference names, replaces the related list")
    rt.add_argument("--repo", default="", help="the git repo that consumes this secret")
    rt.add_argument("--source-files", default="",
                     help="comma-separated file paths in that repo, replaces the source_files list")
    rt.set_defaults(func=cmd_retag)

    rtb = sub.add_parser(
        "retag-bulk",
        help="retag every reference whose group starts with a prefix -- backfilling repo/"
             "source_files across many already-grouped references in one call",
    )
    rtb.add_argument("--group-prefix", required=True,
                      help="plain string prefix matched against each reference's group")
    rtb.add_argument("--org", default="", help="organizational umbrella above project, e.g. firefly-events")
    rtb.add_argument("--repo", default="", help="the git repo that consumes these secrets")
    rtb.add_argument("--source-files", default="",
                      help="comma-separated file paths, replaces the source_files list")
    rtb.add_argument("--dry-run", action="store_true",
                      help="report what WOULD change; makes zero writes")
    rtb.add_argument("--json", action="store_true", help="machine-readable output")
    rtb.set_defaults(func=cmd_retag_bulk)

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
    dr.add_argument("--org", default="", help="organizational umbrella above project, e.g. firefly-events")
    dr.add_argument("--provider", default="")
    dr.add_argument("--project", default="")
    dr.add_argument("--env", default="")
    dr.add_argument("--tags", default="", help="comma-separated k=v pairs, e.g. team=platform")
    dr.add_argument("--description", default="", help="what this secret is")
    dr.add_argument("--purpose", default="", help="what this secret is for")
    dr.add_argument("--injected-as", default="",
                     help="comma-separated env=target pairs, e.g. prod=env:STRIPE_KEY")
    dr.add_argument("--group", default="", help="hierarchical path, e.g. project-y/supabase/auth")
    dr.add_argument("--related", default="", help="comma-separated reference names")
    dr.add_argument("--repo", default="", help="the git repo that consumes this secret")
    dr.add_argument("--source-files", default="",
                     help="comma-separated file paths in that repo declaring/referencing this secret")
    dr.add_argument(
        "--backend",
        choices=("", "local", "gcp", "aws", "vault", "infisical", "doppler", "onepassword", "azure"),
        default="",
        help="override which backend this one reference uses (default: '' -- follow the "
             "project's VaultBinding/PORTUNUS_BACKEND as normal)",
    )
    src = dr.add_mutually_exclusive_group(required=True)
    src.add_argument("--stdin", action="store_true", help="read the value from stdin")
    src.add_argument("--value-file", help="read the value from this local file")
    dr.set_defaults(func=cmd_drop)

    drb = sub.add_parser(
        "drop-bulk",
        help="put many secrets INTO Arca at once from a JSON file (local-encrypted); lands state=dropped",
    )
    drb.add_argument("entries_file", help="JSON file: a list of {name, sm_name, value, ...} entries")
    drb.add_argument("--json", action="store_true", help="machine-readable output")
    drb.set_defaults(func=cmd_drop_bulk)

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
    auth_login = auth_sub.add_parser("login", help="wrap `gcloud auth login <email>` -- the one command to remember")
    auth_login.add_argument("email")
    auth_login.set_defaults(func=cmd_auth_login)
    auth_status = auth_sub.add_parser(
        "status", help="cross-reference configured bindings against gcloud's credentialed accounts",
    )
    auth_status.add_argument("--json", action="store_true")
    auth_status.set_defaults(func=cmd_auth_status)

    disc = sub.add_parser(
        "discover",
        help="read-only: list what already exists in a live provider project (never a value)",
    )
    disc.add_argument("--provider", required=True, choices=("gcp",))
    disc.add_argument("--project", required=True)
    disc.add_argument("--register", action="store_true",
                       help="write not-yet-registered secrets as state=requested placeholders")
    disc.add_argument("--json", action="store_true", help="machine-readable output (UI consumer)")
    disc.set_defaults(func=cmd_discover)

    bnd = sub.add_parser("bindings", help="configure per-project vault bindings (backend/sync/account/WIF audience)")
    bnd_sub = bnd.add_subparsers(dest="action", required=True)
    bnd_set = bnd_sub.add_parser("set", help="upsert a project's binding -- only passed fields change")
    bnd_set.add_argument("project")
    bnd_set.add_argument(
        "--backend",
        choices=("local", "gcp", "aws", "vault", "infisical", "doppler", "onepassword", "azure"),
        default="",
        help="which vault backend serves this project's secrets (default: gcp; "
             "vault/infisical/doppler/onepassword/azure are stubs, not yet implemented)",
    )
    bnd_set.add_argument("--sync-mode", choices=("direct", "cached"), default="",
                          help="direct = live-fetch every access (default); cached = recency-aware pull-only sync-down")
    bnd_set.add_argument("--account", default="",
                          help="local gcloud CLI identity to use for this project, e.g. user@example.com")
    bnd_set.add_argument("--wif-audience", default="", help="WIF provider resource name")
    bnd_set.set_defaults(func=cmd_bindings_set)
    bnd_show = bnd_sub.add_parser("show", help="show one or all bindings (real values, not presence-only)")
    bnd_show.add_argument("project", nargs="?", default="")
    bnd_show.add_argument("--json", action="store_true")
    bnd_show.set_defaults(func=cmd_bindings_show)

    rbnd = sub.add_parser(
        "rotation-bindings",
        help="configure per-provider rotation provenance (status/account) -- "
             "every provider is a stub today, this is config only, no real rotation ever fires",
    )
    rbnd_sub = rbnd.add_subparsers(dest="action", required=True)
    rbnd_set = rbnd_sub.add_parser("set", help="upsert a provider's rotation binding -- only passed fields change")
    rbnd_set.add_argument("provider", help="e.g. vercel, github, stripe")
    rbnd_set.add_argument("--status", choices=("", "real", "stub"), default="",
                           help="whether a real RotationAdapter exists for this provider (default: stub)")
    rbnd_set.add_argument("--account", default="",
                           help="free-text rotation context, e.g. a Vercel team slug or GitHub org")
    rbnd_set.set_defaults(func=cmd_rotation_bindings_set)
    rbnd_show = rbnd_sub.add_parser("show", help="show one or all rotation bindings")
    rbnd_show.add_argument("provider", nargs="?", default="")
    rbnd_show.add_argument("--json", action="store_true")
    rbnd_show.set_defaults(func=cmd_rotation_bindings_show)

    sy = sub.add_parser(
        "sync", help="force a recency check (and re-fetch if stale) for every cached-mode reference in a project",
    )
    sy.add_argument("project")
    sy.add_argument("--json", action="store_true")
    sy.set_defaults(func=cmd_sync)

    vault = sub.add_parser(
        "vault", help="portable, passphrase-locked vault backup (export/import)",
    )
    vault_sub = vault.add_subparsers(dest="action", required=True)

    vault_status = vault_sub.add_parser(
        "status", help="has this PORTUNUS_HOME ever been initialized -- drives the UI's first-run wizard",
    )
    vault_status.add_argument("--json", action="store_true")
    vault_status.set_defaults(func=cmd_vault_status)

    vault_export = vault_sub.add_parser(
        "export",
        help="export a coordinated, passphrase-locked snapshot of the vault's critical state",
    )
    vault_export.add_argument(
        "--out", help="output archive path (default: ./portunus-vault-export.pvault)",
    )
    vault_export.set_defaults(func=cmd_vault_export)

    vault_import = vault_sub.add_parser("import", help="restore a vault export archive")
    vault_import.add_argument("archive")
    vault_import.add_argument(
        "--force", action="store_true",
        help="replace existing vault state in PORTUNUS_HOME (full replace, not merge)",
    )
    vault_import.set_defaults(func=cmd_vault_import)

    vault_access = vault_sub.add_parser(
        "access",
        help="scoped, plain-JSON access-info transfer between Portunus instances (no secret values)",
    )
    vault_access_sub = vault_access.add_subparsers(dest="access_action", required=True)

    vault_access_export = vault_access_sub.add_parser(
        "export",
        help="export a scoped, plain-JSON bundle of registry+bindings metadata (never a value)",
    )
    vault_access_export.add_argument("--project", default="", help="filter to one project")
    vault_access_export.add_argument("--org", default="", help="filter to one org")
    vault_access_export.add_argument(
        "--tags", default="", help="filter by tag(s), e.g. --tags repo=my-repo,env=prod",
    )
    vault_access_export.add_argument(
        "--out", help="output bundle path (default: ./portunus-vault-access.json)",
    )
    vault_access_export.set_defaults(func=cmd_vault_access_export)

    vault_access_import = vault_access_sub.add_parser(
        "import",
        help="import a scoped access-info bundle -- reconstructs registry entries + bindings",
    )
    vault_access_import.add_argument("bundle", help="path to a bundle written by `vault access export`")
    vault_access_import.add_argument(
        "--force", action="store_true",
        help="overwrite a conflicting entry (different sm_name/backend) instead of refusing it",
    )
    vault_access_import.set_defaults(func=cmd_vault_access_import)

    vault_access_verify = vault_access_sub.add_parser(
        "verify",
        help="real per-reference reachability check -- boundary-safe, never prints a value",
    )
    vault_access_verify.add_argument("--project", default="", help="filter to one project")
    vault_access_verify.set_defaults(func=cmd_vault_access_verify)

    vw = sub.add_parser(
        "views", help="named, human-curated reference collections for ad-hoc task clustering",
    )
    vw_sub = vw.add_subparsers(dest="action", required=True)

    vw_create = vw_sub.add_parser("create", help="create a new, empty view")
    vw_create.add_argument("name")
    vw_create.add_argument("--description", default="")
    vw_create.set_defaults(func=cmd_views_create)

    vw_add = vw_sub.add_parser("add", help="add a reference to a view (idempotent)")
    vw_add.add_argument("name")
    vw_add.add_argument("ref_name")
    vw_add.set_defaults(func=cmd_views_add)

    vw_remove = vw_sub.add_parser("remove", help="remove a reference from a view (idempotent)")
    vw_remove.add_argument("name")
    vw_remove.add_argument("ref_name")
    vw_remove.set_defaults(func=cmd_views_remove)

    vw_delete = vw_sub.add_parser("delete", help="delete a view entirely")
    vw_delete.add_argument("name")
    vw_delete.set_defaults(func=cmd_views_delete)

    vw_show = vw_sub.add_parser("show", help="show one or all views")
    vw_show.add_argument("name", nargs="?", default="")
    vw_show.add_argument("--json", action="store_true")
    vw_show.set_defaults(func=cmd_views_show)

    rl = sub.add_parser(
        "roles",
        help="STUB ONLY -- role/policy config surface, not enforced by check_injectable/retag "
             "yet (Petitio's future access-level engine, portunus-vault-trust-and-access)",
    )
    rl_sub = rl.add_subparsers(dest="action", required=True)

    rl_set = rl_sub.add_parser("set", help="create/update a policy record (writes persist; not enforced)")
    rl_set.add_argument("--scope-type", required=True, choices=VALID_SCOPE_TYPES)
    rl_set.add_argument("--scope-value", required=True)
    rl_set.add_argument("--role", required=True)
    rl_set.add_argument("--actions", default="", help="comma-separated, e.g. read,test,prod-release")
    rl_set.add_argument("--principal", default="", help="which agent/identity this applies to (default: everyone)")
    rl_set.set_defaults(func=cmd_roles_set)

    rl_delete = rl_sub.add_parser("delete", help="delete a policy record")
    rl_delete.add_argument("--scope-type", required=True, choices=VALID_SCOPE_TYPES)
    rl_delete.add_argument("--scope-value", required=True)
    rl_delete.add_argument("--role", required=True)
    rl_delete.add_argument("--principal", default="", help="must match what --principal was set to (default: everyone)")
    rl_delete.set_defaults(func=cmd_roles_delete)

    rl_show = rl_sub.add_parser("show", help="show policy records (optionally filtered)")
    rl_show.add_argument("--scope-type", choices=VALID_SCOPE_TYPES, default="")
    rl_show.add_argument("--scope-value", default="")
    rl_show.add_argument("--json", action="store_true")
    rl_show.set_defaults(func=cmd_roles_show)

    rl_enforce = rl_sub.add_parser(
        "enforce",
        help="opt-in enforcement -- when on, check_injectable() raises NotAuthorized on a "
             "would-deny decision (default: off; a scope with no configured policy always "
             "still allows regardless of this setting)",
    )
    rl_enforce.add_argument("state", choices=("on", "off", "status"))
    rl_enforce.set_defaults(func=cmd_roles_enforce)

    cr = sub.add_parser(
        "crawl",
        help="discovery only -- bundle known context for references missing metadata, for an "
             "LLM/human to review and call `metadata confirm`/portunus_suggest_metadata against",
    )
    cr.add_argument("--org", default="")
    cr.add_argument("--project", default="")
    cr.add_argument("--json", action="store_true")
    cr.set_defaults(func=cmd_crawl)

    rp = sub.add_parser(
        "report", help="render current vault state as Markdown -- a real 'deploy docs' starting point",
    )
    rp.add_argument("--org", default="")
    rp.add_argument("--project", default="")
    rp.add_argument("--out", default="")
    rp.set_defaults(func=cmd_report)

    ls = sub.add_parser(
        "leak-scan",
        help="scan configured local paths for occurrences of managed secret values -- "
             "advisory only, never automatic, never blocks check_injectable/resolve",
    )
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_leak_scan)
    ls_sub = ls.add_subparsers(dest="leak_scan_action")

    ls_config = ls_sub.add_parser("config", help="manage configured scan paths")
    ls_config_sub = ls_config.add_subparsers(dest="config_action", required=True)

    ls_config_add = ls_config_sub.add_parser("add-path", help="add a scan-path glob")
    ls_config_add.add_argument("glob")
    ls_config_add.set_defaults(func=cmd_leak_scan_config_add_path)

    ls_config_remove = ls_config_sub.add_parser("remove-path", help="remove a scan-path glob")
    ls_config_remove.add_argument("glob")
    ls_config_remove.set_defaults(func=cmd_leak_scan_config_remove_path)

    ls_config_show = ls_config_sub.add_parser("show", help="show configured scan paths")
    ls_config_show.add_argument("--json", action="store_true")
    ls_config_show.set_defaults(func=cmd_leak_scan_config_show)

    ls_config_add_repo = ls_config_sub.add_parser(
        "add-repo", help="add a git repo to scan its full history (all branches, all commits)",
    )
    ls_config_add_repo.add_argument("repo_path")
    ls_config_add_repo.set_defaults(func=cmd_leak_scan_config_add_repo)

    ls_config_remove_repo = ls_config_sub.add_parser("remove-repo", help="remove a configured git repo")
    ls_config_remove_repo.add_argument("repo_path")
    ls_config_remove_repo.set_defaults(func=cmd_leak_scan_config_remove_repo)

    ls_config_show_repos = ls_config_sub.add_parser("show-repos", help="show configured git repos")
    ls_config_show_repos.add_argument("--json", action="store_true")
    ls_config_show_repos.set_defaults(func=cmd_leak_scan_config_show_repos)

    lk = sub.add_parser("leak", help="query/manage leak-scan findings for a reference")
    lk_sub = lk.add_subparsers(dest="action", required=True)

    lk_status = lk_sub.add_parser("status", help="show current leak severity + finding counts")
    lk_status.add_argument("name", nargs="?", default="")
    lk_status.add_argument("--json", action="store_true")
    lk_status.add_argument(
        "--detail", action="store_true",
        help="include the raw per-finding path/line list and a distinct-files count",
    )
    lk_status.set_defaults(func=cmd_leak_status)

    lk_rotated = lk_sub.add_parser(
        "mark-rotated",
        help="mark a reference's leak findings as resolved -- a human assertion, not verified",
    )
    lk_rotated.add_argument("name")
    lk_rotated.set_defaults(func=cmd_leak_mark_rotated)

    md = sub.add_parser(
        "metadata",
        help="confirm/reject agent-suggested description/purpose/tags/group "
             "(portunus_suggest_metadata MCP tool's human-review counterpart)",
    )
    md_sub = md.add_subparsers(dest="action", required=True)

    md_confirm = md_sub.add_parser("confirm", help="accept a pending suggestion -- applies it via retag()")
    md_confirm.add_argument("name")
    md_confirm.add_argument("field", choices=SUGGESTIBLE_FIELDS)
    md_confirm.set_defaults(func=cmd_metadata_confirm)

    md_reject = md_sub.add_parser("reject", help="discard a pending suggestion -- live field untouched")
    md_reject.add_argument("name")
    md_reject.add_argument("field", choices=SUGGESTIBLE_FIELDS)
    md_reject.set_defaults(func=cmd_metadata_reject)

    md_pending = md_sub.add_parser("pending", help="list every reference with a pending suggestion")
    md_pending.add_argument("--json", action="store_true")
    md_pending.set_defaults(func=cmd_metadata_pending)

    tr = sub.add_parser(
        "tree",
        help="render secrets by group hierarchy + related links (metadata only, never a value)",
    )
    tr.add_argument("--project", default="")
    tr.add_argument("--by", choices=("group", "repo"), default="group",
                     help="which field to nest by (default: group, unchanged from before this flag existed)")
    tr.add_argument("--json", action="store_true", help="machine-readable output (UI/LLM consumer)")
    tr.set_defaults(func=cmd_tree)

    mcp_p = sub.add_parser(
        "mcp", help="start the Portunus MCP stdio server (for other agents/harnesses)",
    )
    mcp_p.set_defaults(func=cmd_mcp)

    ag = sub.add_parser(
        "agent",
        help="wire the MCP server + usage skills into agent CLIs already on this machine "
             "(Claude Code, Codex CLI today) -- the single-command onboarding path",
    )
    ag_sub = ag.add_subparsers(dest="action", required=True)

    ag_init = ag_sub.add_parser(
        "init", help="register the MCP server and install usage skills -- idempotent, safe to re-run",
    )
    ag_init.add_argument(
        "--harness", action="append", choices=("claude", "codex"), default=None,
        help="limit to this harness (repeatable); default: every harness detected on this machine",
    )
    ag_init.add_argument("--json", action="store_true")
    ag_init.set_defaults(func=cmd_agent_init)

    ag_status = ag_sub.add_parser("status", help="show what's currently wired -- never mutates anything")
    ag_status.add_argument("--json", action="store_true")
    ag_status.set_defaults(func=cmd_agent_status)

    up = sub.add_parser(
        "update",
        help="self-update the CLI -- checks GitHub releases via `gh`, never a silent unattended install",
    )
    up_sub = up.add_subparsers(dest="action", required=True)

    up_check = up_sub.add_parser("check", help="live, read-only check -- never installs anything")
    up_check.add_argument("--json", action="store_true")
    up_check.set_defaults(func=cmd_update_check)

    up_run = up_sub.add_parser(
        "run", help="check, then install if newer -- requires an interactive confirm or --yes",
    )
    up_run.add_argument("--yes", action="store_true", help="install without prompting (for scripts/cron)")
    up_run.set_defaults(func=cmd_update_run)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # `mcp` is a long-running stdio server (a passive-check subprocess/stderr
    # write would be pointless at best, protocol-adjacent noise at worst);
    # `update` already does its own live check -- a stale passive notice on
    # top of it would be confusing, not helpful.
    skip_notify = args.cmd in ("mcp", "update")
    if not args.home:
        rc = args.func(args)
        if not skip_notify:
            update_mod.maybe_notify()
        return rc

    # --home overrides PORTUNUS_HOME for this invocation only. paths.home()
    # reads the env fresh on every call, so setting it here (and restoring
    # it afterward) is sufficient to route every Registry()/AuditChain()
    # construction site -- including any that don't go through _build() --
    # without threading an override parameter through each one by hand.
    prior = os.environ.get("PORTUNUS_HOME")
    os.environ["PORTUNUS_HOME"] = args.home
    try:
        rc = args.func(args)
        if not skip_notify:
            update_mod.maybe_notify()
        return rc
    finally:
        if prior is None:
            os.environ.pop("PORTUNUS_HOME", None)
        else:
            os.environ["PORTUNUS_HOME"] = prior


if __name__ == "__main__":
    raise SystemExit(main())
