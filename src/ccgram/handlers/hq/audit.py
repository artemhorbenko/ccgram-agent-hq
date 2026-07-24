"""Agent HQ audit log — structured JSONL record of HQ commands.

Appends one JSON line per HQ action (user, command, target session,
timestamp, result) to ``~/.ccgram/hq_audit.jsonl``. Best-effort: audit
failures are logged and never break the command that triggered them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import structlog

from ...config import config

logger = structlog.get_logger()

AUDIT_FILE_NAME = "hq_audit.jsonl"


def audit_log_path() -> Path:
    """Path of the HQ audit log inside the ccgram config directory."""
    return config.config_dir / AUDIT_FILE_NAME


def log_hq_action(
    *,
    user_id: int,
    command: str,
    target: str = "",
    result: str = "ok",
    detail: str = "",
) -> None:
    """Append one audit record. Never raises."""
    record = {
        "ts": round(time.time(), 3),
        "user_id": user_id,
        "command": command,
        "target": target,
        "result": result,
        "detail": detail,
    }
    try:
        with audit_log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("HQ audit write failed: %s", exc)
