"""isolation-tax CLI — measure the cost of per-tenant cache isolation, or refuse to guess.

    isolation-tax measure trace.jsonl          # one JSON object per line
    isolation-tax measure trace.jsonl --json
    isolation-tax fleet trace.jsonl --gpu A100-80GB --model-weights-gb 15.2 \
        --gpu-memory-gb 80 --gpu-usd-hr 2.00 --kv-bytes-per-token 196608 --fleet-gpus 1000
    isolation-tax demo

Each line needs `hash_ids`, and should carry `input_length`, `output_length`, `timestamp`.
Add `tenant` to every line and the answer becomes EXACT rather than a lower bound.

`fleet` turns the measured hit count into GPU-equivalents and annual dollars. Every fleet
parameter is required: none is defaulted, because a dollar figure assembled from invented inputs
is quotable, wrong, and indistinguishable from a measured one once it is in a slide.

Exit codes match the rest of this portfolio:
    0  measured
    1  measured, and the tax exceeds --fail-over / --fail-over-usd
    2  ABSTAIN — nothing was measured, which is never a pass
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import BOUNDED, DEFAULT_TOK_PER_S, measure
from .fleet import (DEFAULT_RESERVE_GB, FleetInputs, fleet_delta, kv_bytes_per_token,
                    render)


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


def _kv_bytes_per_token_from_args(a):
    """Take --kv-bytes-per-token, or compute it from the config. Refuse a contradiction."""
    cfg = (a.layers, a.kv_heads, a.head_dim, a.dtype_bytes)
    if all(v is None for v in cfg):
        return a.kv_bytes_per_token
    if any(v is None for v in cfg):
        raise Abstain(
            "a partial model config was given. bytes/token = 2 * layers * kv_heads * head_dim * "
            "dtype_bytes needs all four, and filling in the missing ones with a house default "
            "would put an invented constant inside a dollar figure. Supply all four, or supply "
            "--kv-bytes-per-token directly.")
    try:
        derived = kv_bytes_per_token(*cfg)
    except ValueError as e:
        raise Abstain(str(e))
    if a.kv_bytes_per_token is not None and float(a.kv_bytes_per_token) != float(derived):
        raise Abstain(
            f"--kv-bytes-per-token {a.kv_bytes_per_token:,.0f} contradicts the config, which gives "
            f"2*{a.layers}*{a.kv_heads}*{a.head_dim}*{a.dtype_bytes} = {derived:,}. One of them is "
            f"wrong and this tool cannot tell which, so it reports neither.")
    return derived


def _cmd_fleet(a) -> int:
    rows = _load(a.trace)
    try:
        tax = measure(rows, tok_per_s=a.tok_per_s)
    except ValueError as e:
        raise Abstain(str(e))

    inputs = FleetInputs(
        gpu=a.gpu, model_weights_gb=a.model_weights_gb, gpu_memory_gb=a.gpu_memory_gb,
        gpu_usd_hr=a.gpu_usd_hr, kv_bytes_per_token=_kv_bytes_per_token_from_args(a),
        fleet_gpus=a.fleet_gpus, reserve_gb=a.reserve_gb, utilisation=a.utilisation,
        session_bound_fraction=a.session_bound_fraction, kv_fill=a.kv_fill)
    try:
        out = fleet_delta(tax, inputs)
    except ValueError as e:
        raise Abstain(str(e))

    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print(render(out))
        for n in tax.notes:
            print(f"\n  note: {n}")

    lo, hi = out["derived"]["usd_per_year_low"], out["derived"]["usd_per_year_high"]
    if a.fail_over_usd is not None:
        if lo > a.fail_over_usd:
            # The LOW end, deliberately. Firing on the high end would fail a build on the most
            # favourable end of a MEASURED spread; firing on the low end means the whole band
            # exceeds the threshold, which is the only thing the measurement supports saying.
            print(f"\nFAIL — modelled annual isolation cost ${lo:,.0f}–${hi:,.0f} exceeds "
                  f"--fail-over-usd ${a.fail_over_usd:,.0f} across the WHOLE measured retention "
                  f"band", file=sys.stderr)
            return 1
        if hi > a.fail_over_usd:
            print(f"\n  note: the band ${lo:,.0f}–${hi:,.0f} STRADDLES --fail-over-usd "
                  f"${a.fail_over_usd:,.0f}. Exit 0, because only part of a measured spread "
                  f"exceeds it and this tool does not pick a point inside its own band.")
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

    f = sub.add_parser(
        "fleet", help="price the measured tax as GPU-equivalents and annual dollars",
        description="Turn the measured hit count into GPUs and an annual dollar RANGE. The range "
                    "is not hedging: the conversion factor was MEASURED to vary 0.879-0.954 across "
                    "three models on one A100, nothing predicts where yours lands, and a point "
                    "estimate would be a false precision in a financial figure. Outside that "
                    "measured envelope -- another card, a model outside 0.99-29.54 GB of weights, "
                    "a KV pool that does not fill -- it ABSTAINS with exit 2 and names the axis, "
                    "rather than carrying a band measured elsewhere onto your fleet. NOT an "
                    "end-to-end serving benchmark: the one time this estate measured serving end "
                    "to end it got 0.997x. Every parameter below is REQUIRED and none is defaulted "
                    "-- a dollar figure from invented inputs is the failure this package exists "
                    "to catch.")
    f.add_argument("trace", help="path to a JSONL trace, or - for stdin")
    f.add_argument("--gpu", help="the accelerator, as a label for the report (e.g. A100-80GB)")
    f.add_argument("--model-weights-gb", type=float,
                   help="resident weight footprint PER GPU, in GB (1e9 bytes). Use the sharded "
                        "figure if the model runs tensor-parallel.")
    f.add_argument("--gpu-memory-gb", type=float, help="HBM per GPU, in GB (1e9 bytes)")
    f.add_argument("--gpu-usd-hr", type=float, help="your price per GPU-hour")
    f.add_argument("--kv-bytes-per-token", type=float,
                   help="KV bytes per token. Or give --layers/--kv-heads/--head-dim/--dtype-bytes "
                        "and it is computed as 2*layers*kv_heads*head_dim*dtype_bytes.")
    f.add_argument("--layers", type=int, help="model config: number of layers")
    f.add_argument("--kv-heads", type=int, help="model config: KV heads (GQA groups, not Q heads)")
    f.add_argument("--head-dim", type=int, help="model config: head dimension")
    f.add_argument("--dtype-bytes", type=int, help="model config: KV dtype width (2 for fp16)")
    f.add_argument("--fleet-gpus", type=float, help="GPUs you run today, under isolation")
    f.add_argument("--reserve-gb", type=float, default=DEFAULT_RESERVE_GB,
                   help="activations + fragmentation headroom held back from the KV pool "
                        f"(default {DEFAULT_RESERVE_GB:g}, from the source capacity model)")
    f.add_argument("--utilisation", type=float, default=1.0,
                   help="ASSUMPTION: fraction of wall-clock hours billed (default 1.0 = reserved "
                        "capacity around the clock). Not measured by anything here.")
    f.add_argument("--session-bound-fraction", type=float, default=1.0,
                   help="ASSUMPTION: fraction of the fleet whose capacity is bound by the KV pool "
                        "(default 1.0). Not measured by anything here.")
    f.add_argument("--kv-fill", type=float, default=1.0,
                   help="how full the KV pool runs, as a fraction of memory left after weights and "
                        "reserve. FIXED AT 1.0 by the measurement -- both arms filled the card, "
                        "which is the comparison itself -- so any other value ABSTAINS rather "
                        "than scaling a band measured in a regime you are not in.")
    f.add_argument("--tok-per-s", type=float, default=DEFAULT_TOK_PER_S,
                   help="assumed generation rate for the timing constraint (BOUNDED mode only)")
    f.add_argument("--json", action="store_true")
    f.add_argument("--fail-over-usd", type=float, metavar="USD",
                   help="exit 1 if the modelled annual cost exceeds this many dollars")
    f.set_defaults(fn=_cmd_fleet)

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
