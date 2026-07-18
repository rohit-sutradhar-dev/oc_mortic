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
    if info.get("error") or str(info.get("finish") or "").lower() == "error":
        return False
    time_info = info.get("time") or {}
    return "completed" in time_info or bool(info.get("finish"))


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
