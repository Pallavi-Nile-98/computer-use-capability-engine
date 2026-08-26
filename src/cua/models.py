"""Every data shape in the system.

This file is the contract. The artifact defined here is what an AI agent invokes
in production, what a human reviews before approving, and what the replay engine
executes — so it has to serve all three audiences at once.

Three decisions shape everything below:

1. A capability stores *references*, not the values a recording happened to use.
   `${member_id}`, never `12345`. Otherwise the recording only ever works for the
   one customer it was recorded against.

2. A locator states its own justification. `rationale` is not documentation; it is
   the field a reviewer reads to judge whether this will still work next month.

3. The application's possible endings are declared as data, not discovered by
   catching exceptions. A "no such member" screen is a known answer, a dropped
   session is a known recoverable condition, and anything else is genuinely
   unknown. Replay can only tell those apart if the artifact says which is which.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class ActionKind(StrEnum):
    """The complete set of things this system can do to a surface.

    Deliberately small. Every action added here is one more thing that has to be
    implementable on a desktop surface later, and one more thing a reviewer has
    to reason about when approving a capability.
    """

    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    READ = "read"
    WAIT = "wait"
    COMPLETE = "complete"
    ESCALATE = "escalate"


class RiskLevel(StrEnum):
    """How much damage this action could do if it fires when it shouldn't."""

    SAFE = "safe"  # reads, navigation — no side effects
    REVERSIBLE = "reversible"  # writes something that can be undone
    IRREVERSIBLE = "irreversible"  # changes the system of record


class LocatorStrategy(StrEnum):
    """How to find a control, roughly best to worst.

    The ordering matters. `ROLE` and `LABEL` describe a control the way a human
    operator would ("the button called Search"), so they survive the layout churn
    that breaks `CSS`. `COORDINATE` is last because it breaks if anything moves at
    all — it exists for surfaces where nothing better is available, which is the
    honest situation on some desktop apps.
    """

    ROLE = "role"
    LABEL = "label"
    TEXT = "text"
    PLACEHOLDER = "placeholder"
    CSS = "css"
    COORDINATE = "coordinate"


class ResultStatus(StrEnum):
    """What a run can conclude. The caller branches on exactly this.

    The split between BUSINESS_OUTCOME and the two failure kinds is the whole
    point. "No such member" is an answer the caller asked for; returning it as a
    failure makes a calling agent retry forever against a member who was never
    there.
    """

    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE_FAILURE = "recoverable_failure"
    HARD_FAILURE = "hard_failure"
    INTERVENTION_REQUIRED = "intervention_required"


class ControlOwner(StrEnum):
    """Who may act on the session right now. Never both."""

    AUTOMATION = "automation"
    HUMAN = "human"


class RecoveryAction(StrEnum):
    """What replay is permitted to do about a known runtime condition."""

    RETRY = "retry"
    RELOAD = "reload"
    DISMISS = "dismiss"
    RENAVIGATE = "renavigate"


# ---------------------------------------------------------------------------
# Locating a control
# ---------------------------------------------------------------------------


class Locator(BaseModel):
    """How to find one control on a surface.

    Note this describes *intent*, not mechanism: "the button named Search", not a
    Playwright call. That is what lets a different surface adapter — a desktop
    accessibility tree, a legacy frameset — resolve the same recorded flow.
    """

    strategy: LocatorStrategy
    value: str
    role: str | None = None  # only meaningful when strategy is ROLE
    exact: bool = True
    frame: str | None = None  # legacy apps still use framesets
    rationale: str  # required: why a reviewer should believe this is stable


# ---------------------------------------------------------------------------
# The agent-facing contract
# ---------------------------------------------------------------------------


class ParameterSpec(BaseModel):
    """One input the calling agent must supply per invocation."""

    name: str
    type: Literal["string", "integer", "number", "boolean"]
    description: str
    required: bool = True
    # Marks data that must never reach a log or an artifact. Defaults to False,
    # but the compiler sets it True for anything caller-supplied, because in a
    # regulated environment the safe default is to assume it is protected.
    sensitive: bool = False
    pattern: str | None = None  # validated before the browser even opens


