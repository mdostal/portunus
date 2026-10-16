# Process & Design

The rest of this site covers how to **use** Portunus. This section covers how it was
**designed**: the reasoning, trade-offs and lessons behind each feature, published from the
planning artifacts kept in the repo's `.pHive/` folder.

## How Portunus is built

Each feature is planned as an **epic** by the Hive planning process before code is written.
An epic normally goes through these steps:

1. **Research brief**: what already exists (in this codebase and elsewhere), the constraints,
   and the open questions.
2. **Design discussion**: the decisions, each with the alternatives that were rejected and
   why. This is the core of the design record.
3. **Grill record**: an adversarial review of the design before it is approved.
4. **Horizontal / vertical plans and stories**: the design broken down into test-first (TDD)
   stories, each built on its own branch and reviewed before merge.
5. **Lessons learned**: written after shipping, when the build showed something the plan
   missed.

## What is published here

Only the part that tells the design story:

- **Foundations**: the project context and glossary (OSTIARIUS, ARCA, Petitio, and the
  rest), the structured outline of the standalone core that the later epics build on, the
  research synthesis behind per-agent access control, and the visual-planning HTML sidecars.
- **Design records**: one page per epic, containing its design discussion plus its lessons
  learned and cost-benefit analysis where those exist. Each page links to the full planning
  folder on GitHub.

Research briefs, grill records, plans and per-story YAML stay in the repo. They are the
working churn of each epic and would bury the decisions they led to.

!!! warning "Records, not reference"
    Each design record reflects intent at the time it was written. Some designs changed
    during the build, and a few epics were re-scoped or replaced. When a record disagrees
    with the usage docs or the code, the usage docs and the code win.

The section is generated at build time by `scripts/mkdocs_process_hook.py` (curation rules in
`scripts/process_docs.py`), so a new epic's design discussion appears here on the next
deploy with no manual step.

## Why this site uses MkDocs Material (evaluated 2026-10)

This docs site was evaluated against **Astro Starlight**. It stays on **MkDocs Material**
for now:

- **Same toolchain as the project.** Portunus is a Python package. MkDocs is one
  `pip install`, while Starlight would add a second Node/Astro toolchain to the docs build
  (the `ui/` Next.js app is a separate product surface with its own lifecycle).
- **The existing deploy is already safe.** `gh-pages` root is a hand-built landing page, and
  the docs workflow publishes only into its `docs/` subdirectory, without force-pushing.
  Switching generators would not improve that.
- **Branding works without a theme fork.** The landing page's palette and typefaces are
  applied through CSS variables in `docs/stylesheets/portunus.css`.
- **The content is mostly long technical prose.** Starlight's main advantages, its
  component islands and i18n, aren't needed here.

**Known risk:** MkDocs 2.0 is announced as backward-incompatible and drops the plugin and
theme systems that Material depends on, and the Material team now points to Zensical as
its successor. The build pins `mkdocs<2` and `mkdocs-material<10`. The curation logic is
plain Python with no MkDocs imports, so if a migration is needed (to Zensical, which reads
`mkdocs.yml`, or to Starlight), only the small `scripts/mkdocs_process_hook.py` adapter needs rewriting.
