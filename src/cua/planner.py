"""Deciding the next action from one observation.

One action per turn, never a batch. A plan that assumes the next three steps will
succeed cannot notice that the second one silently did nothing — and on a legacy
app, silently doing nothing is the common failure, not an exception.

Two implementations of the same interface:

  ClaudePlanner    the real one. Asks a model, and constrains its answer to the
                   same `PlannedAction` type the replay engine consumes, so a
                   malformed decision fails at the boundary instead of mid-click.

  ScriptedPlanner  a deterministic stand-in. Not a simulation of the model and not
                   a mock — it reads the same observation and returns the same
                   type, it simply does not reason. It exists so the orchestration,
                   artifact, guardrail and handoff layers can be run and reviewed
                   without credentials, and so the test suite stays fast and
                   repeatable.

The value of the second one is easy to undersell. Everything interesting in this
project is the machinery *around* the model, and that machinery is much easier to
trust when it can be exercised without a network call.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod

from .models import (
    ActionKind,
    Checkpoint,
    Locator,
    LocatorStrategy,
    Observation,
    PlannedAction,
    RiskLevel,
)

# Written to be read by a reviewer as much as by the model. Every line here is a
# decision about behaviour, and the ones about locators and risk are the two that
# determine whether the recorded artifact is worth anything.
SYSTEM_PROMPT = """\
You operate back-office banking applications one action at a time, the way a careful
human operator would. You are working out how to accomplish a goal so the flow can be
recorded and replayed later without you.

Choose exactly one action per turn, and only target controls that appear in the
observation you were given. Do not assume a control exists because it usually would.

Prefer locators in this order: accessible role and name, then an associated label,
then visible text, then placeholder, then a narrowly scoped CSS attribute. Use
coordinates only when nothing else exists. Whatever you choose has to still work
months from now on a slightly different version of this application, so put your
reasoning in the locator's `rationale` field — a reviewer reads it to decide whether
to trust the recording.

When a value comes from the goal rather than from the page — an account number, a
member id — set `parameter_name`. That is what turns a value you happened to be
given into a typed input the capability accepts. When you read a value the caller
asked for, set `output_name`.

When reading a value, target the element holding the value, not the label beside it.
A row reading "Current Balance    $4,281.73" has "Current Balance" as its label and
"$4,281.73" as its value; targeting the label extracts the word "Balance", which is
the same for every customer and therefore useless.

A checkpoint has to hold for every future invocation, not just this one. Never put a
value you read off the page into `expected` — the next caller is a different member
with a different balance. Assert on something structural instead: a section heading,
a status label, a page title. "Savings Account" is a good checkpoint; "$4,281.73" is
a recording of today.

Classify anything that changes the system of record as `irreversible`. Reaching a
confirmation screen is safe; confirming is not.

If the page shows an error, a permission denial, or any state you cannot safely act
on, choose `escalate` and explain what a human needs to decide. Escalating is a
normal outcome, not a failure.

