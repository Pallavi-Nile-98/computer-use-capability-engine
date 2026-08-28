# Design report

A model works out a flow inside a legacy UI once; the flow becomes a typed,
reviewable capability; the capability replays deterministically with no model in the
decision loop. Both halves run against a real browser — the live model-driven run is
in `evidence/discovery-balance-live/`, with deterministic runs covering every branch
of the result contract alongside it.

One finding shaped several decisions below. The system passed 55 tests against a
deterministic stand-in planner; running it with the real model then exposed three
defects the stand-in had hidden — a dropped final action that produced a capability
with no declared outputs, a checkpoint asserting on the balance the model had just
read, and observations that reported what was clickable but not what was readable. A
stand-in written by the same author in the same hour agrees with the code about
things neither has checked.

## Architecture

The split is where cost and determinism change. **Discovery** (`engine.py`,
`planner.py`) puts a model in the loop: observe, decide one bounded action, act,
repeat. **Replay** (`replay.py`) executes a recorded capability with no model at
all. `compiler.py` between them generalises a specific run into a reusable pattern.

`surface.py` is the load-bearing boundary. It defines a `Surface` protocol — nine
methods — and *nothing above it imports Playwright*. A recorded flow says "click the
button named Search"; the adapter decides what that means. Two adapters ship: a
Playwright one, and an in-memory one modelling the target application's state
machine. The second is not a mock — it is a different implementation, which keeps
the suite at seven seconds and demonstrates the seam is real rather than claimed.

Three kinds of knowledge are kept apart because they change on different clocks. The
**flow** changes when a capability does. The **application's vocabulary** of endings
— how this vendor product reports a missing member, a dropped session — lives in
`profiles.py`, keyed by product and shared across capabilities and institutions.
**Tenant deviations** are narrow overrides. Collapsing these is the obvious move and
the mistake: twenty capabilities would carry twenty copies of the same outcome
rules, drifting apart independently.

Single process, no queues. The brief says scaling infrastructure is not rewarded,
and none of the judgment being evaluated lives in a message broker.

The target application is built rather than borrowed: no third party's terms are
involved, all data is synthetic, and runtime errors can be triggered on demand.
Since runtime error handling is weighted third of eight criteria, waiting for a
session timeout to happen by luck on someone else's demo site is not viable. It is
deliberately hostile — table layout, no test ids, a label naming a field the form
calls something else, a POST-then-redirect mid-flow.

## Artifact schema

`CapabilityArtifact` in `models.py`, strict Pydantic, `extra="forbid"`. It serves a
calling agent, a human reviewer and the replay engine at once, and its shape follows
from that.

**References, not recorded values.** A step stores `"${member_id}"`, never `12345`.
This one line is what turns a run into a capability.

**Locators state intent and justify themselves.** A locator names a strategy
(`role`, `label`, `text`, `placeholder`, `css`, `coordinate` — best to worst) and a
*required* `rationale`. Intent rather than mechanism is what lets another adapter
resolve the same flow; the mandatory rationale is what makes approval meaningful,
since a reviewer reads it to judge whether this survives the next release.

**The contract is separate from the steps.** Typed `inputs` and `outputs` sit apart
from the flow, so an agent deciding whether to call a capability need not read its
implementation. `catalog.py` renders approved artifacts as function-calling tools
from exactly this.

**Known endings are declared data, not caught exceptions.** `business_outcomes`
lists legitimate answers that are not success; `recovery_rules` lists transient
conditions and the bounded response *authorised* for each. Recovery in the artifact
rather than the engine means the approver can see that a capability may reload twice
on a host error. Buried in a retry loop, nobody would review it.

**A checkpoint is mandatory and comes from the planner.** The schema rejects a
completing action without one. Only the planner has seen the finished screen, so
only it can honestly name a condition proving the goal was reached.

**Recordings are validated, not trusted.** The live model asserted on `$4,281.73` —
the value it had just extracted — as its checkpoint. That passes for one member and
fails for every other, and it fails *as a checkpoint*, so a correct replay is
reported as a hard failure: worse than no checkpoint, because it looks like
verification. `validate_recording()` rejects a capability whose checkpoint contains
an extracted output or supplied parameter, and one whose read step returns the text
its own locator searched for (meaning it selected a label rather than the value).
Prompting reduces how often a model does this; validation decides whether the result
may be saved. Only one is a guarantee.

