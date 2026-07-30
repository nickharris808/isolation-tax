"""What the fleet model must never do: emit a dollar figure it cannot support.

A percentage is hard to misuse. A dollar figure is trivially quotable and, once it is in a slide,
indistinguishable from a measured one. So these tests are weighted toward the refusals -- the
places where an invented input could have become money -- and one hand-computed case that pins the
arithmetic end to end.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from isolation_tax import (FleetInputs, MEASURED_LADDER, OutOfEnvelope, FleetInputs as _FI,
                           check_envelope, fleet_delta, kv_bytes_per_token, max_kv_weight_ratio,
                           measure, retention_band, throughput_ratio_band)
from isolation_tax.cli import main


def _r(ts, inp, out, chain, tenant=None):
    d = {"timestamp": ts, "input_length": inp, "output_length": out, "hash_ids": chain}
    if tenant:
        d["tenant"] = tenant
    return d


# THE HAND-COMPUTED FIXTURE. Two tenants send the same 3-block document with a different tail.
#
#   total_blocks 8, total_hits 3 (blocks 10,11,12 on the second request), all 3 lost across tenants
#   stored_shared   = 8 - 3               = 5
#   stored_isolated = 5 + 3               = 8
#   keep            = 5/8                 = 0.625
#   r               = (60 - 14 - 4)/14    = 3
#   ratio           = (1+3)/(1+3*0.625)   = 4/2.875   = 1.3913043478...
#   shared arm      = 1000 * 2.875/4                  = 718.75 GPUs   (exact)
#   attributable    = 1000 - 718.75                   = 281.25 GPU-equivalents
#   annual          = 281.25 * $2.00 * 8760           = $4,927,500    (exact)
DOC = [10, 11, 12]
ROWS = [_r(0, 2048, 50, DOC + [90], "acme"), _r(1000, 2048, 50, DOC + [91], "globex")]
FULL = dict(gpu="A100-80GB", model_weights_gb=14.0, gpu_memory_gb=80.0, gpu_usd_hr=2.0,
            kv_bytes_per_token=196608, fleet_gpus=1000)


def _out(**over):
    kw = dict(FULL)
    kw.update(over)
    return fleet_delta(measure(ROWS), FleetInputs(**kw))


# ---------------------------------------------------------------- the arithmetic

def test_the_whole_chain_matches_a_hand_computed_case():
    """Every step above, checked. If one constant drifts, this is the test that catches it."""
    out = _out()
    m, d = out["measured_on_your_trace"], out["derived"]
    assert (m["total_blocks"], m["total_hits"], m["isolation_tax_hits"]) == (8, 3, 3)
    assert m["blocks_stored_shared_arm"] == 5
    assert m["blocks_stored_isolated_arm"] == 8
    assert m["kv_working_set_ratio_keep"] == pytest.approx(0.625)
    assert d["r_max"] == pytest.approx((80.0 - 14.0 - 4.0) / 14.0)
    # A BAND, not a point. The old point assertion used the (1+r)/(1+r*keep) closed form that the
    # A100 ladder falsified in direction; asserting it here would have pinned the refuted model in
    # place with a green test.
    lo, hi = throughput_ratio_band(0.625)
    assert d["capacity_ratio_low"] == pytest.approx(lo)
    assert d["capacity_ratio_high"] == pytest.approx(hi)
    assert d["identity_ceiling_1_over_keep"] == pytest.approx(1.6)
    assert lo < hi <= 1.6 + 1e-9, "the band must sit under the 1/keep arithmetic ceiling"
    assert d["usd_per_year_low"] < d["usd_per_year_high"], "a range, reported as one"


def test_bytes_per_token_is_the_config_formula_and_nothing_else():
    assert kv_bytes_per_token(48, 8, 128, 2) == 2 * 48 * 8 * 128 * 2 == 196608
    for bad in ((0, 8, 128, 2), (48, 8, 128, 0), (48, -1, 128, 2)):
        with pytest.raises(ValueError):
            kv_bytes_per_token(*bad)


def test_r_max_is_still_computed_but_no_longer_drives_the_ratio():
    """r_max is retained as a REPORTED diagnostic and must not reach the capacity ratio.

    The previous version of this test asserted `throughput_ratio(big, 0.5) < throughput_ratio(
    small, 0.5)` -- i.e. that a 32B gets a LOWER multiplier than a 7B. The A100 ladder measured the
    opposite (0.5B 1.66x, 7B 1.70x, 14B 1.80x), so that test was pinning a FALSIFIED claim in place
    with a green check. A test asserting a refuted claim is worse than no test, so it is deleted
    rather than adjusted.
    """
    assert max_kv_weight_ratio(80.0, 15.2, 4.0) == pytest.approx((80 - 15.2 - 4) / 15.2)
    assert max_kv_weight_ratio(80.0, 65.0, 4.0) < max_kv_weight_ratio(80.0, 15.2, 4.0)
    # The band depends ONLY on keep. If r ever re-enters it, this fails.
    import inspect
    sig = inspect.signature(throughput_ratio_band)
    assert list(sig.parameters) == ["keep"], (
        "throughput_ratio_band must not take r: the ladder falsified r as the driver")


def test_the_band_brackets_every_measured_ladder_point():
    """The calibration is only honest if it actually contains the data it was fitted from."""
    from isolation_tax.fleet import MEASURED_ENVELOPE
    keep = MEASURED_ENVELOPE["keep_at_measurement"]
    for row in MEASURED_LADDER:
        lo, hi = throughput_ratio_band(keep)
        assert lo - 1e-9 <= row["measured_ratio"] <= hi + 1e-9, (
            f"{row['model']}: measured {row['measured_ratio']} outside band [{lo:.3f}, {hi:.3f}]")


def test_retention_is_a_measured_spread_not_a_fitted_curve():
    """Three points on one GPU. If someone interpolates, the band stops being measured."""
    lo, hi = retention_band()
    assert 0.87 < lo < hi < 0.96
    assert hi > lo, "a degenerate band would hide the variation it exists to report"


def test_the_capped_ratio_never_exceeds_the_naive_identity():
    """1/keep is the working-set ratio restated and is the ceiling. Reporting a capacity ratio
    ABOVE it would mean the memory model had invented capacity the card does not have."""
    for keep in (0.1, 0.25, 0.625, 0.9, 1.0):
        lo, hi = throughput_ratio_band(keep)
        assert hi <= 1.0 / keep + 1e-9, f"band top {hi} exceeds the 1/keep ceiling at keep={keep}"
        assert lo <= hi
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            throughput_ratio_band(bad)


def test_a_trace_with_no_lost_hits_prices_at_zero_rather_than_at_something():
    """One tenant continuing its own conversation loses nothing. keep = 1, and the honest answer
    is a zero delta stated as a result -- not a small positive number rounded into existence."""
    rows = [_r(0, 1000, 200, [10, 11], "acme"), _r(9e5, 1400, 200, [10, 11, 12], "acme")]
    out = fleet_delta(measure(rows), FleetInputs(**FULL))
    assert out["measured_on_your_trace"]["kv_working_set_ratio_keep"] == 1.0
    assert out["derived"]["usd_per_year_low"] == 0.0
    assert out["derived"]["usd_per_year_high"] == 0.0
    assert "zero and that is a result" in out["interpretation"]


# ---------------------------------------------------------------- the refusals

def test_missing_fleet_parameters_abstain_and_name_every_one():
    """The whole point. No parameter is defaulted, and the error says which are absent so the
    caller does not have to guess -- guessing is how invented inputs get into a dollar figure."""
    with pytest.raises(ValueError) as e:
        fleet_delta(measure(ROWS), FleetInputs(gpu="A100-80GB"))
    msg = str(e.value)
    for flag in ("--model-weights-gb", "--gpu-memory-gb", "--gpu-usd-hr",
                 "--kv-bytes-per-token", "--fleet-gpus"):
        assert flag in msg
    assert "--gpu" in msg and "not defaulted" in msg


@pytest.mark.parametrize("attr,flag", [("model_weights_gb", "--model-weights-gb"),
                                       ("gpu_memory_gb", "--gpu-memory-gb"),
                                       ("gpu_usd_hr", "--gpu-usd-hr"),
                                       ("kv_bytes_per_token", "--kv-bytes-per-token"),
                                       ("fleet_gpus", "--fleet-gpus"),
                                       ("gpu", "--gpu")])
def test_every_required_parameter_is_individually_required(attr, flag):
    """A blanket check can pass while one field quietly carries a default. Drop each in turn."""
    with pytest.raises(ValueError, match="fleet parameters missing") as e:
        _out(**{attr: None})
    assert flag in str(e.value)


@pytest.mark.parametrize("fleet_gpus", [0, 0.0, -1, -1000])
def test_a_zero_or_negative_fleet_abstains_rather_than_dividing_by_zero(fleet_gpus):
    with pytest.raises(ValueError, match="infinity rather than an answer"):
        _out(fleet_gpus=fleet_gpus)


def test_a_model_that_does_not_fit_abstains_rather_than_reporting_a_negative_pool():
    """weights + reserve >= memory leaves negative KV. A negative r would produce a ratio below 1
    and a NEGATIVE dollar figure, which reads as isolation paying you."""
    with pytest.raises(ValueError, match="does not fit"):
        _out(model_weights_gb=78.0)


def test_a_trace_with_no_first_touch_blocks_abstains():
    """A zero denominator is the absence of a measurement, not a small number."""
    class _Fake:
        mode, is_exact = "exact", True
        n_requests = n_tenants = 1
        total_blocks = total_hits = 4
        lost_hits = lost_universal_hits = 0
    with pytest.raises(ValueError, match="absence of a measurement"):
        fleet_delta(_Fake(), FleetInputs(**FULL))


@pytest.mark.parametrize("field,value", [("utilisation", 0.0), ("utilisation", 1.5),
                                         ("session_bound_fraction", -0.2),
                                         ("session_bound_fraction", 2.0),
                                         ("kv_fill", 0.0), ("kv_fill", 1.01)])
def test_an_out_of_range_assumption_abstains_rather_than_scaling_the_money(field, value):
    with pytest.raises(ValueError, match=r"must be in \(0, 1\]"):
        _out(**{field: value})


def test_a_negative_price_and_a_nonsense_bytes_per_token_are_refused():
    with pytest.raises(ValueError, match="not a price"):
        _out(gpu_usd_hr=-1.0)
    with pytest.raises(ValueError, match="2 \\* layers"):
        _out(kv_bytes_per_token=0)


# ---------------------------------------------------------------- the honesty surface

def test_measured_and_assumed_are_labelled_and_kept_apart():
    """The buyer's own team has to be able to see which numbers came off their trace and which
    are their own assumptions. Folding them into one block is how a model gets read as a
    measurement."""
    out = _out()
    assert set(out) >= {"measured_on_your_trace", "calibrated_elsewhere_not_on_your_trace",
                        "supplied_by_you", "assumed_by_you", "derived", "not_modelled"}
    assert out["measured_on_your_trace"]["source"].startswith("isolation_tax.measure")
    # the three provider-side assumptions each carry a note saying they are not measured here
    a = out["assumed_by_you"]
    for k in ("utilisation", "session_bound_fraction", "kv_fill"):
        assert k in a
        assert "NOT measured here" in a[f"{k}_note"]
    # the calibration is attributed to hardware that is NOT the caller's
    c = out["calibrated_elsewhere_not_on_your_trace"]
    # eta -> results/data/statefabric/gpu/elision_throughput.json .cells[0].eta_kv_bound_median
    # (elision_gather_efficiency). This asserted 0.998 -- a figure that appears in NO revision of
    # that cert -- and so held a fabricated number in place with a green test. The measured value is
    # 0.8644 and the claim that mattered, "no gather penalty", was never true.
    #
    # WITHDRAWN 2026-07-30. This asserted 0.8644 -- which was ALSO wrong: it is the UPPER median
    # (med() returned xs[len(xs)//2], the 75th percentile of the four KV-bound cells; the true median
    # is 0.70685). And the metric is definitionally vacuous -- eta == byte_ratio *
    # throughput_ratio_same_batch to 2.2e-16, while make_cache() builds BOTH arms contiguous, so the
    # harness never gathers. A test pinning a vacuous metric to a wrong statistic is worse than no
    # test. It now asserts the WITHDRAWAL, so re-introducing a value fails here.
    assert c["gather_efficiency_eta"] is None, (
        "eta must stay withdrawn until a harness that actually scatters measures it; "
        f"got {c['gather_efficiency_eta']!r}")
    # The old `eta < 0.9` guard is subsumed: a withdrawn metric cannot be near 1.0 either. Kept as a
    # conditional so it re-arms automatically the moment a real gather measurement is registered.
    if c["gather_efficiency_eta"] is not None:
        assert c["gather_efficiency_eta"] < 0.9, (
            "the gather is NOT free; an eta at or near 1.0 is the claim this estate published wrongly")
    assert "0.998" in c["eta_note"] and "WITHDRAWN" in c["eta_note"].upper(), (
        "both retractions must travel with the field: the 0.998 that never existed, and the "
        "withdrawal of the metric itself")
    assert "A100-80GB" in c["measured_on"]
    assert "exponent" in c["retention_exponent_definition"]
    assert "modelling choice, not a measurement" in c["carry_is_a_form_choice"]


def test_every_input_appears_in_the_output_so_the_arithmetic_can_be_re_run():
    """No hidden constants. The reserve in particular used to live only in a source file."""
    out = _out(reserve_gb=6.0, utilisation=0.7, session_bound_fraction=0.4)
    s, a, d = out["supplied_by_you"], out["assumed_by_you"], out["derived"]
    assert s["model_weights_gb"] == 14.0 and s["gpu_memory_gb"] == 80.0
    assert s["gpu_usd_hr"] == 2.0 and s["kv_bytes_per_token"] == 196608
    assert s["fleet_gpus"] == 1000 and s["reserve_gb"] == 6.0 and s["hours_per_year"] == 8760
    assert "1e9 bytes" in s["gb_convention"]
    assert (a["utilisation"], a["session_bound_fraction"], a["kv_fill"]) == (0.7, 0.4, 1.0)
    # and the derived figures are reproducible from exactly those, by hand, at BOTH ends of the band
    for end, key in (("low", "usd_per_year_low"), ("high", "usd_per_year_high")):
        ratio = throughput_ratio_band(0.625)[0 if end == "low" else 1]
        saved = 1000 * 0.4 * (1 - 1 / ratio)
        assert d[key] == pytest.approx(saved * 2.0 * 8760 * 0.7), f"the {end} end does not re-run"


def test_the_identity_is_reported_and_labelled_as_not_a_prediction():
    """1/keep is the working-set ratio restated. This estate has already retracted one headline
    that was an identity wearing a prediction's clothes."""
    d = _out()["derived"]
    assert d["identity_ceiling_1_over_keep"] == pytest.approx(1.6)
    assert "NOT a prediction" in d["identity_warning"]


