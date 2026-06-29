from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def session_context_tokens(session: dict[str, Any]) -> int:
    tokens = session.get("tokens") or {}
    return int(tokens.get("input") or 0) + int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)


def session_title(session: dict[str, Any]) -> str:
    return str(session.get("title") or session.get("id") or "Untitled")


@dataclass
class AssistantText:
    message_id: str
    text: str
    completed: bool
    error: Any | None = None


@dataclass
class AssistantUpdate:
    deltas: list[str]
    completed: bool
    full_text: str
    message_id: str | None
    error: Any | None = None


def assistant_texts(messages: list[dict[str, Any]]) -> list[AssistantText]:
    results: list[AssistantText] = []
    for message in messages:
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        message_id = str(info.get("id") or "")
        if not message_id:
            continue
        text = "".join(
            str(part.get("text") or "")
            for part in message.get("parts", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        time_info = info.get("time") or {}
        completed = "completed" in time_info or bool(info.get("error"))
        results.append(AssistantText(message_id=message_id, text=text, completed=completed, error=info.get("error")))
    return results


class AssistantTextTracker:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.offsets = {item.message_id: len(item.text) for item in assistant_texts(messages)}
        self.active_message_id: str | None = None

    def update(self, messages: list[dict[str, Any]]) -> AssistantUpdate:
        deltas: list[str] = []
        full_text = ""
        completed = False
        error: Any | None = None
        for item in assistant_texts(messages):
            is_new_message = item.message_id not in self.offsets
            old_len = self.offsets.get(item.message_id, 0)
            if is_new_message:
                self.active_message_id = item.message_id
            if len(item.text) > old_len:
                deltas.append(item.text[old_len:])
                self.active_message_id = item.message_id
            self.offsets[item.message_id] = len(item.text)
            if self.active_message_id == item.message_id:
                full_text = item.text
                completed = item.completed
                error = item.error
        return AssistantUpdate(
            deltas=deltas,
            completed=completed,
            full_text=full_text,
            message_id=self.active_message_id,
            error=error,
        )
