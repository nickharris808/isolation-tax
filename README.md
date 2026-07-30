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

**Not yet on PyPI.** The command below is the one that works today. It installs from this repository, pinned to a tag.

```bash
pip install "git+https://github.com/nickharris808/isolation-tax@v0.1.0"
```

`pip install isolation-tax` is the intended command once the name is published. **It 404s today**, which is why it is not the first step above. The tag is pinned rather than `@main` so a reader installs the exact code this README documents.

## 30 seconds

```bash
isolation-tax demo                      # a worked example
isolation-tax measure trace.jsonl       # your trace, one JSON object per line
isolation-tax fleet trace.jsonl ...     # the same trace, in GPUs and dollars
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

## `fleet` — the same trace, in GPUs and dollars

A percentage does not survive contact with a budget meeting. `fleet` turns the measured hit count
into GPU-equivalents and an annual figure, using the KV working-set ratio the trace already gives
you and a capacity band that was **measured on real hardware, three models on one A100-80GB**.

```bash
isolation-tax fleet trace.jsonl \
  --gpu A100-80GB --model-weights-gb 29.54 --gpu-memory-gb 80 --gpu-usd-hr 2.00 \
  --layers 48 --kv-heads 8 --head-dim 128 --dtype-bytes 2 \
  --fleet-gpus 1000
