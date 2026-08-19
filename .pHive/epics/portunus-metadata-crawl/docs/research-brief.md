# Research Brief — portunus-metadata-crawl

## 1. The ask

User (verbatim, across two messages): *"we need a default crawl and attempt to gather the
metadata FOR the projects by going back and forth with the LLM implementation or the UI in
portunus so that we can basically pre-fill the vaults that are crawled and added by pointing at
github repos, other credentials, other workers, and anything else -- portunus should be the top
level config expert and then let the human verify so that we aren't reading the key itself."*

Clarifying answer on "other workers": *"probably poorly worded but i meant things using the
keys -- deploy workers, github actions, vercel, things using the WIF, integrations, etc and
basically trying to see what we have as we get the parts in place to make a more complete
picture. It should also be able to be done from solid documentation and ideally when we get
this, it can act as documentation and give out a report for us to make deploy docs on a company
that doesn't have them."*

Two confirmed pieces: (1) a crawl that infers/proposes metadata for existing references without
ever reading the secret value itself, using the already-shipped suggest/confirm workflow so a
human still verifies everything; (2) the crawl's own findings should be assemblable into a
human-readable **report** — "deploy docs" a company without any could start from.

## 2. What already exists — verified, not assumed

- **The write path is already built.** `Registry.suggest_metadata(name, by, fields)` +
  `portunus_suggest_metadata` (MCP tool) + `portunus metadata confirm/reject/pending` (CLI) +
  `DetailDrawer`'s confirm/reject UI (all portunus-vault-trust-and-access, just shipped) are the
  complete "propose, never write directly, human confirms" mechanism this epic needs. A crawl
  is a NEW CONSUMER of this — not a new write path. `retag()` stays the only code path that
  ever writes a live field.
- **Bulk suggestion was explicitly named and deferred**, in this exact codebase, as recently as
  the previous epic's own design discussion: *"Should retag-bulk's existing bulk path also gain
  a requester/suggestion mode... Deliberately deferred past this epic's own Slice 6... leaving
  bulk-apply as an explicit human-confirmed follow-up."* This epic IS that follow-up.
- **Nothing crawl-related exists today** — confirmed by grep across `src/`/`ui/app` for
  "crawl"/"enrich"/scan-adjacent terms. Every GitHub reference in the codebase today is
  incidental (issue-template URLs, `GitHubRotationAdapter` — a rotation stub, not a scanner).
- **"Workers/consumers" already have SOME structural signal in the vault today**, just not
  exposed as metadata inference sources yet: `VaultBinding.wif_audience`/`.account` (which
  identity/WIF pool a project's GCP access goes through), `RotationBinding.provider`/`.account`
  (which external system — Vercel, GitHub, Stripe — a reference's rotation would target, when
  configured). Both are per-project/per-provider, not per-reference, but are real, already-
  collected "who/what uses this" context a crawl can read (config, never a value) before ever
  needing to touch an external repo.

## 3. Real vault fill-rate — the single most consequential finding

Checked directly against the real vault (`~/.portunus`, 393 live references, read-only):

| field | filled | % |
|---|---|---|
| `group` | 356 | 91% |
| `description` | 13 | 3% |
| `purpose` | 3 | 1% |
| `repo` | 1 | <1% |
| `org` | 0 | 0% |

This inverts the obvious plan. "Clone the GitHub repo named in `repo` and grep for context" has
**almost nothing to anchor on** — only 1 reference in the entire vault names a repo at all. The
field that's actually populated, at real scale, is `group` — hierarchical paths like
`demo-cicd/event-api/prod` already encoding project/app/env structure a naming-convention parser
can mine for `description`/`purpose`/`org` inference RIGHT NOW, with zero external access
required. Real external-repo crawling (GitHub Actions workflows, Vercel config, deploy scripts)
is genuinely valuable — the user named it directly and it's real, wanted work — but it's gated
on `repo` actually getting populated first, which a `group`-based first pass can itself help
with (inferring a likely `repo` value from `group`'s own path segments, as a suggestion, same as
every other field).

## 4. The report/documentation angle — a second, related but distinct deliverable

*"It can act as documentation and give out a report... to make deploy docs on a company that
doesn't have them"* is not the same ask as metadata inference — it's a rendering/synthesis step
on top of whatever the vault (crawled or not) already knows: which secrets exist, grouped by
org/project/env, what (if anything) is known to consume each (from `VaultBinding`/
`RotationBinding`/confirmed `repo`/`source_files`), and where gaps remain. Valuable
independently of whether the crawl found anything new — a `portunus report` command run against
today's vault, even with zero crawl-inferred metadata, already has real information to render
(393 references, their org/project/group structure, provider/backend routing). The crawl makes
the report richer over time; it doesn't gate the report's own existence.

## 5. Scope for v1, and what's explicitly out

**In scope**: (a) a bulk metadata-suggestion pass driven by `group`/`sm_name` naming-convention
parsing — the new, real, immediately-actionable consumer of the existing suggest/confirm
machinery; (b) surfacing `VaultBinding`/`RotationBinding`'s existing "who/what consumes this"
data as suggestible/reportable context, without inventing a new store; (c) a `portunus report`
command rendering current vault state (crawled or not) as a human-readable Markdown document.

**Explicitly deferred, real but bigger**: actual external-repo cloning/scanning (GitHub Actions
YAML parsing, Vercel project API calls, docker-compose/k8s manifest scanning) — gated on `repo`
having real fill-rate first, which v1's group-based pass works toward. Named directly by the
user as wanted, not rejected — just correctly sequenced after the naming-convention pass proves
out the same suggest/confirm/report pipeline on data that already exists.
