"""The whole thread, end to end: goal -> discovery -> capability -> replay -> result.

These assert the *result contract*, because that is what a calling agent depends on.
The single most important one is `test_missing_member_is_an_answer_not_a_failure` —
the brief names conflating those two as the most common design mistake on this
project.
"""

import tempfile
import unittest
from pathlib import Path

from cua.compiler import highest_risk
from cua.models import ResultStatus, RiskLevel
from cua.replay import resolve_value, validate_parameters
from cua.surface import FakeSurface

from .helpers import (
    BALANCE_GOAL,
    BALANCE_SPEC,
    SUBACCOUNT_GOAL,
    SUBACCOUNT_SPEC,
    approve,
    discover,
    replay_engine,
)


class Discovery(unittest.IsolatedAsyncioTestCase):
    async def test_the_recorded_value_becomes_a_parameter(self) -> None:
        """The generalisation that makes a recording reusable."""
        with tempfile.TemporaryDirectory() as directory:
            _, artifact = await discover(Path(directory), BALANCE_GOAL, BALANCE_SPEC)
            serialised = artifact.model_dump_json()

            self.assertIn("${member_id}", serialised)
            # The run used 12345. If that survived, the capability would only ever
            # work for one customer.
            self.assertNotIn('"value_template":"12345"', serialised)
            self.assertEqual([s.name for s in artifact.contract.inputs], ["member_id"])
            self.assertEqual([s.name for s in artifact.contract.outputs], ["savings_balance"])

    async def test_a_new_capability_is_never_immediately_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, artifact = await discover(Path(directory), BALANCE_GOAL, BALANCE_SPEC)
            self.assertEqual(artifact.approval_state, "draft")
            self.assertIsNone(artifact.approved_by)

    async def test_permissions_are_derived_from_what_the_run_needed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, artifact = await discover(Path(directory), BALANCE_GOAL, BALANCE_SPEC)
            # It never navigated, so it may never navigate.
            self.assertNotIn("navigate", [a.value for a in artifact.allowed_actions])

    async def test_a_run_that_never_reached_the_goal_records_nothing(self) -> None:
        """A flow recorded against a member who happened to exist is an anecdote."""
        with tempfile.TemporaryDirectory() as directory:
            result, artifact = await discover(
                Path(directory), BALANCE_GOAL, BALANCE_SPEC, member_exists=False
            )
            self.assertEqual(result.status, ResultStatus.INTERVENTION_REQUIRED)
            self.assertIsNone(artifact)

    async def test_a_risky_step_needs_a_person_before_it_is_recorded(self) -> None:
        """The gate sits at recording time, not only at replay time."""
        with tempfile.TemporaryDirectory() as directory:
            result, artifact = await discover(
                Path(directory), SUBACCOUNT_GOAL, SUBACCOUNT_SPEC
            )
            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertTrue(result.human_intervened)
            self.assertEqual(highest_risk(artifact), RiskLevel.IRREVERSIBLE)