def test_it_says_it_is_a_model_and_names_the_0_997x_end_to_end_result():
    out = _out()
    assert out["status"].startswith("MODEL")
    joined = " ".join(out["not_modelled"])
    assert "0.997x" in joined
    assert "not a benchmark" in joined
    assert "ADDED PREFILL COMPUTE" in joined, "the channel this figure omits must be named"
    assert "EVICTION" in joined
    assert "does not recommend lifting it" in joined, "the shared arm is the one that leaks"


def test_no_speedup_language_anywhere_in_the_emitted_report():
    """Ratios and dollars, never 'faster'. The repo has a standing no-speedup-claim rule, and a
    capacity model is exactly the kind of artifact that leaks one by accident."""
    import re

    from isolation_tax.fleet import render
    out = _out()
    text = (json.dumps(out) + "\n" + render(out)).lower()
    # "accelerator" the noun is hardware and is fine; "accelerates" the verb is a claim.
    for banned in ("speedup", "speed-up", "accelerates", "acceleration", "throughput gain",
                   "x faster", "% faster", "improvement"):
        assert banned not in text, f"{banned!r} appeared in the report"
    # "faster" is allowed in exactly one place: the sentence that DENIES the claim.
    for m in re.finditer("faster", text):
        assert "claims anything runs faster" in text[max(0, m.start() - 60):m.end()], \
            "the word 'faster' appeared outside its own disclaimer"


