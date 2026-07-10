from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opencode_voice.config import redact_secrets
from opencode_voice.telemetry import RunClock

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
    def __init__(self, root: str | Path = "runs/voice", *, clock: RunClock | None = None) -> None:
        self.run_dir = Path(root) / utc_timestamp()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "events.jsonl"
        self.clock = clock or RunClock()
        # Line-buffered handles held open for the logger's lifetime: write()
        # runs per event on the voice hot path, so no per-record open/close,
        # and the mirror env cannot change mid-process.
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        self._mirror = None
        mirror_path = helper_event_log_path()
        if mirror_path:
            try:
                self._mirror = Path(mirror_path).open("a", encoding="utf-8", buffering=1)
            except OSError:
                self._mirror = None

    def write(self, event: str, **fields: Any) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
            # A process-local monotonic timestamp makes phase durations robust
            # to wall-clock adjustment and comparable within one run.
            "run_elapsed_ms": self.clock.elapsed_ms(),
        }
        line = json.dumps(redact_secrets(safe_log_fields(record)), ensure_ascii=False, default=str) + "\n"
        self._handle.write(line)
        if self._mirror is not None:
            try:
                self._mirror.write(line)
            except OSError:
                self._mirror = None

    def state_transition(self, from_state: str, to_state: str, **fields: Any) -> None:
        self.write("state.transition", from_state=from_state, to_state=to_state, **fields)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        mirror, self._mirror = self._mirror, None
        for stream in (handle, mirror):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def __del__(self) -> None:
        # Tests and failed startup paths may not reach FastAPI shutdown. Keep
        # file ownership explicit so GC never reports an unclosed hot-path log.
        try:
            self.close()
        except Exception:
            pass
