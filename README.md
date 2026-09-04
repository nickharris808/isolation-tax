# isolation-tax

**What does per-tenant KV-cache isolation cost you? Measure it on your own traffic — the prompts never leave.**

[![tests](https://github.com/nickharris808/isolation-tax/actions/workflows/tests.yml/badge.svg)](https://github.com/nickharris808/isolation-tax/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](pyproject.toml)

Share the prefix cache across tenants and you leak. Isolate per tenant and you pay full prefill for
work someone else already did. This counts what that choice costs on **your** traffic — from block
hashes and integers only, so no prompt text ever leaves your perimeter.

## Install

**Not yet on PyPI.** The command below is the one that works today. It installs from this repository, pinned to a tag.

```bash
pip install "git+https://github.com/nickharris808/isolation-tax@v0.1.0"
```

`pip install isolation-tax` is the intended command once the name is published. **It 404s today**, which is why it is not the first step above. The tag is pinned rather than `@main` so a reader installs the exact code this README documents.

Zero runtime dependencies. Python 3.9+.

---

## Why this exists — the number is not a constant, and everyone quotes it as one

Every multi-tenant LLM provider faces the same forced choice. Share the prefix cache across tenants
and you leak — that is a published attack with a CVE ([ICML 2502.07776][paper]; [vLLM PR
#17045][pr] / CVE-2025-46570). Isolate per tenant and you pay full prefill for work another tenant
already did.

Everyone chose isolate. **What it costs them depends entirely on their workload — and published
figures span an order of magnitude.**

| workload | mean prompt | cross-session reuse lost |
|---|---|---|
| WildChat-1M (consumer chat) | 1,181 tok | **1.4%** |
| arXiv 2605.18825 (ShareGPT-style, *published*) | short chat | 0.72% |
| Mooncake FAST'25 (long-context API) | 12,035 tok | **11.8%** |

Same code, unmodified, across all three rows.

It is tempting to conclude that prompt length is the driver. **We tested that and it is wrong.**
Bucketing *one* trace by session length — holding service and tenancy fixed, whole conversations to
a bucket — the tax moves the other way
(`results/data/statefabric/isolation_tax_stratified.json`):

| WildChat sessions, by length | mean prompt | cross-session lost |
|---|---|---|
| Q1 (shortest) | 40 tok | **65.9%** |
| Q2 | 272 tok | 20.6% |
| Q3 | 983 tok | 6.7% |
| Q4 (longest) | 3,485 tok | **3.4%** |

Monotone *decreasing*: a longer conversation generates more intra-session reuse, and that reuse is
the denominator. So the cross-trace difference is real but its cause is **not established** — and an
earlier version of this README that asserted "the variable is prompt length" is withdrawn, in the
certificate as well as here.

What survives is the part that matters to you: **the tax ranges from 3% to 66% across slices of a
single real workload.** No one number characterises it, a figure quoted without its workload is
meaningless, and the only number worth having is the one from *your* trace.

> **A novelty claim we withdrew.** An earlier version of `core.py` said nobody had published what
> cache isolation costs. That is false. arXiv 2506.02634 (USENIX ATC'25) publishes a user-by-user
> cache-hit heatmap on two production traces; PrefixWall (arXiv 2603.10726) plots
> isolation-vs-global hit rate on synthetic workloads. The narrower claim that survives: no prior
> work reports this on a **released production trace**.

## 30-second quickstart

Every block below is pasted from a real run, exit codes included.

```bash
isolation-tax demo                      # a worked example, exact vs bounded
isolation-tax measure trace.jsonl       # your trace, one JSON object per line
isolation-tax fleet trace.jsonl ...     # the same trace, in GPUs and dollars
```

```console
$ isolation-tax demo
  three tenants, each sending the SAME 3-block document with a different tail:

    cache hits 9   universal 0   content 9
    shared-prompt cold start   0
    isolation destroys 8 content hits (89% of the benefit) — EXACT, because tenants were labelled

  Drop the `tenant` field and the same trace can only be BOUNDED:
    mode BOUNDED   floor 9 hits (100%)

  That gap is why this tool wants your labels: with them the answer is a count,
  without them it is a bound, and no public trace carries them.
$ echo $?
0
```

Now on 12,031 requests of the [Mooncake FAST'25][mooncake] production trace (the fixture is
`statefabric/fixtures/mooncake_timed_trace.json`, one JSON object per line):

```console
$ isolation-tax measure mooncake.jsonl
  mode              BOUNDED   (a FLOOR, not the answer — see below)
  requests          12,031
  blocks read       288,500
  cache hits        105,710   (36.6%)
    universal       12,030   (shared system prompt)
    content         93,680

  >>> per-tenant isolation COSTS AT LEAST 12,428 cache hits
      = 11.8% of your cache benefit
      = 4.3% added to total prefill
      plus 313 shared-prompt cold starts, reported apart (a per-tenant fixed cost, not lost sharing)

  note: BOUNDED: this is a FLOOR on cross-session sharing, not the tax. The tests are necessary, not sufficient, so two users sending the same document often pass by coincidence and are counted as possibly-one-session.

  note: 1 block(s) appear in EVERY request (a shared system prompt). Of their 12,030 hits, 313 do not survive isolation -- that is the per-tenant cold start, not lost sharing, and it is reported apart because it is the one component you can remove outright by marking the system prompt public.
$ echo $?
0
```

To be precise about the quantity — **11.8% of prefix-cache HITS lost**, not a latency and not an
overhead. (SafeKV, arXiv 2508.08438 §7.4, reports 11.74% for residual TTFT overhead; the numerals
nearly collide and the quantities are unrelated. Never state ours without naming what it measures in
the same sentence.)

## Worked example — add one field and the bound becomes a count

| | `tenant` field | answer |
|---|---|---|
| **EXACT** | on every request | the hits that don't survive isolation, **counted** |
| **BOUNDED** | absent | a **floor**, inferred from constraints the trace can't fake |

Public serving traces carry no tenant labels, which is why the published work either uses synthetic
workloads or reports a proxy metric. **You have those labels.** Here is the same 8,000-request
WildChat trace measured both ways — first with its real per-user labels, then with the `tenant` field
stripped and nothing else changed:

```console
$ isolation-tax measure wildchat.jsonl
  mode              EXACT
  requests          8,000   tenants 1,001
  blocks read       586,878
  cache hits        411,678   (70.1%)
    universal       0   (shared system prompt)
    content         411,678

  >>> per-tenant isolation COSTS 987 cache hits
      = 0.2% of your cache benefit
      = 0.2% added to total prefill
$ echo $?
0
```

```console
$ isolation-tax measure wildchat_unlabelled.jsonl
  mode              BOUNDED   (a FLOOR, not the answer — see below)
  requests          8,000
  blocks read       586,878
  cache hits        411,678   (70.1%)
    universal       0   (shared system prompt)
    content         411,678

  >>> per-tenant isolation COSTS AT LEAST 5,921 cache hits
      = 1.4% of your cache benefit
      = 1.0% added to total prefill

  note: BOUNDED: this is a FLOOR on cross-session sharing, not the tax. The tests are necessary, not sufficient, so two users sending the same document often pass by coincidence and are counted as possibly-one-session.
$ echo $?
0
```

**Read the two together.** The BOUNDED floor is 6× the EXACT count on the identical trace — because
cross-*session* is not cross-*tenant*: a tenant owns many conversations, so cross-tenant reuse is a
strict subset. The floor equals the tax only under **per-user tenancy**. Two questions, two figures,
and collapsing them is the most likely way to misquote this tool.

## Worked example — the refuse path

**No fleet parameter is defaulted.** Omit one and you get exit `2` and a list of what is missing, not
a number:

```console
$ isolation-tax fleet mooncake.jsonl --gpu A100-80GB --fleet-gpus 1000
ABSTAIN — nothing was measured.
  fleet parameters missing: --model-weights-gb, --gpu-memory-gb, --gpu-usd-hr, --kv-bytes-per-token. These are not defaulted, because a dollar figure computed from invented inputs is quotable, wrong, and indistinguishable from a measured one once it is in a slide. Supply them or take no number.
$ echo $?
2
```

**And outside the envelope the capacity band was measured in, it abstains rather than transplanting
someone else's hardware onto your fleet.** Change nothing but the accelerator:

```console
$ isolation-tax fleet mooncake.jsonl --gpu H100-80GB --model-weights-gb 15.23 \
    --gpu-memory-gb 80 --gpu-usd-hr 4 --layers 28 --kv-heads 4 --head-dim 128 \
    --dtype-bytes 2 --fleet-gpus 1000
ABSTAIN — nothing was measured.
  this fleet is outside the envelope the retention band was measured in, on 1 axis:
    - GPU: you gave 'H100-80GB'. The retention band was measured on A100-80GB and only there. Retention is set by where the larger batch lands relative to the compute crossover, which is a property of the accelerator; carrying an A100 number onto another card would be transplanting a measurement, not applying one.
  The band is (1/keep) ** [0.7983, 0.9261], measured on three models on one A100-80GB. Applying it here would mean quoting a number measured somewhere else as if it had been measured on your fleet. Run the ladder on your hardware, or take no number.
$ echo $?
2
```

An empty or unparseable trace abstains too — it has no isolation tax, and reporting `0%` for one
would be a vacuous pass rather than a measurement:

```console
$ : > empty.jsonl && isolation-tax measure empty.jsonl
ABSTAIN — nothing was measured.
  no usable request objects in 'empty.jsonl' (0 unparseable line(s)). Expected JSONL: one JSON object per line, each with `hash_ids`. A trace that parses to nothing has no isolation tax, and reporting 0 would be a vacuous pass.
$ echo $?
2
```

## `fleet` — the same trace, in GPUs and dollars

A percentage does not survive contact with a budget meeting. `fleet` turns the measured hit count
into GPU-equivalents and an annual figure, using the KV working-set ratio the trace already gives you
and a capacity band that was **measured on real hardware, three models on one A100-80GB**.

```bash
isolation-tax fleet mooncake.jsonl \
  --gpu A100-80GB --model-weights-gb 29.54 --gpu-memory-gb 80 --gpu-usd-hr 2.00 \
  --layers 48 --kv-heads 8 --head-dim 128 --dtype-bytes 2 \
  --fleet-gpus 1000
```

```console
  fleet model — A100-80GB

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
    THREE POINTS ON ONE GPU. Retention rose with model size across them, but no curve is fitted and none is implied: the FULL measured band is applied to every model inside the envelope. A model-size-dependent point estimate would be a fit to three points presented as a law.
    source                      results/data/statefabric/gpu/elision_throughput.json

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
    conventions                 GB = 1e9 bytes (decimal). Stated because GiB inputs would move the pool size by ~7%.  8760 h/yr

  ASSUMED BY YOU (not measured by anything here; the answer moves with these)
    utilisation                 1
    session-bound fraction      1
    KV pool fill                1   (fixed by the measurement)
    workload mix                assumed identical between arms. The trace is replayed unchanged; only the cache policy differs.

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
      A RANGE, not a point: retention was MEASURED to vary 0.879 - 0.954 and nothing here predicts where your model lands in it. A point estimate would be a false precision in a financial figure.
```

Exit `0`. **The `NOT MODELLED` block is elided above; the tool prints it on every run** — eight named
limitations, including the retraction below.

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

**A model this README used to teach, and the measurement that killed it.** Earlier versions converted
with `(1+r)/(1+r*keep)` at `r_max = (memory − weights − reserve)/weights`, and concluded in prose that
*"isolation costs most where there is memory headroom."* The A100 ladder falsified it in
**direction** — predicted 1.87 / 1.61 / 1.41 against measured **1.66 / 1.70 / 1.80**. `r_max` governs
a different comparison (same batch, smaller KV); when *both* arms fill the card it does not enter at
all. The closed form was **removed rather than tuned**, and it is no longer importable.

The retention was measured at one `keep` (0.5282) and is carried to yours in log space —
`ratio = (1/keep) ** exponent`. That form is a **modelling choice**, printed as one under
`NOT MODELLED`. Multiplying instead was tried and is wrong: it returns 0.879× at `keep = 1`, pricing
a *negative* saving on a problem of size zero — isolation cost nothing, so nothing was lost and there
is nothing to recover.

**RETRACTION (2026-07-30) — the ≥ 1.0× floor is not a claim that elision is never slower.** Earlier
text here and in `fleet.py` justified that floor with `elision_never_slower`, machine-checked in
`serving_limits/formal/lean/CertifiedElision.lean`. That citation is **withdrawn**: the theorem is a
proof over a *declared* per-object cost model in `Nat`, it says nothing about wall-clock, and this
estate has measured otherwise. `results/data/statefabric/gpu/elision_throughput_l4_0p5b.json` (L4,
Qwen2.5-0.5B, ctx 4096) reports `throughput_ratio_same_batch` **below 1.0× in 4 of its 9 cells** —
0.9908 / 0.9466 / 0.9009 / 0.9941 at batch 2 / 4 / 8 / 16, worst **0.9009× at batch 8** — crossing
back above 1.0× only from batch 32. The floor is a property of the form `(1/keep) ** positive` and of
a ladder whose every arm ran above batch 32 (1492/2827, 264/501, 61/116). **The band therefore has no
support at small batch and cannot express the regime where elision measured slower**; that is printed
under `SMALL BATCH` in `NOT MODELLED` on every run.

## What it never sees

Prompt text. Completions. User identities. It reads **block hashes, two integer lengths, and a
timestamp** — the fields your serving logs already have. The measurement runs where the traffic is,
and a number comes out.

```jsonl
{"timestamp": 0, "input_length": 6758, "output_length": 500, "hash_ids": [0,1,2,3], "tenant": "acme"}
{"timestamp": 1200, "input_length": 7322, "output_length": 490, "hash_ids": [0,9,10], "tenant": "globex"}
```

`tenant` is the only optional field, and it is the one that turns a bound into a count.

## How the bounded mode works

A conversation's context grows monotonically. Turn *k+1*'s prompt **contains** turn *k*'s prompt and
its generated output, arrives **after** turn *k* finished generating, and its block chain **extends**
turn *k*'s. A request that violates all three against *every* candidate ancestor is provably in a
different conversation — no labels required.

The chain constraint does the heavy lifting: two users sharing a long document have a common prefix
and then **diverge**, so neither chain contains the other — which is exactly the case a
length-and-timing test alone would let through.

Two independent methods are run and the *stronger* floor is reported, not the average: pairwise
continuation (12,428 provably-cross-session hits on Mooncake) and a greedy antichain witness
(8,917 blocks provably multi-session, same 12,428-hit lower bound). A third, exact
minimum-session assignment, is **refused rather than sampled** above 5,000 requests — it is
`O(V·E)` with `E` growing as `n²`, and silently sampling it would report a partial result as exact.

It is robust. Sweeping the one free parameter — assumed generation speed, `--tok-per-s` — from 50 to
500 tok/s moves the result by under half a point. The length and chain constraints do the work.

## CLI reference

```
usage: isolation-tax [-h] [--version] {measure,fleet,demo} ...

what does per-tenant KV-cache isolation cost you?
```

| command | what it does |
|---|---|
| `isolation-tax measure <trace>` | measure the tax on a JSONL trace (`-` for stdin) |
| `isolation-tax fleet <trace>` | price the measured tax as GPU-equivalents and annual dollars |
| `isolation-tax demo` | a worked example, exact vs bounded |

### `isolation-tax measure`

| flag | what it does |
|---|---|
| `trace` | positional; path to a JSONL trace, or `-` for stdin |
| `--json` | machine-readable result (`mode`, `exact`, `n_requests`, `n_tenants`, `total_blocks`, `total_hits`, `isolation_tax_hits`, `shared_prompt_cold_start_hits`, `isolation_tax_share_of_reuse`, `added_prefill_share`, `interpretation`, `notes`) |
| `--tok-per-s TOK_PER_S` | assumed generation rate for the timing constraint, **BOUNDED mode only**. Faster = weaker constraint = more conservative floor |
| `--fail-over FRAC` | exit `1` if the tax exceeds this fraction of cache benefit, e.g. `0.20` |

```console
$ isolation-tax measure mooncake.jsonl --fail-over 0.05

FAIL — tax 11.8% exceeds --fail-over 5.0%
$ echo $?
1
```

### `isolation-tax fleet`

Every parameter below is **required and none is defaulted**, except where a default is stated.

| flag | what it does |
|---|---|
| `trace` | positional; JSONL trace, or `-` for stdin |
| `--gpu GPU` | the accelerator, as a label for the report (e.g. `A100-80GB`). **Checked against the measured envelope** |
| `--model-weights-gb N` | resident weight footprint **per GPU**, in GB (1e9 bytes). Use the sharded figure under tensor parallelism |
| `--gpu-memory-gb N` | HBM per GPU, in GB (1e9 bytes) |
| `--gpu-usd-hr N` | your price per GPU-hour |
| `--kv-bytes-per-token N` | KV bytes per token — *or* give the four config flags below and it is computed as `2*layers*kv_heads*head_dim*dtype_bytes` |
| `--layers N` · `--kv-heads N` · `--head-dim N` · `--dtype-bytes N` | model config. `kv-heads` means GQA groups, not Q heads |
| `--fleet-gpus N` | GPUs you run today, under isolation |
| `--reserve-gb N` | activations + fragmentation headroom held back from the KV pool (default `4`, from the source capacity model) |
| `--utilisation N` | **ASSUMPTION**: fraction of wall-clock hours billed (default `1.0`). Not measured by anything here |
| `--session-bound-fraction N` | **ASSUMPTION**: fraction of the fleet whose capacity is bound by the KV pool (default `1.0`). Not measured by anything here |
| `--kv-fill N` | how full the KV pool runs. **Fixed at `1.0` by the measurement** — both arms filled the card, which is the comparison itself — so any other value ABSTAINS |
| `--tok-per-s N` | as for `measure` |
| `--json` | machine-readable result |
| `--fail-over-usd USD` | exit `1` if the modelled annual cost exceeds this many dollars |

```console
$ isolation-tax fleet mooncake.jsonl --gpu A100-80GB --model-weights-gb 29.54 \
    --gpu-memory-gb 80 --gpu-usd-hr 2 --layers 48 --kv-heads 8 --head-dim 128 \
    --dtype-bytes 2 --fleet-gpus 1000 --fail-over-usd 500000

FAIL — modelled annual isolation cost $917,550–$1,059,851 exceeds --fail-over-usd $500,000 across the WHOLE measured retention band
$ echo $?
1
```

Note the gate fires only when the threshold is exceeded **across the whole band**, not at its
midpoint — a threshold crossed by one end of a measured range is not a finding.

### Exit codes

| code | meaning |
|---:|---|
| `0` | measured |
| `1` | measured, and the tax exceeds `--fail-over` (or `--fail-over-usd` on `fleet`) |
| `2` | **ABSTAIN** — nothing was measured, which is never a pass |

`fleet` exits `2` on a missing fleet parameter, a zero or negative fleet, a model that does not fit
the card, a `--kv-bytes-per-token` that contradicts the config it was also given, or **a fleet
outside the envelope the capacity band was measured in** — naming *every* axis that is out of range,
not just the first.

## Honest scope

**It proves:**

- in EXACT mode, the hits lost to per-tenant isolation on your trace, **counted**
- in BOUNDED mode, a **floor** on cross-**session** sharing, inferred from constraints the trace
  cannot fake
- a number derived only from block hashes and integers — no prompt text, no completions, no
  identities
- with `fleet`: a capacity/throughput model over that count, with every input echoed so the
  arithmetic can be re-run

**It does NOT prove:**

- **that the floor is the tax.** The discriminators are *necessary, not sufficient*: two users
  sending the same document often satisfy them by coincidence and are counted as possibly-one-session.
  The true number is **higher**
- **that cross-session equals cross-tenant.** A tenant owns many conversations, so cross-tenant is a
  strict *subset*. The floor equals the tax only under **per-user tenancy**
- anything about *what* was shared. It cannot tell a system prompt from a leaked document
- any latency or dollar figure from `measure`. **`measure` reports no time and no money.** `fleet`
  converts, and only under parameters you supply
- **an end-to-end serving result.** The one time this estate measured serving end to end it got
  **0.997×** — below 1.0 — and published it as a falsification. Nothing here claims anything runs
  faster
- **the added prefill compute.** Recomputing the lost hits is real work and it is *not* in the dollar
  figure; only the KV-residency channel is
- **an instantaneous residency.** The working-set ratio is cumulative over the trace; no eviction is
  modelled, and a fleet already past the compute crossover sees less than the figure
- a bound in either direction. A BOUNDED trace pushes the figure **down** and `--kv-fill 1.0` pushes
  it **up**; the tool does **not** net them out. It is a model
- anything about EXACT mode's correctness if your labels are wrong. Garbage labels, garbage count
- that the shared-prompt cold start is lost sharing. It is a fixed per-tenant charge, reported apart,
  and it is the one part you can remove outright by marking the prompt public

The two halves are inseparable. A tool that states only the first is marketing.

Full CLI reference, generated by running `--help` on the installed tool:
[`docs/CLI.md`](docs/CLI.md). Its own scope section is currently marked **MISSING** — the generator's
curated registry has no entry for this package yet — so the section above is the authoritative one.
That gap is printed into the generated document rather than silently omitted, which is the point.

## Troubleshooting

| you see | what it means and how to fix it |
|---|---|
| `ABSTAIN — nothing was measured` + `fleet parameters missing: ...` (exit 2) | Supply every named flag. Nothing is defaulted, because a dollar figure computed from invented inputs is quotable, wrong, and indistinguishable from a measured one once it is in a slide. |
| `outside the envelope the retention band was measured in` (exit 2) | Your GPU, model size or KV fill is outside what was actually measured (A100-80GB, 0.988–29.540 GB of weights, `--kv-fill 1.0`). Every out-of-range axis is named, not just the first. Run the ladder on your own hardware, or take no number. |
| `the model does not fit: N GB of weights plus M GB reserve leaves -X GB for KV` (exit 2) | There is no KV pool to size, so there is no capacity delta to price. If the model runs tensor-parallel, pass the **sharded per-GPU** weight footprint. |
| `no usable request objects in '<file>'` (exit 2) | Empty, or not JSONL. One JSON object per line, each with `hash_ids`. Reporting `0%` here would be a vacuous pass. |
| `mode BOUNDED` when you expected EXACT | Your trace has no `tenant` field on its requests. Add it and the floor becomes a count — on WildChat that is the difference between 1.4% and 0.2%. |
| the number looks too low | Check whether you are reading a BOUNDED floor as the tax. It is a floor on cross-**session** sharing; the true cross-tenant tax under per-user tenancy is higher, and under coarser tenancy it is lower. |
| `FAIL — tax N% exceeds --fail-over` (exit 1) | Working as intended: `--fail-over` is a CI gate, and `1` means *measured and over threshold* — distinct from `2`, which means nothing was measured at all. |

**Offline behaviour.** There is no network path in this package. `measure` and `fleet` read a local
file (or stdin) and do arithmetic; no model is downloaded, no endpoint is contacted, no telemetry is
sent. That is the point of the design: **the measurement runs where the traffic is, so the prompts
never leave your perimeter** — and there is nothing in the input format that could carry them out if
it tried.

## FAQ

**"11.8% — is that a latency number?"**
No, and this is the single most likely misreading. It is the **share of prefix-cache hits lost**.
SafeKV (2508.08438 §7.4) reports 11.74% for residual **TTFT overhead** on a different scheme. The
numerals nearly collide; the quantities are unrelated. The provenance register requires the quantity
to be named in the same sentence as the number, every time.

**"You claim a floor from an unlabelled trace. Why should I believe it?"**
Because the constraints are ones the trace cannot fake: monotone context growth, arrival after the
previous turn finished generating, and block-chain containment. A request violating all three against
*every* candidate ancestor is provably in a different conversation. The floor is not synthesised or
sampled. What it is not is *tight* — the tests are necessary, not sufficient, so the real number is
higher.

**"Isn't the whole thing sensitive to your assumed generation speed?"**
Sweeping `--tok-per-s` from 50 to 500 moves the result by under half a point. The length and chain
constraints do the work; the timing constraint is the weakest of the three and it is the only free
parameter.

**"Where did the dollar figure's conversion factor come from?"**
Three models on one A100-80GB, each arm run at its own maximum resident batch:
`results/data/statefabric/gpu/elision_throughput.json`. It is a ratio of timed runs, not a fit. Three
points do not make a curve, so the **full** band is applied to every model inside the envelope rather
than interpolated — and outside the envelope the tool abstains instead of transplanting the band.

**"Your model told me isolation costs most where there's memory headroom. Was that right?"**
No. That was `(1+r)/(1+r*keep)` at `r_max`, and the A100 ladder falsified it in **direction** —
predicted 1.87 / 1.61 / 1.41 against measured 1.66 / 1.70 / 1.80. It was removed rather than tuned,
and it is no longer importable. `r_max` still appears in the report because it sizes the pool; it is
labelled `FALSIFIED as a predictor` where it prints.

**"So sharing is faster and you're telling me to give it up?"**
Neither half of that. This prices the **ban**; it does not recommend lifting it — the shared arm is
the counterfactual the published attack breaks. And the band cannot express small-batch serving at
all: measured `throughput_ratio_same_batch` runs **below 1.0× in 4 of 9 cells** on an L4, worst
0.9009× at batch 8, crossing above 1.0× only from batch 32. A fleet that serves at small batch is
outside what this prices, in the direction that costs you money.

**"Can it tell me *what* leaked?"**
No. It sees block hashes and integers. It cannot distinguish a shared system prompt from a leaked
document, and that is by construction, because the alternative is shipping prompt text out of your
perimeter. If you want to know whether your stack leaks at all, that is
[`kvleak`](https://github.com/nickharris808/kvleak).

**"Why would you run this at all?"**
You are choosing between a leak and a bill, and you currently know the size of neither. If the tax is
small, isolate everything and stop worrying. If it is large, you now know what a provably-safe
sharing scheme is worth to you — and `fleet` puts that in the units a budget meeting runs on, with
every input on the page so your own team can check the arithmetic. On the best public trace the floor
is **11.8%** and the ceiling — if every content share crosses sessions — is **88.6%**. Where you sit
in that range is a fact about *your* traffic that only you can measure.

## Provenance — where every number here comes from

| number | certificate | register key |
|---|---|---|
| 11.8% Mooncake floor | `results/data/statefabric/isolation_tax.json` (`headline.cross_session_floor_share_of_reuse`) | `isolation_tax_mooncake` |
| 0.24% WildChat per-user | `results/data/statefabric/isolation_tax_wildchat_peruser.json` (`arms.per_user_hashed_ip.share_of_reuse_lost`) | `isolation_tax_wildchat_peruser` |
| 5.82× vs the published 0.72% | `results/data/statefabric/isolation_tax_replication.json` (`verdict.ratio_to_published`) | `isolation_tax_replication` |
| 3.4% – 65.9% by session length | `results/data/statefabric/isolation_tax_stratified.json` | — |
| retention band 0.879 – 0.954 | `results/data/statefabric/gpu/elision_throughput.json` | — |
| below-1.0× small-batch cells | `results/data/statefabric/gpu/elision_throughput_l4_0p5b.json` | — |

Register keys live in `oss/provenance.py`, which fails when a registered value is absent from its own
certificate. The benchmark that recomputes these figures from the certificates is
[`llm-tenant-isolation-bench`](https://github.com/nickharris808/llm-tenant-isolation-bench).

## Related tools

| | |
|---|---|
| [`kvleak`](https://github.com/nickharris808/kvleak) | the other half of the trade-off: does your stack actually leak between tenants? |
| [`llm-tenant-isolation-bench`](https://github.com/nickharris808/llm-tenant-isolation-bench) | recomputes our published isolation figures from their certificates |
| [`kv-reuse-econ-bench`](https://github.com/nickharris808/kv-reuse-econ-bench) | recomputes our reuse-economics headline the same way |
| [`kv-reuse-econ-traces`](https://huggingface.co/datasets/nickh007/kv-reuse-econ-traces) | per-workload reuse accounting plus the closed form |
| [`sf-verify`](https://github.com/nickharris808/sf-verify) | the same three-valued discipline for audit logs — exit 2 is never a pass |
| [`gridlock`](https://github.com/nickharris808/gridlock) | and for wait-for graphs — an empty graph ABSTAINS |

Everything above, explained in one place:
**<https://nickharris808.github.io/evidence-docs/>**

## Licence

Apache-2.0. [`LICENSE-TAG`](LICENSE-TAG) is **CLEAN**: this measures and reports a count. It admits
nothing, refuses nothing, and gates no request — its only refusal is a refusal to *state a number*.
See [`CLAIMS-MAP.md`](CLAIMS-MAP.md) for the claim ranges it approaches and the terminal step it does
not perform.

Citation metadata is in [CITATION.cff](CITATION.cff).

[paper]: https://arxiv.org/abs/2502.07776
[pr]: https://github.com/vllm-project/vllm/pull/17045
[mooncake]: https://github.com/kvcache-ai/Mooncake

<!-- PORTFOLIO -->
---

## The rest of the portfolio

24 artifacts, one idea: **a measurement you cannot check is a press release.** Every tool
here reports; none of them gates.

**Tools**

| | |
|---|---|
| [`abstain-bench`](https://github.com/nickharris808/abstain-bench) | how often does a verifier pass input it could not check? |
| [`evidence`](https://github.com/nickharris808/evidence) | run the whole portfolio over your repo — the weakest leg, never the mean |
| [`floorgen`](https://github.com/nickharris808/floorgen) | what must your system remember? an exact lower bound |
| [`formal-proof-mcp`](https://github.com/nickharris808/formal-proof-mcp) | a proof kernel for your coding agent |
| [`gatecount`](https://github.com/nickharris808/gatecount) | exactly how many states does removing this check admit? |
| [`gridlock`](https://github.com/nickharris808/gridlock) | certify a wait-for relation cannot wedge |
| [`honestbench`](https://github.com/nickharris808/honestbench) | measure your CI's escape rate |
| [`kvleak`](https://github.com/nickharris808/kvleak) | cross-tenant leak scanner |
| [`kvprobe`](https://github.com/nickharris808/kvprobe) | model-substitution detector with a measured FPR |
| [`preregister`](https://github.com/nickharris808/preregister) | refuses to seal a plan whose conclusion is already fixed |
| [`proof-carrying-ci`](https://github.com/nickharris808/proof-carrying-ci) | the whole portfolio as one CI check, with SARIF |
| [`proof-to-code-drift`](https://github.com/nickharris808/proof-to-code-drift) | fail the build when the proof stops matching |
| [`sf-verify`](https://github.com/nickharris808/sf-verify) | re-derive admission decisions offline |
| [`signoff-cert`](https://github.com/nickharris808/signoff-cert) | certificates that carry their own false-pass bound |
| [`tokencount`](https://github.com/nickharris808/tokencount) | a token count both parties can recompute |

**Benchmarks** — each recomputes one of our own published numbers from its certificate

| | |
|---|---|
| [`illusion-bench`](https://github.com/nickharris808/illusion-bench) | how many broken kernels does your oracle admit? |
| [`kv-reuse-econ-bench`](https://github.com/nickharris808/kv-reuse-econ-bench) | recompute our economics headline |
| [`llm-tenant-isolation-bench`](https://github.com/nickharris808/llm-tenant-isolation-bench) | recompute our isolation figures |

**Datasets**

| | |
|---|---|
| [`abstain-corpus`](https://huggingface.co/datasets/nickh007/abstain-corpus) | 32 inputs a verifier must NOT pass |
| [`kv-reuse-econ-traces`](https://huggingface.co/datasets/nickh007/kv-reuse-econ-traces) | per-workload reuse accounting + the closed form |
| [`kv-tenant-isolation-bench`](https://huggingface.co/datasets/nickh007/kv-tenant-isolation-bench) | isolation observations, uninterpretable rows included |
| [`llm-precision-fingerprints`](https://huggingface.co/datasets/nickh007/llm-precision-fingerprints) | precision-labelled logprobs with a negative control |

**Try it in a browser** — no install, no GPU

| | |
|---|---|
| [`tenant-leak-demo`](https://huggingface.co/spaces/nickh007/tenant-leak-demo) | the residency calculator |
| [`wait-for-visualiser`](https://huggingface.co/spaces/nickh007/wait-for-visualiser) | paste a wait-for graph, see the cycle |

### Documentation

Everything above, explained in one place: **<https://nickharris808.github.io/evidence-docs/>** —
the [tutorial](https://nickharris808.github.io/evidence-docs/start/tutorial/),
[what this proves and what it does not](https://nickharris808.github.io/evidence-docs/concepts/what-this-proves/),
and a [CLI reference](https://nickharris808.github.io/evidence-docs/reference/cli/) generated by
running `--help` on every published command.

### The commercial edition

Everything above is **measure-only** and Apache-2.0: it tells you what is true and never acts on
it. The **enforcement** side — binding a partition key at the admission decision, the compiled gate
corpus, and the certificate-*issuing* faucet — is covered by filed patents and licensed separately.

**Reading is free. Enforcing is licensed.**
<!-- /PORTFOLIO -->

<!-- BEGIN VERIFY-IN-TEN-MINUTES (generated by oss/tools/gen_readme_standard.py) -->

## Verify this in ten minutes

### 1. Install the version that exists today

```bash
pip install "git+https://github.com/nickharris808/isolation-tax@v0.1.0"
```

*not on PyPI; the git tag is pinned so a reader installs the exact code this README documents.*

### 2. Run one command

```bash
isolation-tax --help
```

Prints the subcommands; measuring needs a trace.

### 3. Where the numbers come from

Numbers in this README carry paths like `results/data/...`. **Those receipts live in a private research monorepo and you cannot open them** — they are cited so you can see exactly what was measured and where, not because the link resolves. What is public, and what you can check yourself, is: this package's own tests and `--selftest`; the benchmarks, which recompute the headline numbers from published inputs; and the Hugging Face datasets, whose every row names the certificate it came from. If a number here matters to you and none of those covers it, treat it as unverified.

### 4. What a proof here does and does not buy you

**A machine-checked proof is not evidence that the thing proved means anything.** This lane's own theorem-transfer engine emits an instance describing a domain that does not exist, and the Lean kernel accepts it clean; a sibling lane reached the same conclusion from the other side with `theorem t : True := trivial`, which is axiom-clean and proves nothing. Kernel-checking tells you a derivation is sound. Whether the statement models your system is a question no kernel answers, and it is the question worth asking.

---

Version 0.1.0 in the source tree · Apache-2.0 · cite via `CITATION.cff` in this repository · this block is generated by `oss/tools/gen_readme_standard.py` from a measurement of PyPI, the git tags and this tree, and `--check` fails if anyone edits it by hand.

<!-- END VERIFY-IN-TEN-MINUTES -->