def test_a_bounded_trace_says_the_dollar_figure_is_under_stated():
    rows = [{k: v for k, v in r.items() if k != "tenant"} for r in ROWS]
    out = fleet_delta(measure(rows), FleetInputs(**FULL))
    assert out["measured_on_your_trace"]["exact"] is False
    assert "FLOOR" in out["interpretation"] and "under-stated" in out["interpretation"]
    assert any("NOT NETTED OUT" in n for n in out["not_modelled"]), \
        "the two error directions must not be quietly netted into a bound"


# ---------------------------------------------------------------- the CLI contract

def _write(tmp_path, rows):
    p = tmp_path / "trace.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return str(p)


def test_cli_exit_0_on_a_measured_fleet_and_the_json_carries_the_dollars(tmp_path, capsys):
    argv = ["fleet", _write(tmp_path, ROWS), "--gpu", "A100-80GB", "--model-weights-gb", "14",
            "--gpu-memory-gb", "80", "--gpu-usd-hr", "2.0", "--kv-bytes-per-token", "196608",
            "--fleet-gpus", "1000", "--json"]
    assert main(argv) == 0
    d = json.loads(capsys.readouterr().out)["derived"]
    lo_ratio, hi_ratio = throughput_ratio_band(0.625)
    for ratio, key in ((lo_ratio, "usd_per_year_low"), (hi_ratio, "usd_per_year_high")):
        assert d[key] == pytest.approx(1000 * (1 - 1 / ratio) * 2.0 * 8760)