**Approval is enforced.** Artifacts are born `draft`; replay refuses a draft;
`approved` requires a recorded name and timestamp; a specialised tenant copy reverts
to draft, because approval does not survive an edit. `allowed_actions` is *derived*
from what the recording used, so a capability that only read cannot later navigate —
a hand-written permission list rots, a computed one cannot.

The model's transcript is evidence, not contract. It appears nowhere in the artifact,
so a capability's meaning does not depend on the prompt that discovered it.

## Determinism & error handling

Replay consults no model. It validates parameters against the contract *before*
opening a browser — a typo should cost nothing — resolves references, intersects the
deployment policy with the artifact's allowlists, executes each step against its
primary locator then any fallbacks, and verifies the checkpoint. Every step running
without error is not the same as reaching the goal: a click can succeed while the
application quietly does nothing, so success is asserted against an independent
condition.

When a step fails, three questions in order:

1. Does the screen match a declared business outcome? → an **answer** for the caller.
2. Does it match an authorised recovery rule? → **bounded** retry.
3. Neither? → **unknown**; stop, capture evidence, escalate.

Order matters: a "member not found" page and a broken page are both "the step
failed" from the surface's point of view, and only the artifact knows which is which.

The contract distinguishes `success`, `business_outcome`, `recoverable_failure`,
`hard_failure` and `intervention_required`. `MEMBER_NOT_FOUND` and
`PERMISSION_DENIED` are business outcomes and exit zero — an agent receiving them as
errors retries forever against a member who was never there. `recoverable_failure`
means a *known* condition outlived its budget and is safe to retry later;
`hard_failure` needs a person. `recovered_conditions` reports conditions handled
successfully, so a caller can see a capability that technically works but recovers
every time — that one needs re-recording, not celebrating.

Retries are bounded three ways, because budgets compose: per rule, per run, and per
full flow restart. Three rules with two attempts each is six recoveries before any
restart, which is how error handling becomes the outage. A recovery that invalidates
mid-flow state — a dropped session — replays from step one rather than resuming into
a session that no longer exists.

Two distinct problems are handled separately: fallback locators cover "the control
moved or was renamed"; the retry policy covers "the page had not settled".
Collapsing them means retrying a genuinely absent control for nothing.

UI drift is the secondary concern, and the response is the locator strategy above
plus checkpoint failure as a signal — failing for one tenant is drift, for all
tenants a vendor release.

## Heterogeneity & multi-tenant

**Other surfaces.** The seam is `Surface`'s nine methods. A legacy-web adapter
walking framesets, or a desktop adapter over OS accessibility APIs, implements them
and the artifact, compiler, replay engine, policy and handoff are untouched.
`Locator.frame` already exists for framesets; `current_url`/`reload`/`navigate` map
to a window handle, a refresh, and reopening an application.

What makes this credible is that observations are *semantic*, not markup:
`observe()` returns visible text, controls with accessible role and name, and
readable label/value pairs — roughly what a human operator sees, and producible from
a desktop accessibility tree. This is where the live model taught me something. It
guessed a CSS selector (`tr:has(td:text-is(...))`) because I reported what was
clickable but not what was readable, and the guess was wrong because the label was a
`<th>`. The fix was to report readable fields semantically, *not* to expose raw DOM —
which would have solved the immediate problem by welding recordings to one browser.

**Many tenants, one product.** `app_family` keys the artifact to the vendor product,
not the institution, and `profiles.py` holds that product's vocabulary once. A
`TenantOverride` supplies only an entry point and per-step locator replacements — it
deliberately *cannot* change steps, contract or checkpoint, because an override
powerful enough to restructure a flow is a second copy of the flow with more places
to hide. A tenant needing different steps needs its own recording. The specialised
copy reverts to draft and records which tenant it was built for, so a failure is
attributable to the override rather than the base: the difference between
quarantining one institution and rolling back a capability for everyone.

**Drift detection is designed, not built.** Run approved capabilities against each
tenant on a schedule, treat checkpoint failure as a per-tenant quarantine signal, and
promote draft to approved on sustained clean replays. What is built is what makes
that possible: outcomes keyed by product, overrides keyed by tenant, failures
attributable to one or the other.

## Escalation & handoff

Three conditions escalate: a **policy refusal** (an irreversible step nobody
authorised for this invocation), an **unknown runtime state** (a failure matching
neither a business outcome nor a recovery rule), and the planner's own **`escalate`**
action when it decides it should not act — a correct outcome, not a loop failure.
The request carries capability, goal, step, reason and a screenshot; a reason string
says "unrecognised state", the screenshot says there is a banner over the page.

