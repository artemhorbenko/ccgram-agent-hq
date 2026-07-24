"""Tests for the Agent HQ audit log."""

import json
from unittest.mock import patch

import pytest

from ccgram.handlers.hq.audit import log_hq_action


@pytest.fixture
def audit_file(tmp_path):
    path = tmp_path / "hq_audit.jsonl"
    with patch("ccgram.handlers.hq.audit.audit_log_path", return_value=path):
        yield path


class TestAuditLog:
    def test_appends_structured_record(self, audit_file) -> None:
        log_hq_action(
            user_id=100,
            command="tell",
            target="@1",
            result="ok",
            detail="Sent to wallet",
        )
        log_hq_action(user_id=100, command="agents")

        lines = audit_file.read_text().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["user_id"] == 100
        assert first["command"] == "tell"
        assert first["target"] == "@1"
        assert first["result"] == "ok"
        assert first["ts"] > 0
        second = json.loads(lines[1])
        assert second["command"] == "agents"
        assert second["result"] == "ok"

    def test_write_failure_never_raises(self, tmp_path) -> None:
        missing_dir = tmp_path / "nope" / "hq_audit.jsonl"
        with patch("ccgram.handlers.hq.audit.audit_log_path", return_value=missing_dir):
            log_hq_action(user_id=1, command="agents")  # must not raise