class OutputSpec(BaseModel):
    """One value the capability returns."""

    name: str
    type: Literal["string", "integer", "number", "boolean", "object"]
    description: str
    sensitive: bool = False


class CapabilityContract(BaseModel):
    """What the capability needs and what it gives back.

    Separate from `steps` on purpose. An agent deciding whether to call this
    capability should not have to read the click-by-click flow to find out what
    arguments it takes.
    """

    inputs: list[ParameterSpec]
    outputs: list[OutputSpec]


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class RetryPolicy(BaseModel):
    """Bounded retry for a single step. Bounds are enforced by the schema."""

    max_attempts: int = Field(default=2, ge=1, le=5)
    retry_delay_ms: int = Field(default=500, ge=0, le=10_000)


class CapabilityStep(BaseModel):
    """One recorded action."""

    id: str
    action: ActionKind
    target: Locator | None = None
    # Alternate ways to find the same control, tried in order if the primary
    # fails. Cheap insurance against a single brittle selector.
    fallback_targets: list[Locator] = Field(default_factory=list)
    # "${member_id}", not the value the recording used.
    value_template: str | None = None
    output_name: str | None = None
    risk: RiskLevel = RiskLevel.SAFE
    precondition: str | None = None
    postcondition: str | None = None
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def validate_shape(self) -> "CapabilityStep":
        """Reject steps that cannot possibly execute.

        Catching these here means a malformed capability fails at load time with a
        clear message, instead of halfway through a flow with a browser open.
        """
        if self.action in {ActionKind.CLICK, ActionKind.FILL, ActionKind.READ}:
            if not self.target:
                raise ValueError(f"{self.action} needs a target to act on")
        if self.action == ActionKind.FILL and self.value_template is None:
            raise ValueError("fill needs a value_template — what should it type?")
        if self.action == ActionKind.READ and not self.output_name:
            raise ValueError("read needs an output_name — where does the value go?")
        return self


class Checkpoint(BaseModel):
    """Proof that the goal was actually reached.

    Without this, "every step ran" gets mistaken for "it worked". A click can
    succeed while the application quietly does nothing.
    """

    description: str
    locator: Locator
    expected: str | None = None


# ---------------------------------------------------------------------------
# The application's known endings
# ---------------------------------------------------------------------------


class BusinessOutcomeRule(BaseModel):
    """A legitimate answer that is not success.

    "No member matches that identifier" is true, useful, and exactly what the
    caller asked to find out. It is data, not an error.
    """

    code: str
    description: str
    locator: Locator
    expected: str | None = None


class RecoveryRule(BaseModel):
    """A known transient condition, and the bounded response authorised for it.

    Recovery is declared in the artifact rather than hard-coded in the engine, so
    the person approving a capability can see exactly what it is allowed to do on
    its own. "Reload once if the host errors" is a decision a reviewer should get
    to make; it should not be buried in a retry loop.
    """

    code: str
    description: str
    detector: Locator
    expected: str | None = None
    action: RecoveryAction
    dismiss_target: Locator | None = None
    max_attempts: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def validate_shape(self) -> "RecoveryRule":
        if self.action == RecoveryAction.DISMISS and not self.dismiss_target:
            raise ValueError("a dismiss recovery needs to know what to click")
        return self


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


