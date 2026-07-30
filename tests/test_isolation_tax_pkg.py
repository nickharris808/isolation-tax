"""What the tool must never do: report a tax it cannot support, or hide one it can."""
from __future__ import annotations

import pytest

from isolation_tax import BOUNDED, EXACT, Request, measure, public_share_arm


def _r(ts, inp, out, chain, tenant=None):
    d = {"timestamp": ts, "input_length": inp, "output_length": out, "hash_ids": chain}
    if tenant:
        d["tenant"] = tenant
    return d


def test_labels_make_it_exact_and_their_absence_makes_it_bounded():
    rows = [_r(0, 1000, 100, [1, 2], "a"), _r(9e5, 1000, 100, [1, 3], "b")]
    assert measure(rows).mode == EXACT
    assert measure([{k: v for k, v in r.items() if k != "tenant"} for r in rows]).mode == BOUNDED


def test_a_partial_labelling_is_refused_rather_than_half_used():
    """Half a labelling is not a labelling. Using it would silently mix exact and inferred."""
    rows = [_r(0, 1000, 100, [1, 2], "a"), _r(9e5, 1000, 100, [1, 3])]
    res = measure(rows)
    assert res.mode == BOUNDED
    assert any("PARTIAL" in n for n in res.notes)


def test_two_tenants_sharing_a_document_lose_every_shared_hit():
    doc = [10, 11, 12]
    rows = [_r(0, 2048, 50, doc + [90], "acme"), _r(1000, 2048, 50, doc + [91], "globex")]
    res = measure(rows)
    assert res.mode == EXACT
    assert res.lost_hits == res.content_hits > 0, "no shared hit can survive across tenants"


def test_one_tenant_continuing_its_own_conversation_loses_nothing():
    rows = [_r(0, 1000, 200, [10, 11], "acme"),
            _r(9e5, 1400, 200, [10, 11, 12], "acme")]
    assert measure(rows).lost_hits == 0


def test_the_shared_system_prompt_is_reported_apart_not_hidden():
    """An earlier version excluded universal blocks from the tax entirely, which implied they were
    free. With T tenants the prompt is stored T times and costs T-1 cold starts."""
    rows = [_r(i * 1000, 1000, 50, [0, 100 + i], f"t{i}") for i in range(12)]
    res = measure(rows)
    assert res.universal_hits == 11
    assert res.lost_universal_hits == 11, "each new tenant pays its own cold start"
    assert res.lost_hits == 0, "and that cost must NOT be folded into the sharing tax"


def test_a_tiny_trace_refuses_to_guess_at_a_system_prompt():
    """On few requests a shared DOCUMENT is 'in every request' too. Calling it universal would
    move real cross-tenant sharing onto the cold-start line and hide it."""
    doc = [10, 11, 12]
    rows = [_r(0, 2048, 50, doc + [90], "acme"), _r(1000, 2048, 50, doc + [91], "globex")]
    res = measure(rows)
    assert res.universal_hits == 0, "too few requests to identify a system prompt"
    assert res.lost_hits == 3, "the shared document is counted as sharing, where it belongs"
    assert any("too few" in n for n in res.notes)


def test_an_empty_trace_abstains_rather_than_reporting_zero_tax():
    with pytest.raises(ValueError, match="vacuous"):
        measure([])


def test_a_request_without_hash_ids_is_refused_with_the_reason():
    with pytest.raises(ValueError, match="hash_ids"):
        measure([{"input_length": 10}])


def test_bounded_never_exceeds_exact_on_the_same_trace():
    """The floor must be a floor. If the bounded path ever reported MORE than the labelled truth,
    it would be inventing sharing that the labels say does not exist."""
    doc = [20, 21]
    rows = [_r(0, 2048, 50, doc + [1], "a"), _r(1e6, 4096, 50, doc + [2], "a"),
            _r(2e6, 2048, 50, doc + [3], "b")]
    exact = measure(rows).lost_hits
    bounded = measure([{k: v for k, v in r.items() if k != "tenant"} for r in rows]).lost_hits
    assert bounded <= exact + measure(rows).content_hits


def test_the_bounded_result_says_it_is_only_a_floor():
    rows = [_r(0, 1000, 50, [1, 2]), _r(1000, 1000, 50, [1, 3])]
    d = measure(rows).to_dict()
    assert d["exact"] is False
    assert "LOWER BOUND" in d["interpretation"]
    assert "per-user tenancy" in d["interpretation"]


# ---------------------------------------------------------------- the recovery arm

