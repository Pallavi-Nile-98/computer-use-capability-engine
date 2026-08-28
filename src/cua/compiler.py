"""Turning a successful run into a reusable capability.

The compiler generalises. A run is a specific thing that happened once — this
member, this balance, this afternoon. A capability is the pattern behind it. The
translation is small but every part of it is a decision:

  a value that came from the goal        becomes a typed input
  a value that was read off the screen   becomes a typed output
  the actions that were actually used    become the permitted action list
  the app's known endings                come from its profile
  the proof the goal was reached         comes from the planner that saw it

Note the last two. The compiler deliberately invents nothing. It does not guess a
checkpoint, because only the planner saw the finished screen; it does not guess the
application's error vocabulary, because that belongs to the app rather than to this
recording; and it does not name the capability, because the platform team
registering it owns the identifier an agent will later call.

A compiler that guessed any of those would produce artifacts that look right and
are quietly wrong — every capability inheriting the assumptions of whichever flow
happened to be recorded first.

One thing that is deliberately *not* carried across: the model's transcript. The
reasoning that produced the flow is evidence, and it lives in the evidence log. It
is not part of the contract, because a capability's meaning must not depend on the
wording of the prompt that discovered it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    ActionKind,
    CapabilityArtifact,
    CapabilityContract,
    CapabilityStep,
    Checkpoint,
    ExecutedAction,
    OutputSpec,
    ParameterSpec,
    RiskLevel,
)
from .profiles import profile_for


@dataclass(frozen=True)
class CapabilitySpec:
    """Who this capability is, supplied by the caller rather than inferred.

    An identifier derived from the goal text would change whenever someone
    rephrased the goal, and an agent's tool call would break for no reason. The
    name is part of the published interface, so a human owns it.
    """

    capability_id: str
    name: str
    description: str
    app_family: str
    version: str = "1.0.0"


def _parameter_spec(name: str) -> ParameterSpec:
    """Describe one input the calling agent must supply."""
    return ParameterSpec(
        name=name,
        type="string",
        description=f"Value supplied by the calling agent for {name!r} at invocation time.",
        # Sensitive by default. In a regulated environment the safe assumption is
        # that a caller-supplied identifier is protected until a reviewer decides
        # otherwise — the cost of over-redacting a log is much lower than the cost
        # of writing a member number into one.
        sensitive=True,
        # Identifiers get a shape constraint so a malformed invocation is rejected
        # before a browser is even opened. Cheap, and it turns a confusing
        # mid-flow failure into a clear argument error.
        pattern=r"^\d{5,12}$" if name.endswith("_id") else None,
    )


def _output_spec(name: str) -> OutputSpec:
    return OutputSpec(
        name=name,
        type="string",
        description=f"Value {name!r} as displayed by the application at replay time.",
        sensitive=True,
    )


def compile_artifact(
    goal: str,
    entry_point: str,
    trace: list[ExecutedAction],
    spec: CapabilitySpec,
    checkpoint: Checkpoint,
) -> CapabilityArtifact:
    """Build a reusable capability from a completed discovery trace."""
    profile = profile_for(spec.app_family)

    steps: list[CapabilityStep] = []
    input_names: list[str] = []
    output_names: list[str] = []

    for item in trace:
        action = item.planned

        # Control-flow decisions are not steps. `complete` and `escalate` describe
        # the run's conclusion, not something to replay.
        if action.action in {ActionKind.COMPLETE, ActionKind.ESCALATE}:
            continue

        value_template = action.value
        if action.parameter_name:
            if action.parameter_name not in input_names:
                input_names.append(action.parameter_name)
            # The generalisation, in one line. Store the reference, never the value
            # this particular run happened to be given.
            value_template = "${" + action.parameter_name + "}"

        if action.output_name and action.output_name not in output_names:
            output_names.append(action.output_name)

        steps.append(
            CapabilityStep(
                id=f"step-{len(steps) + 1:02d}",
                action=action.action,
                target=action.target,
                value_template=value_template,
                output_name=action.output_name,
                risk=action.risk,
                postcondition=(
                    "The declared output is present and non-empty."
                    if action.action == ActionKind.READ
                    else "The application accepted the action without an error signal."
                ),
            )
        )

    if not steps:
        raise ValueError(
            "Cannot compile a capability from a run with no executed steps. A flow "
            "that did nothing is not a capability."
        )

    return CapabilityArtifact(
        capability_id=spec.capability_id,
        version=spec.version,
        name=spec.name,
        description=spec.description,
        app_family=spec.app_family,
        surface_type=profile.surface_type,
        entry_point=entry_point,
        contract=CapabilityContract(
            inputs=[_parameter_spec(name) for name in input_names],
            outputs=[_output_spec(name) for name in output_names],
        ),
        steps=steps,
        checkpoint=checkpoint,
        # The application's vocabulary, not this flow's. Shared by every capability
        # recorded against the product, and by every institution running it.
        business_outcomes=profile.business_outcomes,
        recovery_rules=profile.recovery_rules,
        allowed_domains=["127.0.0.1", "localhost"],
        # Least privilege, derived rather than declared: a capability may only ever
        # perform the kinds of action its recording actually needed. A flow that
        # only ever read cannot later be replayed into navigating somewhere new,
        # even if its file is edited.
        allowed_actions=sorted({step.action for step in steps}),
    )


class OverfittedRecording(ValueError):
    """The recording only works for the invocation it was recorded against."""


def validate_recording(
    artifact: CapabilityArtifact,
    outputs: dict[str, str],
    parameters: dict[str, str],
) -> None:
    """Reject a capability that has baked this run's data into itself.

    A planner works from one concrete run, so the tempting mistake is to assert on
    what it happened to see. A checkpoint of "$4,281.73" passes for the member it
    was recorded against and fails for every other one — and it fails *as a
    checkpoint*, meaning a perfectly good replay gets reported as a hard failure.
    That is worse than having no checkpoint at all, because it looks like verification.

    Detected here rather than left to prompting. Prompting reduces how often a model
    does this; validation decides whether the result is allowed to be saved. Both are
    worth having, and only one of them is a guarantee.
    """
    checkpoint_text = " ".join(
        filter(None, [artifact.checkpoint.expected, artifact.checkpoint.locator.value])
    )

    for name, value in outputs.items():
        value = str(value).strip()
        # Short values are too likely to appear coincidentally in a legitimate
        # structural assertion to accuse the recording of over-fitting.
        if len(value) >= 4 and value in checkpoint_text:
            raise OverfittedRecording(
                f"The checkpoint asserts on {value!r}, which is the value read into "
                f"output {name!r}. That is this run's data, so the checkpoint would "
                f"fail for every other invocation. Assert on a stable heading or "
                f"label instead."
            )

    for name, value in parameters.items():
        value = str(value).strip()
        if len(value) >= 4 and value in checkpoint_text:
            raise OverfittedRecording(
                f"The checkpoint asserts on {value!r}, which was supplied as input "
                f"{name!r}. The next caller will pass something different."
            )

    # A read that returns the text its own locator was searching for has almost
    # certainly selected a label rather than the value beside it.
    for step in artifact.steps:
        if step.action != ActionKind.READ or not step.target or not step.output_name:
            continue
        observed = str(outputs.get(step.output_name, "")).strip()
        if observed and observed == step.target.value.strip():
            raise OverfittedRecording(
                f"Step {step.id} read {observed!r} using a locator that searches for "
                f"that same text, so it has selected the label rather than the value "
                f"next to it. Output {step.output_name!r} would be identical for "
                f"every member."
            )


def highest_risk(artifact: CapabilityArtifact) -> RiskLevel:
    """The risk class a reviewer is really signing off when they approve this.

    Surfaced at approval time so "this capability writes to the system of record"
    is something a person is told, rather than something they have to notice by
    reading every step.
    """
    order = [RiskLevel.SAFE, RiskLevel.REVERSIBLE, RiskLevel.IRREVERSIBLE]
    return max(
        (step.risk for step in artifact.steps),
        key=order.index,
        default=RiskLevel.SAFE,
    )
