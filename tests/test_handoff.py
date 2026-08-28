"""Control transfer: same session, one owner, recorded transitions."""

import tempfile
import unittest
from pathlib import Path

from cua.evidence import EvidenceRecorder
from cua.handoff import HandoffController, ScriptedOperator
from cua.models import ControlOwner
from cua.surface import FakeSurface

ENTRY = "http://127.0.0.1:8000/legacy"


class ControlTransfer(unittest.IsolatedAsyncioTestCase):
    async def _surface(self) -> FakeSurface:
        surface = FakeSurface()
        await surface.start(ENTRY)
        return surface

    async def test_the_operator_gets_the_same_session(self) -> None:
        """The requirement the brief is explicit about: not a fresh session."""
        with tempfile.TemporaryDirectory() as directory:
            surface = await self._surface()
            controller = HandoffController(operator=ScriptedOperator())

            await controller.intervene(
                surface=surface,
                capability_id="lookup-balance",
                goal="read a balance",
                step_id="step-03",
                reason="unrecognised state",
                evidence=EvidenceRecorder(Path(directory)),
            )

            # Never restarted, never re-entered: the accumulated state survived.
            self.assertEqual(surface.navigations, 0)
            self.assertEqual(surface.entry_point, ENTRY)

    async def test_control_returns_to_automation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = HandoffController(operator=ScriptedOperator())
            resolution = await controller.intervene(
                surface=await self._surface(),
                capability_id="c",
                goal="g",
                step_id="step-01",
                reason="r",
                evidence=EvidenceRecorder(Path(directory)),
            )
            self.assertEqual(controller.owner, ControlOwner.AUTOMATION)
            self.assertEqual(resolution.returned_to, ControlOwner.AUTOMATION)

    async def test_what_the_operator_did_is_recorded(self) -> None:
        """An operator who fixed it and one who shrugged both press Enter."""
        with tempfile.TemporaryDirectory() as directory:
            surface = await self._surface()
            surface.member_id = "12345"

            async def repair() -> None:
                surface.state = "details"

            controller = HandoffController(
                operator=ScriptedOperator(note="Cleared a stale filter.", repair=repair)
            )
            resolution = await controller.intervene(
                surface=surface,
                capability_id="c",
                goal="g",
                step_id="step-03",
                reason="r",
                evidence=EvidenceRecorder(Path(directory)),
            )

            self.assertTrue(resolution.state_changed)
            self.assertNotEqual(resolution.url_before, resolution.url_after)
            self.assertEqual(resolution.note, "Cleared a stale filter.")
            self.assertEqual(resolution.resolved_by, "scripted-operator")

    async def test_an_operator_who_changed_nothing_is_recorded_as_such(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = HandoffController(operator=ScriptedOperator())
            resolution = await controller.intervene(
                surface=await self._surface(),
                capability_id="c",
                goal="g",
                step_id="step-01",
                reason="r",
                evidence=EvidenceRecorder(Path(directory)),
            )
            self.assertFalse(resolution.state_changed)

    async def test_both_transitions_are_written_to_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = EvidenceRecorder(Path(directory))
            await HandoffController(operator=ScriptedOperator()).intervene(
                surface=await self._surface(),
                capability_id="c",
                goal="g",
                step_id="step-01",
                reason="r",
                evidence=recorder,
            )
            self.assertEqual(
                [event["event_type"] for event in recorder.read_events()],
                ["control_transferred_to_human", "control_returned_to_automation"],
            )

    async def test_two_parties_cannot_hold_one_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = HandoffController(operator=ScriptedOperator())
            controller.owner = ControlOwner.HUMAN
            with self.assertRaisesRegex(RuntimeError, "already holds"):
                await controller.intervene(
                    surface=await self._surface(),
                    capability_id="c",
                    goal="g",
                    step_id="step-01",
                    reason="r",
                    evidence=EvidenceRecorder(Path(directory)),
                )


if __name__ == "__main__":
    unittest.main()
