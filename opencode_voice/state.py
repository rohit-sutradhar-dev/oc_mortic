from __future__ import annotations

import time
from dataclasses import dataclass
from math import ceil
from typing import Any


CONTEXT_ESTIMATE_OVERHEAD_TOKENS = 8_000
CHARS_PER_TOKEN = 4
_METADATA_TEXT_KEYS = {
    "id",
    "sessionID",
    "messageID",
    "partID",
    "callID",
    "toolCallID",
    "providerID",
    "modelID",
    "type",
    "role",
    "status",
}


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def session_usage_tokens(session: dict[str, Any]) -> int:
    tokens = session.get("tokens") or {}
    return int(tokens.get("input") or 0) + int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)


def session_context_tokens(session: dict[str, Any]) -> int:
    return session_usage_tokens(session)


def session_title(session: dict[str, Any]) -> str:
    return str(session.get("title") or session.get("id") or "Untitled")


@dataclass(frozen=True)
class ContextEstimate:
    tokens: int
    source: str
    summary_message_id: str | None = None
    measured_message_id: str | None = None
    included_messages: int = 0


def active_context_estimate(messages: list[dict[str, Any]]) -> ContextEstimate:
    summary = latest_completed_summary(messages)
    summary_created = message_created_ms(summary) if summary else None
    included = active_messages(messages, summary)
    measured = latest_measured_assistant(included, after_ms=summary_created)
    if measured:
        info = measured.get("info") or {}
        tokens = prompt_context_tokens(info.get("tokens") or {})
        if tokens > 0:
            return ContextEstimate(
                tokens=tokens,
                source="assistant_input",
                summary_message_id=message_id(summary),
                measured_message_id=message_id(measured),
                included_messages=len(included),
            )

    chars = sum(message_text_chars(message) for message in included)
    estimated_tokens = CONTEXT_ESTIMATE_OVERHEAD_TOKENS + ceil(chars / CHARS_PER_TOKEN)
    return ContextEstimate(
        tokens=estimated_tokens,
        source="content_estimate",
        summary_message_id=message_id(summary),
        included_messages=len(included),
    )


