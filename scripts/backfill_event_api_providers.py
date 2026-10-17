#!/usr/bin/env python3
"""Backfill the real `provider` on event-api's third-party event-source
references (PANT-1126 / GH #203).

`event-api-{env}-{meetup,eventbrite,ticketmaster,seatgeek}-*` were imported
with `provider=gcp` -- the vault backend, not the service -- so they can't be
grouped, queried, rotated or liveness-checked by provider. This sets
`provider` to the service named in the reference name. Nothing else changes:
tags, backend, values are untouched (`Registry.retag(provider=...)` only),
and each write is audited as a `retag`, same as `portunus retag`.

Dry run by default; `--apply` writes. Re-running is a no-op.

    python3 scripts/backfill_event_api_providers.py            # what would change
    python3 scripts/backfill_event_api_providers.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import List, Tuple

PROVIDERS = ("meetup", "eventbrite", "ticketmaster", "seatgeek")
NAME_RE = re.compile(r"^event-api-[a-z0-9]+-(%s)-[a-z0-9-]+$" % "|".join(PROVIDERS))


def plan(registry) -> List[Tuple[str, str, str]]:
    """(name, current provider, new provider) for every reference to change."""
    changes = []
    for ref in registry:
        m = NAME_RE.match(ref.name)
        if m and ref.provider != m.group(1):
            changes.append((ref.name, ref.provider, m.group(1)))
    return sorted(changes)


def apply(registry, audit, changes) -> Tuple[List[str], List[dict]]:
    from portunus.registry import AmbiguousMatch

    updated, failed = [], []
    for name, _old, new in changes:
        try:
            ref = registry.retag(name, provider=new)
        except AmbiguousMatch as exc:
            failed.append({"name": name, "error": f"would collide with: {', '.join(exc.candidates)}"})
            continue
        audit.append("retag", ref.sm_name, "ok")
        updated.append(name)
    return updated, failed


def main(argv=None) -> int:
    from portunus import AuditChain, Registry

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    registry = Registry()
    changes = plan(registry)
    if not args.apply:
        if args.json:
            print(json.dumps({"would_update": [{"name": n, "from": o, "to": p} for n, o, p in changes]}))
        else:
            for name, old, new in changes:
                print(f"  would retag  {name}  provider {old or '(none)'} -> {new}")
            print(f"{len(changes)} reference(s); re-run with --apply to write")
        return 0

    updated, failed = apply(registry, AuditChain(), changes)
    if args.json:
        print(json.dumps({"updated": updated, "failed": failed}))
    else:
        for name in updated:
            print(f"  retagged  {name}")
        for entry in failed:
            print(f"  failed    {entry['name']}: {entry['error']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