def test_the_public_share_arm_recovers_sharing_without_a_cross_tenant_leak():
    """Three tenants send the same public template; each also sends private content."""
    tmpl = [1, 2, 3]
    rows = [_r(i * 1000, 2048, 50, tmpl + [90 + i], f"t{i}") for i in range(3)]
    out = public_share_arm(rows, public_blocks=set(tmpl))
    assert out["arms"]["global"]["hits"] > out["arms"]["isolated"]["hits"], \
        "the fixture must have sharing to recover, or the test proves nothing"
    assert out["hits_recovered"] > 0
    assert out["vacuous"] is False


def test_an_arm_that_recovers_nothing_is_called_VACUOUS():
    """A scheme sharing nothing scores a perfect zero cross-tenant hits and is worthless.
    That has to be reported as vacuous, not as a clean result."""
    rows = [_r(i * 1000, 2048, 50, [1, 2, 90 + i], f"t{i}") for i in range(3)]
    out = public_share_arm(rows, public_blocks=set())      # nothing declared public
    assert out["hits_recovered"] == 0
    assert out["vacuous"] is True
    assert "VACUOUS" in out["verdict"]


def test_private_blocks_are_never_shared_across_tenants():
    """The whole safety claim. A block not declared public must not produce a cross-tenant hit."""
    secret = [7, 8]
    rows = [_r(0, 2048, 50, secret + [90], "acme"),
            _r(1000, 2048, 50, secret + [91], "globex")]
    out = public_share_arm(rows, public_blocks=set())
    assert out["arms"]["public_share"]["cross_tenant_hits"] == 0
    assert out["arms"]["public_share"]["hits"] == out["arms"]["isolated"]["hits"]


def test_tightening_the_publicness_rule_recovers_less():
    """The trade the arm exists to expose: stricter publicness, less recovery. If this were flat,
    the heuristic would not be doing anything."""
    rows = [_r(i * 1000, 2048, 50, [1, 2] + [90 + i], f"t{i}") for i in range(4)]
    loose = public_share_arm(rows, min_tenants=2)["hits_recovered"]
    tight = public_share_arm(rows, min_tenants=99)["hits_recovered"]
    assert loose > tight, "a stricter public rule must recover no more than a looser one"


def test_the_heuristic_declares_itself_a_heuristic():
    """N tenants sending a block may be N victims of one leaked document. The result must say so
    rather than let a reader take it for a safety proof."""
    rows = [_r(i * 1000, 2048, 50, [1, 2, 90 + i], f"t{i}") for i in range(3)]
    assert "HEURISTIC, not a proof" in public_share_arm(rows, min_tenants=2)["honest_limit"]
    assert "caller-supplied" in public_share_arm(rows, public_blocks={1})["honest_limit"]


def test_unlabelled_requests_are_refused_not_silently_counted_as_one_tenant():
    with pytest.raises(ValueError, match="tenant on EVERY request"):
        public_share_arm([_r(0, 100, 10, [1, 2])])


def test_exact_mode_stays_linear_on_a_hot_shared_block():
    """The performance property, as a test rather than a benchmark nobody re-runs.

    EXACT mode used to scan every prior TOUCHER of a block to answer "has my tenant been here?".
    A system-prompt block is touched by every request, so the pass was quadratic: exponent 1.7 at
    n=20k, and 1M requests did not finish in ten minutes. It now keeps a set of TENANTS per block
    and answers in O(1).

    This asserts the scaling, not a wall-clock number, so it does not go red on a slow machine.

    FLAKE FIXED 2026-07-29, and the docstring above was overconfident. A *ratio* of two wall-clock
    timings is still timing-sensitive: run inside the full 917-test suite this went red at ratio > 9
    while passing 3/3 standalone. Contention inflated the n=16k arm relative to the n=4k arm.

    The threshold is UNCHANGED at 9.0 -- loosening it would have hidden the very regression the test
    exists to catch. Instead each arm is now the MINIMUM of REPEATS runs. Scheduler contention can
    only ever make a measurement slower, so the minimum is the least-contaminated estimator of how
    fast the pass can go, and min-vs-min is the honest comparison. This is the repo's own lesson --
    gate a count, not a stopwatch -- applied as far as a pure-timing property allows.
    """
    import random
    import time

    REPEATS = 5

    def synth(n, rng):
        rows = []
        for i in range(n):
            chain = [0] + [rng.randrange(50) for _ in range(2)] + [10_000 + i]
            rows.append({"hash_ids": chain, "input_length": 512 * len(chain),
                         "output_length": 128, "timestamp": 10.0 * i,
                         "tenant": f"t{rng.randrange(500)}"})
        return rows

    timings = []
    for n in (4_000, 16_000):
        # Same seed per arm, so both arms see the same generator sequence and the only difference
        # between them is n. Re-seeding inside the loop keeps repeat k identical to repeat 0.
        best = None
        for _ in range(REPEATS):
            rows = synth(n, random.Random(7))
            t0 = time.perf_counter()
            measure(rows)
            dt = max(time.perf_counter() - t0, 1e-4)
            best = dt if best is None else min(best, dt)
        timings.append(best)

    # 4x the input must not cost anywhere near 16x the time. A quadratic pass scores ~16; the
    # linear one scores ~4. The threshold sits well clear of both so timing noise cannot flip it.
    ratio = timings[1] / timings[0]
    assert ratio < 9.0, (
        f"EXACT mode scaled {ratio:.1f}x for a 4x input (best of {REPEATS}) — the per-hit toucher "
        f"scan is back, and a provider-sized trace will not finish")


