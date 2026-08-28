"""Redaction: what must never reach disk, and what must survive so logs stay useful."""

import tempfile
import unittest
from pathlib import Path

from cua.evidence import EvidenceRecorder
from cua.redaction import redact


class SecretsNeverPersist(unittest.TestCase):
    def test_credentials_are_removed(self) -> None:
        payload = {
            "auth": "Authorization: Bearer sk-ant-abc123verysecretvalue",
            "config": "api_key = ZmFrZS1rZXktdmFsdWU",
            "login": "password: hunter2",
        }
        rendered = str(redact(payload))
        self.assertNotIn("sk-ant-abc123verysecretvalue", rendered)
        self.assertNotIn("ZmFrZS1rZXktdmFsdWU", rendered)
        self.assertNotIn("hunter2", rendered)

    def test_regulated_identifiers_are_removed_from_free_text(self) -> None:
        """The page text the model reads is prose — nothing in it is a named field."""
        text = "Member 12345 SSN 123-45-6789 card 4111 1111 1111 1111 balance $4,281.73"
        result = redact(text)
        for secret in ("12345", "123-45-6789", "4111 1111 1111 1111", "$4,281.73"):
            self.assertNotIn(secret, result)

    def test_nested_structures_are_walked(self) -> None:
        payload = {"outer": [{"inner": {"deep": "SSN 123-45-6789"}}]}
        self.assertNotIn("123-45-6789", str(redact(payload)))


class LogsStayUseful(unittest.TestCase):
    def test_the_same_value_yields_the_same_token(self) -> None:
        """A debugger must be able to follow one member through a trace."""
        a = redact({"member_id": "12345"})["member_id"]
        b = redact({"member_id": "12345"})["member_id"]
        self.assertEqual(a, b)

    def test_different_values_are_distinguishable(self) -> None:
        a = redact({"member_id": "12345"})["member_id"]
        b = redact({"member_id": "99999"})["member_id"]
        self.assertNotEqual(a, b)

    def test_tokens_do_not_contain_the_original(self) -> None:
        self.assertNotIn("12345", redact({"member_id": "12345"})["member_id"])

    def test_non_sensitive_fields_survive_intact(self) -> None:
        """Redaction that eats the debugging information has failed too."""
        result = redact({"step_id": "step-03", "attempt": 2, "action": "click"})
        self.assertEqual(result, {"step_id": "step-03", "attempt": 2, "action": "click"})


class EvidenceBoundary(unittest.TestCase):
    def test_every_recorded_event_passes_through_redaction(self) -> None:
        """The chokepoint: callers cannot forget, because they are not asked to."""
        with tempfile.TemporaryDirectory() as directory:
            recorder = EvidenceRecorder(Path(directory))
            recorder.record("replay_step_completed", {"member_id": "12345"})
            written = recorder.log_path.read_text(encoding="utf-8")
            self.assertNotIn("12345", written)
            self.assertIn("replay_step_completed", written)

    def test_a_new_run_does_not_inherit_the_previous_log(self) -> None:
        """Two runs in one file read as a single coherent story that never happened."""
        with tempfile.TemporaryDirectory() as directory:
            first = EvidenceRecorder(Path(directory))
            first.record("replay_started", {"note": "first run"})
            second = EvidenceRecorder(Path(directory))
            second.record("replay_started", {"note": "second run"})
            events = second.read_events()
            self.assertEqual(len(events), 1)
            self.assertNotEqual(first.run_id, second.run_id)


if __name__ == "__main__":
    unittest.main()