def active_messages(
    messages: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    summary = summary if summary is not None else latest_completed_summary(messages)
    if summary is None:
        return list(messages)
    summary_created = message_created_ms(summary)
    summary_id = message_id(summary)
    return [
        message
        for message in messages
        if message_id(message) == summary_id or message_created_ms(message) > summary_created
    ]


def latest_completed_summary(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    summaries = [
        message
        for message in messages
        if is_completed_assistant_summary(message)
    ]
    if not summaries:
        return None
    return max(summaries, key=message_created_ms)


def latest_measured_assistant(
    messages: list[dict[str, Any]],
    after_ms: int | None = None,
) -> dict[str, Any] | None:
    candidates = []
    for message in messages:
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant" or info.get("summary") is True:
            continue
        if after_ms is not None and message_created_ms(message) <= after_ms:
            continue
        if prompt_context_tokens(info.get("tokens") or {}) <= 0:
            continue
        candidates.append(message)
    if not candidates:
        return None
    return max(candidates, key=message_created_ms)


def is_completed_assistant_summary(message: dict[str, Any]) -> bool:
    info = message.get("info") if isinstance(message, dict) else None
    if not isinstance(info, dict) or info.get("role") != "assistant" or info.get("summary") is not True:
        return False
    time_info = info.get("time") or {}
    return "completed" in time_info or bool(info.get("finish")) or bool(info.get("error"))


def message_id(message: dict[str, Any] | None) -> str | None:
    if not message:
        return None
    info = message.get("info") if isinstance(message, dict) else None
    if not isinstance(info, dict):
        return None
    value = info.get("id")
    return str(value) if value else None


def message_created_ms(message: dict[str, Any] | None) -> int:
    if not message:
        return 0
    info = message.get("info") if isinstance(message, dict) else None
    time_info = info.get("time") if isinstance(info, dict) else None
    if not isinstance(time_info, dict):
        return 0
    try:
        return int(time_info.get("created") or 0)
    except (TypeError, ValueError):
        return 0


def message_text_chars(message: dict[str, Any]) -> int:
    return textual_chars(message.get("parts") or [])


def prompt_context_tokens(tokens: dict[str, Any]) -> int:
    cache = tokens.get("cache") or {}
    return int(tokens.get("input") or 0) + int(cache.get("read") or 0)


def textual_chars(value: Any, key: str | None = None) -> int:
    if isinstance(value, str):
        return 0 if key in _METADATA_TEXT_KEYS else len(value)
    if isinstance(value, list):
        return sum(textual_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(textual_chars(item, key=str(item_key)) for item_key, item in value.items())
    return 0


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


class OpenCodeEventTurnTracker:
    def __init__(self, session_id: str, existing_message_ids: set[str]) -> None:
        self.session_id = session_id
        self.existing_message_ids = existing_message_ids
        self.message_roles: dict[str, str] = {}
        self.active_message_id: str | None = None
        self.part_text: dict[str, str] = {}
        self.part_order: list[str] = []
        self.completed = False
        self.stale_idles = 0
        self.error: Any | None = None

    @property
    def full_text(self) -> str:
        return "".join(self.part_text.get(part_id, "") for part_id in self.part_order)

    def update(self, event: dict[str, Any]) -> AssistantUpdate:
        event_type = str(event.get("type") or "")
        properties = event_properties(event)
        if event_session_id(event) != self.session_id:
            return self._empty()

        deltas: list[str] = []
        if event_type == "message.updated":
            self._handle_message_updated(properties)
        elif event_type == "message.part.delta":
            delta = self._handle_part_delta(properties)
            if delta:
                deltas.append(delta)
        elif event_type == "message.part.updated":
            delta = self._handle_part_updated(properties)
            if delta:
                deltas.append(delta)
        elif event_type == "session.idle":
            if self.active_message_id:
                self.completed = True
            else:
                # An idle left over from the previous (usually aborted) turn
                # can arrive on this turn's fresh subscription before our
                # prompt produces anything; honoring it would report a
                # completed-empty turn and force the poll fallback.
                self.stale_idles += 1
        elif event_type == "session.status":
            status = properties.get("status") or {}
            if isinstance(status, dict) and status.get("type") == "idle" and self.active_message_id:
                self.completed = True

        return AssistantUpdate(
            deltas=deltas,
            completed=self.completed,
            full_text=self.full_text,
            message_id=self.active_message_id,
            error=self.error,
        )

    def _handle_message_updated(self, properties: dict[str, Any]) -> None:
        info = properties.get("info")
        if not isinstance(info, dict):
            return
        message_id_value = info.get("id")
        role = str(info.get("role") or "")
        if not message_id_value or not role:
            return
        message_id = str(message_id_value)
        self.message_roles[message_id] = role
        if role != "assistant" or message_id in self.existing_message_ids:
            return
        self.active_message_id = message_id
        self.error = info.get("error") or self.error
        time_info = info.get("time") or {}
        if "completed" in time_info or info.get("error"):
            self.completed = True

    def _handle_part_delta(self, properties: dict[str, Any]) -> str:
        if properties.get("field") != "text":
            return ""
        message_id = str(properties.get("messageID") or "")
        part_id = str(properties.get("partID") or "")
        delta = str(properties.get("delta") or "")
        if not part_id or not delta or not self._is_active_assistant_message(message_id):
            return ""
        self._remember_part(part_id)
        self.part_text[part_id] = self.part_text.get(part_id, "") + delta
        return delta

    def _handle_part_updated(self, properties: dict[str, Any]) -> str:
        part = properties.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            return ""
        message_id = str(part.get("messageID") or "")
        part_id = str(part.get("id") or "")
        text = str(part.get("text") or "")
        if not part_id or not self._is_active_assistant_message(message_id):
            return ""
        old = self.part_text.get(part_id, "")
        self._remember_part(part_id)
        # 1.17 may stream delta-only updates (`properties.delta`) without the
        # full part text; fall back to diffing full-text snapshots otherwise.
        delta = properties.get("delta")
        if not text and isinstance(delta, str) and delta:
            self.part_text[part_id] = old + delta
            return delta
        self.part_text[part_id] = text
        if text.startswith(old):
            return text[len(old):]
        return text if not old else ""

    def _is_active_assistant_message(self, message_id: str) -> bool:
        if not message_id or message_id in self.existing_message_ids:
            return False
        if self.message_roles.get(message_id) != "assistant":
            return False
        self.active_message_id = message_id
        return True

    def _remember_part(self, part_id: str) -> None:
        if part_id not in self.part_order:
            self.part_order.append(part_id)

    def _empty(self) -> AssistantUpdate:
        return AssistantUpdate(
            deltas=[],
            completed=self.completed,
            full_text=self.full_text,
            message_id=self.active_message_id,
            error=self.error,
        )


def event_properties(event: dict[str, Any]) -> dict[str, Any]:
    properties = event.get("properties")
    if isinstance(properties, dict):
        return properties
    data = event.get("data")
    if isinstance(data, dict):
        return data
    return {}


def event_session_id(event: dict[str, Any]) -> str:
    """Resolve the session id an OpenCode SSE event belongs to.

    OpenCode 1.17 nests it per event family: `message.updated` carries it in
    `properties.info.sessionID`, `message.part.updated` in
    `properties.part.sessionID`, and session-level events keep it at
    `properties.sessionID`.
    """
    properties = event_properties(event)
    direct = properties.get("sessionID")
    if isinstance(direct, str) and direct:
        return direct
    for container_key in ("info", "part"):
        container = properties.get(container_key)
        if isinstance(container, dict):
            nested = container.get("sessionID")
            if isinstance(nested, str) and nested:
                return nested
    return ""


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
