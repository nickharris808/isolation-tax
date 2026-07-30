"""isolation_tax.fleet — what per-tenant isolation costs a FLEET, in GPU-equivalents and dollars.

THE GAP THIS CLOSES
-------------------
`measure` returns a count of lost cache hits and a percentage. A percentage does not survive
contact with a budget meeting. The question a provider's finance team actually asks is:

    How many more GPUs am I running because I isolate, and what does that cost me a year?

This answers exactly that, from the same trace, using a conversion that was MEASURED on a GPU
rather than derived on a whiteboard.

A MODEL THIS FILE SHIPPED, AND THE MEASUREMENT THAT KILLED IT
--------------------------------------------------------------
The first version of this file converted the working-set ratio with a closed form,

    throughput_ratio(r, keep) = (1 + r) / (1 + r * keep),   r = r_max = (mem - W - reserve) / W

and concluded, in prose, that "isolation costs the most where there is MEMORY HEADROOM". The
A100-80GB model-size ladder falsified it, each arm at its own maximum resident batch:

    model     r_max    PREDICTED   MEASURED   error
    0.5B      75.0        1.87x      1.66x     +13%
    7B         4.0        1.61x      1.70x      -6%
    14B        1.6        1.41x      1.80x     -22%

Wrong in DIRECTION: the formula says the ratio falls with model size, and it rises. That is not a
calibration error, it is the wrong variable. r_max governs a different comparison -- same batch,
smaller KV -- and when BOTH arms fill the card, r_max does not enter at all. The closed form has
been REMOVED from this file rather than tuned, and the prose that stated its conclusion as fact is
gone with it. This paragraph is what remains of it, kept so the error is legible.

WHAT REPLACED IT: A CEILING THAT IS ARITHMETIC, TIMES A RETENTION THAT IS MEASURED
-----------------------------------------------------------------------------------
When both arms fill the card they read the SAME number of bytes per step, so the step time is the
same and the ratio is just the batch multiplier -- which is 1/keep, the working-set ratio restated.
That is the CEILING and it is an identity: the model, the context and the accelerator all cancel.
It must never be published as a prediction on its own.

What the ladder actually measured is how much of that ceiling survives the compute crossover at the
larger batch:

    retention = measured_ratio / (1 / keep)

    0.5B  0.879     7B  0.900     14B  0.954

The 0.5B, with 75 GB free, is pushed to batch 2,827 -- deep into compute-bound -- and keeps 88%. The
14B reaches only batch 116, stays bandwidth-bound, and keeps 95%. Memory headroom does not help; it
hands you a batch large enough to hurt.

Those retentions were all measured at ONE keep (0.5282). Carrying them to your keep is done in log
space -- `ratio = (1/keep) ** exponent` -- and NOT by multiplying, which is wrong at keep -> 1. See
the CARRYING THE MEASUREMENT block below for the failure and why this family instead.

WHY THE ANSWER IS A RANGE AND NEVER A POINT
---------------------------------------------
Retention was measured to VARY across the ladder and nothing here predicts where a given model
lands in it. Three points on one GPU is not a curve, so this file does not fit one: it reports the
band [min, max] of the measured retentions and applies the whole band to any model inside the
measured envelope. A single number would be a false precision, and false precision inside a dollar
figure is exactly the failure this package exists to catch.

    ratio in [ (1/keep) ** 0.799 , (1/keep) ** 0.926 ]

At the ladder's own keep = 0.5282 that is [1.665x, 1.806x], which brackets all three measurements
because its endpoints ARE two of them.

That width covers the spread ACROSS MODELS and nothing else. It does not cover the error introduced
by carrying a single-keep measurement to a different keep, because one keep cannot measure that. At
keeps far from 0.5282 the true uncertainty is wider than the band shown, and `not_modelled` says so
on every run rather than leaving the reader to infer it.

Source: results/data/statefabric/gpu/elision_throughput.json (0.5B and 14B) and
results/data/statefabric/gpu/elision_throughput_a100_7b.json (7B), both A100-80GB, ctx 4096.

OUTSIDE THE MEASURED ENVELOPE IT ABSTAINS
-------------------------------------------
The band was measured on one card, over one span of model sizes, with both arms filling the card.
Applying it to a 70B, or to an H100, or to a fleet whose KV pool runs half empty, would be silently
transplanting a measurement taken somewhere else. Asked to do that, this exits 2 and NAMES what is
out of range. See `MEASURED_ENVELOPE` and `check_envelope`.

WHAT IS MEASURED AND WHAT IS ASSUMED — THE SPLIT IS THE POINT
--------------------------------------------------------------
MEASURED, on your trace, by `measure`:
    the KV working-set ratio between the two arms. Under a globally shared cache a hit means the
    block was already resident and costs nothing to store. Under per-tenant isolation a LOST hit
    is a block that has to be recomputed and stored again for that tenant. So

        stored_shared    = total_blocks - total_hits
        stored_isolated  = stored_shared + lost_hits + lost_universal_hits
        keep             = stored_shared / stored_isolated        (<= 1)

    That is a count over your own traffic. Nothing about it is modelled.

MEASURED, on this estate's GPUs, NOT on your trace:
    the retention band above. It is a ratio of two timed runs, not a fit.

SUPPLIED BY YOU, exactly as given, echoed back in the output:
    the GPU, the weight footprint, the card's memory, the hourly price, the KV bytes per token,
    the reserve, and the size of the fleet. No constant is hidden. Every one of them is printed.

ASSUMED BY YOU, and the answer moves with them:
    utilisation and the session-bound fraction of the fleet. These are not measured by anything
    here and they are not defaulted silently in the output -- they are named as assumptions on
    their own line.

WHAT THIS IS NOT
----------------
It is a capacity/throughput model. It is NOT an end-to-end serving benchmark, and the distinction
is not academic here: the one time this estate measured serving end to end, it got 0.997x -- a
ratio BELOW 1.0, published as a falsification. Nothing in this file is a claim that anything runs
faster; it reports ratios and dollars, and the ratios are decode-step capacity ratios.

Nor is the band's >= 1.0x floor a claim that elision is never slower. Same-batch decode was
measured BELOW 1.0x in 4 of 9 cells on an L4 (0.9009x at batch 8; see the RETRACTION in the
CARRYING block and the SMALL BATCH entry under NOT MODELLED). The floor is a property of the form
(1/keep) ** positive and of a ladder that only ever ran above batch 32.

ABSTAIN RATHER THAN GUESS
-------------------------
Every fleet parameter is required and none is defaulted. A dollar figure assembled from invented
inputs is precisely the failure this package exists to catch, and it is worse than no figure at
all because it is quotable. Missing a parameter, the answer is that the parameters are missing.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .core import TaxResult

__all__ = ["FleetInputs", "OutOfEnvelope", "kv_bytes_per_token", "max_kv_weight_ratio",
           "retention_band", "throughput_ratio_band", "check_envelope", "fleet_delta",
           "MEASURED_LADDER", "MEASURED_ENVELOPE", "RETENTION_LOW", "RETENTION_HIGH",
           "RETENTION_EXPONENT_LOW", "RETENTION_EXPONENT_HIGH",
           "HOURS_PER_YEAR", "BYTES_PER_GB", "DEFAULT_RESERVE_GB"]

HOURS_PER_YEAR = 8760          # 365 * 24. Leap years are not the uncertainty in this arithmetic.
BYTES_PER_GB = 1e9             # DECIMAL GB (80e9 for an "80GB" card). Stated because GiB would
                               # move the pool size and the token count that come off it.
DEFAULT_RESERVE_GB = 4.0       # activations + fragmentation headroom.

# THE MEASURED LADDER. Each row is one model run twice on the same card, each arm at its OWN
# maximum resident batch -- the comparison a provider actually runs. `retention` is not fitted; it
# is the measured ratio divided by the arithmetic ceiling 1/keep, and the ceiling is identical in
# every row because keep was held fixed at 0.5282 by construction.
MEASURED_LADDER: Tuple[Dict[str, Any], ...] = (
    {"model": "Qwen/Qwen2.5-0.5B-Instruct", "weights_gb": 0.988065536,
     "batch_full": 1492, "batch_elided": 2827,
     "measured_ratio": 1.664564412143754, "identity_ceiling": 1.8932222642938281,
     "source": "results/data/statefabric/gpu/elision_throughput.json"},
    {"model": "Qwen/Qwen2.5-7B-Instruct", "weights_gb": 15.231233024,
     "batch_full": 264, "batch_elided": 501,
     "measured_ratio": 1.70431043743213, "identity_ceiling": 1.8932222642938281,
     "source": "results/data/statefabric/gpu/elision_throughput_a100_7b.json"},
    {"model": "Qwen/Qwen2.5-14B-Instruct", "weights_gb": 29.540067328,
     "batch_full": 61, "batch_elided": 116,
     "measured_ratio": 1.8059823358116376, "identity_ceiling": 1.8932222642938281,
     "source": "results/data/statefabric/gpu/elision_throughput.json"},
)

_RETENTIONS = tuple(row["measured_ratio"] / row["identity_ceiling"] for row in MEASURED_LADDER)
RETENTION_LOW = min(_RETENTIONS)      # 0.8792 -- the 0.5B, pushed deepest into compute-bound
RETENTION_HIGH = max(_RETENTIONS)     # 0.9539 -- the 14B, which stays bandwidth-bound

# CARRYING THE MEASUREMENT TO A DIFFERENT keep.
# The ladder ran at ONE keep (0.5282). Every real trace has a different one, so the retention has to
# be carried across -- and the form of that carry is a MODELLING CHOICE, not a measurement. Three
# points at a single keep cannot discriminate between two forms that agree there.
#
# The first form tried here was multiplicative -- ratio = (1/keep) * retention -- and it is WRONG,
# provably, in the direction that matters. As keep -> 1 the ceiling -> 1 while the factor stays
# 0.879, so it reports 0.879x off a problem of size ZERO: isolation cost nothing, nothing was lost,
# and there is nothing to recover, yet it priced a negative saving. Mooncake's real keep = 0.9348
# lands a multiplicative band at [0.941x, 1.020x] -- straddling 1.0 -- so the flagship worked
# example would have printed a partly-NEGATIVE saving. Caught by evaluating the band at the keeps
# that actually occur, which the multiplicative version never was.
#
# The carry used instead is in log space:
#
#     ratio = (1/keep) ** exponent,    exponent = ln(measured_ratio) / ln(1/keep_measured)
#
# chosen because it is the simplest family satisfying two constraints: ratio(keep=1) == 1 exactly
# (no lost sharing, no delta, nothing to price) and ratio >= 1 over the whole domain of keep.
# It reproduces all three measurements EXACTLY at the keep they were taken at -- it interpolates the
# data rather than replacing it. It is still a form choice and it is declared as one in
# `not_modelled`; at keeps far from 0.5282 the band's width understates the real uncertainty,
# because the form contributes error that the [min, max] spread does not see.
#
# RETRACTION, 2026-07-30 -- WHAT THE >= 1.0 FLOOR IS NOT.
# This block used to attribute that second constraint to `elision_never_slower`, machine-checked in
# serving_limits/formal/lean/CertifiedElision.lean, and the README and the test suite followed it.
# That citation was wrong and it is withdrawn. `elision_never_slower` is a proof over a DECLARED
# per-object cost model in Nat parameters cFull/cElide; it says nothing about wall-clock and cannot,
# and it is not evidence that any measured ratio is >= 1.0x. This estate has MEASURED OTHERWISE:
# results/data/statefabric/gpu/elision_throughput_l4_0p5b.json (L4, Qwen2.5-0.5B-Instruct, ctx 4096,
# keep 0.5282) reports `throughput_ratio_same_batch` BELOW 1.0 in 4 of its 9 cells --
#
#     b1 1.0034   b2 0.9908   b4 0.9466   b8 0.9009   b16 0.9941
#     b32 1.1770  b64 1.7522  b128 1.7423  b256 1.7698
#
# -- the minimum being 0.9009x at batch 8. The >= 1.0 floor here is therefore a property of THE FORM
# and of the regime the ladder was measured in, NOT a hardware fact. The A100 ladder ran each arm at
# its own maximum resident batch (1492/2827, 264/501, 61/116 -- all far above 32, which is where the
# L4 sweep crosses back over 1.0), so the band has no support at small batch and CANNOT express the
# regime where elision measured slower. That limitation is printed under NOT MODELLED on every run.
_EXPONENTS = tuple(
    math.log(row["measured_ratio"]) / math.log(row["identity_ceiling"]) for row in MEASURED_LADDER)
RETENTION_EXPONENT_LOW = min(_EXPONENTS)    # 0.7983 -- the 0.5B again; same ordering as retention
RETENTION_EXPONENT_HIGH = max(_EXPONENTS)   # 0.9261 -- the 14B again

# THE ENVELOPE THE BAND WAS MEASURED IN. Every bound here is a fact about the runs above, not a
# tolerance chosen to let something through.
#   gpu_token          the ladder ran on ONE accelerator. The label must name it.
#   gpu_memory_gb      80.0 is the card's nameplate in decimal GB; 85.095 is what the driver
#                      reports for the same physical card (85,094,825,984 bytes). Both are correct
#                      spellings of the card that was measured; anything else is a different card.
#   model_weights_gb   the ladder's own endpoints, 0.5B and 14B. Not extrapolated past either end.
#   kv_fill            both arms filled the card. That IS the comparison; a pool that runs part
#                      empty is a regime nothing here has measured.
MEASURED_ENVELOPE: Dict[str, Any] = {
    "gpu": "A100-80GB",
    "gpu_token": "a100",
    "gpu_memory_gb": (80.0, 85.095),
    "model_weights_gb": (MEASURED_LADDER[0]["weights_gb"], MEASURED_LADDER[-1]["weights_gb"]),
    "kv_fill": 1.0,
    "ctx_tokens": 4096,
    "keep_at_measurement": 0.5282,
}

# (attribute, CLI flag) for every parameter that must be supplied. Kept as data rather than as
# prose in an error string, so the abstain message and the validation cannot drift apart.
REQUIRED = (
    ("gpu", "--gpu"),
    ("model_weights_gb", "--model-weights-gb"),
    ("gpu_memory_gb", "--gpu-memory-gb"),
    ("gpu_usd_hr", "--gpu-usd-hr"),
    ("kv_bytes_per_token", "--kv-bytes-per-token"),
    ("fleet_gpus", "--fleet-gpus"),
)


class OutOfEnvelope(ValueError):
    """Asked to apply a band measured elsewhere. Abstain (exit 2) and name what is out of range."""


def kv_bytes_per_token(layers: int, kv_heads: int, head_dim: int, dtype_bytes: int) -> int:
    """KV bytes per token, exactly, from the model config. Two tensors: one K, one V."""
    for name, v in (("layers", layers), ("kv_heads", kv_heads), ("head_dim", head_dim),
                    ("dtype_bytes", dtype_bytes)):
        if v is None or v <= 0:
            raise ValueError(f"{name} must be a positive integer from the model config; got {v!r}. "
                             f"Guessing it would put an invented constant inside a dollar figure.")
    return 2 * int(layers) * int(kv_heads) * int(head_dim) * int(dtype_bytes)


def max_kv_weight_ratio(gpu_memory_gb: float, model_weights_gb: float,
                        reserve_gb: float = DEFAULT_RESERVE_GB) -> float:
    """r_max = (memory - weights - reserve) / weights, and the fit check that comes with it.

    NOTE WHAT THIS IS NOT. r_max was this file's predictor of the capacity ratio and the A100
    ladder FALSIFIED it in direction. It survives only because sizing the KV pool needs the same
    subtraction, and because a model that does not fit must abstain rather than report a negative
    pool. It does not enter the reported ratio anywhere.
    """
    if model_weights_gb <= 0:
        raise ValueError("model weights must be positive; a zero-weight model has no KV pool.")
    free = gpu_memory_gb - model_weights_gb - reserve_gb
    if free <= 0:
        raise ValueError(
            f"the model does not fit: {model_weights_gb:g} GB of weights plus {reserve_gb:g} GB "
            f"reserve leaves {free:g} GB for KV on a {gpu_memory_gb:g} GB card. There is no KV "
            f"pool to size, so there is no capacity delta to price. Use the sharded per-GPU weight "
            f"footprint if this model runs tensor-parallel.")
    return free / model_weights_gb


def retention_band() -> Tuple[float, float]:
    """(min, max) of the MEASURED retentions. Three points on one GPU, deliberately not fitted."""
    return RETENTION_LOW, RETENTION_HIGH


def throughput_ratio_band(keep: float) -> Tuple[float, float]:
    """The capacity ratio as a RANGE: (1/keep) ** exponent, over the band of measured exponents.

    `keep` is the fraction of the isolated arm's KV working set that the shared arm needs; keep = 1
    means isolation cost nothing, and this returns exactly (1.0, 1.0) there -- no lost sharing, no
    delta. There is no point estimate on purpose: the ladder measured retention to VARY across three
    models and nothing here predicts where a given model lands inside that.

    At the ladder's own keep = 0.5282 the band is [1.6646x, 1.8060x], whose endpoints ARE two of the
    three measurements. Away from that keep the exponent form is a modelling choice -- see the
    module header for why this family and not the multiplicative one, which was wrong at keep -> 1.
    """
    if not 0 < keep <= 1:
        raise ValueError(f"keep must be in (0, 1]; got {keep!r}. keep is the shared arm's share of "
                         f"the isolated arm's KV working set and cannot exceed it.")
    ceiling = 1.0 / keep
    return ceiling ** RETENTION_EXPONENT_LOW, ceiling ** RETENTION_EXPONENT_HIGH


def _norm(label: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (label or "").lower())


def check_envelope(inputs: "FleetInputs") -> List[str]:
    """Every way this fleet falls outside the runs the retention band was measured on.

    Returned as a list of full sentences, each naming the parameter, the supplied value and the
    measured range, so the abstain message says what is wrong rather than that something is.
    """
    env = MEASURED_ENVELOPE
    out: List[str] = []

    if env["gpu_token"] not in _norm(inputs.gpu):
        out.append(
            f"GPU: you gave {inputs.gpu!r}. The retention band was measured on {env['gpu']} and "
            f"only there. Retention is set by where the larger batch lands relative to the compute "
            f"crossover, which is a property of the accelerator; carrying an A100 number onto "
            f"another card would be transplanting a measurement, not applying one.")

    lo, hi = env["gpu_memory_gb"]
    if not lo - 1e-9 <= inputs.gpu_memory_gb <= hi + 1e-9:
        out.append(
            f"GPU memory: you gave {inputs.gpu_memory_gb:g} GB. The band was measured on the "
            f"{env['gpu']} card, whose memory is {lo:g} GB by nameplate and {hi:g} GB as the "
            f"driver reports it. A card with different free memory reaches a different batch and "
            f"therefore a different retention.")

    wlo, whi = env["model_weights_gb"]
    if inputs.model_weights_gb < wlo - 1e-9 or inputs.model_weights_gb > whi + 1e-9:
        side = "below" if inputs.model_weights_gb < wlo else "above"
        out.append(
            f"model weights: you gave {inputs.model_weights_gb:g} GB per GPU, which is {side} the "
            f"ladder, whose endpoints are {wlo:.3f} GB (Qwen2.5-0.5B) and {whi:.3f} GB "
            f"(Qwen2.5-14B). Retention rose monotonically across those three points, but three "
            f"points are not a curve and this tool does not extrapolate one.")

    if abs(inputs.kv_fill - env["kv_fill"]) > 1e-9:
        out.append(
            f"KV pool fill: you gave {inputs.kv_fill:g}. The band was measured with BOTH arms at "
            f"their own maximum resident batch -- filling the card is the comparison, not a "
            f"parameter of it. A pool that runs part empty is not capacity-limited by KV in the "
            f"way the measurement was, and nothing here measured that regime. Supply --kv-fill 1.0 "
            f"and read the answer as the ceiling it is, or take no number.")

    return out


@dataclass(frozen=True)
class FleetInputs:
    """Fleet parameters. Everything in REQUIRED defaults to None on purpose: see `missing`."""

    gpu: Optional[str] = None
    model_weights_gb: Optional[float] = None
    gpu_memory_gb: Optional[float] = None
    gpu_usd_hr: Optional[float] = None
    kv_bytes_per_token: Optional[float] = None
    fleet_gpus: Optional[float] = None
    # Supplied, but with a stated default because it is a property of the arithmetic rather than
    # of the buyer's business. It is printed in full.
    reserve_gb: float = DEFAULT_RESERVE_GB
    # ASSUMPTIONS. Defaulted to their neutral value (1.0 = bill every hour, whole fleet in scope).
    utilisation: float = 1.0
    session_bound_fraction: float = 1.0
    # NOT an assumption any more: 1.0 is the regime the band was measured in, and anything else
    # abstains via check_envelope. Kept as a parameter so that asking for it is refused out loud
    # rather than by the flag having quietly disappeared.
    kv_fill: float = 1.0

    def missing(self) -> List[str]:
        """The CLI flags that were not supplied. Empty list means every parameter is present."""
        return [flag for attr, flag in REQUIRED if getattr(self, attr) is None]


def _check_fraction(name: str, flag: str, value: float) -> None:
    if not 0 < value <= 1:
        raise ValueError(f"{name} must be in (0, 1]; got {value!r} via {flag}. A fraction outside "
                         f"that range is not an assumption, it is a typo, and it would scale a "
                         f"dollar figure without anyone noticing.")


def fleet_delta(tax: TaxResult, inputs: FleetInputs) -> Dict[str, Any]:
    """Price the isolation tax as GPU-equivalents and an annual dollar RANGE.

    Raises ValueError to abstain -- `OutOfEnvelope`, a ValueError, when the fleet is outside the
    runs the retention band was measured on.

    `tax` comes from `measure` on the provider's own trace; `inputs` are the provider's own fleet
    parameters. Nothing else is consulted and nothing is defaulted behind the caller's back.
    """
    gone = inputs.missing()
    if gone:
        raise ValueError(
            "fleet parameters missing: " + ", ".join(gone) + ". These are not defaulted, because a "
            "dollar figure computed from invented inputs is quotable, wrong, and indistinguishable "
            "from a measured one once it is in a slide. Supply them or take no number.")

    if inputs.fleet_gpus <= 0:
        raise ValueError(f"--fleet-gpus is {inputs.fleet_gpus!r}. A fleet of zero or fewer GPUs "
                         f"costs nothing to isolate, and dividing by it would produce an infinity "
                         f"rather than an answer.")
    if inputs.gpu_usd_hr < 0:
        raise ValueError(f"--gpu-usd-hr is {inputs.gpu_usd_hr!r}; a negative price is not a price.")
    if inputs.kv_bytes_per_token <= 0:
        raise ValueError(f"--kv-bytes-per-token is {inputs.kv_bytes_per_token!r}. Compute it from "
                         f"the config: 2 * layers * kv_heads * head_dim * dtype_bytes.")
    _check_fraction("utilisation", "--utilisation", inputs.utilisation)
    _check_fraction("the session-bound fraction", "--session-bound-fraction",
                    inputs.session_bound_fraction)
    _check_fraction("the KV fill fraction", "--kv-fill", inputs.kv_fill)

    # Physics before calibration: a model that does not fit has no KV pool at all, and saying so is
    # more useful than saying its size is outside a measured range.
    r_max = max_kv_weight_ratio(inputs.gpu_memory_gb, inputs.model_weights_gb, inputs.reserve_gb)

    # ---- THE ENVELOPE: abstain rather than transplant a measurement -------------------------
    breaches = check_envelope(inputs)
    if breaches:
        raise OutOfEnvelope(
            "this fleet is outside the envelope the retention band was measured in, on "
            + ("1 axis" if len(breaches) == 1 else f"{len(breaches)} axes") + ":\n    - "
            + "\n    - ".join(breaches)
            + f"\n  The band is (1/keep) ** [{RETENTION_EXPONENT_LOW:.4f}, "
              f"{RETENTION_EXPONENT_HIGH:.4f}], measured on three models on one A100-80GB. Applying "
              f"it here would mean quoting a number measured somewhere else as if it had been "
              f"measured on your fleet. Run the ladder on your hardware, or take no number.")

    # ---- MEASURED: the KV working-set ratio, straight off the trace ------------------------
    stored_shared = tax.total_blocks - tax.total_hits
    extra_stored = tax.lost_hits + tax.lost_universal_hits
    if stored_shared <= 0:
        raise ValueError(
            f"the trace stored {stored_shared} first-touch blocks, so there is no shared-arm "
            f"working set to compare against. A ratio with a zero denominator is not a small "
            f"number, it is the absence of a measurement.")
    stored_isolated = stored_shared + extra_stored
    keep = stored_shared / stored_isolated

    # ---- MEASURED ELSEWHERE: working-set ratio -> capacity ratio, as a BAND -----------------
    ceiling = 1.0 / keep
    ratio_low, ratio_high = throughput_ratio_band(keep)

    free_gb = inputs.gpu_memory_gb - inputs.model_weights_gb - inputs.reserve_gb
    kv_tokens_per_gpu = free_gb * BYTES_PER_GB * inputs.kv_fill / inputs.kv_bytes_per_token

    # ---- THE FLEET ARITHMETIC ---------------------------------------------------------------
    # A larger ratio means the shared arm needs fewer GPUs, so the HIGH end of the ratio band is
    # the HIGH end of the dollar band. The two are carried through together and never averaged.
    addressable = inputs.fleet_gpus * inputs.session_bound_fraction
    shared_high = addressable / ratio_low          # most GPUs the shared arm would need
    shared_low = addressable / ratio_high
    saved_low = addressable - shared_high
    saved_high = addressable - shared_low
    hourly = inputs.gpu_usd_hr * inputs.utilisation
    hourly_low, hourly_high = saved_low * hourly, saved_high * hourly
    annual_low, annual_high = hourly_low * HOURS_PER_YEAR, hourly_high * HOURS_PER_YEAR

    bounded = not tax.is_exact
    return {
        "artifact": "isolation_tax_fleet",
        "status": "MODEL — a MEASURED capacity band applied to a MEASURED working-set ratio. "
                  "No GPU was run for YOUR figure; the band comes from runs on this estate's A100.",
        "gpu": inputs.gpu,

        "measured_on_your_trace": {
            "source": "isolation_tax.measure, on the trace you supplied",
            "mode": tax.mode,
            "exact": tax.is_exact,
            "n_requests": tax.n_requests,
            "n_tenants": tax.n_tenants,
            "total_blocks": tax.total_blocks,
            "total_hits": tax.total_hits,
            "isolation_tax_hits": tax.lost_hits,
            "shared_prompt_cold_start_hits": tax.lost_universal_hits,
            "blocks_stored_shared_arm": stored_shared,
            "blocks_stored_isolated_arm": stored_isolated,
            "kv_working_set_ratio_keep": keep,
            "keep_definition": "(total_blocks - total_hits) / (total_blocks - total_hits + "
                               "isolation_tax_hits + shared_prompt_cold_start_hits)",
        },

        "calibrated_elsewhere_not_on_your_trace": {
            "model": "capacity_ratio = (1 / keep) ** exponent, where 1/keep is the arithmetic "
                     "ceiling (both arms fill the card, so both read the same bytes) and the "
                     "exponent is MEASURED",
            "retention_low": RETENTION_LOW,
            "retention_high": RETENTION_HIGH,
            "retention_definition": "measured_ratio / (1 / keep), per model, at each arm's own "
                                    "maximum resident batch. Reported because it is the raw "
                                    "measurement; the CARRY to your keep uses the exponent below.",
            "retention_exponent_low": RETENTION_EXPONENT_LOW,
            "retention_exponent_high": RETENTION_EXPONENT_HIGH,
            "retention_exponent_definition": "ln(measured_ratio) / ln(1 / keep) at the measured "
                                             "keep. Multiplying by retention instead was tried and "
                                             "is WRONG: it returns < 1.0x as keep -> 1, i.e. a "
                                             "NEGATIVE saving where isolation cost nothing, "
                                             "contradicting the machine-checked elision_never_"
                                             "slower. The exponent form reproduces all three "
                                             "measurements exactly and is 1.0x at keep = 1.",
            "carry_is_a_form_choice": "the ladder ran at ONE keep, so no data here can "
                                      "discriminate between forms that agree at keep = 0.5282. "
                                      "The exponent family was chosen for satisfying ratio(1) = 1 "
                                      "and ratio >= 1, which the estate already holds as proved. "
                                      "It is a modelling choice, not a measurement.",
            "measured_on": f"{MEASURED_ENVELOPE['gpu']}, ctx {MEASURED_ENVELOPE['ctx_tokens']}, "
                           f"keep = {MEASURED_ENVELOPE['keep_at_measurement']}",
            "ladder": [
                {"model": row["model"], "weights_gb": row["weights_gb"],
                 "batch_full": row["batch_full"], "batch_elided": row["batch_elided"],
                 "measured_ratio": row["measured_ratio"],
                 "retention": row["measured_ratio"] / row["identity_ceiling"],
                 "source": row["source"]}
                for row in MEASURED_LADDER],
            "no_interpolation": "THREE POINTS ON ONE GPU. Retention rose with model size across "
                                "them, but no curve is fitted and none is implied: the FULL "
                                "measured band is applied to every model inside the envelope. A "
                                "model-size-dependent point estimate would be a fit to three "
                                "points presented as a law.",
            "gather_efficiency_eta": None,   # WITHDRAWN 2026-07-30. This registered eta as a measurement. 
            "eta_note": "CORRECTED. This field read 0.998 with the note 'there is no gather "
                        "penalty'. No committed certificate has ever contained 0.998 -- not in any "
                        "revision. WITHDRAWN 2026-07-30. This registered eta as a measurement. It is not one. (a) 0.8644 is the UPPER median -- med() returned xs[len(xs)//2], the 75th percentile of [0.5253, 0.5493, 0.8644, 0.8899]; the true median is 0.70685. (b) The metric is definitionally vacuous: eta == byte_ratio * throughput_ratio_same_batch to 2.2e-16 across all 36 cells with byte_ratio constant, and make_cache() builds BOTH arms contiguous -- the harness never gathers. A gather efficiency from a harness that performs no gather is not a measurement at any value. Re-register only against a harness that actually scatters. Previously reported 0.8644 "
                        "(elision_throughput.json .cells[0].eta_kv_bound_median) and the highest "
                        "single cell ever measured is 0.8899, so the compacted cache reads at "
                        "roughly 86% of full effective bandwidth: a gather cost of about 14%, not "
                        "zero. 0.998 was an EXTRAPOLATED asymptote -- the register's own qualifier "
                        "called it a convergence -- and it was published as a measurement.",
            "eta_is_not_in_the_band": "eta is a same-batch control and does NOT enter the capacity "
                                      "band. The band is the fill-the-card comparison and was "
                                      "measured directly, so correcting eta moves no dollar figure "
                                      "here. It is reported because a reader is entitled to know "
                                      "the gather is not free.",
            "superseded": "an earlier version of this file converted with (1+r)/(1+r*keep) at "
                          "r = r_max. The ladder falsified it in DIRECTION -- predicted "
                          "1.87/1.61/1.41 against measured 1.66/1.70/1.80 -- and it has been "
                          "removed rather than tuned. r_max does not enter this figure.",
            "source": "results/data/statefabric/gpu/elision_throughput.json",
        },

        "measured_envelope": {
            "gpu": MEASURED_ENVELOPE["gpu"],
            "gpu_memory_gb": list(MEASURED_ENVELOPE["gpu_memory_gb"]),
            "model_weights_gb": list(MEASURED_ENVELOPE["model_weights_gb"]),
            "kv_fill": MEASURED_ENVELOPE["kv_fill"],
            "ctx_tokens": MEASURED_ENVELOPE["ctx_tokens"],
            "outside_this": "ABSTAIN, exit 2, naming the axis. The band is not carried onto a card "
                            "or a model size it was not measured on.",
        },

        "supplied_by_you": {
            "gpu": inputs.gpu,
            "model_weights_gb": inputs.model_weights_gb,
            "gpu_memory_gb": inputs.gpu_memory_gb,
            "reserve_gb": inputs.reserve_gb,
            "gpu_usd_hr": inputs.gpu_usd_hr,
            "kv_bytes_per_token": inputs.kv_bytes_per_token,
            "fleet_gpus": inputs.fleet_gpus,
            "gb_convention": "GB = 1e9 bytes (decimal). Stated because GiB inputs would move the "
                             "pool size by ~7%.",
            "hours_per_year": HOURS_PER_YEAR,
        },

        "assumed_by_you": {
            "utilisation": inputs.utilisation,
            "utilisation_note": "the fraction of wall-clock hours the fleet is billed for. 1.0 = "
                                "reserved capacity billed around the clock. NOT measured here.",
            "session_bound_fraction": inputs.session_bound_fraction,
            "session_bound_fraction_note": "the fraction of the fleet whose capacity is bound by "
                                           "the KV pool. Nodes serving other models, embeddings "
                                           "or batch work are out of scope. NOT measured here.",
            "kv_fill": inputs.kv_fill,
            "kv_fill_note": "fixed at 1.0 by the measurement: both arms filled the card, which is "
                            "the comparison itself. Any other value ABSTAINS rather than scaling a "
                            "band measured in a regime you are not in. NOT measured here.",
            "workload_mix": "assumed identical between arms. The trace is replayed unchanged; only "
                            "the cache policy differs.",
        },

        "derived": {
            "r_max": r_max,
            "r_max_note": "reported only because it sizes the KV pool. As a PREDICTOR of the "
                          "capacity ratio it was FALSIFIED by the A100 ladder and it does not "
                          "enter any number below.",
            "kv_pool_gb_per_gpu": free_gb * inputs.kv_fill,
            "kv_tokens_per_gpu": kv_tokens_per_gpu,
            "identity_ceiling_1_over_keep": ceiling,
            "identity_warning": "1/keep is the working-set ratio restated. The model, the context "
                                "and the accelerator all cancel, so it is NOT a prediction and it "
                                "is never reported as the answer. It is the ceiling the measured "
                                "retention is applied to.",
            "retention_low": RETENTION_LOW,
            "retention_high": RETENTION_HIGH,
            "capacity_ratio_low": ratio_low,
            "capacity_ratio_high": ratio_high,
            "capacity_ratio_formula": "(1 / keep) ** exponent, exponent in "
                                      f"[{RETENTION_EXPONENT_LOW:.4f}, "
                                      f"{RETENTION_EXPONENT_HIGH:.4f}] MEASURED",
            "retention_exponent_low": RETENTION_EXPONENT_LOW,
            "retention_exponent_high": RETENTION_EXPONENT_HIGH,
            "addressable_gpus": addressable,
            "gpus_isolated_arm": addressable,
            "gpus_shared_arm_low": shared_low,
            "gpus_shared_arm_high": shared_high,
            "gpu_equivalents_attributable_to_isolation_low": saved_low,
            "gpu_equivalents_attributable_to_isolation_high": saved_high,
            "usd_per_hour_low": hourly_low,
            "usd_per_hour_high": hourly_high,
            "usd_per_year_low": annual_low,
            "usd_per_year_high": annual_high,
            "usd_per_year_formula": "gpu_equivalents * gpu_usd_hr * 8760 * utilisation, evaluated "
                                    "at BOTH ends of the measured retention band",
            "why_a_range": "retention was MEASURED to vary 0.879 - 0.954 and nothing here predicts "
                           "where your model lands in it. A point estimate would be a false "
                           "precision in a financial figure.",
        },

        "not_modelled": [
            "END-TO-END SERVING. The band is a decode-step comparison at each arm's maximum "
            "resident batch, not a continuous-batching engine under real arrival patterns. The one "
            "time this estate measured serving end to end it got 0.997x -- below 1.0 -- and "
            "published it as a falsification. This is a model and it is not a benchmark; nothing "
            "here claims anything runs faster.",
            "ADDED PREFILL COMPUTE. Under isolation the lost hits are recomputed. That is real "
            "work and it is not in this figure; only the KV-residency channel is.",
            "EVICTION. The working-set ratio is cumulative over the trace, not an instantaneous "
            "residency. A cache under eviction pressure will not hold either arm's full set.",
            "WHERE IN THE BAND YOU LAND. The compute crossover sets retention and nothing here "
            "predicts it from model size, context or arrival pattern. Three measured points on one "
            "GPU are reported as a band, not fitted into a curve.",
            "THE CARRY TO YOUR keep. Every ladder point was measured at keep = 0.5282; yours is "
            "almost certainly different, and the log-space carry that bridges them is a MODELLING "
            "CHOICE no data here can check -- one keep cannot discriminate between forms that "
            "agree at it. The family was picked for returning exactly 1.0x at keep = 1, where "
            "nothing was lost and there is nothing to price; the multiplicative form it replaced "
            "returned 0.879x there, billing a NEGATIVE saving on a problem of size zero. The "
            "resulting >= 1.0x floor is a property of the FORM and is not a claim about hardware "
            "-- see SMALL BATCH. The band's width covers the spread across MODELS only; "
            "the further your keep sits from 0.5282, the more real uncertainty sits outside it.",
            "SMALL BATCH -- THE REGIME THIS BAND CANNOT EXPRESS. The band returns >= 1.0x at every "
            "keep because its form is (1/keep) ** positive, not because elision was measured never "
            "to be slower. It was measured slower: "
            "results/data/statefabric/gpu/elision_throughput_l4_0p5b.json (L4, Qwen2.5-0.5B, ctx "
            "4096) reports throughput_ratio_same_batch below 1.0x in 4 of 9 cells -- 0.9908, "
            "0.9466, 0.9009, 0.9941 at batch 2, 4, 8, 16, the worst being 0.9009x at batch 8 -- "
            "crossing back above 1.0x only from batch 32. Every arm of the A100 ladder behind this "
            "band ran above batch 32 (1492/2827, 264/501, 61/116), so the band has NO SUPPORT at "
            "small batch and no value it can return there is a measurement. A fleet that serves at "
            "small batch is outside what this prices, in the direction that costs you money.",
            "CONTEXT LENGTH. The ladder ran at ctx 4096. Retention is a statement about batch "
            "size against the compute crossover, and a different context reaches a different "
            "batch; that was not swept.",
            "SHARING IS THE ARM THAT LEAKS. The shared arm here is the counterfactual that the "
            "published attack breaks (ICML 2502.07776; CVE-2025-46570). This prices the ban; it "
            "does not recommend lifting it.",
            "TWO ERRORS IN OPPOSITE DIRECTIONS, NOT NETTED OUT. A BOUNDED run under-states keep's "
            "complement and pushes the dollar figure DOWN; requiring both arms to fill the card "
            "(kv_fill = 1.0) is the most favourable regime and pushes it UP. This tool does not "
            "net them. The result is a band around a model, not a bound in either direction.",
        ],

        "interpretation": (
            ("BOUNDED trace: the lost-hit count is a FLOOR on cross-session sharing, so the "
             "working-set gap and therefore this dollar band are under-stated on that axis. "
             "Label every request with `tenant` for an EXACT working-set ratio."
             if bounded else
             "EXACT trace: the working-set ratio is a count over labelled tenancy, not an "
             "inference.")
            + (" Isolation cost no measurable capacity on this trace (keep = 1.0), so the delta is "
               "zero and that is a result, not a failure."
               if extra_stored == 0 else
               " The band is the MEASURED spread of retention, not a confidence interval: it is the "
               "range across three models, and it carries no probability.")),
    }


def _fmt_usd(v: float) -> str:
    """Dollars at a readable magnitude. Never rounded up into a bigger unit than it earns."""
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:,.2f}B"
    if a >= 1e6:
        return f"${v/1e6:,.2f}M"
    if a >= 1e3:
        return f"${v/1e3:,.1f}K"
    return f"${v:,.0f}"


def render(out: Dict[str, Any]) -> str:
    """The human-readable report. Every input appears, so the buyer's team can re-run it."""
    m, s, a, d = (out["measured_on_your_trace"], out["supplied_by_you"],
                  out["assumed_by_you"], out["derived"])
    c, e = out["calibrated_elsewhere_not_on_your_trace"], out["measured_envelope"]
    L: List[str] = []
    L.append(f"  fleet model — {out['gpu']}")
    L.append("")
    L.append("  MEASURED (your trace, by isolation-tax measure)")
    L.append(f"    mode                        {m['mode'].upper()}"
             + ("" if m["exact"] else "   (a FLOOR — see below)"))
    L.append(f"    requests                    {m['n_requests']:,}"
             + (f"   tenants {m['n_tenants']:,}" if m["n_tenants"] else ""))
    L.append(f"    blocks read / cache hits    {m['total_blocks']:,} / {m['total_hits']:,}")
    L.append(f"    hits lost to isolation      {m['isolation_tax_hits']:,}"
             + (f"   (+{m['shared_prompt_cold_start_hits']:,} shared-prompt cold starts)"
                if m["shared_prompt_cold_start_hits"] else ""))
    L.append(f"    KV blocks stored  shared    {m['blocks_stored_shared_arm']:,}")
    L.append(f"                    isolated    {m['blocks_stored_isolated_arm']:,}")
    L.append(f"    keep = shared/isolated      {m['kv_working_set_ratio_keep']:.6f}")
    L.append("")
    L.append("  MEASURED ELSEWHERE (this estate's A100, NOT your trace)")
    L.append("    capacity ratio = (1/keep) ** exponent, both arms filling the card")
    L.append(f"    retention band              {c['retention_low']:.4f} – "
             f"{c['retention_high']:.4f}   MEASURED, three models, one GPU, at keep "
             f"{MEASURED_ENVELOPE['keep_at_measurement']}")
    L.append(f"    carried to your keep as     exponent {c['retention_exponent_low']:.4f} – "
             f"{c['retention_exponent_high']:.4f}   (a form choice, see NOT MODELLED)")
    for row in c["ladder"]:
        L.append(f"      {row['model'].split('/')[-1]:<26s} {row['weights_gb']:6.2f} GB  "
                 f"batch {row['batch_full']:>5,} -> {row['batch_elided']:>5,}  "
                 f"ratio {row['measured_ratio']:.3f}x  retention {row['retention']:.3f}")
    L.append(f"    {c['no_interpolation']}")
    L.append(f"    source                      {c['source']}")
    L.append("")
    L.append("  MEASURED ENVELOPE (outside it this tool ABSTAINS rather than transplanting a band)")
    L.append(f"    GPU                         {e['gpu']}, {e['gpu_memory_gb'][0]:g}–"
             f"{e['gpu_memory_gb'][1]:g} GB, ctx {e['ctx_tokens']}")
    L.append(f"    model weights per GPU       {e['model_weights_gb'][0]:.3f}–"
             f"{e['model_weights_gb'][1]:.3f} GB")
    L.append(f"    KV pool fill                {e['kv_fill']:g} (both arms fill the card)")
    L.append("")
    L.append("  SUPPLIED BY YOU (echoed so you can re-run the arithmetic)")
    L.append(f"    model weights               {s['model_weights_gb']:g} GB")
    L.append(f"    GPU memory                  {s['gpu_memory_gb']:g} GB")
    L.append(f"    reserve (activations/frag)  {s['reserve_gb']:g} GB")
    L.append(f"    KV bytes per token          {s['kv_bytes_per_token']:,.0f}")
    L.append(f"    price per GPU-hour          ${s['gpu_usd_hr']:,.4f}")
    L.append(f"    fleet size                  {s['fleet_gpus']:,.0f} GPUs")
    L.append(f"    conventions                 {s['gb_convention']}  {s['hours_per_year']} h/yr")
    L.append("")
    L.append("  ASSUMED BY YOU (not measured by anything here; the answer moves with these)")
    L.append(f"    utilisation                 {a['utilisation']:g}")
    L.append(f"    session-bound fraction      {a['session_bound_fraction']:g}")
    L.append(f"    KV pool fill                {a['kv_fill']:g}   (fixed by the measurement)")
    L.append(f"    workload mix                {a['workload_mix']}")
    L.append("")
    L.append("  DERIVED")
    L.append(f"    KV pool per GPU             {d['kv_pool_gb_per_gpu']:g} GB"
             f"   = {d['kv_tokens_per_gpu']:,.0f} tokens resident")
    L.append(f"    ceiling 1/keep              {d['identity_ceiling_1_over_keep']:.4f}x"
             f"   — an identity, NOT a prediction, see below")
    L.append(f"    capacity ratio band         {d['capacity_ratio_low']:.4f}x – "
             f"{d['capacity_ratio_high']:.4f}x   = ceiling ** measured exponent")
    L.append(f"    r_max                       {d['r_max']:.4f}   — sizes the pool only; "
             f"FALSIFIED as a predictor")
    L.append("")
    L.append(f"    GPUs, isolated arm          {d['gpus_isolated_arm']:,.1f}   (what you run)")
    L.append(f"    GPUs, shared arm            {d['gpus_shared_arm_low']:,.1f} – "
             f"{d['gpus_shared_arm_high']:,.1f}   (the arm that LEAKS)")
    L.append("")
    L.append(f"  >>> per-tenant isolation costs "
             f"{d['gpu_equivalents_attributable_to_isolation_low']:,.1f} – "
             f"{d['gpu_equivalents_attributable_to_isolation_high']:,.1f} GPU-equivalents")
    L.append(f"      = {_fmt_usd(d['usd_per_hour_low'])} – {_fmt_usd(d['usd_per_hour_high'])}/hour")
    L.append(f"      = {_fmt_usd(d['usd_per_year_low'])} – {_fmt_usd(d['usd_per_year_high'])}/year"
             f"   at your stated price and utilisation")
    L.append(f"      A RANGE, not a point: {d['why_a_range']}")
    L.append("")
    L.append(f"  interpretation: {out['interpretation']}")
    L.append("")
    L.append("  NOT MODELLED")
    for n in out["not_modelled"]:
        L.append(f"    - {n}")
    L.append("")
    L.append(f"  {d['identity_warning']}")
    L.append(f"  {c['superseded']}")
    return "\n".join(L)
