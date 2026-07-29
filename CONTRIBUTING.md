# Contributing to gridlock

## The one rule

**gridlock measures. It never grants.**

No entry point may admit, refuse, provision, or grant a resource. `certify` returns a
certificate; the caller decides. Enforced in CI. A pull request adding an actuation path will be
rejected — the correct home for that is a downstream scheduler that consumes our certificates.

## The second rule

**A new domain ships with a foil that fires.**

Adding a domain to `src/gridlock/domains.py` means adding *both* a safe arrangement and a foil
that the certifier actually refuses. The test suite asserts this per domain
(`test_every_domain_foil_actually_wedges`). A domain whose foil does not fire makes its own safe
case vacuous, and will not be merged.

A good foil is the *plausible mistake* in that domain — the thing a competent engineer would
actually build — not an obviously broken graph.

## The third rule

**Keep the traversals iterative.**

`find_cycle` and `rank` are iterative by design; the tests include 5,000-node chains. A recursive
rewrite will pass the small tests and crash on real inputs, which means silently not covering
them.

## Practicalities

```bash
pip install -e ".[dev]"
python -m pytest -q
gridlock demo
```

- Zero runtime dependencies. Hard constraint.
- Python 3.9+.
- Don't claim gridlock infers wait-for graphs from source. It does not, and saying so in docs is
  the one change that would make a SAFE verdict actively dangerous.

## Licence

By contributing you agree your contributions are licensed under Apache-2.0.
