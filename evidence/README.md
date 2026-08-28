# Evidence

Every run in this directory was produced against the real synthetic application in
a real browser, using the commands in the root `README.md`. Nothing here is
hand-written or simulated.

Each directory contains `events.jsonl` — one JSON object per event, appended as the
run progressed — and screenshots where the run captured them. Replay runs also
contain `result.json`, the structured answer the caller received.

## What each run demonstrates

| Directory | Command | Shows |
|---|---|---|
| `discovery-balance/` | `discover` | The observe → decide → act loop against a live UI, and the draft capability it compiled |
| `discovery-subaccount/` | `discover` | The same loop hitting an irreversible action, escalating to a human, being authorised, and only then recording it |
| `replay-success/` | `replay` `12345` | Deterministic replay with no model involved, returning the declared output |
| `replay-business-outcome/` | `replay` `99999` | `MEMBER_NOT_FOUND` returned as an **answer**, not a failure |
| `replay-recovered/` | `replay` `12345` + injected fault | A transient host error detected, recovered from within its budget, and reported in `recovered_conditions` |
| `replay-blocked-irreversible/` | `replay` sub-account | The irreversible step refused for want of confirmation — and the application confirms nothing was written |
| `replay-confirmed-irreversible/` | `replay --confirm-irreversible` | The same capability proceeding once a human authorised it for that invocation |

The error case the brief asks for is `replay-business-outcome/`, and
`replay-recovered/` and `replay-blocked-irreversible/` cover the other two branches
of the result contract.

## Reproducing the fault injection

`replay-recovered/` used the target application's test-control endpoint to arm a
one-shot failure before the run:

```bash
curl -X POST http://127.0.0.1:8000/__control/fault -d "code=APPLICATION_ERROR&count=1"
```

That endpoint exists so runtime errors can be demonstrated on demand. It is part of
the practice target, not of the system under test, and a real integration would
have no equivalent.

## Redaction

Every event passes through the redaction boundary before it is written. Member
identifiers and balances appear as stable tokens such as `[REDACTED:a5f93581]` —
the same value yields the same token within a run, so a single member can be
followed through a trace without the log revealing who they are.

**Screenshots are not redacted.** A full-page capture of a member details screen
contains the name and balance as pixels, and the text-based redaction does not
touch images. Every record in this repository is synthetic, so nothing here is
sensitive, but the gap is real and is listed under `## Cuts` in `REPORT.md`.

## A note on the planner

These runs used `--planner scripted`, the deterministic stand-in, so the evidence is
reproducible byte for byte by anyone without an API key. The browser, the
application, the artifacts, the guardrails, the handoff and the result
classification are all real; only the choice of next action was made by hardcoded
logic rather than by a model. `ClaudePlanner` implements the identical interface and
is selected with `--planner claude`.
