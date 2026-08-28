"""The commands you type. Five verbs, mirroring the capability lifecycle.

    serve      run the synthetic target application
    discover   drive it with a model and compile a draft capability
    approve    review a draft and sign it off
    replay     invoke an approved capability with typed inputs, no model involved
    catalog    show approved capabilities as tools an agent could call

`approve` being a separate command you have to run yourself is the point, not
friction to be smoothed away. Discovery produces a proposal; a person turns it into
something replayable. Collapsing those two into one command would remove the only
human checkpoint in the system.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .catalog import render_catalog
from .compiler import CapabilitySpec, highest_risk
from .engine import DiscoveryEngine, save_artifact
from .evidence import EvidenceRecorder
from .handoff import HandoffController, ScriptedOperator, TerminalOperator
from .models import CapabilityArtifact, ResultStatus, RiskLevel, RunResult, utc_now
from .planner import ClaudePlanner, ScriptedPlanner
from .policy import Policy
from .redaction import redact
from .replay import ReplayEngine
from .surface import PlaywrightSurface

DEFAULT_TARGET = os.environ.get("TARGET_BASE_URL", "http://127.0.0.1:8000/legacy")

# Capability identity is registered, not inferred from the goal text. An agent's
# tool call must not break because somebody rephrased a sentence.
CAPABILITIES: dict[str, CapabilitySpec] = {
    "lookup-member-savings-balance": CapabilitySpec(
        capability_id="lookup-member-savings-balance",
        name="Look up member savings balance",
        description=(
            "Searches for a member by identifier in the servicing console and returns "
            "their current savings balance."
        ),
        app_family="demo-legacy-member-servicing",
    ),
    "open-member-subaccount": CapabilitySpec(
        capability_id="open-member-subaccount",
        name="Open a member sub-account",
        description=(
            "Opens a new sub-account for a member. Contains an irreversible step that "
            "writes to the system of record."
        ),
        app_family="demo-legacy-member-servicing",
    ),
}


def load_artifact(path: str) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _operator(args: argparse.Namespace):
    """A real person by default; a scripted stand-in for automated runs."""
    return ScriptedOperator() if args.non_interactive else TerminalOperator()


async def discover(args: argparse.Namespace) -> int:
    spec = CAPABILITIES[args.capability]
    surface = PlaywrightSurface(headless=args.headless)
    planner = ScriptedPlanner() if args.planner == "scripted" else ClaudePlanner(args.model)

    engine = DiscoveryEngine(
        surface=surface,
        planner=planner,
        policy=Policy(),
        evidence=EvidenceRecorder(args.evidence),
        handoff=HandoffController(operator=_operator(args)),
        max_steps=args.max_steps,
    )

    try:
        result, artifact = await engine.run(args.goal, args.target, spec)
        print(result.model_dump_json(indent=2))
        if artifact is None:
            return 2

        path = save_artifact(artifact, args.artifact)
        print(f"\nSaved draft capability: {path}")
        print(f"Highest risk in this capability: {highest_risk(artifact)}")
        print("Review the file, then run `cua approve` before it can be replayed.")
        return 0
    finally:
        await surface.close()


async def replay(args: argparse.Namespace) -> int:
    artifact = load_artifact(args.artifact)
    surface = PlaywrightSurface(headless=args.headless)

    engine = ReplayEngine(
        surface=surface,
        policy=Policy(),
        evidence=EvidenceRecorder(args.evidence),
        handoff=HandoffController(operator=_operator(args)),
        # Authorisation is granted per invocation, never inherited from approval.
        confirmed_risks={RiskLevel.IRREVERSIBLE} if args.confirm_irreversible else set(),
    )

    try:
        try:
            result = await engine.run(
                artifact, json.loads(args.params), goal=f"replay {artifact.capability_id}"
            )
        except ValueError as exc:
            # A contract violation is the caller's mistake, not a system failure, and
            # it deserves the same structured shape as every other outcome. An agent
            # on the other end of this cannot act on a stack trace.
            result = RunResult(
                status=ResultStatus.HARD_FAILURE,
                capability_id=artifact.capability_id,
                error_code="INVALID_ARGUMENTS",
                message=str(exc),
                evidence_dir=args.evidence,
            )
        print(result.model_dump_json(indent=2))

        # The caller sees what it asked for on stdout; the persisted copy goes
        # through the same redaction boundary as every other piece of evidence.
        (Path(args.evidence) / "result.json").write_text(
            json.dumps(redact(result.model_dump(mode="json")), indent=2), encoding="utf-8"
        )

        # A known business outcome is a successful invocation — the caller got the
        # answer it asked for. Only genuine failures exit non-zero, so a CI job
        # does not treat "no such member" as a broken pipeline.
        return 0 if result.status in {"success", "business_outcome"} else 2
    finally:
        await surface.close()


def approve(args: argparse.Namespace) -> int:
    """Show a reviewer what they are signing off, then record who signed it."""
    artifact = load_artifact(args.artifact)
    risk = highest_risk(artifact)

    print(f"Capability  : {artifact.capability_id} v{artifact.version}")
    print(f"Application : {artifact.app_family} ({artifact.surface_type})")
    print(f"Entry point : {artifact.entry_point}")
    print(f"Steps       : {len(artifact.steps)}")
    print(f"Inputs      : {[spec.name for spec in artifact.contract.inputs]}")
    print(f"Outputs     : {[spec.name for spec in artifact.contract.outputs]}")
    print(f"Checkpoint  : {artifact.checkpoint.description}")
    print(f"Outcomes    : {[rule.code for rule in artifact.business_outcomes]}")
    print(f"Highest risk: {risk}")

    approved = artifact.model_copy(
        update={
            "approval_state": "approved",
            "approved_by": args.reviewer,
            "approved_at": utc_now(),
        }
    )
    save_artifact(approved, args.artifact)
    print(f"\nApproved by {args.reviewer}.")

    if risk is RiskLevel.IRREVERSIBLE:
        print(
            "\nNote: approving this capability does not authorise its irreversible\n"
            "step. Each replay still needs --confirm-irreversible, or it will stop\n"
            "and ask a human."
        )
    return 0


def catalog(args: argparse.Namespace) -> int:
    print(render_catalog(args.directory))
    return 0


def schema(_: argparse.Namespace) -> int:
    """Emit the artifact JSON Schema, for documentation and external validation."""
    print(json.dumps(CapabilityArtifact.model_json_schema(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cua", description="Computer-use capability engine")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_session_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--headless", action="store_true", help="Run without a visible browser.")
        p.add_argument(
            "--non-interactive",
            action="store_true",
            help="Use a scripted operator instead of prompting a person on handoff.",
        )

    d = sub.add_parser("discover", help="Drive the app with a model and compile a capability")
    d.add_argument("--goal", required=True, help="What to accomplish, in plain English.")
    d.add_argument("--capability", required=True, choices=sorted(CAPABILITIES))
    d.add_argument("--target", default=DEFAULT_TARGET)
    d.add_argument("--planner", choices=["claude", "scripted"], default="claude")
    d.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL"))
    d.add_argument("--artifact", required=True, help="Where to write the draft capability.")
    d.add_argument("--evidence", default="evidence/discovery")
    d.add_argument("--max-steps", type=int, default=12)
    add_session_flags(d)
    d.set_defaults(coro=discover)

    r = sub.add_parser("replay", help="Invoke an approved capability, with no model involved")
    r.add_argument("--artifact", required=True)
    r.add_argument("--params", required=True, help='JSON, e.g. {"member_id":"12345"}')
    r.add_argument("--evidence", default="evidence/replay")
    r.add_argument(
        "--confirm-irreversible",
        action="store_true",
        help="Authorise irreversible steps for this invocation only.",
    )
    add_session_flags(r)
    r.set_defaults(coro=replay)

    a = sub.add_parser("approve", help="Review a draft capability and sign it off")
    a.add_argument("--artifact", required=True)
    a.add_argument("--reviewer", required=True, help="Recorded in the capability.")
    a.set_defaults(fn=approve)

    c = sub.add_parser("catalog", help="Show approved capabilities as callable tools")
    c.add_argument("--directory", default="artifacts")
    c.set_defaults(fn=catalog)

    s = sub.add_parser("schema", help="Print the capability artifact JSON Schema")
    s.set_defaults(fn=schema)

    v = sub.add_parser("serve", help="Run the synthetic target application")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.set_defaults(fn=None)

    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "serve":
        try:
            import uvicorn
        except ImportError as exc:
            raise SystemExit(
                "Install project dependencies before running the target application."
            ) from exc
        uvicorn.run("cua.target_app:app", host=args.host, port=args.port, reload=False)
        return

    if getattr(args, "coro", None) is not None:
        raise SystemExit(asyncio.run(args.coro(args)))
    raise SystemExit(args.fn(args))


if __name__ == "__main__":
    main()
