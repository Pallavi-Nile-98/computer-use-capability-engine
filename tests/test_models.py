"""Schema invariants: the things the artifact must refuse to represent.

Each of these is a bug that would otherwise surface mid-flow with a browser open.
Catching them at load time turns a confusing runtime failure into a clear message.
"""

import unittest

from pydantic import ValidationError

from cua.models import (
    ActionKind,
    CapabilityStep,
    Checkpoint,
    Locator,
    LocatorStrategy,
    PlannedAction,
    RecoveryAction,
    RecoveryRule,
    utc_now,
)


def label(value: str = "Member ID") -> Locator:
    return Locator(
        strategy=LocatorStrategy.LABEL, value=value, rationale="stable associated label"
    )


class StepInvariants(unittest.TestCase):
    def test_fill_must_say_what_to_type(self) -> None:
        with self.assertRaisesRegex(ValidationError, "value_template"):
            CapabilityStep(id="step-01", action=ActionKind.FILL, target=label())

    def test_read_must_say_where_the_value_goes(self) -> None:
        with self.assertRaisesRegex(ValidationError, "output_name"):
            CapabilityStep(id="step-01", action=ActionKind.READ, target=label())

    def test_click_must_have_something_to_click(self) -> None:
        with self.assertRaisesRegex(ValidationError, "target"):
            CapabilityStep(id="step-01", action=ActionKind.CLICK)

    def test_a_locator_must_justify_itself(self) -> None:
        # `rationale` is required, not optional. A recorded locator that nobody can
        # evaluate is not reviewable, and reviewability is the point of the artifact.
        with self.assertRaises(ValidationError):
            Locator(strategy=LocatorStrategy.CSS, value="#x")  # type: ignore[call-arg]


class CompletionInvariants(unittest.TestCase):
    def test_completion_requires_proof(self) -> None:
        """A planner may not claim success without saying how to verify it."""
        with self.assertRaisesRegex(ValidationError, "checkpoint"):
            PlannedAction(action=ActionKind.COMPLETE, reasoning="all finished", done=True)

    def test_completion_with_a_checkpoint_is_accepted(self) -> None:
        action = PlannedAction(
            action=ActionKind.COMPLETE,
            reasoning="balance was read",
            done=True,
            checkpoint=Checkpoint(
                description="details page rendered",
                locator=Locator(
                    strategy=LocatorStrategy.TEXT,
                    value="Savings Account",
                    rationale="business-state text",
                ),
                expected="Savings Account",
            ),
        )
        self.assertTrue(action.done)


class RecoveryInvariants(unittest.TestCase):
    def test_dismiss_must_know_what_to_click(self) -> None:
        with self.assertRaisesRegex(ValidationError, "dismiss"):
            RecoveryRule(
                code="NOTICE",
                description="a popup",
                detector=label("notice"),
                action=RecoveryAction.DISMISS,
            )

    def test_recovery_attempts_are_bounded_by_the_schema(self) -> None:
        # Not a convention someone must remember — the type will not hold a value
        # large enough to thrash against a failing application.
        with self.assertRaises(ValidationError):
            RecoveryRule(
                code="X",
                description="d",
                detector=label("x"),
                action=RecoveryAction.RELOAD,
                max_attempts=99,
            )


class ArtifactInvariants(unittest.TestCase):
    def test_approval_must_record_who_and_when(self) -> None:
        from cua.models import CapabilityArtifact, CapabilityContract

        base = dict(
            capability_id="c",
            name="n",
            description="d",
            app_family="demo-legacy-member-servicing",
            surface_type="legacy_web",
            entry_point="http://127.0.0.1:8000/legacy",
            contract=CapabilityContract(inputs=[], outputs=[]),
            steps=[CapabilityStep(id="step-01", action=ActionKind.CLICK, target=label("Search"))],
            checkpoint=Checkpoint(description="c", locator=label("x")),
            allowed_domains=["127.0.0.1"],
            allowed_actions=[ActionKind.CLICK],
        )

        with self.assertRaisesRegex(ValidationError, "approved"):
            CapabilityArtifact(**base, approval_state="approved")

        signed = CapabilityArtifact(
            **base, approval_state="approved", approved_by="a reviewer", approved_at=utc_now()
        )
        self.assertEqual(signed.approved_by, "a reviewer")

    def test_unknown_fields_are_refused_rather_than_ignored(self) -> None:
        """An executor that ignores an unknown field ignores a contract change."""
        from cua.models import CapabilityArtifact

        with self.assertRaises(ValidationError):
            CapabilityArtifact.model_validate(
                {"capability_id": "c", "some_future_field": True}
            )


if __name__ == "__main__":
    unittest.main()