The essential property is that there is **exactly one session**. The operator acts in
the same browser context the automation was using — same cookies, same page, same
half-filled form — because that accumulated state is the reason the situation is
recoverable at all. Ownership is single-valued, flips *before* the operator is
contacted rather than after they reply (otherwise both believe they may act), and a
second concurrent escalation raises rather than queues.

What the human did is **recorded, not assumed**: URL before and after, screenshots of
both, their note, and whether anything actually changed — an operator who fixed the
problem and one who shrugged both press Enter. Resume makes exactly one attempt at
the failed step; looping would mean repeatedly calling a person about a problem they
have already seen. Repeated interventions on the same step are counted, because that
signals a capability needing re-recording rather than more operators.

The risk gate also sits at **discovery** time, which is the more useful moment:
"should a capability that writes to the system of record exist at all?" is better
asked before the recording exists than after it has been approved.

**The cut:** the operator surface is a terminal prompt. The control-transfer model is
real — same session, one owner, recorded transitions, resume that continues. A
production console adds transport and access control: a queue routing to whoever is
on shift, an authenticated view of the live session, RBAC on which capabilities an
operator may take over, and grants that expire so a session does not sit open waiting
for someone who went home.

## Safety

Guardrails are ordinary Python in `policy.py`, checked before an action reaches a
surface — not prompt instructions. A model that decides to click "Confirm and create"
still has to pass a function that does not read English. Asked how the agent is
prevented from doing something dangerous, the answer is not "I told it not to" but
"the model proposes and a separate layer disposes".

Allowlists, never blocklists — a blocklist is a list of the attacks already thought
of. Hostname matching is **exact**: `127.0.0.1.evil.net` is registrable, contains an
allowlisted host as a substring, and passes a careless suffix or `in` check. Two
independent locks are intersected: the deployment policy says what is permissible at
all, the artifact says what this recording ever needed.

Irreversible actions require confirmation **per invocation**, passed as an argument
rather than stored on the policy, so approving one risky step cannot silently
authorise the next. Approving a capability and authorising an invocation are
different acts.

Redaction happens at one chokepoint, inside `EvidenceRecorder.record()`, so no caller
can forget it. Two mechanisms because they fail differently: field-name matching is
exact but only covers named fields; pattern matching catches secrets in free text
such as a page's visible content, where nothing is named. Sensitive values become
stable per-run digests rather than being erased, so one member can be followed
through a trace without the log revealing who they are — redaction that destroys the
evidence has also failed. Inputs are sensitive by default: over-redacting a log costs
far less than writing a member number into one.

**Limits.** Screenshots are unredacted (below). Nothing defends against prompt
injection from page content — a legacy app rendering attacker-controlled text could
try to influence the planner, and the mitigations that matter are the ones already
outside the model: the action allowlist, the host allowlist, the irreversible gate.
The target emits machine-readable error markers, which many real legacy apps do not;
there you would match on display text, which is more brittle and breaks under
localisation — a per-app-profile decision, not an engine assumption.

## Cuts

**Screenshot redaction.** Text redaction does not touch pixels, and a full-page
capture of a member details screen holds the name and balance as an image. All
records here are synthetic, but the gap is real. Fix: capture only the asserted
region, or OCR-and-blur before writing.

**A real operator console.** Terminal prompt only; the handoff section states which
half is real.

**Desktop and legacy-web adapters.** Designed for, not built. The in-memory adapter
is evidence the protocol is implementable without a DOM.

**Multi-tenant storage and the drift scheduler.** Overrides and per-product profiles
exist; the catalog service and scheduled compatibility sweep do not.

**Scaling infrastructure**, deliberately. None of the judgment being evaluated lives
there.

**`coordinate` locators.** In the schema, unimplemented in the web adapter. They
belong there for desktop surfaces where nothing better exists, and any capability
using one should be flagged for review.

**Assisted LLM recovery on replay failure.** Appealing, and the change most likely to
quietly reintroduce nondeterminism into the path whose entire value is not having any.

**Next, in order:** screenshot redaction, because it is the one cut with a real
data-handling consequence; a second tenant variant exercising `apply_tenant_override`
end to end, because the multi-tenant story is currently argued rather than
demonstrated; multi-run stability scoring to gate draft → approved on evidence rather
than a human's read; and an authenticated operator service around the existing
control-transfer state machine.
