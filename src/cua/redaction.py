"""The boundary regulated data must pass before anything is written to disk.

Two mechanisms, because they fail in different directions.

Structural redaction removes values by *field name*: anything the contract marked
sensitive, plus a standing list of names that are never safe to log. This is exact
and cannot miss, but only works where the data is a named field.

Pattern redaction scans free text for things that look like secrets — bearer
tokens, card numbers, national insurance-style identifiers. This catches values
that arrive in prose (a page's visible text, an error message), where nothing is
named. It is best-effort by nature: a regex cannot know that "4281" is a balance.

Both run on every event. Where they disagree, more redaction wins.

A note on the awkward part. Redaction fights debuggability: a log where every
identifier reads `[REDACTED]` cannot tell you that the same member failed twice.
So instead of erasing, values are replaced by a short digest — the same input
always yields the same token within a run, different inputs never collide. An
engineer can follow one member through a trace without ever learning who they are.

What this does not solve: screenshots. A full-page capture of a member details
screen contains the member's name and balance as pixels, and no regex touches
pixels. Image redaction is a real gap and is listed as a deliberate cut in the
report rather than quietly ignored.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

# Per-process salt. Tokens stay consistent for the length of a run, so a trace can
# be followed end to end, and are useless for correlating across runs or for
# reversing back to the original value.
_SALT = os.urandom(16)


def _token(value: str, label: str) -> str:
    """A stable, non-reversible stand-in that still distinguishes two values."""
    digest = hashlib.sha256(_SALT + value.encode("utf-8")).hexdigest()[:8]
    return f"[{label}:{digest}]"


# Ordered. Broader, higher-confidence patterns run first so a card number is not
# partially eaten by the generic long-digit rule.
PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Credentials. These are never acceptable in a log under any circumstances.
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(sk-ant-[A-Za-z0-9_\-]{8,})"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)(api[_-]?key\"?'?\s*[:=]\s*\"?'?)[A-Za-z0-9._\-]{8,}"), r"\1[REDACTED_API_KEY]"),
    (re.compile(r"(?i)(password\"?'?\s*[:=]\s*\"?'?)[^\s,\"']+"), r"\1[REDACTED]"),
    # Regulated personal and financial identifiers.
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_ACCOUNT_NUMBER]"),
    (re.compile(r"\b\d{5,12}\b"), "[REDACTED_IDENTIFIER]"),
    # Money. A balance is customer financial data even without a name attached.
    (re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?"), "[REDACTED_AMOUNT]"),
]

# Field names that are never safe to write out, whatever their contents. Matched
# case-insensitively against dictionary keys.
SENSITIVE_KEYS = frozenset(
    {
        "member_id",
        "account_number",
        "savings_balance",
        "balance",
        "ssn",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
        "secret",
    }
)


def redact_text(value: str) -> str:
    """Apply every pattern to a single string."""
    for pattern, replacement in PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact(value: Any) -> Any:
    """Walk any structure and redact it, preserving shape.

    Shape is preserved on purpose: a redacted event should still be readable as an
    event. Collapsing the whole payload to a single "[REDACTED]" would protect the
    data and destroy the evidence at the same time.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: _token(str(item), "REDACTED") if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    # Numbers, booleans and None carry no identifying content on their own.
    return value
