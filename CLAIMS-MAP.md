# CLAIMS-MAP — isolation-tax

**Tag: CLEAN. Licence: Apache-2.0.**

This file exists so the CLEAN tag is *auditable* rather than asserted.

## The line

Every independent claim in the corresponding filed specification terminates in a **physical
actuation** step: admitting or refusing an operation, and thereby granting or withholding a
physical resource.

`isolation-tax` counts cache hits. It grants and withholds nothing.

## Claims approached, and the step not performed

| Filed claim family | What it recites | What isolation-tax does instead |
|---|---|---|
| Tenant-partitioned cache admission | derive a per-tenant key; **admit or refuse a cache lookup accordingly, granting or withholding served capacity** | Counts, offline and after the fact, how many hits *would not* have survived such a partition. It performs no lookup, admits nothing, and is not in any serving path. |
| Certificate-carrying decision verified by a relying party | emit a certificate binding a decision to its evidence; **admit or refuse on it** | Emits a result object with its own limits attached. Nothing consumes it as a decision. |
| Fail-closed behaviour when evidence is insufficient | on insufficient evidence, refuse the operation | Exits `2` and reports that nothing was measured. That refuses to *state a number*, not to serve a request. |

## The objection worth taking seriously

*"A tool that tells you what isolation costs is a tool for deciding whether to isolate — that is the
claimed decision."*

It is not, and the distinction is the whole point. This produces a **retrospective count over a log**.
It has no access to a live request, no hook into any engine, and no output any admission path
consumes. A finance model that prices a policy does not practise the policy.

Note also what it deliberately cannot see: it reads block hashes and integers, never prompt text.
It cannot tell a shared system prompt from a leaked document — only that some blocks were reused
across sessions. A tool that could make an admission decision would need exactly the content this
one refuses to read.

## Enforcement

`oss/tools/check_measure_only.py` scans every CLEAN-tagged artifact and fails the build on an
actuation construct. Exit codes are deliberately not flagged: exiting non-zero to *report* a tax
above a threshold is a fact delivered to a shell, not the claimed actuation.

## The commercial boundary

`isolation-tax` tells you **what per-tenant isolation is costing you**.

It does not isolate anything, and it does not give you the sharing back. A cache-admission gate that
shares provably-public prefixes across tenants while refusing everything else — carrying a
machine-checked proof that the partition is sound, and emitting a certificate a relying party
verifies offline — is a separate, commercially licensed product covered by the filed claims above.
