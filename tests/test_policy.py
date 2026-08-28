"""Guardrails. Each test is an attack or a mistake the system must refuse."""

import unittest

from cua.models import ActionKind, PlannedAction, RiskLevel
from cua.policy import Policy, PolicyViolation


def action(kind: ActionKind = ActionKind.CLICK, risk: RiskLevel = RiskLevel.SAFE) -> PlannedAction:
    return PlannedAction(action=kind, risk=risk, reasoning="test action")


class UrlAllowlist(unittest.TestCase):
    def test_allows_a_listed_host(self) -> None:
        Policy().check_url("http://127.0.0.1:8000/legacy")

    def test_blocks_an_unlisted_host(self) -> None:
        with self.assertRaisesRegex(PolicyViolation, "not allowlisted"):
            Policy().check_url("https://example.com/anything")

    def test_blocks_non_web_schemes(self) -> None:
        # Without this, a capability could be pointed at the local filesystem.
        with self.assertRaisesRegex(PolicyViolation, "scheme"):
            Policy().check_url("file:///C:/Windows/win.ini")

    def test_blocks_a_lookalike_host(self) -> None:
        """The reason matching is exact rather than suffix- or substring-based.

        `127.0.0.1.evil.net` is a domain an attacker can register. It contains an
        allowlisted host as a substring and ends with something else entirely.
        """
        with self.assertRaises(PolicyViolation):
            Policy().check_url("http://127.0.0.1.evil.net/steal")
        with self.assertRaises(PolicyViolation):
            Policy().check_url("http://evil-127.0.0.1/steal")


class RiskGate(unittest.TestCase):
    def test_safe_actions_need_no_confirmation(self) -> None:
        Policy().check_action(action(ActionKind.READ))

    def test_irreversible_actions_are_refused_by_default(self) -> None:
        with self.assertRaisesRegex(PolicyViolation, "irreversible"):
            Policy().check_action(action(risk=RiskLevel.IRREVERSIBLE))

    def test_irreversible_actions_proceed_once_confirmed(self) -> None:
        Policy().check_action(action(risk=RiskLevel.IRREVERSIBLE), human_confirmed=True)

    def test_confirmation_does_not_persist_between_calls(self) -> None:
        """Approving one risky action must not authorise the next one.

        Confirmation is an argument rather than state on the policy precisely so
        that it cannot leak forward through a run.
        """
        policy = Policy()
        policy.check_action(action(risk=RiskLevel.IRREVERSIBLE), human_confirmed=True)
        with self.assertRaises(PolicyViolation):
            policy.check_action(action(risk=RiskLevel.IRREVERSIBLE))


class ScopedPolicy(unittest.TestCase):
    def test_a_capability_cannot_exceed_what_it_recorded(self) -> None:
        """Defence in depth: editing the artifact is not enough on its own."""
        scoped = Policy().scoped_to(
            ["127.0.0.1"], [ActionKind.CLICK, ActionKind.FILL, ActionKind.READ]
        )
        scoped.check_action(action(ActionKind.CLICK))
        with self.assertRaisesRegex(PolicyViolation, "not allowlisted"):
            scoped.check_action(action(ActionKind.NAVIGATE))

    def test_scoping_cannot_widen_the_deployment_policy(self) -> None:
        """An artifact claiming a host the deployment forbids gains nothing."""
        scoped = Policy().scoped_to(["evil.com", "127.0.0.1"], list(ActionKind))
        with self.assertRaises(PolicyViolation):
            scoped.check_url("https://evil.com/")
        scoped.check_url("http://127.0.0.1:8000/legacy")

    def test_policy_cannot_be_mutated_at_runtime(self) -> None:
        """A check at step one is meaningless if the rules can change by step nine."""
        policy = Policy()
        with self.assertRaises(Exception):
            policy.allowed_hosts = frozenset({"evil.com"})  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
