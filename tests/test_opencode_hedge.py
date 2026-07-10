from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import time
import unittest
from typing import Any

from opencode_voice.state import HybridOpenCodeTurnTracker
from tests.fakes import FakeOpenCodeClient
from tests.test_sidepod_lane import lane_connection


SESSION_ID = "ses_hedge"
MESSAGE_ID = "msg_reply"
PART_ID = "prt_reply"


def assistant_message(text: str, *, completed: bool) -> dict[str, Any]:
    time_info = {"created": 1}
    if completed:
        time_info["completed"] = 2
    return {
        "info": {
            "id": MESSAGE_ID,
            "role": "assistant",
            "time": time_info,
        },
        "parts": [{"id": PART_ID, "type": "text", "text": text}],
    }


def assistant_role_event() -> dict[str, Any]:
    return {
        "type": "message.updated",
        "properties": {
            "info": {
                "id": MESSAGE_ID,
                "role": "assistant",
                "sessionID": SESSION_ID,
            }
        },
    }


def assistant_text_event(text: str) -> dict[str, Any]:
    return {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": PART_ID,
                "sessionID": SESSION_ID,
                "messageID": MESSAGE_ID,
                "type": "text",
                "text": text,
            }
        },
    }


class HybridOpenCodeTurnTrackerTests(unittest.TestCase):
    def test_event_then_poll_of_same_message_emits_text_once(self) -> None:
        tracker = HybridOpenCodeTurnTracker(session_id=SESSION_ID, before_messages=[])
        tracker.update_event(assistant_role_event())

        event_update = tracker.update_event(assistant_text_event("Hello from the event stream."))
        poll_update = tracker.update_messages(
            [assistant_message("Hello from the event stream.", completed=True)]
        )

        self.assertEqual(event_update.deltas, ["Hello from the event stream."])
        self.assertEqual(poll_update.deltas, [])
        self.assertEqual(poll_update.full_text, "Hello from the event stream.")
        self.assertTrue(poll_update.completed)
        self.assertEqual(poll_update.message_id, MESSAGE_ID)

    def test_poll_then_event_emits_only_the_unseen_suffix(self) -> None:
        tracker = HybridOpenCodeTurnTracker(session_id=SESSION_ID, before_messages=[])

        poll_update = tracker.update_messages([assistant_message("Hello", completed=False)])
        tracker.update_event(assistant_role_event())
        event_update = tracker.update_event(assistant_text_event("Hello, world."))
        duplicate_event = tracker.update_event(assistant_text_event("Hello, world."))

        self.assertEqual(poll_update.deltas, ["Hello"])
        self.assertEqual(event_update.deltas, [", world."])
        self.assertEqual(event_update.full_text, "Hello, world.")
        self.assertEqual(duplicate_event.deltas, [])


class SilentLiveEventClient(FakeOpenCodeClient):
    """An open SSE reader that never receives a model event."""

    def __init__(self) -> None:
        super().__init__("http://opencode.test")
        self.reply = "Polling found this while the event reader stayed open."
        self._messages: list[dict[str, Any]] = []
        self.stream_alive = False
        self.stream_cancelled = False
        self.poll_saw_live_reader: list[bool] = []
        self.poll_calls = 0
        self.prompt_calls = 0

    def events(self, on_open: Any = None, directory: str | None = None) -> Any:
        return self._silent_events(on_open)

    async def _silent_events(self, on_open: Any) -> Any:
        self.stream_alive = True
        if on_open:
            on_open()
        try:
            await asyncio.Event().wait()
            if False:  # pragma: no cover - preserves the async-generator shape.
                yield {}
        finally:
            self.stream_alive = False
            self.stream_cancelled = True

    async def prompt_async(self, session_id: str, text: str, model: Any, agent: str) -> dict[str, bool]:
        self.prompt_calls += 1
        self._messages = [assistant_message(self.reply, completed=True)]
        return {"ok": True}

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        self.poll_calls += 1
        self.poll_saw_live_reader.append(self.stream_alive)
        return list(self._messages)


class StalledPollEventClient(FakeOpenCodeClient):
    """Polling hangs while the still-live SSE stream later produces text."""

    def __init__(self) -> None:
        super().__init__("http://opencode.test")
        self.events_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.poll_started = asyncio.Event()
        self.publish_task: asyncio.Task[None] | None = None

    def events(self, on_open: Any = None, directory: str | None = None) -> Any:
        return self._events(on_open)

    async def _events(self, on_open: Any) -> Any:
        if on_open:
            on_open()
        while True:
            yield await self.events_queue.get()

    async def prompt_async(self, session_id: str, text: str, model: Any, agent: str) -> dict[str, bool]:
        async def publish_after_hedge() -> None:
            await asyncio.sleep(3.2)
            await self.events_queue.put(assistant_role_event())
            await self.events_queue.put(assistant_text_event("SSE stayed responsive."))
            await self.events_queue.put(
                {"type": "session.idle", "properties": {"sessionID": SESSION_ID}}
            )

        self.publish_task = asyncio.create_task(publish_after_hedge())
        return {"ok": True}

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        self.poll_started.set()
        await asyncio.Event().wait()
        return []