class CapabilityArtifact(BaseModel):
    """A recorded flow, as a reusable capability an agent can invoke.

    `extra="forbid"` is deliberate: an executor that silently ignores an unknown
    field is an executor that silently ignores a contract change. Better to refuse
    to load than to run something subtly different from what was reviewed.
    """

    model_config = ConfigDict(extra="forbid")

    # Shape of this file, versus version of this capability. They change for
    # different reasons and at different rates, so they are separate fields.
    schema_version: Literal["1.0"] = "1.0"
    capability_id: str
    version: str = "1.0.0"

    # Review gate. A draft capability cannot be replayed unattended. This is a
    # real control, not metadata — the replay engine enforces it.
    approval_state: Literal["draft", "approved", "retired"] = "draft"
    approved_by: str | None = None
    approved_at: datetime | None = None

    name: str
    description: str

    # The vendor product this was recorded against, not the tenant. Hundreds of
    # institutions run the same underlying software, so keying on the product is
    # what makes one recording reusable across many of them.
    app_family: str
    surface_type: Literal["web", "legacy_web", "desktop"]
    entry_point: str

    contract: CapabilityContract
    steps: list[CapabilityStep]
    checkpoint: Checkpoint

    business_outcomes: list[BusinessOutcomeRule] = Field(default_factory=list)
    recovery_rules: list[RecoveryRule] = Field(default_factory=list)

    # Guardrails travel with the capability, so a stolen or edited artifact still
    # cannot reach a host or perform an action it was not recorded to need.
    allowed_domains: list[str]
    allowed_actions: list[ActionKind]

    # Narrow, reviewable per-tenant deviations from the base recording — a renamed
    # button, a different route — so a branding difference does not force a
    # re-recording for every institution.
    tenant_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_approval(self) -> "CapabilityArtifact":
        if self.approval_state == "approved" and not (self.approved_by and self.approved_at):
            raise ValueError("an approved capability must record who approved it, and when")
        return self


# ---------------------------------------------------------------------------
# Runtime shapes: what the planner sees, decides, and produces
# ---------------------------------------------------------------------------


class Observation(BaseModel):
    """What the surface looks like right now, as the planner sees it."""

    url: str
    title: str
    visible_text: str
    controls: list[dict[str, str | None]]
    screenshot_path: str | None = None


class PlannedAction(BaseModel):
    """A single decision. This is also the schema the LLM is constrained to.

    One action per turn, never a batch. A plan that assumes three steps will all
    succeed cannot notice that the second one silently failed.
    """

    action: ActionKind
    target: Locator | None = None
    value: str | None = None
    # Set when the value came from the goal rather than from the page, which is
    # what turns a recorded constant into a typed input.
    parameter_name: str | None = None
    output_name: str | None = None
    risk: RiskLevel = RiskLevel.SAFE
    reasoning: str
    done: bool = False
    # Only on the completing action. The planner is the sole participant that has
    # seen the finished state, so it is the only one that can honestly name a
    # checkpoint for it.
    checkpoint: Checkpoint | None = None

    @model_validator(mode="after")
    def validate_completion(self) -> "PlannedAction":
        if self.done and self.checkpoint is None:
            raise ValueError(
                "a completing action must declare the checkpoint that proves the "
                "goal was reached, or the recorded capability cannot verify itself"
            )
        return self


class ExecutedAction(BaseModel):
    """One entry in the discovery trace: what was decided, and what happened."""

    sequence: int
    planned: PlannedAction
    observed_url: str
    result: str | None = None


class RunResult(BaseModel):
    """The structured answer a caller gets back. Never an exception."""

    status: ResultStatus
    capability_id: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)

    # Which known ending this was, when status is BUSINESS_OUTCOME or
    # RECOVERABLE_FAILURE.
    outcome_code: str | None = None

    # Enough to debug a failure without re-running it.
    failed_step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    error_code: str | None = None

    message: str
    evidence_dir: str | None = None

    # Conditions that were detected and handled. The run may have succeeded, and
    # the caller can still see that it is degrading — a capability that recovers
    # on every invocation needs re-recording, not celebrating.
    recovered_conditions: list[str] = Field(default_factory=list)

    human_intervened: bool = False


# ---------------------------------------------------------------------------
# Human handoff
# ---------------------------------------------------------------------------


class InterventionRequest(BaseModel):
    """Everything a human needs to pick up a stuck session and act on it."""

    id: str
    capability_id: str
    goal: str
    step_id: str
    reason: str
    screenshot_path: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class InterventionResolution(BaseModel):
    """What the human actually did while they held control.

    Recorded so a reviewer can tell "the operator fixed the state" apart from
    "the operator clicked resume and hoped", and so repeated interventions on the
    same step become a visible signal rather than folklore.
    """

    intervention_id: str
    resolved_by: str
    note: str
    url_before: str
    url_after: str
    state_changed: bool
    returned_to: ControlOwner = ControlOwner.AUTOMATION
    resolved_at: datetime = Field(default_factory=utc_now)
