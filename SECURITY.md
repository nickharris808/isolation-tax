# Security Policy

## Reporting a vulnerability

Please open a **private security advisory** on this repository
(`Security` -> `Report a vulnerability`) rather than a public issue.

## Scope

`isolation-tax` is a **measure-only** tool. It reports; it does not gate, admit, refuse, or modify any
system under test. Findings about what it *reports* are in scope. Findings that require it to be
wired into an enforcement path are out of scope, because that wiring is not part of this package.

## Responsible use

If this tool probes a remote endpoint, probe only infrastructure you operate or are authorised to
test.
