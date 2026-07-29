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