def test_cli_exit_2_when_the_fleet_parameters_are_absent(tmp_path, capsys):
    assert main(["fleet", _write(tmp_path, ROWS)]) == 2
    err = capsys.readouterr().err
    assert "ABSTAIN" in err and "--fleet-gpus" in err


def test_cli_exit_2_on_a_zero_fleet(tmp_path, capsys):
    argv = ["fleet", _write(tmp_path, ROWS), "--gpu", "A100", "--model-weights-gb", "14",
            "--gpu-memory-gb", "80", "--gpu-usd-hr", "2.0", "--kv-bytes-per-token", "196608",
            "--fleet-gpus", "0"]
    assert main(argv) == 2
    assert "infinity rather than an answer" in capsys.readouterr().err


def test_cli_derives_bytes_per_token_from_the_config_and_refuses_a_contradiction(tmp_path, capsys):
    base = ["fleet", _write(tmp_path, ROWS), "--gpu", "A100", "--model-weights-gb", "14",
            "--gpu-memory-gb", "80", "--gpu-usd-hr", "2.0", "--fleet-gpus", "1000",
            "--layers", "48", "--kv-heads", "8", "--head-dim", "128", "--dtype-bytes", "2"]
    assert main(base + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["supplied_by_you"]["kv_bytes_per_token"] == 196608

    assert main(base + ["--kv-bytes-per-token", "1024"]) == 2
    assert "contradicts the config" in capsys.readouterr().err

    partial = [x for x in base if x not in ("--kv-heads", "8")]
    assert main(partial) == 2
    assert "partial model config" in capsys.readouterr().err


def test_cli_exit_1_only_when_the_threshold_is_exceeded(tmp_path, capsys):
    argv = ["fleet", _write(tmp_path, ROWS), "--gpu", "A100", "--model-weights-gb", "14",
            "--gpu-memory-gb", "80", "--gpu-usd-hr", "2.0", "--kv-bytes-per-token", "196608",
            "--fleet-gpus", "1000"]
    assert main(argv + ["--fail-over-usd", "1000000"]) == 1
    assert "exceeds --fail-over-usd" in capsys.readouterr().err
    assert main(argv + ["--fail-over-usd", "9000000"]) == 0


def test_the_human_report_shows_every_input(tmp_path, capsys):
    """A buyer's team re-runs the arithmetic off the printed page or they do not believe it."""
    argv = ["fleet", _write(tmp_path, ROWS), "--gpu", "A100-80GB", "--model-weights-gb", "14",
            "--gpu-memory-gb", "80", "--gpu-usd-hr", "2.0", "--kv-bytes-per-token", "196608",
            "--fleet-gpus", "1000", "--utilisation", "0.85"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    for frag in ("MEASURED", "MEASURED ELSEWHERE", "SUPPLIED BY YOU", "ASSUMED BY YOU", "DERIVED",
                 "NOT MODELLED", "MEASURED ENVELOPE", "14 GB", "80 GB", "196,608", "$2.0000",
                 "1,000 GPUs", "0.85", "8760 h/yr", "0.625000", "1.4553x", "1.5454x", "1.6000x",
                 "0.997x", "FALSIFIED as a predictor"):
        assert frag in out, f"the report omits {frag!r}"


def test_the_entry_point_exposes_fleet_in_a_cold_process():
    """--help is the first thing a buyer runs. If the subcommand is not listed it does not exist,
    and a stale in-process import cannot be what says otherwise."""
    import os
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = dict(os.environ, PYTHONPATH=src)
    r = subprocess.run([sys.executable, "-m", "isolation_tax.cli", "--help"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "fleet" in r.stdout
    h = subprocess.run([sys.executable, "-m", "isolation_tax.cli", "fleet", "--help"],
                       capture_output=True, text=True, env=env)
    assert h.returncode == 0, h.stderr
    assert "--fleet-gpus" in h.stdout and "0.997x" in h.stdout


# ------------------------------------------- the carry to a keep the ladder did not run at
# These are the regression tests for a real defect. The first version of the band multiplied the
# 1/keep ceiling by a retention measured at keep = 0.5282 and applied it at EVERY keep. Nobody ever
# evaluated it at the keeps that actually occur. At keep = 1 -- isolation cost you nothing, so
# nothing was lost and nothing is there to recover -- it returned 0.879x, i.e. a NEGATIVE annual
# saving off a zero-sized problem. Mooncake's real keep is 0.9348 and its band straddled 1.0, so the
# flagship worked example was going to print part of a negative number.
#
# NOTE ON THE AUTHORITY THESE TESTS USED TO CITE. Earlier versions justified the >= 1.0 floor with
# `elision_never_slower`. They should not have: that theorem is a proof over a DECLARED Nat cost
# model, not a wall-clock claim, and this estate's own L4 certificate measures ratios below 1.0.
# The defect above is real on its own terms -- pricing a saving on a problem of size zero is an
# arithmetic error, not a violated theorem -- and that is the only ground these tests stand on now.

def test_keep_of_one_means_no_delta_and_the_band_says_so_exactly():
    """Isolation cost nothing, so there is nothing to price. Not 0.879x. Not 1.02x. Exactly 1."""
    assert throughput_ratio_band(1.0) == (1.0, 1.0)


# ------------------------------------------- the regime the band cannot express
# RETRACTION, 2026-07-30. The test that used to sit here asserted `lo >= 1.0` at every keep and
# cited `elision_never_slower` as its authority. It was wrong twice and it is kept here as the
# record rather than deleted.
#
#   (1) IT COULD NOT FAIL. `throughput_ratio_band(keep) = (1/keep) ** exponent` with keep in (0, 1],
#       so (1/keep) >= 1 and the result is >= 1 for ANY strictly positive exponent. The assertion
#       held identically at exponent 0.001 and at exponent 50. It tested the sign of a constant,
#       not the constant, and it would have passed over a band that was off by a factor of 1000.
#       `test_the_exponents_are_the_ladder_not_merely_positive` below is the assertion it should
#       have been: it pins the exponents to the runs they came from and fails at 0.001 or at 50.
#
#   (2) IT MISCITED THE THEOREM, WHICH THE ESTATE'S OWN CERTIFICATE CONTRADICTS IN WALL-CLOCK.
#       `elision_never_slower` (serving_limits/formal/lean/CertifiedElision.lean) is a statement
#       about a DECLARED per-object cost model over Nat parameters cFull/cElide. It is not a claim
#       that any measured ratio is >= 1.0x, and it cannot be, because
#       results/data/statefabric/gpu/elision_throughput_l4_0p5b.json measures
#       `throughput_ratio_same_batch` BELOW 1.0 in 4 of its 9 cells -- 0.9908 / 0.9466 / 0.9009 /
#       0.9941 at batch 2 / 4 / 8 / 16, the minimum being 0.9009 at batch 8. So a band that returns
#       >= 1.0 everywhere does not "agree with the theorem"; it is a model that CANNOT EXPRESS a
#       regime this estate has already measured, and the tests below make that a checked fact
#       instead of an unstated one.
#
# The L4 cells are same-batch decode-step measurements; the A100 ladder the band is built from ran
# each arm at its OWN maximum resident batch (1492/2827, 264/501, 61/116 -- every one of them far
# above 32). They are different comparisons, which is exactly why the band's >= 1.0 floor is a
# property of its FORM and its measured support, and never evidence about the small-batch regime.

# batch -> throughput_ratio_same_batch, verbatim from
# results/data/statefabric/gpu/elision_throughput_l4_0p5b.json (L4, Qwen2.5-0.5B-Instruct, ctx 4096,
# keep_frac 0.5282). All nine cells, so the four below 1.0 cannot be read as a cherry-pick.
L4_SAME_BATCH_CELLS = (
    (1, 1.0033668403170275), (2, 0.9907947161315749), (4, 0.9466411908142343),
    (8, 0.9009241984066175), (16, 0.9941284034310489), (32, 1.1769713790377754),
    (64, 1.7522394901950207), (128, 1.742330157332512), (256, 1.7697762295585946),
)
L4_CERT = "results/data/statefabric/gpu/elision_throughput_l4_0p5b.json"


def test_the_exponents_are_the_ladder_not_merely_positive():
    """The replacement for the tautology. `lo >= 1.0` holds for any positive exponent, so it says
    nothing about whether the band is the measurement. This does: each exponent must reproduce the
    run it was derived from, at the keep it was measured at, to floating-point equality.

    Fails at exponent 0.001. Fails at exponent 50. Fails if a row of MEASURED_LADDER is edited.
    """
    from isolation_tax.fleet import RETENTION_EXPONENT_LOW, RETENTION_EXPONENT_HIGH
    keep = 0.5282
    ratios = sorted(row["measured_ratio"] for row in MEASURED_LADDER)
    lo, hi = throughput_ratio_band(keep)
    assert lo == pytest.approx(ratios[0], rel=1e-12), (
        f"the band's low end {lo} is not the slowest ladder run {ratios[0]} at its own keep")
    assert hi == pytest.approx(ratios[-1], rel=1e-12), (
        f"the band's high end {hi} is not the fastest ladder run {ratios[-1]} at its own keep")
    # And the exponents themselves, pinned. A band whose endpoints happened to land right through a
    # compensating pair of errors would still be the wrong band.
    assert RETENTION_EXPONENT_LOW == pytest.approx(0.7983381150058174, rel=1e-12)
    assert RETENTION_EXPONENT_HIGH == pytest.approx(0.9260895149816523, rel=1e-12)


def test_the_band_cannot_express_the_four_cells_that_measured_below_one():
    """The band's floor at 1.0x is a property of the FORM (1/keep) ** positive, not a fact about
    hardware, and this estate has MEASURED the form to be wrong in that direction.

    4 of the 9 same-batch cells in elision_throughput_l4_0p5b.json are below 1.0x -- batch 2, 4, 8
    and 16, minimum 0.9009x at batch 8. The band cannot return any of them at any keep. That is a
    KNOWN BLIND SPOT, not agreement with `elision_never_slower`, which is a proof over a declared
    Nat cost model and is not evidence about wall-clock at all.

    This fails if anyone re-reads the floor as a hardware claim by making the band able to dip below
    1.0 without also retracting the prose, and it fails if the measured cells stop being what they
    are.
    """
    below = [(b, r) for b, r in L4_SAME_BATCH_CELLS if r < 1.0]
    assert len(below) == 4, f"expected 4 of 9 measured cells below 1.0x, got {len(below)}"
    assert [b for b, _ in below] == [2, 4, 8, 16]
    assert min(r for _, r in below) == pytest.approx(0.9009241984066175, rel=1e-12)

    # The form's reachable range, over the whole legal domain of keep. Its infimum is exactly 1.0.
    lows = [throughput_ratio_band(0.02 + 0.005 * i)[0] for i in range(0, 197)
            if 0.02 + 0.005 * i <= 1.0]
    assert min(lows) == pytest.approx(1.0, abs=1e-12), (
        "the band's low end no longer bottoms out at exactly 1.0; the blind spot documented here "
        "has moved and the prose in fleet.py and the README must move with it")
    for batch, ratio in below:
        assert all(lo > ratio for lo in lows), (
            f"the band can now return {ratio} (batch {batch}) -- update this test and the "
            f"NOT MODELLED disclosure together, never one without the other")

    # And the ladder the band IS built from never ran anywhere near those batches: every arm of it
    # sat above 32, which is where the L4 sweep crosses back over 1.0.
    for row in MEASURED_LADDER:
        assert row["batch_full"] > 32 and row["batch_elided"] > 32, (
            f"{row['model']} ran at batch {row['batch_full']}/{row['batch_elided']}, inside the "
            f"regime the L4 sweep measured below 1.0x -- the band's support claim is no longer true")


def test_the_report_discloses_the_regime_the_band_cannot_express(tmp_path, capsys):
    """A blind spot a buyer cannot see is not disclosed. The sub-1.0 measurement, the batch floor
    and the cert path must all reach the printed page, or this fails."""
    argv = ["fleet", _write(tmp_path, ROWS), "--gpu", "A100-80GB", "--model-weights-gb", "14",
            "--gpu-memory-gb", "80", "--gpu-usd-hr", "2.0", "--kv-bytes-per-token", "196608",
            "--fleet-gpus", "1000"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    for frag in ("SMALL BATCH", "0.9009", "4 of 9", "elision_throughput_l4_0p5b.json"):
        assert frag in out, f"the report never discloses {frag!r}: the blind spot is undisclosed"


def test_the_cert_backing_the_blind_spot_still_says_what_this_test_says():
    """The four cells above are transcribed literals. Transcribed literals rot. When the estate
    tree is reachable, re-read them from the certificate itself rather than trusting the copy."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    path = os.path.join(root, L4_CERT)
    if not os.path.exists(path):
        pytest.skip(f"{L4_CERT} is not reachable from this checkout (staged package); the "
                    f"transcribed cells in L4_SAME_BATCH_CELLS are unverified here")
    cert = json.load(open(path))
    cells = [(c["batch"], c["throughput_ratio_same_batch"])
             for m in cert["cells"] for c in m["cells"]]
    assert len(cells) == 9, f"the cert now has {len(cells)} cells, not 9"
    for (b_lit, r_lit), (b_cert, r_cert) in zip(L4_SAME_BATCH_CELLS, cells):
        assert b_lit == b_cert
        assert r_lit == pytest.approx(r_cert, rel=1e-15), (
            f"batch {b_cert}: this file says {r_lit}, {L4_CERT} says {r_cert}")


def test_the_band_low_end_never_exceeds_the_high_end():
    """Separated out of the retracted test, which bundled this real check in with the tautology and
    so was never run for its own sake."""
    keep = 0.02
    while keep <= 1.0:
        lo, hi = throughput_ratio_band(keep)
        assert lo <= hi, f"band inverted at keep={keep}: [{lo}, {hi}]"
        keep += 0.005


def test_the_band_is_monotone_in_keep_so_less_lost_sharing_is_never_worth_more():
    """More sharing survived isolation => less to recover. A band that rose with keep would pay
    more for a smaller problem, which is the shape the multiplicative form had near keep = 1."""
    prev = None
    for keep in (0.3, 0.5, 0.5282, 0.7, 0.9, 0.9348, 0.99, 1.0):
        lo, hi = throughput_ratio_band(keep)
        if prev is not None:
            assert lo <= prev[0] + 1e-12 and hi <= prev[1] + 1e-12, f"not monotone at keep={keep}"
        prev = (lo, hi)


def test_a_real_traces_keep_prices_a_positive_saving():
    """Mooncake keep = 0.934839, the worked example in the README. Under the form this replaced the
    band was [0.941x, 1.020x] and the low end billed the buyer for isolating."""
    lo, hi = throughput_ratio_band(0.934839)
    assert 1.0 < lo < hi < 1.1, f"Mooncake's keep gives [{lo}, {hi}]"


# ------------------------------------------- the envelope refuses rather than transplants
@pytest.mark.parametrize("over,axis", [
    (dict(gpu="H100"), "GPU"),
    (dict(gpu_memory_gb=141.0), "GPU memory"),
    (dict(model_weights_gb=40.0), "model weights"),
    (dict(kv_fill=0.8), "KV pool fill"),
])
def test_each_envelope_axis_abstains_on_its_own(over, axis):
    """One axis out of range is enough. Each must fire alone, or a check that only fires in
    combination is a check that never fires."""
    with pytest.raises(OutOfEnvelope, match=axis):
        _out(**over)


def test_the_envelope_names_every_breach_not_just_the_first():
    with pytest.raises(OutOfEnvelope) as e:
        _out(gpu="H100", kv_fill=0.5)
    assert "2 axes" in str(e.value) and "GPU" in str(e.value) and "KV pool fill" in str(e.value)


def test_the_measured_configuration_itself_is_inside_its_own_envelope():
    """If the runs the band came from would be refused by the envelope built from them, the
    envelope is wrong. This is the check that the bounds were read off the data, not chosen."""
    for row in MEASURED_LADDER:
        assert check_envelope(FleetInputs(
            gpu="A100-80GB", model_weights_gb=row["weights_gb"], gpu_memory_gb=80.0,
            gpu_usd_hr=1.0, kv_bytes_per_token=196608, fleet_gpus=1, kv_fill=1.0)) == []