class ReplayContract(unittest.IsolatedAsyncioTestCase):
    async def _approved(self, root: Path, goal=BALANCE_GOAL, spec=BALANCE_SPEC):
        _, artifact = await discover(root, goal, spec)
        return approve(artifact)

    async def test_success_returns_the_declared_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = await self._approved(root)
            result = await replay_engine(root, "ok", FakeSurface()).run(
                artifact, {"member_id": "12345"}
            )
            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(result.outputs["savings_balance"], "$4,281.73")
            self.assertFalse(result.human_intervened)

    async def test_missing_member_is_an_answer_not_a_failure(self) -> None:
        """The brief calls conflating these the most common mistake on this project."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = await self._approved(root)
            result = await replay_engine(root, "nf", FakeSurface(member_exists=False)).run(
                artifact, {"member_id": "99999"}
            )
            self.assertEqual(result.status, ResultStatus.BUSINESS_OUTCOME)
            self.assertEqual(result.outcome_code, "MEMBER_NOT_FOUND")

    async def test_permission_denied_is_also_an_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = await self._approved(root)
            result = await replay_engine(root, "pd", FakeSurface(restricted=True)).run(
                artifact, {"member_id": "77777"}
            )
            self.assertEqual(result.status, ResultStatus.BUSINESS_OUTCOME)
            self.assertEqual(result.outcome_code, "PERMISSION_DENIED")

    async def test_a_transient_error_is_recovered_and_still_reported(self) -> None:
        """Succeeding quietly would hide a capability that is degrading."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = await self._approved(root)
            surface = FakeSurface(faults=["APPLICATION_ERROR"])
            result = await replay_engine(root, "rec", surface).run(
                artifact, {"member_id": "12345"}
            )
            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(result.recovered_conditions, ["APPLICATION_ERROR"])
            self.assertEqual(surface.reloads, 1)

    async def test_a_persistent_known_error_is_recoverable_not_hard(self) -> None:
        """The distinction that tells a caller whether retrying later is sensible."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = await self._approved(root)
            surface = FakeSurface(faults=["APPLICATION_ERROR"] * 6)
            result = await replay_engine(root, "exh", surface).run(
                artifact, {"member_id": "12345"}
            )
            self.assertEqual(result.status, ResultStatus.RECOVERABLE_FAILURE)
            self.assertEqual(result.outcome_code, "APPLICATION_ERROR")
            self.assertEqual(result.error_code, "RECOVERY_BUDGET_EXHAUSTED")

    async def test_a_dropped_session_replays_the_flow_from_the_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = await self._approved(root)
            surface = FakeSurface(faults=["SESSION_EXPIRED"])
            result = await replay_engine(root, "sess", surface).run(
                artifact, {"member_id": "12345"}
            )
            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(result.recovered_conditions, ["SESSION_EXPIRED"])
            self.assertEqual(surface.navigations, 1)

    async def test_a_dismissable_popup_does_not_stop_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = await self._approved(root)
            surface = FakeSurface(faults=["INTERSTITIAL_NOTICE"])
            result = await replay_engine(root, "pop", surface).run(
                artifact, {"member_id": "12345"}
            )
            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertEqual(result.recovered_conditions, ["INTERSTITIAL_NOTICE"])

    async def test_a_draft_capability_cannot_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, artifact = await discover(root, BALANCE_GOAL, BALANCE_SPEC)
            result = await replay_engine(root, "draft", FakeSurface()).run(
                artifact, {"member_id": "12345"}
            )
            self.assertEqual(result.error_code, "ARTIFACT_NOT_APPROVED")


class IrreversibleActions(unittest.IsolatedAsyncioTestCase):
    async def test_an_unconfirmed_write_does_not_happen(self) -> None:
        """Approval of a capability is not consent to run its irreversible step."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, artifact = await discover(root, SUBACCOUNT_GOAL, SUBACCOUNT_SPEC)
            surface = FakeSurface()
            result = await replay_engine(root, "risky", surface).run(
                approve(artifact), {"member_id": "12345"}
            )
            self.assertEqual(result.status, ResultStatus.INTERVENTION_REQUIRED)
            self.assertEqual(result.error_code, "RISKY_ACTION_REQUIRES_CONFIRMATION")
            # The point of the whole gate: nothing was written.
            self.assertFalse(surface.subaccount_created)

    async def test_a_confirmed_write_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, artifact = await discover(root, SUBACCOUNT_GOAL, SUBACCOUNT_SPEC)
            surface = FakeSurface()
            result = await replay_engine(
                root, "risky-ok", surface, confirmed_risks={RiskLevel.IRREVERSIBLE}
            ).run(approve(artifact), {"member_id": "12345"})
            self.assertEqual(result.status, ResultStatus.SUCCESS)
            self.assertTrue(surface.subaccount_created)


class InvocationContract(unittest.IsolatedAsyncioTestCase):
    def test_a_template_without_its_parameter_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Missing required parameter"):
            resolve_value("${member_id}", {})

    async def test_a_malformed_argument_is_rejected_before_anything_opens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, artifact = await discover(Path(directory), BALANCE_GOAL, BALANCE_SPEC)
            with self.assertRaisesRegex(ValueError, "declared pattern"):
                validate_parameters(artifact, {"member_id": "Robert"})

    async def test_an_unknown_argument_is_rejected_rather_than_ignored(self) -> None:
        """A caller passing the wrong name has misunderstood something."""
        with tempfile.TemporaryDirectory() as directory:
            _, artifact = await discover(Path(directory), BALANCE_GOAL, BALANCE_SPEC)
            with self.assertRaisesRegex(ValueError, "Unknown parameter"):
                validate_parameters(artifact, {"member_id": "12345", "ssn": "123-45-6789"})


if __name__ == "__main__":
    unittest.main()
