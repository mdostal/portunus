# Contributing to Portunus

We welcome contributions to Portunus! Here's how you can help.

## Code of Conduct

Be respectful and constructive. This project is released with a Contributor Code of Conduct;
by participating you agree to abide by its terms.

## Submitting pull requests

1. Fork the repo and create your branch from `dev` (not `main` — see the Development section
   in README.md for this repo's git flow).
2. If you've added code, add tests. This project follows TDD throughout — see `tests/` for the
   existing style and coverage.
3. Ensure the full test suite passes (`pytest`) and, for UI changes, `cd ui && npm run build`
   is clean.
4. Keep the secret-boundary invariant intact: a resolved secret value must never be returned,
   logged, printed, or otherwise surfaced outside a boundary sink. If your change touches a
   code path that could see a decrypted value, add a test that proves it doesn't leak (several
   examples in `tests/` use an AST-level structural check for exactly this).
5. Match the existing code style and formatting.

## Issues

Feel free to submit issues and enhancement requests.

## Architecture

See `docs/architecture.md` for how Portunus's components (OSTIARIUS, ARCA, Petitio) fit
together, and `README.md`'s "Architecture, vision & design decisions" section for the reasoning
behind them.
