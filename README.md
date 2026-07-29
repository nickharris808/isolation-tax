# isolation-tax

**Sharing the KV cache across tenants leaks. Isolating costs you money. How much depends on your workload — published figures span an order of magnitude.**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](pyproject.toml)
[![Claims](https://img.shields.io/badge/claims--map-CLEAN-brightgreen.svg)](CLAIMS-MAP.md)

Every multi-tenant LLM provider faces the same forced choice. Share the prefix cache and you leak —
that's a published attack with a CVE ([ICML 2502.07776][paper]; [vLLM PR #17045][pr] /
CVE-2025-46570). Isolate per tenant and you pay full prefill for work another tenant already did.

Everyone chose isolate. **What it costs them depends entirely on their workload — and the
published figures span an order of magnitude.** This measures it on your traffic, inside your
perimeter.

```bash
pip install isolation-tax
```

## 30 seconds

```bash
isolation-tax demo                      # a worked example
isolation-tax measure trace.jsonl       # your trace, one JSON object per line
```

```console
$ isolation-tax measure production.jsonl
  mode              BOUNDED   (a FLOOR, not the answer — see below)
  requests          12,031
  blocks read       288,500
  cache hits        105,710   (36.6%)
    universal       12,030   (shared system prompt)
    content         93,680

  >>> per-tenant isolation COSTS AT LEAST 12,428 cache hits
      = 11.8% of your cache benefit
      = 4.3% added to total prefill
      plus 313 shared-prompt cold starts, reported apart
```

That run is real: the [Mooncake FAST'25][mooncake] production trace, 12,031 requests of live
traffic. To be precise about the quantity — **11.8% of prefix-cache HITS lost**, not a latency and
not an overhead. (SafeKV reports 11.74% for residual TTFT overhead; the numerals nearly collide and
the quantities are unrelated.)

### The number is not a constant, and that is the point

| workload | mean prompt | cross-session reuse lost |
|---|---|---|
| WildChat-1M (consumer chat) | 1,181 tok | **1.4%** |
| arXiv 2605.18825 (ShareGPT-style, *published*) | short chat | 0.72% |
| Mooncake FAST'25 (long-context API) | 12,035 tok | **11.8%** |

Same code, unmodified, across all three rows. A published paper reports cross-session reuse
"below 0.01%" on chat traces; we reproduce its order of magnitude on chat traffic and get 10× more
on long-context traffic.

It is tempting to conclude that prompt length is the driver. **We tested that and it is wrong.**
Bucketing *one* trace by session length — holding service and tenancy fixed — the tax moves the
other way:

| WildChat sessions, by length | max prompt | cross-session lost |
|---|---|---|
| Q1 (shortest) | 40 tok | **65.9%** |
| Q2 | 272 tok | 20.6% |
| Q3 | 983 tok | 6.7% |
| Q4 (longest) | 3,485 tok | **3.4%** |

Monotone *decreasing*: a longer conversation generates more intra-session reuse, and that reuse is
the denominator. So the cross-trace difference is real but its cause is **not established**.

What survives is the part that matters to you: **the tax ranges from 3% to 66% across slices of a
single real workload.** No one number characterises it, and a figure quoted without its workload is
meaningless — which is why the only number worth having is the one from *your* trace.

## Add one field and the bound becomes a count

| | `tenant` field | Answer |
|---|---|---|
| **EXACT** | on every request | the hits that don't survive isolation, **counted** |
| **BOUNDED** | absent | a **floor**, inferred from constraints the trace can't fake |

Public serving traces carry no tenant labels, which is why the published work either uses synthetic
workloads or reports a proxy metric. **You have those labels.** Add `"tenant": "..."` to each line
and you get a count instead of a floor.

## What it never sees

Prompt text. Completions. User identities. It reads **block hashes, two integer lengths, and a
timestamp** — the fields your serving logs already have. The measurement runs where the traffic is,
and a number comes out.

```jsonl
{"timestamp": 0, "input_length": 6758, "output_length": 500, "hash_ids": [0,1,2,3], "tenant": "acme"}
{"timestamp": 1200, "input_length": 7322, "output_length": 490, "hash_ids": [0,9,10], "tenant": "globex"}
```

## How the bounded mode works

A conversation's context grows monotonically. Turn *k+1*'s prompt **contains** turn *k*'s prompt and
its generated output, arrives **after** turn *k* finished generating, and its block chain
**extends** turn *k*'s. A request that violates all three against *every* candidate ancestor is
provably in a different conversation — no labels required.

The chain constraint does the heavy lifting: two users sharing a long document have a common prefix
and then **diverge**, so neither chain contains the other. Adding it moved the measured floor from
8.4% to 11.8%.

It's robust. Sweeping the one free parameter — assumed generation speed — from 50 to 500 tok/s moves
the result by under half a point. The length and chain constraints do the work.

## HONEST SCOPE

| What it **does** establish | What it does **not** |
|---|---|
| In EXACT mode, the hits lost to isolation, counted | Requires you to supply tenancy. Garbage labels, garbage count |
| In BOUNDED mode, a **floor** on cross-**session** sharing | Cross-**tenant** is a strict *subset* — a tenant may own many conversations. The floor equals the tax only under **per-user tenancy** |
| A number derived only from hashes and integers | Nothing about *what* was shared. It cannot tell a system prompt from a leaked document |
| Reuse **counts** | **No time, no dollars.** Converting to cost needs your store-fetch cost, which this does not measure |
| The shared-prompt cold start, reported apart | That cost is real but it is a fixed per-tenant charge, not lost sharing — and it's the one part you can remove by marking the prompt public |

The bounded floor is **not the tax**. Both constraints are *necessary, not sufficient*: two users
sending the same document often satisfy them by coincidence and get counted as possibly-one-session.
The true number is **higher**.

## Exit codes

| code | meaning |
|---|---|
| `0` | measured |
| `1` | measured, and the tax exceeds `--fail-over` |
| `2` | **ABSTAIN** — nothing was measured, which is never a pass |

An empty or unparseable trace exits `2`. It has no isolation tax, and reporting `0%` for one would
be a vacuous pass rather than a measurement.

## Why you'd run this

You are choosing between a leak and a bill, and you currently know the size of neither. If the tax
is small, isolate everything and stop worrying. If it's large, you now know what a
provably-safe sharing scheme is worth to you — and you can put a number on it in a budget meeting.

On the best public trace the floor is **11.8%** and the ceiling — if every content share crosses
sessions — is **88.6%**. Where you sit in that range is a fact about *your* traffic that only you
can measure.

## Licence

Apache-2.0. [`LICENSE-TAG`](LICENSE-TAG) is **CLEAN**: this measures and reports a count. It admits
nothing, refuses nothing, and gates no request.

[paper]: https://arxiv.org/abs/2502.07776
[pr]: https://github.com/vllm-project/vllm/pull/17045
[mooncake]: https://github.com/kvcache-ai/Mooncake
