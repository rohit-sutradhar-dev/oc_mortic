from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable

from opencode_voice.config import ModelRef
from opencode_voice.opencode_client import OpenCodeClient
from opencode_voice.response_contract import RESPONSE_SCHEMA, ResponseEnvelope, StructuredTurnTracker, ToolActivity


ToolCallback = Callable[[ToolActivity], Awaitable[None]]


@dataclass(frozen=True)
class StructuredTurnResult:
    raw: Any | None
    response: ResponseEnvelope | None
    error: Any | None
    tool_activity: tuple[ToolActivity, ...]
    first_activity_ms: int | None
    final_ms: int
    stream_source: str


@lru_cache(maxsize=1)
def load_structured_voice_prompt() -> str:
    return Path(__file__).with_name("response_eval_agent.md").read_text(encoding="utf-8")


async def run_structured_turn(
    client: OpenCodeClient,
    *,
    session_id: str,
    directory: str | None,
    prompt: str,
    model: ModelRef,
    agent: str,
    max_turn_sec: float,
    poll_after_sec: float = 3.0,
    final_grace_sec: float = 1.0,
    tools: dict[str, bool] | None = None,
    on_tool_activity: ToolCallback | None = None,
) -> StructuredTurnResult:
    before_messages = await client.messages_for_tracking(session_id)
    tracker = StructuredTurnTracker(session_id, before_messages)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    ready = asyncio.Event()
    reader = asyncio.create_task(
        _read_events(client, directory, queue, ready),
        name=f"mortic-structured-events-{session_id}",
    )
    started = time.perf_counter()
    first_activity_ms: int | None = None
    last_activity_count = 0
    emitted_tools = 0
    structured_seen_at: float | None = None
    event_healthy = True
    stream_source = "event"
    next_poll = started + poll_after_sec
    deadline = started + max_turn_sec

    async def observe_tools() -> None:
        nonlocal emitted_tools
        while emitted_tools < len(tracker.state.tool_activity):
            activity = tracker.state.tool_activity[emitted_tools]
            emitted_tools += 1
            if on_tool_activity is not None:
                await on_tool_activity(activity)

    try:
        try:
            await asyncio.wait_for(ready.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            event_healthy = False
            stream_source = "poll"
        await client.prompt_async(
            session_id,
            prompt,
            model,
            agent,
            output_format={
                "type": "json_schema",
                "schema": {key: value for key, value in RESPONSE_SCHEMA.items() if key != "$schema"},
            },
            system=load_structured_voice_prompt(),
            tools=tools,
        )

        while time.perf_counter() < deadline:
            if tracker.state.activity_count > last_activity_count:
                last_activity_count = tracker.state.activity_count
                if first_activity_ms is None:
                    first_activity_ms = _elapsed_ms(started)
            await observe_tools()
            if tracker.state.raw_structured is not None:
                structured_seen_at = structured_seen_at or time.perf_counter()
                if tracker.state.idle_seen or time.perf_counter() - structured_seen_at >= final_grace_sec:
                    return _result(tracker, started, first_activity_ms, stream_source)
            if tracker.state.assistant_error is not None and tracker.state.raw_structured is None:
                tracker.update_messages(await client.messages_for_tracking(session_id))
                await observe_tools()
                if tracker.state.raw_structured is None:
                    return _result(tracker, started, first_activity_ms, stream_source)
            if tracker.state.idle_seen and tracker.state.output_seen:
                tracker.update_messages(await client.messages_for_tracking(session_id))
                await observe_tools()
                if tracker.state.raw_structured is not None:
                    stream_source = "hybrid" if event_healthy else "poll"
                    continue
                return _result(
                    tracker,
                    started,
                    first_activity_ms,
                    stream_source,
                    fallback_error="structured_output_missing",
                )

            now = time.perf_counter()
            wait_until = min(deadline, next_poll, (structured_seen_at or deadline) + final_grace_sec)
            try:
                event = await asyncio.wait_for(queue.get(), timeout=max(0.01, wait_until - now))
            except asyncio.TimeoutError:
                event = None
            if event is not None:
                if event.get("type") == "_stream_error":
                    event_healthy = False
                    stream_source = "poll"
                else:
                    tracker.update_event(event)
            if time.perf_counter() >= next_poll:
                previous = tracker.state.raw_structured
                tracker.update_messages(await client.messages_for_tracking(session_id))
                if previous is None and tracker.state.raw_structured is not None:
                    stream_source = "hybrid" if event_healthy else "poll"
                next_poll = time.perf_counter() + 0.5

        return _result(
            tracker,
            started,
            first_activity_ms,
            stream_source,
            fallback_error="turn_timeout",
        )
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def _read_events(
    client: OpenCodeClient,
    directory: str | None,
    queue: asyncio.Queue[dict[str, Any]],
    ready: asyncio.Event,
) -> None:
    try:
        async for event in client.events(on_open=ready.set, directory=directory):
            await queue.put(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - polling remains authoritative.
        ready.set()
        await queue.put({"type": "_stream_error", "reason": type(exc).__name__})


def _result(
    tracker: StructuredTurnTracker,
    started: float,
    first_activity_ms: int | None,
    stream_source: str,
    fallback_error: str | None = None,
) -> StructuredTurnResult:
    return StructuredTurnResult(
        raw=tracker.state.raw_structured,
        response=tracker.state.response,
        error=tracker.state.assistant_error or fallback_error,
        tool_activity=tuple(tracker.state.tool_activity),
        first_activity_ms=first_activity_ms,
        final_ms=_elapsed_ms(started),
        stream_source=stream_source,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
