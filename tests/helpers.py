"""Shared setup for the tests.

Recording a capability takes a discovery run, so rather than hand-building
artifacts — which would test a fixture rather than the compiler — the tests record
one the same way the system does, then approve it.
"""

from __future__ import annotations

from pathlib import Path

from cua.compiler import CapabilitySpec, compile_artifact
from cua.engine import DiscoveryEngine
from cua.evidence import EvidenceRecorder
from cua.handoff import HandoffController, ScriptedOperator
from cua.models import CapabilityArtifact, utc_now
from cua.planner import ScriptedPlanner
from cua.policy import Policy
from cua.replay import ReplayEngine
from cua.surface import FakeSurface

ENTRY = "http://127.0.0.1:8000/legacy"

BALANCE_SPEC = CapabilitySpec(
    capability_id="lookup-member-savings-balance",
    name="Look up member savings balance",
    description="Returns the current savings balance for a member.",
    app_family="demo-legacy-member-servicing",
)

SUBACCOUNT_SPEC = CapabilitySpec(
    capability_id="open-member-subaccount",
    name="Open a member sub-account",
    description="Opens a new sub-account for a member.",
    app_family="demo-legacy-member-servicing",
)

BALANCE_GOAL = "Look up member 12345 and read their current savings balance"
SUBACCOUNT_GOAL = "Open a new sub-account for member 12345"


async def discover(root: Path, goal: str, spec: CapabilitySpec, **surface_kwargs):
    """Run a real discovery loop against the in-memory surface."""
    engine = DiscoveryEngine(
        surface=FakeSurface(**surface_kwargs),
        planner=ScriptedPlanner(),
        policy=Policy(),
        evidence=EvidenceRecorder(root / "discovery"),
        handoff=HandoffController(operator=ScriptedOperator()),
    )
    return await engine.run(goal, ENTRY, spec)


def approve(artifact: CapabilityArtifact, reviewer: str = "test-reviewer") -> CapabilityArtifact:
    return artifact.model_copy(
        update={
            "approval_state": "approved",
            "approved_by": reviewer,
            "approved_at": utc_now(),
        }
    )


def replay_engine(root: Path, name: str, surface: FakeSurface, **kwargs) -> ReplayEngine:
    return ReplayEngine(
        surface=surface,
        policy=Policy(),
        evidence=EvidenceRecorder(root / name),
        handoff=HandoffController(operator=ScriptedOperator()),
        **kwargs,
    )
