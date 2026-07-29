"""isolation-tax CLI — measure the cost of per-tenant cache isolation, or refuse to guess.

    isolation-tax measure trace.jsonl          # one JSON object per line
    isolation-tax measure trace.jsonl --json
    isolation-tax demo

Each line needs `hash_ids`, and should carry `input_length`, `output_length`, `timestamp`.
Add `tenant` to every line and the answer becomes EXACT rather than a lower bound.

Exit codes match the rest of this portfolio:
    0  measured
    1  measured, and the tax exceeds --fail-over
    2  ABSTAIN — nothing was measured, which is never a pass
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import BOUNDED, DEFAULT_TOK_PER_S, measure


class Abstain(Exception):
    """Nothing was measured. Exit 2 — never 0, and never 1."""


def _load(path: str) -> list:
    if path == "-":
        text = sys.stdin.read()
    else:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            raise Abstain(f"cannot read {path!r}: {e}. Nothing was measured.")
    rows, bad = [], 0
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    if not rows:
        raise Abstain(
            f"no usable request objects in {path!r} ({bad} unparseable line(s)). Expected JSONL: "
            f"one JSON object per line, each with `hash_ids`. A trace that parses to nothing has "
            f"no isolation tax, and reporting 0 would be a vacuous pass.")
    return rows


def _cmd_measure(a) -> int:
    rows = _load(a.trace)
    try:
        res = measure(rows, tok_per_s=a.tok_per_s)
    except ValueError as e:
        raise Abstain(str(e))

    if a.json:
        print(json.dumps(res.to_dict(), indent=2))
    else:
        d = res.to_dict()
        print(f"  mode              {res.mode.upper()}"
              + ("" if res.is_exact else "   (a FLOOR, not the answer — see below)"))
        print(f"  requests          {res.n_requests:,}"
              + (f"   tenants {res.n_tenants:,}" if res.n_tenants else ""))
        print(f"  blocks read       {res.total_blocks:,}")
        print(f"  cache hits        {res.total_hits:,}"
              + (f"   ({res.total_hits/res.total_blocks:.1%})" if res.total_blocks else ""))
        print(f"    universal       {res.universal_hits:,}   (shared system prompt)")
        print(f"    content         {res.content_hits:,}")
        print()
        verb = "COSTS" if res.is_exact else "COSTS AT LEAST"
        print(f"  >>> per-tenant isolation {verb} {res.lost_hits:,} cache hits")
        if res.tax_share_of_reuse is not None:
            print(f"      = {res.tax_share_of_reuse:.1%} of your cache benefit")
            print(f"      = {res.added_prefill_share:.1%} added to total prefill")
        if res.lost_universal_hits:
            print(f"      plus {res.lost_universal_hits:,} shared-prompt cold starts, reported "
                  f"apart (a per-tenant fixed cost, not lost sharing)")
        for n in res.notes:
            print(f"\n  note: {n}")

    if a.fail_over is not None and res.tax_share_of_reuse is not None:
        if res.tax_share_of_reuse > a.fail_over:
            print(f"\nFAIL — tax {res.tax_share_of_reuse:.1%} exceeds --fail-over "
                  f"{a.fail_over:.1%}", file=sys.stderr)
            return 1
    return 0


def _cmd_demo(a) -> int:
    """Two tenants sharing one document — the case isolation actually destroys."""
    doc = [101, 102, 103]
    rows = [
        {"hash_ids": [0, 900, 901], "input_length": 1024, "output_length": 50,
         "timestamp": -1000, "tenant": "acme"},          # no document: keeps `doc` non-universal
        {"hash_ids": [0] + doc + [201], "input_length": 2048, "output_length": 100,
         "timestamp": 0, "tenant": "acme"},
        {"hash_ids": [0] + doc + [202], "input_length": 2048, "output_length": 100,
         "timestamp": 1000, "tenant": "globex"},
        {"hash_ids": [0] + doc + [203], "input_length": 2048, "output_length": 100,
         "timestamp": 2000, "tenant": "initech"},
    ]
    res = measure(rows)
    print("  three tenants, each sending the SAME 3-block document with a different tail:\n")
    print(f"    cache hits {res.total_hits}   universal {res.universal_hits}   "
          f"content {res.content_hits}")
    print(f"    shared-prompt cold start   {res.lost_universal_hits}")
    print(f"    isolation destroys {res.lost_hits} content hits "
          f"({res.tax_share_of_reuse:.0%} of the benefit) — EXACT, because tenants were labelled")
    print("\n  Drop the `tenant` field and the same trace can only be BOUNDED:")
    res2 = measure([{k: v for k, v in r.items() if k != "tenant"} for r in rows])
    print(f"    mode {res2.mode.upper()}   floor {res2.lost_hits} hits "
          f"({res2.tax_share_of_reuse:.0%})")
    print("\n  That gap is why this tool wants your labels: with them the answer is a count,")
    print("  without them it is a bound, and no public trace carries them.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="isolation-tax",
        description="what does per-tenant KV-cache isolation cost you?")
    ap.add_argument("--version", action="version", version=f"isolation-tax {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("measure", help="measure the tax on a JSONL trace")
    m.add_argument("trace", help="path to a JSONL trace, or - for stdin")
    m.add_argument("--json", action="store_true")
    m.add_argument("--tok-per-s", type=float, default=DEFAULT_TOK_PER_S,
                   help="assumed generation rate for the timing constraint (BOUNDED mode only). "
                        "Faster = weaker constraint = more conservative floor.")
    m.add_argument("--fail-over", type=float, metavar="FRAC",
                   help="exit 1 if the tax exceeds this fraction of cache benefit (e.g. 0.20)")
    m.set_defaults(fn=_cmd_measure)

    d = sub.add_parser("demo", help="a worked example, exact vs bounded")
    d.set_defaults(fn=_cmd_demo)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except Abstain as e:
        print(f"ABSTAIN — nothing was measured.\n  {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
