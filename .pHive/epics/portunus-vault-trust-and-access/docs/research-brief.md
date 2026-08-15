# Research Brief — portunus-vault-trust-and-access

## 1. The ask, as given across two messages

User (verbatim, combined): *"we need to clean up so we can have keys per REPO and to sort
better and get the keys with where it is used and injected, description meta data, etc across
the board... we may not have them at first, but we can also have a way for a human to VIEW the
key and the info to go through and create that -- they should have a warning if we don't have
extra metadata and some project tags. As it is used and requested, the tags can get updated...
Portunus and the llms can help to tag and improve overall but a human can help nail that down...
similar to the keys, we can have permissions around if they are allowed to update by only
certain roles (human, llm, dev etc)... humans can view and unlock it through the dashboard or
ADD or EDIT keys... We need an about page... and a settings page as well, and the wizard for the
setup with the arca choice, and walk through of the vault..."*

Then, explicitly: *"all of this was in the initial vision, obvs wasn't well defined in the
planning, so we need to fix how we do multi-stage planning epics."* This document + the
horizontal/vertical plan that follows are the fix — the same H/V process `portunus-standalone-
core` (the original foundational epic) used, not the lighter research-brief-only process this
session defaulted to for the smaller epics shipped since.

Deferred, not in scope here: full Apple notarization ("at some point," blocked on a paid Apple
Developer account the user hasn't provisioned — unchanged from `portunus-desktop-app`'s own
deferral).

## 2. What already exists — verified against the real code, not assumed

**Metadata fields**: `Reference` (`registry.py`) already has every field the ask implies —
`description`, `purpose`, `injected_as`, `group`, `related`, `repo`, `source_files`, `tags`,
`project`, `env`, `provider`, `scope`, `kind`. No schema gap for "where it is used and
injected" or "description metadata" — the fields exist. The gap is **fill rate and workflow**,
not schema.

**Real fill-rate reality** (confirmed, not estimated): `portunus-provenance-graph/docs/
research-brief.md` — all 342 real `ffe-cicd` references have `description`/`purpose`/`kind`/
`related`/`injected_as` **empty**. `related` has exactly 2 real data points in the **entire**
385-reference vault. A bulk-backfill tool (`retag-bulk --group-prefix`) was built and dry-run-
verified (91 matches, zero collisions) specifically for this — but the real backfill was
deliberately left unapplied: *"an explicit follow-up for whoever owns that vault to confirm,
not something this release does unsupervised."* This is exactly the user's own framing: *"we
may not have them at first."*

**UI view/edit already exists, partially**: `DetailDrawer.tsx`'s "Move…" form edits `provider`/
`project`/`env`/`description`/`purpose`/`injected_as`/`group`/`related`. `AddSecretForm.tsx`
covers the same set plus `backend`/`tags`/`kind`/`scope` at creation. **A real, live plumbing
gap found**: the CLI (`retag --repo`/`--source-files`) and `Registry.retag()` both already
support `repo`/`source_files`, but `/api/retag/route.ts` never forwards them and neither UI form
has an input for them — the one field pair the user specifically named ("keys per REPO") is
CLI-only today despite everything else around it being wired.

**No missing-metadata signal exists anywhere** — grepped `src/portunus`, `ui/app` for
"incomplete"/"missing metadata"/"quality": zero hits. The user's "warning if we don't have extra
metadata and some project tags" is genuinely new.

**The RBAC seam is already designed, not built** — this is the load-bearing finding.
`docs/architecture.md` §3 and `broker.py` already define:

```python
Identity(name: str, kind: Literal["human", "agent", "system"])
check_injectable(name: str, requester: Optional[Identity] = None)
```

`requester` is accepted by `check_injectable` **everywhere it's called** but never enforced —
architecture.md's own words: *"a deliberately inert seam — every caller is currently allowed
regardless of who's asking."* This maps closely to the user's "permissions around if they are
allowed to update by only certain roles (human, llm, dev etc)" — but scoped today to **secret
access** (resolve/inject), not **metadata edits**. `retag()` (the CLI/API path that would touch
`description`/`tags`/`repo`/etc.) has **no requester parameter at all** — confirmed by reading
`Registry.retag()`'s full signature. Activating this seam for metadata edits is new work, but
it's extending an already-designed concept, not inventing one from nothing.

**About/Help page**: confirmed absent. `page.tsx`'s `Tab` type is `"console" | "map" |
"project"` only. README.md's "Why it's safe," "Component model," and "Usage" sections are
substantial, already-written prose — a real starting point for in-app help content, not a
from-scratch writing task.

**Settings page / setup wizard**: confirmed absent. The most recent epic
(`portunus-bindings-settings-ui`) deliberately extended Project Explorer's existing per-project
panel rather than building a dedicated Settings page — a smaller, correctly-scoped choice for
*that* ask. The user is now asking for something broader: a real Settings surface (likely
including role management once RBAC exists) and a first-run wizard (ARCA backend choice + vault
walkthrough on install) — neither was in scope before.

## 3. Why this needs the heavier H/V process, not another research-brief+design-discussion pass

Three genuinely interdependent threads, not one: (a) metadata completeness/quality UX, (b) RBAC
for who can edit what, (c) onboarding/settings/help surfaces. (b) is a real architectural
activation (an inert seam becoming enforced, touching `broker.py`/`registry.py`/every mutating
CLI command/audit) that (a)'s "LLM suggests, human confirms" workflow and (c)'s "who can change
settings" both want to build on. Scoping any one of these in isolation (the way the last two
epics were planned) risks the RBAC model getting designed three different, incompatible ways
across three separate design-discussions. The horizontal plan below inventories every layer
once; the vertical plan sequences the resulting work into dependency-ordered, independently-
shippable slices — the same shape `portunus-standalone-core` used for the original registry+
adapters+UI buildout.
