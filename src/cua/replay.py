"""Deterministic replay — the path an AI agent actually triggers in production.

No model is consulted here, ever. Given an approved capability and typed inputs,
this executes the recorded steps and returns one of five things the caller can
branch on:

  success               the goal was reached, and the checkpoint proved it
  business_outcome      the application gave a legitimate answer ("no such member")
  recoverable_failure   a known transient condition outlived its retry budget
  hard_failure          something unmodelled — stop, and surface enough to debug it
  intervention_required a person has to decide before this can continue

Conflating the second with the fourth is the failure this contract exists to
prevent. "No such member" is an answer the caller asked for. Return it as an error
and a calling agent retries forever against a member who was never there.

How the three error classes are actually told apart, in order:

  1. Does the screen match a business outcome the artifact declares?  → answer
  2. Does it match a recovery rule the artifact authorises?           → bounded retry
  3. Neither                                                          → unknown; stop

That ordering is deliberate. Business outcomes are checked first because a "member
not found" page and a broken page are both "the step failed" from the surface's
point of view, and only the artifact knows which is which.

Every retry is bounded three ways: per rule, per run, and per flow restart. Without
all three, a handful of two-attempt rules multiply into a run that thrashes for
minutes against an application that is simply down.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .evidence import EvidenceRecorder
from .handoff import HandoffController
from .models import (
    ActionKind,
    CapabilityArtifact,
    CapabilityStep,
    PlannedAction,
    RecoveryAction,
    RecoveryRule,
    ResultStatus,
    RiskLevel,
    RunResult,
)
from .policy import Policy, PolicyViolation
from .surface import Surface, SurfaceError

PARAMETER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# Total recoveries permitted across one run, on top of each rule's own limit.
# Three rules with two attempts each is six recoveries; this caps the compound.
RUN_RECOVERY_BUDGET = 4

# How many times the whole flow may replay from step one after a recovery that
# invalidated mid-flow state. A session that drops twice in one run is an outage.
MAX_FLOW_RESTARTS = 1


def resolve_value(template: str | None, parameters: dict[str, Any]) -> str | None:
    """Substitute `${name}` from the invocation's parameters.

    Only a whole-string reference is substituted, never an embedded one. A template
    is a slot to fill, not a formatting language — supporting interpolation would
    invite building selectors out of caller input, which is how injection bugs
    start.
    """
    if template is None:
        return None
    match = PARAMETER.match(template)
    if not match:
        return template
    name = match.group(1)
    if name not in parameters:
        raise ValueError(f"Missing required parameter: {name}")
    return str(parameters[name])


def validate_parameters(artifact: CapabilityArtifact, parameters: dict[str, Any]) -> None:
    """Check the invocation against the contract before anything is opened.

    Deliberately first and deliberately cheap. A typo in a parameter name should
    cost nothing — not a browser launch, a page load, and a confusing failure four
    steps in.
    """
    expected = {spec.name: spec for spec in artifact.contract.inputs}

    unknown = set(parameters) - set(expected)
    if unknown:
        # Rejected rather than ignored: a caller passing `member_number` when the
        # contract says `member_id` has misunderstood something, and silently
        # dropping it would hide that.
        raise ValueError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")

    for name, spec in expected.items():
        if spec.required and name not in parameters:
            raise ValueError(f"Missing required parameter: {name}")
        if name not in parameters:
            continue

        value = parameters[name]
        if spec.type == "string" and not isinstance(value, str):
            raise ValueError(f"Parameter {name} must be a string")
        if spec.type == "integer" and not isinstance(value, int):
            raise ValueError(f"Parameter {name} must be an integer")
        if spec.pattern and not re.fullmatch(spec.pattern, str(value)):
            raise ValueError(f"Parameter {name} does not match its declared pattern")


class ReplayEngine:
    """Executes one capability against one surface."""

    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        evidence: EvidenceRecorder,
        handoff: HandoffController,
        *,
        confirmed_risks: set[RiskLevel] | None = None,
    ):
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self.handoff = handoff

        # Risk classes a human authorised for this invocation specifically. Empty
        # by default: an approved capability is not standing consent to write.
        self.confirmed_risks = confirmed_risks or set()

        self._recoveries_used = 0
        self._recovered: list[str] = []
        self._human_intervened = False

    # -- detecting what kind of situation we are in --------------------------

    async def _business_outcome(self, artifact: CapabilityArtifact) -> str | None:
        for rule in artifact.business_outcomes:
            if await self.surface.checkpoint(rule.locator, rule.expected):
                return rule.code
        return None

    async def _recovery_rule(self, artifact: CapabilityArtifact) -> RecoveryRule | None:
        for rule in artifact.recovery_rules:
            if await self.surface.checkpoint(rule.detector, rule.expected):
                return rule
        return None

    async def _apply_recovery(self, rule: RecoveryRule) -> None:
        """Do exactly what the reviewed artifact authorised for this condition.

        RENAVIGATE is not handled here. Returning to the entry point discards the
        mid-flow state every later step assumes, so the only correct continuation
        is to replay the capability from step one — which is the caller's decision
        to make, not this method's.
        """
        if rule.action == RecoveryAction.RELOAD:
            await self.surface.reload()
        elif rule.action == RecoveryAction.DISMISS:
            await self.surface.execute(
                PlannedAction(
                    action=ActionKind.CLICK,
                    target=rule.dismiss_target,
                    reasoning=f"Dismiss known interstitial {rule.code}",
                )
            )
        # RETRY needs no surface action — re-running the step is the recovery.

    # -- executing one step --------------------------------------------------

    async def _attempt_step(
        self, step: CapabilityStep, planned: PlannedAction
    ) -> tuple[str | None, SurfaceError | None]:
        """Try the primary locator, then any recorded fallbacks, then retry.

        Two different problems, handled in two layers. Fallback locators cover "the
        control moved or was renamed". The retry loop covers "the page had not
        finished settling". Collapsing them would mean retrying a genuinely absent
        control several times for no reason.
        """
        targets = [step.target, *step.fallback_targets] if step.target else [None]
        last_error: SurfaceError | None = None

        for attempt in range(1, step.retry.max_attempts + 1):
            for target in targets:
                try:
                    candidate = planned.model_copy(update={"target": target})
                    return await self.surface.execute(candidate), None
                except SurfaceError as exc:
                    last_error = exc
            if attempt < step.retry.max_attempts:
                await asyncio.sleep(step.retry.retry_delay_ms / 1000)

        return None, last_error

    # -- building results ----------------------------------------------------

    def _result(
        self,
        artifact: CapabilityArtifact,
        *,
        status: ResultStatus,
        message: str,
        step_id: str | None = None,
        error_code: str | None = None,
        outcome_code: str | None = None,
        expected: str | None = None,
        observed: str | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> RunResult:
        """One construction point, so every exit reports the same shape."""
        return RunResult(
            status=status,
            capability_id=artifact.capability_id,
            outputs=outputs or {},
            outcome_code=outcome_code,
            failed_step_id=step_id,
            expected=expected,
            observed=observed,
            error_code=error_code,
            message=message,
            evidence_dir=str(self.evidence.directory),
            recovered_conditions=list(self._recovered),
            human_intervened=self._human_intervened,
        )

    # -- entry point ---------------------------------------------------------

    async def run(
        self,
        artifact: CapabilityArtifact,
        parameters: dict[str, Any],
        *,
        goal: str = "deterministic replay",
    ) -> RunResult:
        # Cheapest checks first: contract, then approval, then policy, and only
        # then does anything get opened.
        validate_parameters(artifact, parameters)

        if artifact.approval_state != "approved":
            return self._result(
                artifact,
                status=ResultStatus.HARD_FAILURE,
                error_code="ARTIFACT_NOT_APPROVED",
                message=(
                    "Unattended replay is blocked until a human approves this "
                    "capability."
                ),
            )

        # Narrow the deployment policy to what this capability was recorded to
        # need. Two independent locks; either one saying no is a refusal.
        policy = self.policy.scoped_to(artifact.allowed_domains, artifact.allowed_actions)
        policy.check_url(artifact.entry_point)

        await self.surface.start(artifact.entry_point)
        self.evidence.record(
            "replay_started",
            {
                "capability_id": artifact.capability_id,
                "version": artifact.version,
                "parameters": parameters,
            },
        )

        for attempt in range(1, MAX_FLOW_RESTARTS + 2):
            result = await self._execute_steps(artifact, policy, parameters, goal)
            if result is not None:
                return result
            # A recovery invalidated mid-flow state. Re-enter and replay from the
            # top rather than resuming into a session that no longer exists.
            self.evidence.record("flow_restarted", {"attempt": attempt})
            await self.surface.navigate(artifact.entry_point)

        return self._result(
            artifact,
            status=ResultStatus.RECOVERABLE_FAILURE,
            error_code="RESTART_BUDGET_EXHAUSTED",
            message=(
                f"The flow restarted {MAX_FLOW_RESTARTS} time(s) and still could not "
                "complete. The condition is known, so the caller may retry later."
            ),
        )

    async def _execute_steps(
        self,
        artifact: CapabilityArtifact,
        policy: Policy,
        parameters: dict[str, Any],
        goal: str,
    ) -> RunResult | None:
        """Run the recorded steps once. Returns None to request a full restart."""
        outputs: dict[str, Any] = {}

        for step in artifact.steps:
            planned = PlannedAction(
                action=step.action,
                target=step.target,
                value=resolve_value(step.value_template, parameters),
                output_name=step.output_name,
                risk=step.risk,
                reasoning=f"Deterministic replay of {step.id}",
            )

            # Guardrail before the action, never after. Both the action-type
            # allowlist and the risk gate live in policy.check_action.
            try:
                policy.check_action(
                    planned, human_confirmed=planned.risk in self.confirmed_risks
                )
            except PolicyViolation as exc:
                await self._escalate(artifact, step.id, str(exc), goal)
                return self._result(
                    artifact,
                    status=ResultStatus.INTERVENTION_REQUIRED,
                    step_id=step.id,
                    error_code="RISKY_ACTION_REQUIRES_CONFIRMATION",
                    message=str(exc),
                    outputs=outputs,
                )

            result, error = await self._attempt_step(step, planned)

            # The classification loop. Only entered when a step failed.
            attempts_by_code: dict[str, int] = {}
            while error is not None:
                # 1. Is this a legitimate answer rather than a failure?
                outcome = await self._business_outcome(artifact)
                if outcome:
                    self.evidence.record(
                        "business_outcome", {"step_id": step.id, "code": outcome}
                    )
                    return self._result(
                        artifact,
                        status=ResultStatus.BUSINESS_OUTCOME,
                        step_id=step.id,
                        outcome_code=outcome,
                        message=(
                            "The application returned a known business outcome. This "
                            "is an answer for the caller, not a failure."
                        ),
                        outputs=outputs,
                    )

                # 2. Is it a known transient the artifact authorises a fix for?
                rule = await self._recovery_rule(artifact)
                if rule is None:
                    break  # 3. Neither. Unknown state; fall through to hard failure.

                used = attempts_by_code.get(rule.code, 0)
                if used >= rule.max_attempts or self._recoveries_used >= RUN_RECOVERY_BUDGET:
                    shot = self.evidence.path_for(f"recoverable-{step.id}.png")
                    await self.surface.screenshot(str(shot))
                    self.evidence.record(
                        "recovery_budget_exhausted",
                        {
                            "step_id": step.id,
                            "code": rule.code,
                            "rule_attempts": used,
                            "run_recoveries": self._recoveries_used,
                            "screenshot": str(shot),
                        },
                    )
                    return self._result(
                        artifact,
                        status=ResultStatus.RECOVERABLE_FAILURE,
                        step_id=step.id,
                        outcome_code=rule.code,
                        error_code="RECOVERY_BUDGET_EXHAUSTED",
                        expected=step.postcondition,
                        observed=f"{rule.code} persisted after {used} attempt(s)",
                        message=(
                            f"{rule.code} did not clear within its retry budget. The "
                            "condition is understood, so this is safe to retry later "
                            "— unlike a hard failure, which needs a human first."
                        ),
                        outputs=outputs,
                    )

                attempts_by_code[rule.code] = used + 1
                self._recoveries_used += 1
                self.evidence.record(
                    "recovery_applied",
                    {
                        "step_id": step.id,
                        "code": rule.code,
                        "action": rule.action,
                        "attempt": used + 1,
                    },
                )

                if rule.action == RecoveryAction.RENAVIGATE:
                    if rule.code not in self._recovered:
                        self._recovered.append(rule.code)
                    return None  # ask run() to replay the flow from step one

                await self._apply_recovery(rule)
                await asyncio.sleep(step.retry.retry_delay_ms / 1000)
                result, error = await self._attempt_step(step, planned)
                if error is None and rule.code not in self._recovered:
                    self._recovered.append(rule.code)

            if error is not None:
                resumed, failure = await self._hard_failure(
                    artifact, step, planned, error, goal, outputs
                )
                if failure is not None:
                    return failure
                result = resumed

            if step.output_name:
                outputs[step.output_name] = result
            self.evidence.record(
                "replay_step_completed",
                {
                    "step_id": step.id,
                    "action": planned.model_dump(mode="json"),
                    "result": result,
                },
            )

        return await self._verify(artifact, outputs)

    # -- terminal paths ------------------------------------------------------

    async def _escalate(
        self, artifact: CapabilityArtifact, step_id: str, reason: str, goal: str
    ) -> None:
        await self.handoff.intervene(
            surface=self.surface,
            capability_id=artifact.capability_id,
            goal=goal,
            step_id=step_id,
            reason=reason,
            evidence=self.evidence,
        )
        self._human_intervened = True

    async def _hard_failure(
        self,
        artifact: CapabilityArtifact,
        step: CapabilityStep,
        planned: PlannedAction,
        error: SurfaceError,
        goal: str,
        outputs: dict[str, Any],
    ) -> tuple[str | None, RunResult | None]:
        """An unmodelled state: capture it, offer the session to a human, try once.

        Returns `(step_result, None)` if the operator resolved it and the step then
        succeeded — the caller carries on with the remaining steps. Returns
        `(None, RunResult)` if the run has to stop.
        """
        screenshot = self.evidence.path_for(f"failure-{step.id}.png")
        await self.surface.screenshot(str(screenshot))
        self.evidence.record(
            "unrecognised_state",
            {
                "step_id": step.id,
                "error": str(error),
                "url": await self.surface.current_url(),
                "screenshot": str(screenshot),
            },
        )

        await self._escalate(artifact, step.id, f"Unrecognised runtime state: {error}", goal)

        # Exactly one attempt after control comes back. If the operator fixed the
        # state this succeeds; if not, stop. Looping here would mean repeatedly
        # calling a person about a problem they have already looked at.
        retry_result, retry_error = await self._attempt_step(step, planned)
        if retry_error is None:
            self.evidence.record(
                "resumed_after_handoff", {"step_id": step.id, "result": retry_result}
            )
            return retry_result, None

        self.evidence.record(
            "handoff_did_not_resolve", {"step_id": step.id, "error": str(retry_error)}
        )
        return None, self._result(
            artifact,
            status=ResultStatus.HARD_FAILURE,
            step_id=step.id,
            error_code="STEP_EXECUTION_FAILED",
            expected=step.postcondition,
            observed=str(retry_error),
            message=f"Replay stopped at {step.id}: {error}",
            outputs=outputs,
        )

    async def _verify(
        self, artifact: CapabilityArtifact, outputs: dict[str, Any]
    ) -> RunResult:
        """Every step ran. That is not the same as having reached the goal.

        A click can succeed while the application quietly does nothing, so success
        is asserted against an independent condition rather than inferred from the
        absence of errors.
        """
        if await self.surface.checkpoint(
            artifact.checkpoint.locator, artifact.checkpoint.expected
        ):
            self.evidence.record(
                "replay_succeeded", {"outputs": outputs, "recovered": self._recovered}
            )
            return self._result(
                artifact,
                status=ResultStatus.SUCCESS,
                message="Deterministic replay completed and the checkpoint was verified.",
                outputs=outputs,
            )

        # Every step ran but we are not where we expected. Check once more whether
        # the application simply gave a different legitimate answer.
        outcome = await self._business_outcome(artifact)
        if outcome:
            return self._result(
                artifact,
                status=ResultStatus.BUSINESS_OUTCOME,
                outcome_code=outcome,
                message="Replay completed with a known business outcome.",
                outputs=outputs,
            )

        shot = self.evidence.path_for("checkpoint-failed.png")
        await self.surface.screenshot(str(shot))
        return self._result(
            artifact,
            status=ResultStatus.HARD_FAILURE,
            error_code="CHECKPOINT_FAILED",
            expected=artifact.checkpoint.description,
            observed=f"Checkpoint absent at {await self.surface.current_url()}",
            message="All steps ran, but the success condition could not be verified.",
            outputs=outputs,
        )
