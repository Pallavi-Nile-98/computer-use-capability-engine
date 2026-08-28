"""Approved capabilities, presented as tools an agent can discover and call.

This is why the artifact carries a typed contract rather than just a list of steps.
An agent-facing product should not need to know that a capability happens to be a
recorded UI flow — it should see a named tool with typed arguments and a typed
return, and invoke it. Whether the thing on the other side is a REST call or a
browser clicking through a 1990s servicing console is an implementation detail, and
keeping it one is the entire point of the project.

Only `approved` capabilities appear. That is what makes the approval gate mean
something: a draft is not merely marked as unready, it is not callable, because it
never enters the catalog an agent reads.

The description handed to the agent includes the possible business outcomes. A tool
that can answer "no such member" needs to say so up front, or the calling agent
will treat that answer as a malfunction and retry — which is the same failure this
whole system is built to avoid, just moved one layer up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import CapabilityArtifact

# The contract's types are already JSON Schema types; the mapping is explicit so a
# change to either vocabulary fails here rather than producing an odd tool schema.
_JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
}


def load_artifacts(directory: str | Path) -> list[CapabilityArtifact]:
    """Read every capability in a directory, skipping anything unreadable.

    One malformed file should not hide the rest of the catalog, but it should be
    visible rather than silent — an agent missing a capability it expects is a
    confusing failure to debug from the other end.
    """
    artifacts: list[CapabilityArtifact] = []
    for path in sorted(Path(directory).glob("*.json")):
        if path.name.endswith(".schema.json"):
            continue
        try:
            artifacts.append(
                CapabilityArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            )
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"Skipping {path.name}: {exc}")
    return artifacts


def tool_schema(artifact: CapabilityArtifact) -> dict[str, Any]:
    """Render one capability as a function-calling tool definition."""
    outcomes = ", ".join(rule.code for rule in artifact.business_outcomes) or "none declared"

    return {
        # Hyphens are not valid in most function-calling tool names.
        "name": artifact.capability_id.replace("-", "_"),
        "description": (
            f"{artifact.description} Operates {artifact.app_family} "
            f"({artifact.surface_type}). Returns either the declared outputs, or one "
            f"of these known business outcomes: {outcomes}. A business outcome is a "
            f"legitimate answer, not an error — do not retry it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                spec.name: {
                    "type": _JSON_TYPES[spec.type],
                    "description": (
                        spec.description
                        + (" Treated as sensitive and never persisted." if spec.sensitive else "")
                    ),
                    **({"pattern": spec.pattern} if spec.pattern else {}),
                }
                for spec in artifact.contract.inputs
            },
            "required": [spec.name for spec in artifact.contract.inputs if spec.required],
            "additionalProperties": False,
        },
        # Not part of the tool-call standard, but useful to a caller deciding
        # whether to trust a capability, and to an operator auditing what is live.
        "returns": {
            spec.name: {"type": _JSON_TYPES[spec.type], "description": spec.description}
            for spec in artifact.contract.outputs
        },
        "capability_version": artifact.version,
        "approved_by": artifact.approved_by,
    }


def build_catalog(directory: str | Path) -> list[dict[str, Any]]:
    """Every approved capability in a directory, as callable tool definitions."""
    return [
        tool_schema(artifact)
        for artifact in load_artifacts(directory)
        if artifact.approval_state == "approved"
    ]


def render_catalog(directory: str | Path) -> str:
    return json.dumps(build_catalog(directory), indent=2)
