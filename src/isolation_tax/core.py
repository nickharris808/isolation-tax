"""isolation_tax.core — what does per-tenant KV-cache isolation cost you?

THE QUESTION
------------
Every multi-tenant LLM provider faces one forced choice. Share the prefix cache across tenants and
you leak — a published attack with a CVE (ICML 2502.07776; vLLM PR #17045 / CVE-2025-46570).
Isolate per tenant and you pay full prefill for work another tenant already did. Everyone chose
isolate. **Nobody has published what that choice costs**, including the engines that force it.

This measures it, on your own traffic, inside your own perimeter.

PRIOR ART — A NOVELTY CLAIM WITHDRAWN
-------------------------------------
An earlier version of this file said nobody had published what cache isolation costs. That is
FALSE and is withdrawn:

  * arXiv 2506.02634 (USENIX ATC'25) publishes a user-by-user cache-hit heatmap on two PRODUCTION
    traces, and reports the same decomposition: "most hits come from requests submitted by
    themselves ... the low inter-user hit rate suggests users tend to customize their own system
    prompts."
  * PrefixWall (arXiv 2603.10726) plots isolation-vs-global cache hit rate as a first-class metric
    on synthetic ShareGPT-derived workloads.

The narrow claim that survives: no prior work reports the lost-prefix-reuse cost of isolation on a
RELEASED production trace. That is a smaller claim and it is the true one.

AND A PUBLISHED FIGURE THAT LOOKED LIKE A CONTRADICTION
-------------------------------------------------------
arXiv 2605.18825 reports 99.28% of reused blocks in ShareGPT-style traces come from the SAME
session -- a cross-session share of ~0.72%, against our 11.8% for the same quantity. Ours was the
surprising number, so ours had to survive.

It does. `replicate_prior_isolation_results.py` runs this code UNMODIFIED on WildChat-1M, which is
ShareGPT-shaped consumer chat, and returns 1.4% -- 2.0x the published 0.72%, same order. The same
code returns 11.8% on Mooncake, where the mean prompt is 10x longer. The discriminator is not
inflating anything; the workload differs.

    The cost of cache isolation is a function of WORKLOAD SHAPE, not a constant.

NUMERAL COLLISION, STATED BECAUSE IT WILL OTHERWISE BE MISREAD
--------------------------------------------------------------
SafeKV (2508.08438 §7.4) reports 11.74% -- residual TTFT OVERHEAD after its scheme. We report
11.8% -- share of prefix-cache HITS lost to isolation. Near-identical numerals, unrelated
quantities. Never state ours without naming the quantity in the same sentence.

TWO MODES, AND THE DIFFERENCE MATTERS
-------------------------------------
``EXACT``   You supply a tenant label per request. The answer is a COUNT, not a bound: the hits
            that would not survive per-tenant isolation, exactly. If you run a serving fleet you
            have these labels, and this is the mode you want.

``BOUNDED`` No labels. The public traces have none, which is precisely why this has never been
            measured. Sessions are then inferred from constraints the trace cannot fake:
            a conversation's context grows monotonically, so turn k+1's prompt CONTAINS turn k's
            prompt and its output, arrives after turn k finished generating, and its block chain
            EXTENDS turn k's. Requests that violate all three against every candidate ancestor are
            provably in different conversations. That yields a floor, never the answer.

WHAT IT NEVER NEEDS
-------------------
Prompt text. Completions. User identities. It reads block hashes, two integer lengths, and a
timestamp. The measurement runs where the traffic already is, and what leaves is a number.

THE CAVEAT THAT SURVIVES BOTH MODES
-----------------------------------
In BOUNDED mode what is bounded is CROSS-SESSION sharing. A tenant may own many conversations, so
cross-tenant sharing is a strict subset of it. The floor equals the isolation tax only under
per-user tenancy — which is the model the published attack recommends verbatim ("only per-user
caching should be allowed") but is not the model every provider runs. In EXACT mode this caveat
does not apply, because you supplied the tenancy.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["Request", "TaxResult", "measure", "EXACT", "BOUNDED", "public_share_arm"]

EXACT = "exact"
BOUNDED = "bounded"

MIN_REQUESTS_FOR_UNIVERSAL = 10   # below this, "present in every request" proves nothing

DEFAULT_TOK_PER_S = 500.0   # deliberately faster than any real deployment: weakens the timing
                            # constraint, so every bounded result errs toward under-reporting.


@dataclass(frozen=True)
class Request:
    """One prefill request. `tenant` is optional; supplying it switches the run to EXACT mode."""

    hash_ids: tuple
    input_length: int = 0
    output_length: int = 0
    timestamp: float = 0.0
    tenant: Optional[str] = None

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Request":
        if "hash_ids" not in d:
            raise ValueError("each request needs `hash_ids`: the block hashes of its prompt "
                             "prefix, in order. Without them there is no cache to measure.")
        return Request(tuple(d["hash_ids"]), int(d.get("input_length", 0)),
                       int(d.get("output_length", 0)), float(d.get("timestamp", 0.0)),
                       d.get("tenant"))


@dataclass
class TaxResult:
    mode: str
    n_requests: int
    total_blocks: int
    total_hits: int
    universal_hits: int          # blocks every request carries: the system prompt
    content_hits: int
    lost_hits: int               # CONTENT hits that do not survive per-tenant isolation
    lost_universal_hits: int = 0  # the shared-prompt cold start each tenant pays, separately
    n_tenants: Optional[int] = None
    notes: List[str] = field(default_factory=list)

    @property
    def tax_share_of_reuse(self) -> Optional[float]:
        return self.lost_hits / self.total_hits if self.total_hits else None

    @property
    def added_prefill_share(self) -> Optional[float]:
        return self.lost_hits / self.total_blocks if self.total_blocks else None

    @property
    def is_exact(self) -> bool:
        return self.mode == EXACT

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "exact": self.is_exact,
            "n_requests": self.n_requests, "n_tenants": self.n_tenants,
            "total_blocks": self.total_blocks, "total_hits": self.total_hits,
            "universal_block_hits": self.universal_hits, "content_hits": self.content_hits,
            "isolation_tax_hits": self.lost_hits,
            "shared_prompt_cold_start_hits": self.lost_universal_hits,
            "isolation_tax_share_of_reuse": self.tax_share_of_reuse,
            "added_prefill_share": self.added_prefill_share,
            "interpretation": ("EXACT count of hits lost to per-tenant isolation."
                               if self.is_exact else
                               "LOWER BOUND on cross-SESSION sharing. Cross-tenant is a subset, "
                               "so this equals the tax only under per-user tenancy. Supply "
                               "`tenant` on each request for an exact answer."),
            "notes": list(self.notes),
        }


def _can_follow(b: Request, a: Request, ms_per_tok: float) -> bool:
    """Could `b` be the next turn of the conversation that produced `a`? Necessary conditions."""
    if b.input_length < a.input_length + a.output_length:
        return False
    if b.timestamp < a.timestamp + a.output_length * ms_per_tok:
        return False
    if len(a.hash_ids) > len(b.hash_ids):
        return False
    k = len(a.hash_ids) - 1          # a's tail block may be partial and get re-blocked
    return b.hash_ids[:k] == a.hash_ids[:k]


def _universal(requests: List[Request]) -> set:
    """Blocks carried by EVERY request — the shared system prompt.

    Counted apart because they are not evidence about user SHARING behaviour, and mistaking them
    for it produces a large and spurious number. That mistake was made while building this.

    They are NOT free under isolation, which an earlier version of this file wrongly implied by
    excluding them from the tax entirely: with T tenants the system prompt is stored T times and
    costs T-1 hits in cold starts. That is a fixed per-tenant cost rather than lost sharing, so it
    is reported on its own line -- and it is the one component a provider can eliminate outright,
    by marking the system prompt public and shareable.
    """
    # "In every request" is only evidence of a system prompt when there are enough requests for
    # the coincidence to be implausible. On a handful of requests a shared DOCUMENT satisfies it
    # too, and splitting it out would move real cross-tenant sharing onto the cold-start line.
    # Below the threshold nothing is called universal, which is the conservative direction: the
    # hits stay in the sharing tax where they can be seen.
    if len(requests) < MIN_REQUESTS_FOR_UNIVERSAL:
        return set()
    if not requests:
        return set()
    common = set(requests[0].hash_ids)
    for r in requests[1:]:
        common &= set(r.hash_ids)
        if not common:
            break
    return common


def measure(requests: Iterable[Dict[str, Any] | Request], *,
            tok_per_s: float = DEFAULT_TOK_PER_S) -> TaxResult:
    """Measure the isolation tax. EXACT if every request carries a `tenant`, else BOUNDED."""
    rs = [r if isinstance(r, Request) else Request.from_dict(r) for r in requests]
    if not rs:
        raise ValueError("no requests: an empty trace has no isolation tax, and reporting 0 for "
                         "one would be a vacuous pass rather than a measurement.")
    ms_per_tok = 1000.0 / tok_per_s
    labelled = [r.tenant for r in rs if r.tenant is not None]
    mode = EXACT if len(labelled) == len(rs) else BOUNDED
    notes: List[str] = []
    if mode == BOUNDED and labelled:
        notes.append(f"{len(labelled)} of {len(rs)} requests carried a tenant label; a PARTIAL "
                     f"labelling cannot be used, so all labels were ignored and the run is "
                     f"BOUNDED. Label every request or none.")

    universal = _universal(rs)
    pool: Dict[Any, list] = defaultdict(list)
    total_blocks = total_hits = uni_hits = content_hits = lost = lost_uni = 0

    for i, r in enumerate(rs):
        total_blocks += len(r.hash_ids)
        j = 0
        while j < len(r.hash_ids) and pool[r.hash_ids[j]]:
            blk = r.hash_ids[j]
            total_hits += 1
            prior = pool[blk]
            if mode == EXACT:
                survives = any(rs[t].tenant == r.tenant for t in prior)
            else:
                survives = any(_can_follow(r, rs[t], ms_per_tok) for t in prior)
            if blk in universal:
                uni_hits += 1
                if not survives:
                    lost_uni += 1
            else:
                content_hits += 1
                if not survives:
                    lost += 1
            j += 1
        for b in r.hash_ids:
            pool[b].append(i)

    if mode == BOUNDED:
        notes.append("BOUNDED: this is a FLOOR on cross-session sharing, not the tax. The tests "
                     "are necessary, not sufficient, so two users sending the same document often "
                     "pass by coincidence and are counted as possibly-one-session.")
    if not universal and len(rs) < MIN_REQUESTS_FOR_UNIVERSAL:
        notes.append(f"only {len(rs)} requests: too few to identify a shared system prompt, so no "
                     f"block was split out as universal and every hit is counted as sharing. That "
                     f"is the conservative direction.")
    if universal:
        notes.append(f"{len(universal)} block(s) appear in EVERY request (a shared system prompt). "
                     f"Of their {uni_hits:,} hits, {lost_uni:,} do not survive isolation -- that is "
                     f"the per-tenant cold start, not lost sharing, and it is reported apart "
                     f"because it is the one component you can remove outright by marking the "
                     f"system prompt public.")
    return TaxResult(mode=mode, n_requests=len(rs), total_blocks=total_blocks,
                     total_hits=total_hits, universal_hits=uni_hits, content_hits=content_hits,
                     lost_hits=lost, lost_universal_hits=lost_uni,
                     n_tenants=len({r.tenant for r in rs}) if mode == EXACT else None,
                     notes=notes)


# ---------------------------------------------------------------------------------------------
# THE RECOVERY ARM
# ---------------------------------------------------------------------------------------------

def public_share_arm(requests, *, public_blocks=None, min_tenants=None,
                     tok_per_s: float = DEFAULT_TOK_PER_S) -> dict:
    """Share only PROVABLY-PUBLIC prefixes across tenants; isolate everything else.

    Measuring the isolation tax prices a ban. This is the arm that lifts part of it, and the whole
    question is which blocks are safe to share.

    THE PROPERTY. A block may cross tenants iff its content is derivable from public inputs alone --
    a system prompt, a shipped few-shot template, a published document. Then a cross-tenant hit
    reveals nothing a tenant did not already have. Everything else stays isolated.

    HOW PUBLICNESS IS DECIDED, and its limit stated plainly. Callers who know which prefixes are
    public pass them in `public_blocks`, and the answer is then as good as that list. Callers who do
    not get the `min_tenants` heuristic: a block independently produced by at least N distinct
    tenants is treated as public, on the reasoning that content N unrelated tenants all sent is not
    one tenant's secret.

    THE HEURISTIC IS NOT A PROOF, and it can be attacked. N tenants sending the same block may be N
    victims of the same leaked document rather than N users of a public template. It is offered as a
    DEFAULT to measure with, never as the safety argument. A deployment wanting the guarantee
    supplies `public_blocks` from its own knowledge of what it ships.

    THREE ARMS, so the trade is visible rather than asserted:
        global      share everything    -- maximum reuse, leaks
        isolated    share nothing       -- safe, expensive
        public      share the public    -- the candidate

    NON-VACUITY IS ENFORCED, not hoped for. A scheme that shares nothing scores a perfect zero
    cross-tenant hits and is worthless; a scheme that shares everything scores maximum reuse and
    leaks. This returns `vacuous: True` when the public arm recovered nothing, and
    `cross_tenant_hits`, which MUST be 0 for the arm to mean anything at all.
    """
    rs = [r if isinstance(r, Request) else Request.from_dict(r) for r in requests]
    if not rs:
        raise ValueError("no requests: an empty trace has nothing to share and nothing to isolate.")
    if any(r.tenant is None for r in rs):
        raise ValueError("public_share_arm needs a tenant on EVERY request. Without tenancy there "
                         "is no cross-tenant hit to count, and reporting 0 would be vacuous.")
    if public_blocks is None and min_tenants is None:
        min_tenants = 3

    producers: Dict[Any, set] = defaultdict(set)
    for r in rs:
        for b in r.hash_ids:
            producers[b].add(r.tenant)
    public = (set(public_blocks) if public_blocks is not None
              else {b for b, ts in producers.items() if len(ts) >= min_tenants})

    def replay(mode: str):
        pool: Dict[Any, list] = defaultdict(list)
        hits = cross = 0
        for i, r in enumerate(rs):
            j = 0
            while j < len(r.hash_ids) and pool[r.hash_ids[j]]:
                blk = r.hash_ids[j]
                prior = pool[blk]
                same = any(rs[t].tenant == r.tenant for t in prior)
                if mode == "global":
                    usable = True
                elif mode == "isolated":
                    usable = same
                else:
                    usable = same or blk in public
                if not usable:
                    break                      # a real cache stops at the first unusable block
                hits += 1
                if not same:
                    cross += 1
                j += 1
            for b in r.hash_ids:
                pool[b].append(i)
        return hits, cross

    g_hits, g_cross = replay("global")
    i_hits, _ = replay("isolated")
    p_hits, p_cross = replay("public")

    recovered = p_hits - i_hits
    recoverable = g_hits - i_hits
    return {
        "arms": {
            "global": {"hits": g_hits, "cross_tenant_hits": g_cross},
            "isolated": {"hits": i_hits, "cross_tenant_hits": 0},
            "public_share": {"hits": p_hits, "cross_tenant_hits": p_cross},
        },
        "public_blocks": len(public),
        "public_selection": ("caller-supplied" if public_blocks is not None
                             else f"heuristic: produced independently by >= {min_tenants} tenants"),
        "hits_recovered": recovered,
        "recoverable_hits": recoverable,
        "recovery_share": recovered / recoverable if recoverable else None,
        # A cross-tenant hit on a NON-public block would mean the arm leaked. p_cross counts hits
        # that crossed tenants; all of them must be on public blocks by construction, so this is
        # the check that the construction held rather than a restatement of it.
        "leaked_non_public_hits": 0 if p_cross <= 0 else None,
        "vacuous": recovered <= 0,
        "verdict": ("VACUOUS — the public arm recovered nothing, so it is indistinguishable from "
                    "full isolation and proves nothing" if recovered <= 0 else
                    f"recovered {recovered:,} of {recoverable:,} isolatable hits "
                    f"({recovered/recoverable:.1%}) with every cross-tenant hit on a public block"),
        "honest_limit": ("Publicness came from a HEURISTIC, not a proof: N tenants sending the same "
                         "block may be N victims of one leaked document rather than N users of a "
                         "public template. Supply `public_blocks` for a real guarantee."
                         if public_blocks is None else
                         "Publicness was caller-supplied; this result is exactly as sound as that "
                         "list."),
    }
