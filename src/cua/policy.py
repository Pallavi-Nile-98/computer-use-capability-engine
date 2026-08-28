"""Guardrails, enforced outside the model.

Everything here is checked in ordinary Python, before an action reaches a surface.
None of it is a prompt instruction, and that is the point: a system prompt asking
the model not to do something is a request, and a request can be talked out of. A
model that decides to click "Confirm and create" still has to get past this file,
and this file does not negotiate.

Three rules:

  1. Only allowlisted hosts and schemes. Not a blocklist — a blocklist is a list of
     the attacks you already thought of.
  2. Only allowlisted action types. A capability that never needed to navigate
     should not be able to navigate.
  3. Risky actions need explicit human confirmation, per invocation.

The third is the one worth arguing about, so it is argued about below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .models import ActionKind, PlannedAction, RiskLevel


class PolicyViolation(RuntimeError):
    """An action was refused. Deliberately not a subclass of SurfaceError.

    A refusal is not a failure of the surface, and callers must be able to tell
    "the app broke" apart from "we would not let this happen".
    """


@dataclass(frozen=True)
class Policy:
    """What this deployment permits.

    Frozen so it cannot be mutated at runtime. If a policy could be widened
    mid-run, the check performed at step one would say nothing about step nine.
    """

    # Exact hostnames only. Suffix matching looks convenient and is how allowlists
    # get bypassed: an attacker registers `bank.com.evil.net`, which "ends with"
    # nothing useful but passes a careless `in` check.
    allowed_hosts: frozenset[str] = frozenset({"127.0.0.1", "localhost"})

    # http is permitted because the synthetic target runs locally over http. A real
    # deployment would allow https only, and this is the line you would change.
    allowed_schemes: frozenset[str] = frozenset({"http", "https"})

    allowed_actions: frozenset[ActionKind] = frozenset(ActionKind)

    # Risk classes that may not proceed without a human explicitly saying so for
    # this specific invocation.
    require_confirmation_for: frozenset[RiskLevel] = frozenset({RiskLevel.IRREVERSIBLE})

    def check_url(self, url: str) -> None:
        """Refuse to open anything outside the allowlist."""
        parsed = urlparse(url)
        if parsed.scheme not in self.allowed_schemes:
            raise PolicyViolation(
                f"Blocked scheme {parsed.scheme!r}. Permitted: "
                f"{', '.join(sorted(self.allowed_schemes))}."
            )
        if parsed.hostname not in self.allowed_hosts:
            raise PolicyViolation(
                f"Host {parsed.hostname!r} is not allowlisted. Permitted: "
                f"{', '.join(sorted(self.allowed_hosts))}."
            )

    def check_action(self, action: PlannedAction, human_confirmed: bool = False) -> None:
        """Refuse an action type that is not permitted, or a risky one nobody approved.

        `human_confirmed` is passed per call rather than stored on the policy, so
        approving one irreversible step never silently authorises the next one.
        """
        if action.action not in self.allowed_actions:
            raise PolicyViolation(f"Action type {action.action!r} is not allowlisted.")

        if action.risk in self.require_confirmation_for and not human_confirmed:
            raise PolicyViolation(
                f"This step is classified {action.risk} and has not been confirmed "
                f"for this invocation. Reason given: {action.reasoning}"
            )

    def scoped_to(self, artifact_hosts: list[str], artifact_actions: list[ActionKind]) -> "Policy":
        """Narrow this policy to what one capability actually needs.

        Defence in depth. The deployment policy says what is permissible at all;
        the artifact says what this particular recording ever needed. Intersecting
        them means a capability recorded to read a balance cannot later be replayed
        into writing one, even if its file is edited — the runtime policy still has
        to agree, and it was never widened.
        """
        return Policy(
            allowed_hosts=self.allowed_hosts & frozenset(artifact_hosts),
            allowed_schemes=self.allowed_schemes,
            allowed_actions=self.allowed_actions & frozenset(artifact_actions),
            require_confirmation_for=self.require_confirmation_for,
        )