```

```console
  MEASURED (your trace, by isolation-tax measure)
    mode                        BOUNDED   (a FLOOR — see below)
    requests                    12,031
    blocks read / cache hits    288,500 / 105,710
    hits lost to isolation      12,428   (+313 shared-prompt cold starts)
    KV blocks stored  shared    182,790
                    isolated    195,531
    keep = shared/isolated      0.934839

  MEASURED ELSEWHERE (this estate's A100, NOT your trace)
    capacity ratio = (1/keep) ** exponent, both arms filling the card
    retention band              0.8792 – 0.9539   MEASURED, three models, one GPU, at keep 0.5282
    carried to your keep as     exponent 0.7983 – 0.9261   (a form choice, see NOT MODELLED)
      Qwen2.5-0.5B-Instruct        0.99 GB  batch 1,492 -> 2,827  ratio 1.665x  retention 0.879
      Qwen2.5-7B-Instruct         15.23 GB  batch   264 ->   501  ratio 1.704x  retention 0.900
      Qwen2.5-14B-Instruct        29.54 GB  batch    61 ->   116  ratio 1.806x  retention 0.954

  MEASURED ENVELOPE (outside it this tool ABSTAINS rather than transplanting a band)
    GPU                         A100-80GB, 80–85.095 GB, ctx 4096
    model weights per GPU       0.988–29.540 GB
    KV pool fill                1 (both arms fill the card)

  SUPPLIED BY YOU (echoed so you can re-run the arithmetic)
    model weights               29.54 GB
    GPU memory                  80 GB
    reserve (activations/frag)  4 GB
    KV bytes per token          196,608
    price per GPU-hour          $2.0000
    fleet size                  1,000 GPUs

  ASSUMED BY YOU (not measured by anything here; the answer moves with these)
    utilisation                 1
    session-bound fraction      1
    KV pool fill                1   (fixed by the measurement)

  DERIVED
    KV pool per GPU             46.46 GB   = 236,308 tokens resident
    ceiling 1/keep              1.0697x   — an identity, NOT a prediction, see below
    capacity ratio band         1.0553x – 1.0644x   = ceiling ** measured exponent
    r_max                       1.5728   — sizes the pool only; FALSIFIED as a predictor

    GPUs, isolated arm          1,000.0   (what you run)
    GPUs, shared arm            939.5 – 947.6   (the arm that LEAKS)

  >>> per-tenant isolation costs 52.4 – 60.5 GPU-equivalents
      = $105 – $121/hour
      = $917.5K – $1.06M/year   at your stated price and utilisation
```

That run uses the Mooncake counts from the table above, on a Qwen2.5-14B (the model the ladder's top
rung actually ran). Reproduce it with `statefabric/fixtures/mooncake_timed_trace.json`. **The
`NOT MODELLED` block is elided here; the tool prints it every time.**

**Outside the envelope it does not answer.** A 70B, an H100, a KV pool that runs part empty — each
exits `2` and names the axis. The band was measured in one place and carrying it elsewhere would be
quoting someone else's hardware as if it were yours.

### Three kinds of number, kept apart on purpose

| block | where it came from |
|---|---|
| **MEASURED** | your trace. `keep` = `(blocks − hits) / (blocks − hits + lost hits + cold starts)`. A count, not a model |
| **MEASURED ELSEWHERE** | *this estate's* A100-80GB, not yours. Three models run twice each, every arm at its own maximum resident batch: **1.665× / 1.704× / 1.806×**. A ratio of timed runs, not a fit. Outside the envelope those runs covered, the tool **abstains** |
| **SUPPLIED / ASSUMED** | yours. Utilisation, session-bound fraction and KV fill are **not measured by anything here**, and every one is printed so your team can re-run the arithmetic |

### A ceiling that is arithmetic, times a retention that is measured

`1/keep` is the working-set ratio restated — it cannot depend on the model or the accelerator, and
this estate has already retracted one headline that was an identity wearing a prediction's clothes.
It is the **ceiling**, printed but never reported as the answer. What the ladder measured is how much
of that ceiling survives the compute crossover at the larger batch: **87.9% to 95.4%**. Both numbers
are printed, so the gap is visible rather than asserted.

**A model this README used to teach, and the measurement that killed it.** Earlier versions
converted with `(1+r)/(1+r*keep)` at `r_max = (memory − weights − reserve)/weights`, and concluded in
prose that *"isolation costs most where there is memory headroom."* The A100 ladder falsified it in
**direction** — predicted 1.87 / 1.61 / 1.41 against measured **1.66 / 1.70 / 1.80**. `r_max` governs
a different comparison (same batch, smaller KV); when *both* arms fill the card it does not enter at
all. The closed form was **removed rather than tuned**, and it is no longer importable.

The retention was measured at one `keep` (0.5282) and is carried to yours in log space —
`ratio = (1/keep) ** exponent`. That form is a **modelling choice**, printed as one under
`NOT MODELLED`. Multiplying instead was tried and is wrong: it returns 0.879× at `keep = 1`, pricing
a *negative* saving on a problem of size zero — isolation cost nothing, so nothing was lost and
there is nothing to recover.

**RETRACTION (2026-07-30) — the ≥ 1.0× floor is not a claim that elision is never slower.** Earlier
text here and in `fleet.py` justified that floor with `elision_never_slower`, machine-checked in
`serving_limits/formal/lean/CertifiedElision.lean`. That citation is **withdrawn**: the theorem is a
proof over a *declared* per-object cost model in `Nat`, it says nothing about wall-clock, and this
estate has measured otherwise. `results/data/statefabric/gpu/elision_throughput_l4_0p5b.json` (L4,
Qwen2.5-0.5B, ctx 4096) reports `throughput_ratio_same_batch` **below 1.0× in 4 of its 9 cells** —
0.9908 / 0.9466 / 0.9009 / 0.9941 at batch 2 / 4 / 8 / 16, worst **0.9009× at batch 8** — crossing
back above 1.0× only from batch 32. The floor is a property of the form `(1/keep) ** positive` and
of a ladder whose every arm ran above batch 32 (1492/2827, 264/501, 61/116). **The band therefore
has no support at small batch and cannot express the regime where elision measured slower**; that is
printed under `SMALL BATCH` in `NOT MODELLED` on every run.

### It abstains rather than guessing

No fleet parameter is defaulted. Omit one and you get exit `2` and a list of what is missing, not a
number. A dollar figure assembled from invented inputs is quotable, wrong, and indistinguishable
from a measured one once it is in a slide — which is the exact failure this package exists to catch.
A zero or negative fleet abstains too, rather than dividing by it.

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
| Reuse **counts** | **`measure` reports no time and no dollars.** `fleet` converts, and only under the parameters you supply |
| The shared-prompt cold start, reported apart | That cost is real but it is a fixed per-tenant charge, not lost sharing — and it's the one part you can remove by marking the prompt public |
| `fleet`: a **capacity/throughput model** over a measured working-set ratio | **Not an end-to-end serving benchmark.** The one time this estate measured serving end to end it got **0.997×** — below 1.0 — and published it as a falsification. Nothing here claims anything runs faster |
| `fleet`: the **KV-residency** channel | **Not the added prefill compute.** Recomputing the lost hits is real work and it is *not* in the dollar figure |
| `fleet`: a cumulative working-set ratio over the trace | **Not an instantaneous residency.** No eviction is modelled, and a fleet already past the compute crossover sees less than the figure |
| `fleet`: your inputs, echoed in full | A BOUNDED trace pushes the figure **down**; `--kv-fill 1.0` pushes it **up**. The tool does **not** net them out. It is a model, not a bound in either direction |

The bounded floor is **not the tax**. Both constraints are *necessary, not sufficient*: two users
sending the same document often satisfy them by coincidence and get counted as possibly-one-session.
The true number is **higher**.

## Exit codes

| code | meaning |
|---|---|
| `0` | measured |
| `1` | measured, and the tax exceeds `--fail-over` (or `--fail-over-usd` on `fleet`) |
| `2` | **ABSTAIN** — nothing was measured, which is never a pass |

An empty or unparseable trace exits `2`. It has no isolation tax, and reporting `0%` for one would
be a vacuous pass rather than a measurement. `fleet` exits `2` on a missing fleet parameter, a zero
or negative fleet, a model that does not fit the card, a `--kv-bytes-per-token` that contradicts
the config it was also given, or **a fleet outside the envelope the capacity band was measured in** —
naming every axis that is out of range, not just the first.

## Why you'd run this

You are choosing between a leak and a bill, and you currently know the size of neither. If the tax
is small, isolate everything and stop worrying. If it's large, you now know what a
provably-safe sharing scheme is worth to you — and `fleet` puts that in the units a budget meeting
runs on, with every input on the page so your own team can check the arithmetic.

On the best public trace the floor is **11.8%** and the ceiling — if every content share crosses
sessions — is **88.6%**. Where you sit in that range is a fact about *your* traffic that only you
can measure.

## Licence

Apache-2.0. [`LICENSE-TAG`](LICENSE-TAG) is **CLEAN**: this measures and reports a count. It admits
nothing, refuses nothing, and gates no request.

[paper]: https://arxiv.org/abs/2502.07776
[pr]: https://github.com/vllm-project/vllm/pull/17045
[mooncake]: https://github.com/kvcache-ai/Mooncake

**Provenance.** The 11.8% Mooncake floor and the 0.24% WildChat per-user figure resolve to
`results/data/statefabric/isolation_tax.json` and
`results/data/statefabric/isolation_tax_replication.json`, both registered in
`oss/provenance.py`. The capacity band resolves to
`results/data/statefabric/gpu/elision_throughput.json`.
