## What & why

<!-- What does this change, and why? Link an issue if there is one. -->

## Checklist

- [ ] Branched from `dev` (not `main`) — see README's Development section for this repo's git flow
- [ ] Tests added/updated (`pytest`), full suite passes
- [ ] `cd ui && npm run build` is clean, if this touches `ui/`
- [ ] Secret-boundary invariant intact: if this touches a path that could see a decrypted value,
      there's a test proving it never leaks (return value, log, print, exception message, audit
      entry) — including on the failure path, not just the happy one
- [ ] `CHANGELOG.md` updated for anything user-facing
