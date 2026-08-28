"""Goal-driven discovery: observe, decide, act, repeat until done or stopped.

This is the only place a model sits in the decision path, and its job is narrower
than it first appears. It is not "do the task well once". It is "produce a trace
good enough to compile into something that never needs a model again".

That framing changes what the loop optimises for. It records why each action was
chosen, insists on a checkpoint before it will call anything complete, and refuses
to compile a capability from a run that did not actually reach the goal — because
a flow recorded against a member who happened to exist is not a capability, it is
an anecdote.

Every run ends in exactly one of three ways, and all three are recorded:

  success                 the goal was reached and a draft capability was compiled
  intervention_required   stopped safely; a person has to look
  hard_failure            the run finished but produced nothing usable

There is no fourth ending where the loop quietly gives up.
"""

from __future__ import annotations

from pathlib import Path

from .compiler import CapabilitySpec, compile_artifact
from .evidence import EvidenceRecorder
from .handoff import HandoffController
from .models import (
    ActionKind,
    CapabilityArtifact,
    ExecutedAction,
    PlannedAction,
    ResultStatus,
    RiskLevel,
    RunResult,
)
from .planner import Planner
from .policy import Policy, PolicyViolation
from .surface import Surface, SurfaceError


class DiscoveryEngine:
    def __init__(
        self,
        surface: Surface,
        planner: Planner,
        policy: Policy,
        evidence: EvidenceRecorder,
        handoff: HandoffController | None = None,
        max_steps: int = 12,
    ):
        self.surface = surface
        self.planner = planner
        self.policy = policy
        self.evidence = evidence
        # Optional. Without it a risky action simply stops the run; with it, a
        # person can authorise the action and have it recorded.
        self.handoff = handoff
        # A hard stop, not a suggestion. An agent that cannot find its way in a
        # dozen steps is lost, and letting it wander costs money and leaves a
        # confusing trace behind.
        self.max_steps = max_steps

    async def run(
        self, goal: str, entry_point: str, spec: CapabilitySpec
    ) -> tuple[RunResult, CapabilityArtifact | None]:
        self.policy.check_url(entry_point)
        await self.surface.start(entry_point)

        history: list[PlannedAction] = []
        trace: list[ExecutedAction] = []
        outputs: dict[str, str] = {}
        human_intervened = False

        self.evidence.record(
            "discovery_started",
            {"goal": goal, "entry_point": entry_point, "capability_id": spec.capability_id},
        )

        for sequence in range(1, self.max_steps + 1):
            # Screenshot every observation, not just the failures. During discovery
            # the interesting question is usually "what did it think it was looking
            # at when it chose that", and you cannot answer it after the fact.
            shot = self.evidence.path_for(f"discovery-{sequence:02d}.png")
            observation = await self.surface.observe(str(shot))
            self.evidence.record("observation", observation.model_dump(mode="json"))

            action = await self.planner.next_action(goal, observation, history)
            # The model's reasoning is recorded here, in evidence — and nowhere in
            # the artifact. Why a flow was chosen is worth keeping; it is not part
            # of the contract.
            self.evidence.record("model_decision", action.model_dump(mode="json"))

            if action.action == ActionKind.ESCALATE:
                # The planner looked at the screen and decided it should not act.
                # That is a correct outcome, not a failure of the loop.
                return (
                    RunResult(
                        status=ResultStatus.INTERVENTION_REQUIRED,
                        error_code="DISCOVERY_ESCALATED",
                        message=action.reasoning,
                        evidence_dir=str(self.evidence.directory),
                        human_intervened=human_intervened,
                    ),
                    None,
                )

            if action.done or action.action == ActionKind.COMPLETE:
                if action.checkpoint is None:
                    # The schema already rejects `done` without a checkpoint, so
                    # this only fires for a bare COMPLETE. Refusing here keeps the
                    # rule in one place: nothing is recorded that cannot verify itself.
                    return (
                        RunResult(
                            status=ResultStatus.HARD_FAILURE,
                            error_code="NO_CHECKPOINT_DECLARED",
                            message=(
                                "Completion was reported without a checkpoint, so the "
                                "capability would have no way to verify itself."
                            ),
                            evidence_dir=str(self.evidence.directory),
                        ),
                        None,
                    )

                artifact = compile_artifact(goal, entry_point, trace, spec, action.checkpoint)
                self.evidence.record(
                    "artifact_compiled",
                    artifact.model_dump(mode="json", exclude={"created_at"}),
                )
                return (
                    RunResult(
                        status=ResultStatus.SUCCESS,
                        capability_id=artifact.capability_id,
                        outputs=outputs,
                        message="Goal reached; draft capability compiled.",
                        evidence_dir=str(self.evidence.directory),
                        human_intervened=human_intervened,
                    ),
                    artifact,
                )

            # Guardrails apply during discovery too. This is the moment a person
            # decides whether a capability that writes to the system of record is
            # allowed to exist at all — a better place to ask than at replay time,
            # when it is already recorded and approved.
            authorised = False
            try:
                self.policy.check_action(action)
            except PolicyViolation as exc:
                if self.handoff is None or action.risk is RiskLevel.SAFE:
                    # Either nobody can be asked, or the refusal was not about risk
                    # (a disallowed action type, say), which no human should wave
                    # through mid-run.
                    self.evidence.record(
                        "discovery_blocked", {"sequence": sequence, "error": str(exc)}
                    )
                    return (
                        RunResult(
                            status=ResultStatus.INTERVENTION_REQUIRED,
                            failed_step_id=f"discovery-{sequence:02d}",
                            observed=str(exc),
                            error_code="POLICY_BLOCKED_DISCOVERY",
                            message="Discovery stopped safely and needs a human decision.",
                            evidence_dir=str(self.evidence.directory),
                            human_intervened=human_intervened,
                        ),
                        None,
                    )

                resolution = await self.handoff.intervene(
                    surface=self.surface,
                    capability_id=spec.capability_id,
                    goal=goal,
                    step_id=f"discovery-{sequence:02d}",
                    reason=(
                        f"The planner proposed a {action.risk} action on "
                        f"{action.target.value if action.target else action.action}. "
                        "A human must decide whether to perform and record it."
                    ),
                    evidence=self.evidence,
                )
                human_intervened = True
                authorised = True
                self.evidence.record(
                    "risky_action_authorised",
                    {
                        "sequence": sequence,
                        "by": resolution.resolved_by,
                        "note": resolution.note,
                    },
                )

            try:
                if authorised:
                    self.policy.check_action(action, human_confirmed=True)
                result = await self.surface.execute(action)
            except (PolicyViolation, SurfaceError) as exc:
                self.evidence.record(
                    "discovery_blocked", {"sequence": sequence, "error": str(exc)}
                )
                return (
                    RunResult(
                        status=ResultStatus.INTERVENTION_REQUIRED,
                        failed_step_id=f"discovery-{sequence:02d}",
                        observed=str(exc),
                        error_code="DISCOVERY_ACTION_FAILED",
                        message="Discovery stopped safely and needs a human decision.",
                        evidence_dir=str(self.evidence.directory),
                        human_intervened=human_intervened,
                    ),
                    None,
                )

            if action.output_name and result is not None:
                outputs[action.output_name] = result

            trace.append(
                ExecutedAction(
                    sequence=sequence,
                    planned=action,
                    observed_url=observation.url,
                    result=result,
                )
            )
            history.append(action)

        return (
            RunResult(
                status=ResultStatus.INTERVENTION_REQUIRED,
                error_code="MAX_STEPS_REACHED",
                message=f"Discovery hit the {self.max_steps}-step safety limit.",
                evidence_dir=str(self.evidence.directory),
                human_intervened=human_intervened,
            ),
            None,
        )


def save_artifact(artifact: CapabilityArtifact, path: str | Path) -> Path:
    """Write a capability to disk as indented JSON.

    Indented on purpose. This file is meant to be read and reviewed by a person
    before it is approved, and diffed by a reviewer when it changes.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    return destination
