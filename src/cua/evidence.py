"""Append-only, redacted record of what a run did and why.

The brief asks for enough evidence to understand and debug a run, plus something
richer on failure. Three choices behind this file:

Append-only JSONL, one JSON object per line. A run that crashes still leaves every
event up to the crash on disk, which is exactly when the log matters most. A single
JSON document would have to be closed to be valid, so a crash would produce an
unparseable file.

Redaction happens *here*, not at each call site. Every caller writes what it knows
and this layer decides what may be persisted. If redaction were the caller's job,
it would be forgotten in the one place that mattered.

Screenshots are written next to the log. A structured log tells you the click was
refused; a screenshot tells you the page had a modal over it. The brief asks for
"at least one richer signal on failure" and this is it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .redaction import redact


class EvidenceRecorder:
    """Writes one run's events and screenshots into a directory."""

    def __init__(self, directory: str | Path, *, reset: bool = True):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log_path = self.directory / "events.jsonl"

        # Correlates the log with any screenshots and with the returned result,
        # so a support ticket can reference one identifier.
        self.run_id = uuid4().hex[:12]

        # A directory holding events from two different runs is worse than no
        # evidence, because it reads as one coherent story that never happened.
        if reset and self.log_path.exists():
            self.log_path.unlink()

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append one redacted event.

        `sort_keys` makes the output diffable: two runs of the same capability
        produce byte-comparable lines apart from timestamps, so a reviewer can see
        what actually changed between them.
        """
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event_type": event_type,
            "payload": redact(payload),
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str, sort_keys=True) + "\n")

    def path_for(self, filename: str) -> Path:
        """Where a screenshot or snapshot for this run should be written."""
        return self.directory / filename

    def read_events(self) -> list[dict[str, Any]]:
        """Read the log back. Used by tests to assert on what was recorded."""
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
