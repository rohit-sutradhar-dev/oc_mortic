from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opencode_voice.config import redact_secrets

CONTENT_SUMMARY_FIELDS = {"audio", "delta", "prompt", "raw", "text", "transcript"}
EVENT_LOG_ENV = "MORTIC_HELPER_EVENT_LOG"


def helper_event_log_path() -> str | None:
    return os.environ.get(EVENT_LOG_ENV)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_log_fields(value: Any, key: str | None = None) -> Any:
    if key and key.replace("-", "_").lower() in CONTENT_SUMMARY_FIELDS:
        return summarize_content(value)
    if isinstance(value, dict):
        return {item_key: safe_log_fields(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [safe_log_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(safe_log_fields(item) for item in value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes redacted>"
    return value


def summarize_content(value: Any) -> dict[str, int | str]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"kind": "bytes", "bytes": len(value)}
    if isinstance(value, str):
        return {"kind": "text", "chars": len(value), "lines": value.count("\n") + (1 if value else 0)}
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        encoded = str(value)
    return {"kind": type(value).__name__, "chars": len(encoded)}


class RunLogger:
    def __init__(self, root: str | Path = "runs/voice") -> None:
        self.run_dir = Path(root) / utc_timestamp()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "events.jsonl"

    def write(self, event: str, **fields: Any) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(redact_secrets(safe_log_fields(record)), ensure_ascii=False, default=str) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        mirror_path = helper_event_log_path()
        if mirror_path:
            try:
                with Path(mirror_path).open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass

    def state_transition(self, from_state: str, to_state: str, **fields: Any) -> None:
        self.write("state.transition", from_state=from_state, to_state=to_state, **fields)