# ---------------------------------------------------------------- signoff-cert conformance

def _peer():
    import os
    import sys
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "signoff-cert", "src")
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)
    return pytest.importorskip("signoff_cert", reason="signoff-cert absent; conformance NOT checked")


def _arm_and_cert(**kw):
    from isolation_tax import build_certificate
    tmpl = [1, 2, 3]
    rows = [_r(i * 1000, 2048, 50, tmpl + [90 + i], f"t{i}") for i in range(4)]
    arm = public_share_arm(rows, public_blocks=set(tmpl))
    return arm, build_certificate(arm, n_requests=len(rows), n_tenants=4, **kw)


def test_the_certificate_is_admitted_and_the_bound_recomputes_to_zero():
    _peer()
    from signoff_cert.bounds import recompute
    from signoff_cert.verify import verify_certificate
    _, cert = _arm_and_cert(hmac_key=b"k" * 32, key_id="test")
    r = verify_certificate(cert, hmac_key=b"k" * 32)
    assert r.ok, r.reasons
    assert r.effective_verdict == "ADMITTED" and r.trust_level == "authenticated"
    assert recompute(cert) == (0.0, None)


def test_a_partial_enumeration_is_refused_by_name():
    """THE NEGATIVE CONTROL. Drop one request from the count and the 0.0 must stop being valid,
    or the round-trip above would pass even with the bound check bypassed."""
    _peer()
    import json as _json

    from signoff_cert.bounds import recompute
    _, cert = _arm_and_cert(hmac_key=b"k" * 32)
    broken = _json.loads(_json.dumps(cert))
    broken["evidence"]["enumerated"] -= 1
    value, err = recompute(broken)
    assert value is None
    assert err is not None and "partial enumeration" in err


def test_a_vacuous_arm_cannot_be_certified():
    """Zero leaks is not an achievement for a scheme that shares nothing."""
    from isolation_tax import build_certificate
    rows = [_r(i * 1000, 2048, 50, [1, 2, 90 + i], f"t{i}") for i in range(3)]
    with pytest.raises(ValueError, match="VACUOUS"):
        build_certificate(public_share_arm(rows, public_blocks=set()),
                          n_requests=3, n_tenants=3)


def test_gate_legs_hold_only_conditions_the_verdict_requires():
    """The reference verifier fails an ADMITTED certificate carrying any false leg. An earlier
    version put `publicness_caller_supplied` in legs -- metadata, not a safety condition -- and a
    heuristic-derived run was refused for it."""
    _peer()
    _, cert = _arm_and_cert(hmac_key=b"k" * 32)
    assert all(cert["gate"]["legs"].values()), "an ADMITTED cert must not carry a false leg"
    assert "publicness_caller_supplied" in cert["evidence"], "metadata belongs in evidence"


def test_unsigned_is_self_consistent_and_not_admitted_by_default():
    _peer()
    from signoff_cert.verify import verify_certificate
    _, cert = _arm_and_cert()
    assert "signature" not in cert
    assert verify_certificate(cert).ok is False
    assert verify_certificate(cert).trust_level == "self-consistent"
    assert verify_certificate(cert, require_authentication=False).ok is True


def test_the_scope_stops_zero_being_read_as_a_universal_guarantee():
    _peer()
    _, cert = _arm_and_cert(hmac_key=b"k" * 32)
    scope = cert["confidence"]["scope"]
    assert "THIS TRACE ONLY" in scope and "never that" in scope
    joined = " ".join(cert["honesty"]["non_claims"])
    assert "NOT a guarantee for other traffic" in joined
    assert "No timing channel is modelled" in joined
