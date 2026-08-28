# Computer-Use Capability Engine

An LLM works out how to do a task inside a legacy UI **once**. The flow is recorded
as a typed, reviewable capability. From then on it replays deterministically, with
no model in the decision loop.

Built for the case where a system has no API and the only way in is to drive the
screen the way a human operator would.

```
goal in English
      │
      ▼  discovery - a model decides one action at a time, against a live browser
 ┌────────────────────────────────────────────┐
 │  observe ──▶ decide ──▶ act ──▶ observe …  │
 └────────────────────────────────────────────┘
      │
      ▼  a capability artifact: typed inputs, typed outputs, steps, checkpoint
      │
      ▼  a human reviews and approves it
      │
      ▼  replay - no model, thousands of times, for pennies
 success │ business outcome │ recoverable failure │ hard failure
```

The design reasoning, trade-offs and known gaps are in **[REPORT.md](REPORT.md)**.
Worked examples from real runs are in **[evidence/](evidence/)**.

---

## Requirements

- Python 3.11 or newer
- A Chromium-based browser for Playwright (installed in setup below)
- An Anthropic API key - **only** for live model-driven discovery. Everything else,
  including the full test suite and the complete demo path, runs without one.

## Setup

```bash
git clone https://github.com/Pallavi-Nile-98/computer-use-capability-engine.git
cd computer-use-capability-engine
```

```bash
python -m venv .venv
```

```bash
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

```bash
python -m pip install -e ".[dev]"
```

```bash
playwright install chromium
```

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Then open `.env` and set `ANTHROPIC_API_KEY` if you want to run live discovery.
Leave it blank to use the credential-free path described below. `.env` is
gitignored.

---

## Demo path

Five commands, in order. Run the first in its own terminal and leave it running.

### 1. Start the target application

```bash
cua serve
```

A synthetic credit-union servicing console on `http://127.0.0.1:8000/legacy`. It
stands in for a real legacy bank system: table-based layout, no test ids, a
POST-then-redirect mid-flow, and five distinct endings for the same search.

Try it by hand first - it makes the rest concrete:

| Member ID | What the app does |
|---|---|
| `12345` | Member details, savings balance `$4,281.73` |
| `99999` | "No member matches that identifier" |
| `77777` | "You do not have permission to view this member" |

### 2. Discover a capability

In a second terminal:

```bash
cua discover --goal "Look up member 12345 and read their current savings balance" --capability lookup-member-savings-balance --artifact artifacts/lookup-member-savings-balance.json --evidence evidence/discovery
```

A browser opens and drives itself. The model chooses one action per turn from what
it can see on screen. On success this writes a **draft** capability and a full
evidence trail - a screenshot of every observation, plus every decision and its
reasoning.

### 3. Review and approve it

Discovery never produces something runnable. Open the artifact, read the steps and
the locators - each locator carries a `rationale` field explaining why it should
still work next month — then approve it:

```bash
cua approve --artifact artifacts/lookup-member-savings-balance.json --reviewer "Your Name"
```

The command prints what you are signing off, including the highest risk class in
the capability, and records your name in the artifact.

### 4. Replay it - no model involved

```bash
cua replay --artifact artifacts/lookup-member-savings-balance.json --params '{"member_id":"12345"}' --evidence evidence/replay-success
```

```json
{
  "status": "success",
  "outputs": { "savings_balance": "$4,281.73" }
}
```

Note the artifact stores `${member_id}`, not `12345`, so the same capability works
for any member.

### 5. Replay something that isn't the happy path

```bash
cua replay --artifact artifacts/lookup-member-savings-balance.json --params '{"member_id":"99999"}' --evidence evidence/replay-business-outcome
```

```json
{
  "status": "business_outcome",
  "outcome_code": "MEMBER_NOT_FOUND",
  "message": "The application returned a known business outcome. This is an answer for the caller, not a failure."
}
```

**Exit code 0.** "No member matches" is a legitimate answer the caller asked for,
not a malfunction. An agent that receives it as an error will retry forever against
a member who was never there.

---

## Running without an API key

Every command above works with `--planner scripted`, which substitutes a
deterministic planner implementing the identical interface:

```bash
cua discover --goal "Look up member 12345 and read their current savings balance" --capability lookup-member-savings-balance --planner scripted --artifact artifacts/lookup-member-savings-balance.json --evidence evidence/discovery
```

The browser, the target application, the artifact, the guardrails, the handoff and
the result classification are all real; only the choice of next action is made by
hardcoded logic rather than a model. This is how the committed evidence for the
deterministic runs was produced, so anyone can reproduce it byte for byte.

`evidence/discovery-balance-live/` is from the live model path, for comparison.

## Running without a browser

