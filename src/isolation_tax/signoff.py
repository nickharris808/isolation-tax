"""isolation_tax.signoff — emit the recovery arm's SAFETY result as a `signoff-cert/v1`.

WHAT IS CERTIFIABLE HERE, AND WHAT IS NOT
-----------------------------------------
The isolation tax itself is a cost measurement, not a claim with a false-pass bound. A percentage
of lost cache hits has no notion of "passing wrongly", so wrapping it in a certificate whose
required field is a false-pass bound would be a category error dressed as rigour.

What IS certifiable is the recovery arm's safety property:

    Over this trace, the public-share rule admitted ZERO cross-tenant hits on a non-public block.

That has a false-pass bound, and in EXACT mode the enumeration is complete over the trace, so the
bound is 0.0 *for that trace*. `signoff_cert.bounds.recompute` accepts a 0.0 under
`exhaustive-model-count` only when `enumerated >= state_space`, and refuses it by name otherwise --
which is exactly the check this wants.

THE SCOPE THAT STOPS 0.0 BEING MISREAD
--------------------------------------
The enumeration is exhaustive over THE REQUESTS SUPPLIED, not over possible traffic. A 0.0 here
means "no leak occurred on this trace", never "no leak can occur". Different traffic can contain a
block the publicness rule misclassifies, and if publicness came from the built-in heuristic rather
than a caller-supplied list, that is not even unlikely: N tenants sending a block may be N victims
of one leaked document. `confidence.scope` and `honesty.non_claims` carry both, because they are
the fields a reader actually reads.

certhead is the portfolio's first producer of this format; this is the second, and the first to
certify a property of a *policy* rather than of an arithmetic rewrite.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import __version__

__all__ = ["build_certificate", "SIGNOFF_UNAVAILABLE"]

SIGNOFF_UNAVAILABLE = (
    "the `signoff-cert` package is not installed, so the certificate cannot be sealed with the "
    "digests its verifier requires. Install it with:\n"
    "    pip install signoff-cert\n"
    "Emitting an unsealed certificate would produce a document that looks verifiable and is not."
)

UNSIGNED_NOTE = (
    "This certificate is UNSIGNED. Its digests make it tamper-evident but not attributable: a "
    "verifier grades it 'self-consistent' and, by default, does NOT admit it. That is correct -- "
    "isolation-tax holds no key, and a signature checked against a key embedded in the same "
    "document proves nothing about origin."
)


def build_certificate(arm: Dict[str, Any], *, n_requests: int, n_tenants: int,
                      trace_id: str = "unnamed-trace",
                      key_id: Optional[str] = None,
                      hmac_key: Optional[bytes] = None) -> Dict[str, Any]:
    """Seal the result of :func:`public_share_arm` as a ``signoff-cert/v1``.

    Raises ``ValueError`` if the arm was VACUOUS. A scheme that recovered nothing shares nothing,
    trivially leaks nothing, and certifying it would produce a document that is impeccable and
    worthless -- the exact failure the arm's own non-vacuity check exists to catch.
    """
    if arm.get("vacuous"):
        raise ValueError(
            "refusing to certify a VACUOUS arm: it recovered no hits, so it is indistinguishable "
            "from full isolation. Zero cross-tenant hits is not an achievement for a scheme that "
            "shares nothing.")
    try:
        from signoff_cert.canonical import content_sha256, semantic_sha256
    except ImportError as e:                                   # pragma: no cover - env dependent
        raise ImportError(SIGNOFF_UNAVAILABLE) from e

    # Cross-tenant hits on PUBLIC blocks are the arm's purpose, not a leak. Only a hit on a
    # private block is one. An earlier version counted all cross-tenant hits and therefore
    # certified REFUSED on a correctly-functioning arm.
    leaked = arm["arms"]["public_share"]["cross_tenant_hits_on_private_blocks"]
    recovered = arm["hits_recovered"]
    caller_supplied = "caller-supplied" in arm.get("public_selection", "")

    cert: Dict[str, Any] = {
        "schema": "signoff-cert/v1",
        "domain": "kv-isolation",
        "subject": {"id": f"isolation-tax-{__version__}-{trace_id}", "artifact_digests": {}},
        "claim": {"property": "the public-share rule admitted no cross-tenant cache hit on a "
                              "non-public block"},
        "verdict": "ADMITTED" if leaked == 0 else "REFUSED",
        "gate": {
            "name": "isolation_tax.public_share_arm",
            "provenance": "exhaustive replay of the supplied trace through three cache policies",
            "legs": {
                # A leg is a CONDITION THE VERDICT REQUIRES, and the reference verifier
                # enforces that: an ADMITTED certificate with any false leg fails
                # gate_consistency. `publicness_caller_supplied` was here and is metadata, not a
                # safety condition -- a heuristic-derived run is still a valid measurement of what
                # the rule enforced. Moved to evidence, where it belongs; the verifier caught it.
                "no_cross_tenant_hit_on_private_block": leaked == 0,
                # The load-bearing one. Without it, "share nothing" scores a perfect result.
                "recovered_something": recovered > 0,
            },
        },
        "confidence": {
            "method": "exhaustive-model-count",
            "false_pass_bound": 0.0 if leaked == 0 else 1.0,
            "bound_type": "false_pass_rate_upper",
            "coverage_level": 1.0,
            "n_samples": n_requests,
            "machine_checked": False,
            "paper": "exhaustive replay; the leaks-nothing property is proved separately in "
                     "theory/lean/PublicPrefixShare.lean",
            "scope": (f"EXHAUSTIVE OVER THIS TRACE ONLY -- {n_requests:,} requests, "
                      f"{n_tenants:,} tenants. A 0.0 bound means no leak OCCURRED here, never that "
                      f"none CAN occur. Different traffic may contain a block the publicness rule "
                      f"misclassifies."),
        },
        "evidence": {
            "enumerated": n_requests,
            "state_space": n_requests,
            "hits_recovered": recovered,
            "cross_tenant_hits_on_private_blocks": leaked,
            "public_blocks": arm.get("public_blocks"),
            "publicness_caller_supplied": caller_supplied,
        },
        "honesty": {
            "proven": ["no cross-tenant hit on a non-public block, over every request supplied"],
            "simulated": [],
            "aspirational": [],
            "non_claims": [
                "NOT a guarantee for other traffic. The enumeration is exhaustive over the "
                "requests supplied, not over the space of possible requests.",
                "Says NOTHING about whether the publicness rule is correct. This certifies that "
                "the rule was ENFORCED, not that it classifies correctly."
                + ("" if caller_supplied else
                   " Publicness here came from the built-in HEURISTIC ('produced by >= N "
                   "tenants'), which can be wrong: N tenants sending a block may be N victims of "
                   "one leaked document rather than N users of a public template."),
                "No timing channel is modelled and none is claimed closed. A partition removes the "
                "CONTENT oracle; contention signals are untouched.",
                "No speedup, latency or cost claim. This counts cache hits.",
            ],
        },
    }
    rec = content_sha256(cert)
    cert["digests"] = {"semantic_sha256": semantic_sha256(cert), "record_sha256": rec}
    if hmac_key:
        import hashlib
        import hmac as _hmac
        cert["signature"] = {"alg": "HMAC-SHA256", "key_id": key_id or "isolation-tax",
                             "sig": _hmac.new(hmac_key, rec.encode(),
                                              hashlib.sha256).hexdigest()}
    else:
        cert["honesty"]["non_claims"].append(UNSIGNED_NOTE)
        cert.pop("digests", None)
        rec = content_sha256(cert)          # the note is body; the digests must cover it
        cert["digests"] = {"semantic_sha256": semantic_sha256(cert), "record_sha256": rec}
    return cert
