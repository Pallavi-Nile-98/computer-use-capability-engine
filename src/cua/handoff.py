"""Transferring control of the live session to a person, and taking it back.

The property that matters is that there is exactly one session. The operator acts
in the same browser context the automation was using — same cookies, same page,
same half-filled form — because the accumulated state is the entire reason the
situation is recoverable at all. Closing the browser and handing someone a fresh
one would discard the only thing worth handing over, and would make the operator
redo work the automation had already done correctly.

Ownership is single-valued and explicit: at any moment control belongs to the
automation or to a person, never to both, and never to neither. Every transition is
written to the evidence log, so "who was driving when this happened" is answerable
after the fact rather than inferred.

The operator surface here is a terminal prompt, and that is a deliberate cut. What
had to be real is the *control-transfer model* — same session, one owner, recorded
transitions, resume that continues rather than restarts. What a production console
adds on top is transport and access control: a queue that routes to whoever is on
shift, an authenticated view of the live session, RBAC on which capabilities a
given operator may take over, and a grant that expires so a session cannot sit
open forever waiting for someone who went home. Those change how a human is
reached; they do not change the model below.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import uuid4

from .evidence import EvidenceRecorder
from .models import ControlOwner, InterventionRequest, InterventionResolution
from .surface import Surface


class Operator(Protocol):
    """However a human is actually reached.

    Kept as a seam so the routing mechanism can change without touching the
    control-transfer logic. Today a terminal; in production a queue, a Slack
    message, an on-call page.
    """

    name: str

    async def take_control(self, request: InterventionRequest) -> str:
        """Block until the person hands control back. Returns their note."""
        ...


class TerminalOperator:
    """Prompts at the console. The operator works in the visible browser window."""

    def __init__(self, name: str = "console-operator"):
        self.name = name

    async def take_control(self, request: InterventionRequest) -> str:
        print("\n" + "=" * 74)
        print("AUTOMATION PAUSED — you now have control of the live session.")
        print(f"  capability : {request.capability_id}")
        print(f"  goal       : {request.goal}")
        print(f"  step       : {request.step_id}")
        print(f"  reason     : {request.reason}")
        print(f"  screenshot : {request.screenshot_path}")
        print("")
        print("Act in the open browser window, then describe what you did.")
        print("=" * 74)

        # to_thread keeps the blocking input() off the event loop, so the browser
        # session stays responsive while the operator works in it.
        note = await asyncio.to_thread(input, "What did you do? (Enter to resume) > ")
        return note.strip() or "No note recorded."


class ScriptedOperator:
    """Non-interactive operator, for tests and reproducible evidence runs.

    Optionally performs a repair on the same session before handing back, so the
    resume path is genuinely exercised rather than rubber-stamped. A handoff test
    where the operator does nothing only proves the pause works.
    """

    def __init__(
        self,
        note: str = "Scripted operator: acknowledged without changing state.",
        name: str = "scripted-operator",
        repair: Callable[[], Awaitable[None]] | None = None,
    ):
        self.name = name
        self.note = note
        self.repair = repair

    async def take_control(self, request: InterventionRequest) -> str:
        if self.repair is not None:
            await self.repair()
        return self.note


class HandoffController:
    """Owns the question of who is currently allowed to act on the session."""

    def __init__(self, operator: Operator | None = None):
        self.operator = operator or TerminalOperator()
        self.owner = ControlOwner.AUTOMATION
        # Kept for the run's lifetime. Repeated interventions on the same step are
        # a signal that a capability needs re-recording, and that signal is only
        # visible if the interventions are counted somewhere.
        self.history: list[InterventionResolution] = []

    async def intervene(
        self,
        *,
        surface: Surface,
        capability_id: str,
        goal: str,
        step_id: str,
        reason: str,
        evidence: EvidenceRecorder,
    ) -> InterventionResolution:
        """Pause, hand the live session to a person, wait, and take it back."""
        if self.owner is not ControlOwner.AUTOMATION:
            # Two escalations at once would mean two parties believing they may act
            # on one browser. Refusing loudly is the only safe response.
            raise RuntimeError(
                "Refusing to escalate: a human already holds this session."
            )

        # Capture the state the operator is inheriting, before anything moves. The
        # screenshot is the context that makes the request actionable — a reason
        # string alone does not tell you what is on the screen.
        before_shot = evidence.path_for(f"handoff-{step_id}-before.png")
        await surface.screenshot(str(before_shot))
        url_before = await surface.current_url()

        request = InterventionRequest(
            id=str(uuid4()),
            capability_id=capability_id,
            goal=goal,
            step_id=step_id,
            reason=reason,
            screenshot_path=str(before_shot),
        )

        # Ownership flips before the operator is contacted, not after they reply.
        # Otherwise there is a window in which the automation still believes it may
        # act while a human is already clicking.
        self.owner = ControlOwner.HUMAN
        evidence.record("control_transferred_to_human", request.model_dump(mode="json"))

        note = await self.operator.take_control(request)

        # What actually changed while they held it.
        url_after = await surface.current_url()
        after_shot = evidence.path_for(f"handoff-{step_id}-after.png")
        await surface.screenshot(str(after_shot))

        self.owner = ControlOwner.AUTOMATION

        resolution = InterventionResolution(
            intervention_id=request.id,
            resolved_by=self.operator.name,
            note=note,
            url_before=url_before,
            url_after=url_after,
            # Recorded rather than asserted. An operator who changed nothing and one
            # who fixed the state both press Enter; only the session can tell you
            # which happened.
            state_changed=url_before != url_after,
        )

        self.history.append(resolution)
        evidence.record(
            "control_returned_to_automation",
            resolution.model_dump(mode="json") | {"after_screenshot": str(after_shot)},
        )
        return resolution