The test suite uses an in-memory surface adapter, so it needs neither a browser nor
a key:

```bash
python -m pytest -q
```

```
55 passed in 7s
```

---

## More things you can run

### See the guardrails refuse an irreversible action

The second capability writes to the system of record. Replaying it without
explicit authorisation stops and asks a human:

```bash
cua replay --artifact artifacts/open-member-subaccount.json --params '{"member_id":"12345"}' --evidence evidence/replay-blocked-irreversible
```

```json
{ "status": "intervention_required", "error_code": "RISKY_ACTION_REQUIRES_CONFIRMATION" }
```

Nothing was written. Approving a capability is not standing consent to run its
irreversible step - each invocation needs its own:

```bash
cua replay --artifact artifacts/open-member-subaccount.json --params '{"member_id":"12345"}' --confirm-irreversible --evidence evidence/replay-confirmed-irreversible
```

### See a runtime error detected and recovered

The target application can be made to fail on cue, which is the only way to
demonstrate this against a real browser:

```bash
curl -X POST http://127.0.0.1:8000/__control/fault -d "code=APPLICATION_ERROR&count=1"
```

```bash
cua replay --artifact artifacts/lookup-member-savings-balance.json --params '{"member_id":"12345"}' --evidence evidence/replay-recovered
```

The run succeeds, and says so: `"recovered_conditions": ["APPLICATION_ERROR"]`. A
capability that recovers on every invocation is degrading and needs re-recording —
which a caller can only notice if the result reports it.

Arm the same fault six times instead of once and it becomes a
`recoverable_failure`: a known condition that outlived its retry budget, and is
therefore safe for the caller to retry later. That is a different thing from a
`hard_failure`, which needs a person.

### See capabilities as tools an agent could call

```bash
cua catalog --directory artifacts
```

Renders each **approved** capability as a function-calling tool definition with
typed arguments and returns. Drafts do not appear - an unapproved capability is not
merely flagged, it is uncallable.

### Print the artifact schema

```bash
cua schema
```

### Watch a run happen slowly

```bash
PLAYWRIGHT_SLOW_MO=1500 cua discover --goal "Look up member 12345 and read their current savings balance" --capability lookup-member-savings-balance --planner scripted --artifact artifacts/lookup-member-savings-balance.json --evidence evidence/discovery
```

---

## What is in the repo

```
src/cua/
  models.py       every contract: the artifact schema, actions, results
  surface.py      the perceive/act seam - Playwright and in-memory adapters
  planner.py      decides the next action: Claude, or a scripted stand-in
  engine.py       the discovery loop
  compiler.py     turns a discovery trace into a parameterised capability
  profiles.py     per-application error vocabulary and tenant overrides
  replay.py       deterministic execution and the result taxonomy
  policy.py       allowlists and the risk-confirmation gate
  handoff.py      same-session control transfer to a human
  evidence.py     append-only redacted run log
  redaction.py    what must never reach disk
  catalog.py      approved capabilities as callable tools
  target_app.py   the synthetic legacy application
  cli.py          the commands above

tests/            55 tests, no browser or key required
artifacts/        two recorded capabilities, both approved
evidence/         real runs covering every branch of the result contract
```

`surface.py` is the seam worth looking at first: nothing above it imports
Playwright, which is what allows the same recorded flow to run on a different kind
of surface later.

---

## Troubleshooting

**`ANTHROPIC_API_KEY is not set`** - either set it in `.env`, or add
`--planner scripted` to run the same loop without a model.

**Playwright fails with "side-by-side configuration is incorrect"** (some Windows
installs) - the bundled Chromium will not launch. Point Playwright at a browser you
already have:

```bash
PLAYWRIGHT_CHANNEL=msedge cua discover ...
```

Or set `PLAYWRIGHT_CHANNEL=msedge` in `.env`.

**`cua: command not found`** - the virtual environment is not active, or
`pip install -e ".[dev]"` has not been run. As a fallback,
`python -m cua.cli <command>` works from the repo root with `PYTHONPATH=src`.

**Port 8000 already in use** - `cua serve --port 8001`, then pass
`--target http://127.0.0.1:8001/legacy` to `discover`.

---

## Safety notes

- Every member record in this repository is invented. Never point this at real
  member data.
- The artifact stores `${member_id}`, never a concrete identifier.
- All evidence passes through a redaction boundary. Identifiers and balances appear
  as stable tokens, so one member can be followed through a trace without the log
  revealing who they are.
- Only allowlisted hosts, schemes and action types execute, and a capability is
  additionally scoped to the action types its own recording used.
- Irreversible steps require per-invocation human confirmation.
- Screenshots are **not** redacted - text redaction does not touch pixels. See
  `## Cuts` in [REPORT.md](REPORT.md).