Set `done` only when the goal is verifiably met. When you do, fill in `checkpoint`
with a condition that proves it — you are the only participant who has seen this
screen, so if you do not describe how to verify it, nothing downstream can.
"""


class Planner(ABC):
    """Anything that can choose the next action."""

    @abstractmethod
    async def next_action(
        self, goal: str, observation: Observation, history: list[PlannedAction]
    ) -> PlannedAction: ...


class ScriptedPlanner(Planner):
    """Deterministic planner covering the two demo goals."""

    async def next_action(
        self, goal: str, observation: Observation, history: list[PlannedAction]
    ) -> PlannedAction:
        taken = [item.action for item in history]
        text = observation.visible_text
        wants_subaccount = "sub-account" in goal.lower() or "subaccount" in goal.lower()

        # A run that lands on a business outcome or an error never reached the goal,
        # so there is no flow worth recording. Escalate rather than compiling a
        # capability that only works when the data happens to be present.
        for signal, reason in (
            ("MEMBER_NOT_FOUND", "the member in the goal does not exist"),
            ("PERMISSION_DENIED", "this account may not view that member"),
            ("SESSION_EXPIRED", "the servicing session expired during discovery"),
            ("APPLICATION_ERROR", "the servicing host returned an error"),
        ):
            if signal in text:
                return PlannedAction(
                    action=ActionKind.ESCALATE,
                    reasoning=f"Cannot record this flow: {reason} ({signal}).",
                )

        if ActionKind.FILL not in taken:
            match = re.search(r"\b(\d{5,12})\b", goal)
            return PlannedAction(
                action=ActionKind.FILL,
                target=Locator(
                    strategy=LocatorStrategy.LABEL,
                    value="Member ID",
                    rationale=(
                        "The field carries an associated label. Labels outlive the "
                        "table-based layout this page uses for positioning."
                    ),
                ),
                value=match.group(1) if match else "12345",
                parameter_name="member_id",
                reasoning="Enter the member identifier taken from the goal.",
            )

        if ActionKind.CLICK not in taken:
            return PlannedAction(
                action=ActionKind.CLICK,
                target=Locator(
                    strategy=LocatorStrategy.ROLE,
                    role="button",
                    value="Search",
                    rationale=(
                        "Accessible role and name. Independent of position, so it "
                        "survives changes to the surrounding table."
                    ),
                ),
                reasoning="Submit the member search.",
            )

        return (
            await self._subaccount(text) if wants_subaccount else await self._balance(taken)
        )

    async def _balance(self, taken: list[ActionKind]) -> PlannedAction:
        if ActionKind.READ not in taken:
            return PlannedAction(
                action=ActionKind.READ,
                target=Locator(
                    strategy=LocatorStrategy.CSS,
                    value="[data-field='savings-balance']",
                    rationale=(
                        "A business-field marker scoped to the single value being "
                        "extracted, rather than a positional cell reference."
                    ),
                ),
                output_name="savings_balance",
                reasoning="Read the savings balance the caller asked for.",
            )
        return PlannedAction(
            action=ActionKind.COMPLETE,
            done=True,
            reasoning="The balance was read from the member details page.",
            checkpoint=Checkpoint(
                description="Member details page shows the savings account section.",
                locator=Locator(
                    strategy=LocatorStrategy.TEXT,
                    value="Savings Account",
                    exact=False,
                    rationale=(
                        "Business-state text proves the right page rendered, "
                        "independently of the URL shape a given tenant uses."
                    ),
                ),
                expected="Savings Account",
            ),
        )

    async def _subaccount(self, text: str) -> PlannedAction:
        if "Sub-account Created" in text:
            return PlannedAction(
                action=ActionKind.COMPLETE,
                done=True,
                reasoning="The sub-account was created and the confirmation rendered.",
                checkpoint=Checkpoint(
                    description="Confirmation screen reports the sub-account was opened.",
                    locator=Locator(
                        strategy=LocatorStrategy.CSS,
                        value="[data-field='subaccount-status']",
                        rationale=(
                            "A dedicated status field on the confirmation screen. "
                            "Asserting on it proves the write landed, rather than "
                            "proving only that a click fired."
                        ),
                    ),
                    expected="Sub-account opened",
                ),
            )

        if "Review New Sub-account" not in text:
            return PlannedAction(
                action=ActionKind.CLICK,
                target=Locator(
                    strategy=LocatorStrategy.ROLE,
                    role="link",
                    value="Open sub-account",
                    rationale="Accessible link name; navigation intent, not layout.",
                ),
                reasoning="Navigate to the sub-account review screen.",
            )

        # Reaching the review screen is safe. Confirming writes to the system of
        # record, so it is classified irreversible and left for the guardrail to
        # gate rather than quietly performed here.
        return PlannedAction(
            action=ActionKind.CLICK,
            target=Locator(
                strategy=LocatorStrategy.ROLE,
                role="button",
                value="Confirm and create",
                rationale="Accessible role and name on the confirmation control.",
            ),
            risk=RiskLevel.IRREVERSIBLE,
            reasoning="Creating the sub-account changes the system of record.",
        )


class ClaudePlanner(Planner):
    """Structured-output planner backed by the Anthropic Messages API."""

    def __init__(self, model: str | None = None, max_tokens: int = 8_000):
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "The anthropic package is required for live discovery. "
                "Install project dependencies with `pip install -e '.[dev]'`."
            ) from exc

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it, or run discovery with "
                "`--planner scripted` to exercise the same loop without a model."
            )

        self.client = AsyncAnthropic()
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
        # Headroom matters: on this model the budget covers reasoning as well as
        # the reply, so a tight limit truncates the decision rather than the prose.
        self.max_tokens = max_tokens

    @staticmethod
    def _render(goal: str, observation: Observation, history: list[PlannedAction]) -> str:
        """Describe the screen the way a person would describe it over the phone."""
        lines = [
            f"Goal: {goal}",
            "",
            f"Current URL: {observation.url}",
            f"Page title: {observation.title}",
            "",
            "Visible text:",
            observation.visible_text[:6_000],
            "",
            "Interactive controls on this page:",
        ]
        for control in observation.controls:
            described = {key: value for key, value in control.items() if value}
            lines.append(f"- {described}")

        if observation.fields:
            lines += [
                "",
                "Readable values on this page. Use the given locator to extract one;",
                "do not invent a selector, because you cannot see this page's markup:",
            ]
            for field in observation.fields:
                described = {key: value for key, value in field.items() if value}
                lines.append(f"- {described}")

        if history:
            lines += ["", "Actions already taken, oldest first:"]
            for index, item in enumerate(history, start=1):
                target = item.target.value if item.target else "-"
                lines.append(f"{index}. {item.action} target={target} :: {item.reasoning}")

        lines += ["", "Choose the single next action."]
        return "\n".join(lines)

    async def next_action(
        self, goal: str, observation: Observation, history: list[PlannedAction]
    ) -> PlannedAction:
        prompt = self._render(goal, observation, history)
        correction: str | None = None

        # One bounded retry, and only for a schema violation the model can fix from
        # the error text — a `done` with no checkpoint, say. Deliberately not an
        # open-ended repair loop: if the second attempt is also invalid, that is a
        # signal to escalate, not to keep paying for guesses.
        for attempt in (1, 2):
            message = prompt if correction is None else f"{prompt}\n\n{correction}"
            response = await self.client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": message}],
                output_format=PlannedAction,
            )

            if response.stop_reason == "refusal":
                raise RuntimeError(
                    "The model declined to plan this step. Treated as an escalation "
                    "rather than retried, because retrying a refusal is not a fix."
                )

            action = response.parsed_output
            if action is not None:
                return action

            if attempt == 1:
                correction = (
                    "Your previous answer did not satisfy the required schema. "
                    "Return one valid action. If you are setting done=true, you must "
                    "also provide a checkpoint that proves the goal was reached."
                )

        raise RuntimeError(
            "The model did not return a usable action after a correction attempt."
        )