class NoResponseEventClient(SilentLiveEventClient):
    async def prompt_async(self, session_id: str, text: str, model: Any, agent: str) -> dict[str, bool]:
        self.prompt_calls += 1
        return {"ok": True}


class OpenCodePollingHedgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_first_text_has_a_bounded_visible_timeout(self) -> None:
        client = NoResponseEventClient()

        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _ = lane_connection(tmp, client=client)  # type: ignore[arg-type]
            connection.config = dataclasses.replace(
                connection.config,
                first_text_timeout_sec=0.05,
            )
            connection.active_turn_id = 1
            connection.voice_lane_id = "lane_no_response"

            await asyncio.wait_for(
                connection.run_event_text_turn(
                    session_id=SESSION_ID,
                    text="Bound the silent turn.",
                    before_messages=[],
                    turn_id=1,
                    started=time.perf_counter(),
                ),
                timeout=1,
            )
            records = [
                json.loads(line)
                for line in connection.logger.path.read_text(encoding="utf-8").splitlines()
            ]
            connection.logger.close()

        issues = [message for message in websocket.sent if message.get("type") == "voice_bridge_issue"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["diagnosticCode"], "turn_timeout")
        self.assertIsNone(connection.active_turn_id)
        timeout_record = next(record for record in records if record.get("event") == "turn.timeout")
        self.assertEqual(timeout_record["reason"], "first_text_timeout")

    async def test_stalled_poll_never_blocks_later_sse_text(self) -> None:
        client = StalledPollEventClient()
        spoken: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _ = lane_connection(tmp, client=client)  # type: ignore[arg-type]
            connection.config = dataclasses.replace(
                connection.config,
                event_completion_grace_sec=0.05,
            )
            connection.active_turn_id = 1
            connection.voice_lane_id = "lane_stalled_poll"
            connection.fork_directory = "/project/stalled-poll"

            async def record_speech(text: str, turn_id: int) -> None:
                spoken.append(text)

            async def finish_speech(_turn_id: int) -> None:
                return None

            async def no_compaction(*, reason: str, run_in_background: bool) -> None:
                return None

            connection.speak = record_speech  # type: ignore[method-assign]
            connection.finish_speaking_turn = finish_speech  # type: ignore[method-assign]
            connection.maybe_start_compaction = no_compaction  # type: ignore[method-assign]

            await asyncio.wait_for(
                connection.run_event_text_turn(
                    session_id=SESSION_ID,
                    text="Do not let polling block events.",
                    before_messages=[],
                    turn_id=1,
                    started=time.perf_counter(),
                ),
                timeout=8,
            )
            records = [
                json.loads(line)
                for line in connection.logger.path.read_text(encoding="utf-8").splitlines()
            ]
            connection.logger.close()

        deltas = [message["delta"] for message in websocket.sent if message.get("type") == "assistant.delta"]
        self.assertTrue(client.poll_started.is_set())
        self.assertEqual("".join(deltas), "SSE stayed responsive.")
        self.assertEqual("".join(spoken), "SSE stayed responsive.")
        self.assertEqual([message["type"] for message in websocket.sent].count("complete"), 1)
        self.assertTrue(
            any(record.get("event") == "opencode.stream.poll_hedge.timeout" for record in records)
        )

    async def test_silent_live_sse_is_hedged_by_polling_without_reader_restart(self) -> None:
        client = SilentLiveEventClient()
        spoken: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _ = lane_connection(tmp, client=client)  # type: ignore[arg-type]
            connection.active_turn_id = 1
            connection.voice_lane_id = "lane_hedge"
            connection.fork_directory = "/project/hedge"

            async def record_speech(text: str, turn_id: int) -> None:
                spoken.append(text)

            async def finish_speech(_turn_id: int) -> None:
                return None

            async def no_compaction(*, reason: str, run_in_background: bool) -> None:
                return None

            connection.speak = record_speech  # type: ignore[method-assign]
            connection.finish_speaking_turn = finish_speech  # type: ignore[method-assign]
            connection.maybe_start_compaction = no_compaction  # type: ignore[method-assign]

            await asyncio.wait_for(
                connection.run_event_text_turn(
                    session_id=SESSION_ID,
                    text="Use the hedge.",
                    before_messages=[],
                    turn_id=1,
                    started=time.perf_counter(),
                ),
                timeout=8,
            )

            records = [json.loads(line) for line in connection.logger.path.read_text(encoding="utf-8").splitlines()]
            connection.logger.close()

        deltas = [message["delta"] for message in websocket.sent if message.get("type") == "assistant.delta"]
        complete = next(message for message in websocket.sent if message.get("type") == "complete")
        hedge_starts = [record for record in records if record.get("event") == "opencode.stream.poll_hedge.start"]

        self.assertEqual(client.prompt_calls, 1)
        self.assertGreaterEqual(client.poll_calls, 2)  # hedge observation + canonical final fetch
        self.assertTrue(client.poll_saw_live_reader[0], "polling must not replace/cancel a healthy quiet SSE reader")
        self.assertTrue(client.stream_cancelled, "the reader is cancelled only after the turn completes")
        self.assertEqual("".join(deltas), client.reply)
        self.assertEqual("".join(spoken), client.reply)
        self.assertEqual(complete["streamSource"], "poll")
        self.assertEqual(len(hedge_starts), 1)


if __name__ == "__main__":
    unittest.main()
