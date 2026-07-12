"""Protocol v0 lane tests for SidepodConnection.

These drive the connection object directly with fake transport/client/audio so
the full voice turn (STT transcript -> fork turn -> TTS -> complete) can be
machine-verified without devices, keys, or network. Every outbound assertion
runs through opencode_voice.protocol.check_event, so these tests double as
runtime-contract conformance proof for the engine side.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from opencode_voice.config import VoiceConfig
from opencode_voice.interruption import EpisodeIdentity, InterruptionEvent
from opencode_voice.logging import RunLogger
from opencode_voice.playback import PlaybackToken
from opencode_voice.protocol import check_event
from opencode_voice.server import (
    ActiveSidepodLaneRegistry,
    SIDEPOD_PROTOCOL_VERSION,
    SidepodConnection,
    classify_turn_failure,
    compaction_growth_required,
)
from tests.fakes import FakeOpenCodeClient

ENV_WITH_KEYS = {"DEEPGRAM_API_KEY": "audio-key", "INCEPTION_API_KEY": "turn-key"}


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


class LaneFakeClient(FakeOpenCodeClient):
    """FakeOpenCodeClient plus the turn surface: fork directories, a staged
    1.17-shaped event stream, and prompt_async."""

    def __init__(self, base_url: str = "http://opencode.test") -> None:
        super().__init__(base_url)
        self.prompts: list[tuple[str, str]] = []
        self._assistant_messages: list[dict[str, Any]] = []
        self._staged_events: list[dict[str, Any]] = []
        self._events_staged = asyncio.Event()
        self.event_directories: list[str | None] = []

    async def fork_session(self, session_id: str, message_id: str | None = None) -> dict[str, str]:
        fork = await super().fork_session(session_id)
        # Real forks inherit the source thread's directory (not the server's).
        return {**fork, "directory": "/project/source-thread"}

    async def get_session(self, session_id: str) -> dict[str, Any]:
        session = await super().get_session(session_id)
        return {**session, "directory": "/project/source-thread"}

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._assistant_messages)

    def events(self, on_open: Any = None, directory: str | None = None) -> Any:
        self.event_directories.append(directory)
        return self._event_stream(on_open)

    async def _event_stream(self, on_open: Any) -> Any:
        if on_open:
            on_open()
        await self._events_staged.wait()
        staged, self._staged_events = self._staged_events, []
        self._events_staged.clear()
        for event in staged:
            yield event
        # A real stream stays open after the turn; block until cancelled.
        await asyncio.Event().wait()

    async def prompt_async(self, session_id: str, text: str, model: Any, agent: str) -> Any:
        self.prompts.append((session_id, text))
        reply_number = len(self.prompts)
        message_id = f"msg_reply_{reply_number}"
        part_id = f"prt_reply_{reply_number}"
        reply = "On it. I will tighten the summary."
        first, rest = reply[:6], reply[6:]
        self._assistant_messages = self._assistant_messages + [
            {
                "info": {
                    "id": message_id,
                    "role": "assistant",
                    "time": {"created": reply_number * 2 - 1, "completed": reply_number * 2},
                },
                "parts": [{"type": "text", "text": reply}],
            }
        ]
        # Real OpenCode 1.17 shapes: sessionID nested in info/part, delta on
        # part.updated, session-level idle event carries it top-level.
        self._staged_events = [
            {
                "type": "message.updated",
                "properties": {"info": {"id": message_id, "role": "assistant", "sessionID": session_id}},
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "delta": first,
                    "part": {
                        "id": part_id,
                        "sessionID": session_id,
                        "messageID": message_id,
                        "type": "text",
                        "text": first,
                    },
                },
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "delta": rest,
                    "part": {
                        "id": part_id,
                        "sessionID": session_id,
                        "messageID": message_id,
                        "type": "text",
                        "text": reply,
                    },
                },
            },
            {"type": "session.idle", "properties": {"sessionID": session_id}},
        ]
        self._events_staged.set()
        return {"ok": True}

    async def prompt_text(self, session_id: str, text: str, model: Any, agent: str) -> Any:
        raise RuntimeError("poll path should not run when the event stream works")


class StructuredLaneClient(LaneFakeClient):
    def __init__(
        self,
        response: dict[str, str] | None = None,
        *,
        responses: list[dict[str, str]] | None = None,
        with_tool: bool = False,
    ) -> None:
        super().__init__()
        self.response = response or {
            "displayText": "The target is 2026 in interruption.py.",
            "spokenText": "The target is twenty twenty-six in the interruption controller module.",
        }
        self.responses = responses or [self.response]
        self.with_tool = with_tool
        self.prompt_payloads: list[dict[str, Any]] = []

    async def messages_for_tracking(self, session_id: str) -> list[dict[str, Any]]:
        return await self.messages(session_id)

    async def prompt_async(
        self,
        session_id: str,
        text: str,
        model: Any,
        agent: str,
        **kwargs: Any,
    ) -> Any:
        self.prompts.append((session_id, text))
        self.prompt_payloads.append(kwargs)
        message_id = f"msg_structured_{len(self.prompts)}"
        parts: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        if self.with_tool:
            tool_part = {
                "id": f"prt_tool_{len(self.prompts)}",
                "sessionID": session_id,
                "messageID": message_id,
                "type": "tool",
                "tool": "read",
                "state": {"status": "running"},
            }
            parts.append(tool_part)
            events.append({"type": "message.part.updated", "properties": {"part": tool_part}})
        response = self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]
        info = {
            "id": message_id,
            "role": "assistant",
            "sessionID": session_id,
            "structured": response,
            "time": {"created": len(self.prompts), "completed": len(self.prompts) + 1},
        }
        self._assistant_messages = [*self._assistant_messages, {"info": info, "parts": parts}]
        self._staged_events = [
            *events,
            {"type": "message.updated", "properties": {"info": info}},
            {"type": "session.idle", "properties": {"sessionID": session_id}},
        ]
        self._events_staged.set()
        return {"ok": True}


class CompactionLaneClient(LaneFakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.summarize_calls = 0
        self.summarize_started = asyncio.Event()
        self.summarize_release = asyncio.Event()
        self.summarize_release.set()
        self.context_tokens = 80_000
        self.emit_compacted = True
        self.summary_error = False
        self.wait_for_idle_calls = 0

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        messages = [
            {
                "info": {
                    "id": "msg_active",
                    "role": "assistant",
                    "time": {"created": 1, "completed": 2},
                    "tokens": {"input": self.context_tokens, "output": 10},
                },
                "parts": [{"type": "text", "text": "active"}],
            }
        ]
        if self.summarize_calls:
            info: dict[str, Any] = {
                "id": f"msg_summary_{self.summarize_calls}",
                "role": "assistant",
                "summary": True,
                "sessionID": session_id,
                "time": {"created": 3, "completed": 4},
                "finish": "error" if self.summary_error else "stop",
            }
            if self.summary_error:
                info["error"] = {"name": "UnknownError"}
            messages.append(
                {
                    "info": info,
                    "parts": [{"type": "text", "text": "canonical summary"}],
                }
            )
            messages.append(
                {
                    "info": {
                        "id": "msg_post_summary_active",
                        "role": "assistant",
                        "time": {"created": 5, "completed": 6},
                        "tokens": {"input": self.context_tokens, "output": 10},
                    },
                    "parts": [{"type": "text", "text": "retained active tail"}],
                }
            )
        return messages

    async def summarize(self, session_id: str, model: Any, auto: bool = False) -> dict[str, bool]:
        self.summarize_calls += 1
        self.summarize_started.set()
        await self.summarize_release.wait()
        summary = next(
            message["info"]
            for message in await self.messages(session_id)
            if message["info"].get("summary") is True
        )
        self._staged_events = [
            {"type": "message.updated", "properties": {"info": summary}},
        ]
        if self.summary_error:
            self._staged_events.append(
                {
                    "type": "session.error",
                    "properties": {"sessionID": session_id, "error": summary["error"]},
                }
            )
        elif self.emit_compacted:
            self._staged_events.append(
                {"type": "session.compacted", "properties": {"sessionID": session_id}}
            )
        self._events_staged.set()
        return {"ok": True}

    async def wait_for_idle(self, session_id: str) -> None:
        self.wait_for_idle_calls += 1


class StructuredLegacyDecoderClient(StructuredLaneClient):
    """The legacy reader breaks after the first structured prompt in 1.17.18."""

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        if self.prompts:
            raise RuntimeError("legacy structured message decoder rejected retryCount")
        return await super().messages(session_id)

    async def messages_for_tracking(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._assistant_messages)


class BrokenPreflightClient(StructuredLaneClient):
    async def messages_for_tracking(self, session_id: str) -> list[dict[str, Any]]:
        raise RuntimeError("message projection unavailable")


class BlockingPreflightClient(StructuredLaneClient):
    def __init__(self) -> None:
        super().__init__()
        self.block_next = False
        self.blocked = asyncio.Event()

    async def messages_for_tracking(self, session_id: str) -> list[dict[str, Any]]:
        if self.block_next:
            self.block_next = False
            self.blocked.set()
            await asyncio.Event().wait()
        return list(self._assistant_messages)


class OverflowRecoveryClient(LaneFakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.cutoffs: list[tuple[str, str | None]] = []
        self._assistant_messages = [
            {"info": {"id": "msg_before", "role": "assistant"}, "parts": []},
            {"info": {"id": "msg_failed_user", "role": "user"}, "parts": [{"type": "text", "text": "ask"}]},
            {"info": {"id": "msg_overflow", "role": "assistant", "error": "context length"}, "parts": []},
        ]

    async def messages_for_tracking(self, session_id: str) -> list[dict[str, Any]]:
        return await self.messages(session_id)

    async def fork_session(self, session_id: str, message_id: str | None = None) -> dict[str, str]:
        self.cutoffs.append((session_id, message_id))
        return await super().fork_session(session_id, message_id=message_id)


class FakeSpeakSession:
    """Stands in for the streaming TTS socket: first speak() reports audio."""

    def __init__(self, options: Any, on_audio: Any, on_event: Any) -> None:
        self.options = options
        self.on_audio = on_audio
        self.spoken: list[str] = []
        self.active_token: PlaybackToken | None = None
        self.cancelled: list[tuple[PlaybackToken, str]] = []

    async def connect(self) -> None:
        return None

    async def begin_turn(self, token: PlaybackToken) -> None:
        self.active_token = token

    async def append_text(self, token: PlaybackToken, text: str) -> None:
        self.active_token = token
        self.spoken.append(text)
        if len(self.spoken) == 1:
            await self.on_audio(token, b"\x01\x00" * 480)

    async def finish_turn(self, token: PlaybackToken) -> None:
        return None

    async def cancel_turn(self, token: PlaybackToken, reason: str) -> None:
        self.cancelled.append((token, reason))
        if self.active_token == token:
            self.active_token = None

    async def close(self) -> None:
        return None


class FakeCartesiaSpeakSession(FakeSpeakSession):
    """Distinct type from FakeSpeakSession so provider-selection tests can
    prove build_speaker() picked the right class, not just *a* class."""


class FailFirstSpeakSession(FakeSpeakSession):
    """Provider failure after accepting the first spoken chunk.

    Playback must fail closed while the independent OpenCode turn continues
    delivering screen text and its terminal protocol event.
    """

    supports_terminal_events = True

    def __init__(self, options: Any, on_audio: Any, on_event: Any) -> None:
        super().__init__(options, on_audio, on_event)
        self.on_event = on_event
        self.connected = True
        self.failed = False

    async def begin_turn(self, token: PlaybackToken) -> None:
        self.active_token = token
        await self.on_event(
            {
                "type": "tts.turn.begin",
                "provider": "deepgram",
                "turn_id": token.turn_id,
                "playback_generation": token.generation,
            }
        )

    async def append_text(self, token: PlaybackToken, text: str) -> None:
        self.spoken.append(text)
        if self.failed:
            return
        self.failed = True
        await self.on_event(
            {
                "type": "tts.turn.failed",
                "provider": "deepgram",
                "turn_id": token.turn_id,
                "playback_generation": token.generation,
                "error_code": "websocket_closed",
            }
        )


class DelayedLifecycleSpeakSession(FakeSpeakSession):
    """Production-shaped fake: text acceptance is not first PCM or EOF.

    The live Cartesia regression happened because the model completed before
    the provider delivered its first frame.  Keep those edges independently
    controllable so lane tests cannot accidentally make TTS synchronous again.
    """

    latest: "DelayedLifecycleSpeakSession | None" = None

    def __init__(self, options: Any, on_audio: Any, on_event: Any) -> None:
        super().__init__(options, on_audio, on_event)
        self.on_event = on_event
        self.connected = False
        self.connect_calls = 0
        self._release_first_audio = asyncio.Event()
        self._first_audio_delivered = asyncio.Event()
        self._delivery_task: asyncio.Task[None] | None = None
        type(self).latest = self

    async def connect(self) -> None:
        if not self.connected:
            self.connect_calls += 1
            self.connected = True

    async def begin_turn(self, token: PlaybackToken) -> None:
        self.active_token = token
        await self.on_event(
            {
                "type": "tts.turn.begin",
                "provider": "deepgram",
                "turn_id": token.turn_id,
                "playback_generation": token.generation,
            }
        )

    async def append_text(self, token: PlaybackToken, text: str) -> None:
        self.active_token = token
        self.spoken.append(text)

    async def finish_turn(self, token: PlaybackToken) -> None:
        await self.on_event(
            {
                "type": "tts.turn.finish",
                "provider": "deepgram",
                "turn_id": token.turn_id,
                "playback_generation": token.generation,
            }
        )
        if self._delivery_task is None:
            self._delivery_task = asyncio.create_task(self._deliver_first_audio(token))

    async def _deliver_first_audio(self, token: PlaybackToken) -> None:
        await self._release_first_audio.wait()
        await self.on_audio(token, b"\x01\x00" * 480)
        self._first_audio_delivered.set()

    async def release_first_audio(self) -> None:
        self._release_first_audio.set()
        await asyncio.wait_for(self._first_audio_delivered.wait(), timeout=1)

    async def emit_done(self) -> None:
        token = self.active_token
        assert token is not None
        await self.on_event(
            {
                "type": "tts.turn.done",
                "provider": "deepgram",
                "turn_id": token.turn_id,
                "playback_generation": token.generation,
            }
        )

    async def emit_failed(self, error_code: str = "websocket_closed") -> None:
        token = self.active_token
        assert token is not None
        await self.on_event(
            {
                "type": "tts.turn.failed",
                "provider": "deepgram",
                "turn_id": token.turn_id,
                "playback_generation": token.generation,
                "error_code": error_code,
            }
        )

    async def close(self) -> None:
        self.connected = False
        if self._delivery_task and not self._delivery_task.done():
            self._delivery_task.cancel()
            await asyncio.gather(self._delivery_task, return_exceptions=True)


class ReconnectableSpeakSession(FakeSpeakSession):
    """Existing provider object whose transport has gone away."""

    def __init__(self, options: Any, on_audio: Any, on_event: Any) -> None:
        super().__init__(options, on_audio, on_event)
        self.connected = False
        self.connect_calls = 0

    async def connect(self) -> None:
        if not self.connected:
            self.connect_calls += 1
            self.connected = True


class FakeNativeSpeaker:
    def __init__(
        self,
        config: Any,
        logger: Any,
        on_issue: Any,
        on_render: Any = None,
        on_drain: Any = None,
        on_first_frame: Any = None,
    ) -> None:
        self.played: list[bytes] = []
        self.on_render = on_render
        self.on_drain = on_drain
        self.on_first_frame = on_first_frame
        self.audible = False
        self.silent_for: float | None = None  # seconds since playback ended
        self.startup = False  # inside the post-start convergence window
        self.paused = False
        self.ducked = False
        self.playback_generation = 0
        self.began: list[PlaybackToken] = []
        self.finished: list[tuple[PlaybackToken, str]] = []

    async def start(self) -> bool:
        return True

    async def play(self, data: bytes, turn_id: Any) -> bool:
        self.played.append(data)
        self.audible = bool(data.strip(b"\x00"))
        if self.on_render:
            self.on_render(data)
        if self.on_first_frame and data.strip(b"\x00"):
            token = turn_id if isinstance(turn_id, PlaybackToken) else PlaybackToken(self.playback_generation, int(turn_id))
            await self.on_first_frame(token)
        return True

    def begin_turn(self, token: PlaybackToken) -> bool:
        self.began.append(token)
        return token.generation == self.playback_generation

    async def finish_turn(self, token: PlaybackToken, outcome: str = "done") -> bool:
        self.finished.append((token, outcome))
        return token.generation == self.playback_generation

    def is_audible(self, tail_sec: float = 0.3) -> bool:
        if self.audible:
            return True
        return self.silent_for is not None and self.silent_for < tail_sec

    def in_startup_window(self, window_sec: float) -> bool:
        return self.startup and window_sec > 0

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def set_ducked(self, ducked: bool) -> None:
        self.ducked = ducked

    def invalidate_generation(self, generation: int, reason: str) -> None:
        self.playback_generation = generation
        self.flush(reason)

    def flush(self, reason: str) -> None:
        self.played.clear()
        self.audible = False
        self.paused = False

    async def close(self) -> None:
        self.audible = False


class FakeNativeMic:
    ok = True
    input_delay_sec = 0.0

    def __init__(self, config: Any, logger: Any, on_audio: Any, on_issue: Any) -> None:
        self.on_issue = on_issue

    async def start(self) -> bool:
        return type(self).ok

    async def close(self) -> None:
        return None


class UnavailableDeviceAudio:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def start(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class AvailableDeviceAudio(UnavailableDeviceAudio):
    stream_delay_ms = 0
    capture_enabled = True

    async def start(self) -> bool:
        return True

    def is_audible(self, tail_sec: float = 0.3) -> bool:
        return False

    def invalidate_generation(self, generation: int | None = None) -> int:
        return int(generation or 0)

    def set_ducked(self, ducked: bool) -> None:
        return None

    def set_capture_enabled(self, enabled: bool) -> None:
        self.capture_enabled = enabled


class BlockingDeviceAudio(AvailableDeviceAudio):
    started: asyncio.Event
    release: asyncio.Event
    instances: list["BlockingDeviceAudio"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False
        type(self).instances.append(self)

    async def start(self) -> bool:
        type(self).started.set()
        await type(self).release.wait()
        return True

    async def close(self) -> None:
        self.closed = True


class StarvedDeviceAudio(AvailableDeviceAudio):
    state = "starved"
    buffered_frames = 0

    def __init__(self) -> None:
        self.ducked = False

    def set_ducked(self, ducked: bool) -> None:
        self.ducked = ducked


class FakeFlux:
    def __init__(self, config: Any, on_event: Any) -> None:
        self.on_event = on_event
        self.audio: list[bytes] = []

    async def start(self) -> None:
        return None

    async def send_audio(self, data: bytes) -> None:
        self.audio.append(data)

    async def close(self) -> None:
        return None


class CountingFlux(FakeFlux):
    starts = 0

    async def start(self) -> None:
        type(self).starts += 1


class BlockingFlux(FakeFlux):
    started: asyncio.Event
    release: asyncio.Event

    async def start(self) -> None:
        type(self).started.set()
        await type(self).release.wait()


START_PAYLOAD = {
    "type": "start",
    "protocolVersion": SIDEPOD_PROTOCOL_VERSION,
    "clientEventId": "evt_lane_1",
    "sentAt": "2026-07-04T00:00:00.000Z",
    "sourceSessionId": "source_1",
    "keepFork": False,
}


def lane_connection(
    tmp: str,
    client: LaneFakeClient | None = None,
    voice_duplex: str = "auto",
    tts_provider: str = "deepgram",
    device_sample_rate: int = 48_000,
    response_mode: str = "legacy",
    compaction_wait_sec: float = 10.0,
    **kwargs: Any,
) -> tuple[SidepodConnection, FakeWebSocket, LaneFakeClient]:
    websocket = FakeWebSocket()
    fake_client = client or LaneFakeClient()
    connection = SidepodConnection(
        config=VoiceConfig(
            opencode_url="http://opencode.test",
            run_root=tmp,
            voice_duplex=voice_duplex,
            tts_provider=tts_provider,
            device_sample_rate=device_sample_rate,
            response_mode=response_mode,
            compaction_wait_sec=compaction_wait_sec,
        ),
        client=fake_client,  # type: ignore[arg-type]
        logger=RunLogger(root=tmp),
        websocket=websocket,  # type: ignore[arg-type]
        **kwargs,
    )
    return connection, websocket, fake_client


async def wait_for_mic_transition(connection: SidepodConnection) -> None:
    task = connection.mic_start_task
    if task:
        await asyncio.gather(task, return_exceptions=True)


def assert_all_lane_messages_valid(test: unittest.TestCase, sent: list[dict[str, Any]]) -> None:
    for message in sent:
        check = check_event(message)
        test.assertTrue(check.ok, f"{message.get('type')}: {check.errors}")


class TurnFailureClassificationTests(unittest.IsolatedAsyncioTestCase):
    """A model-provider quota/auth failure should reach the sidepod as a
    specific, actionable issue instead of a generic 'Voice turn failed', with
    no provider text on the wire."""

    def test_classifier_buckets_provider_errors_and_falls_back_safely(self) -> None:
        quota = {"name": "APIError", "data": {"message": "Free tier limit reached.", "statusCode": 402}}
        auth = {"name": "APIError", "data": {"statusCode": 403, "code": "model_access_denied"}}
        policy = {"name": "UnknownError", "data": {"message": '"The request was filtered due to content policy violation."'}}
        policy_403 = {"name": "APIError", "data": {"message": "Blocked by content policy.", "statusCode": 403}}
        self.assertEqual(classify_turn_failure(quota), "provider_quota")
        self.assertEqual(classify_turn_failure(auth), "provider_auth")
        self.assertEqual(classify_turn_failure(policy), "content_policy")
        self.assertEqual(classify_turn_failure(policy_403), "content_policy")
        self.assertEqual(classify_turn_failure({"data": {"statusCode": 500}}), "failed")
        self.assertEqual(classify_turn_failure("raw error string"), "failed")
        self.assertEqual(classify_turn_failure(None), "failed")

    def _issue_for(self, tmp: str, payload: dict[str, Any]) -> dict[str, Any]:
        connection, _websocket, _client = lane_connection(tmp)
        connection.voice_lane_id = "lane_1"
        return connection.translate_turn_failure(payload)

    def test_quota_failure_maps_to_a_billing_issue_carrying_no_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            issue = self._issue_for(
                tmp,
                {"type": "turn.error", "turn_id": 1, "message": "Free tier limit reached.", "failure": "provider_quota"},
            )
        self.assertEqual(issue["diagnosticCode"], "model_provider_quota")
        self.assertFalse(issue["retryable"])
        self.assertIn("quota", issue["safeDetail"].lower())
        # No provider name and no raw exception text anywhere on the wire.
        stable_issue = {key: value for key, value in issue.items() if key not in {"sentAt", "debugRef"}}
        blob = json.dumps(stable_issue).lower()
        for leak in ("inception", "mercury", "free tier limit", "402", "api.inceptionlabs"):
            self.assertNotIn(leak, blob)

    def test_unknown_and_timeout_failures_stay_generic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            failed = self._issue_for(tmp, {"type": "turn.error", "turn_id": 1})
            timed = self._issue_for(tmp, {"type": "turn.timeout", "turn_id": 1})
        self.assertEqual(failed["diagnosticCode"], "turn_failed")
        self.assertTrue(failed["retryable"])
        self.assertEqual(timed["diagnosticCode"], "turn_timeout")

    def test_content_policy_failure_maps_to_specific_safe_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            issue = self._issue_for(tmp, {"type": "turn.error", "turn_id": 1, "failure": "content_policy"})

        self.assertEqual(issue["diagnosticCode"], "model_content_policy")
        self.assertFalse(issue["retryable"])
        self.assertIn("safety policy", issue["safeDetail"])


class SidepodLaneTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_turn_flushes_only_ready_completion_before_pruning_old_seam(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _client = lane_connection(tmp)
            connection.voice_lane_id = "lane_1"

            await connection.send_json({"type": "turn.start", "turn_id": 1, "source": "live", "text": "First"})
            connection.turn_spoken_any = True
            connection.native_speaker = FakeNativeSpeaker(None, None, None)
            connection.native_speaker.audible = True
            await connection.send_json({"type": "turn.complete", "turn_id": 1, "latency_ms": 10, "text": "Done"})
            self.assertFalse(any(message["type"] == "complete" for message in websocket.sent))
            seam = connection.turn_seams[1]
            seam.provider_terminal = True
            seam.playback_drained = True

            await connection.send_json({"type": "turn.start", "turn_id": 2, "source": "live", "text": "Second"})

            tail_types = [message["type"] for message in websocket.sent[-2:]]
            self.assertEqual(tail_types, ["complete", "thinking"])
            self.assertEqual(websocket.sent[-2]["turnId"], "turn_0001")
            self.assertEqual(websocket.sent[-2]["fullSpokenText"], "Done")
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_new_turn_never_claims_nonterminal_old_speech_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _client = lane_connection(tmp)
            connection.voice_lane_id = "lane_1"

            await connection.send_json({"type": "turn.start", "turn_id": 1, "source": "live", "text": "First"})
            connection.turn_spoken_any = True
            connection.native_speaker = FakeNativeSpeaker(None, None, None)
            connection.native_speaker.audible = True
            await connection.send_json({"type": "turn.complete", "turn_id": 1, "latency_ms": 10, "text": "Old"})

            await connection.send_json({"type": "turn.start", "turn_id": 2, "source": "live", "text": "Second"})

            self.assertNotIn("complete", [message["type"] for message in websocket.sent])
            self.assertEqual(websocket.sent[-1]["type"], "thinking")
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_full_voice_turn_emits_the_v0_sequence_with_real_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_flux_event({"type": "speech.start"})
            await connection.handle_flux_event(
                {"type": "speech.transcript", "transcript": "Make the test", "is_final": False, "confidence": 0.8}
            )
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "Make the test output easier to scan.", "eager": False}
            )
            self.assertIsNotNone(connection.turn_task)
            await asyncio.wait_for(connection.turn_task, timeout=10)
            self.assertFalse(
                any(message["type"] == "complete" for message in websocket.sent),
                "visible completion must wait until native playback drains",
            )
            await connection.on_playback_drained()

            types = [message["type"] for message in websocket.sent]
            self.assertEqual(
                types,
                [
                    "ready",
                    "transcript",
                    "transcript",
                    "thinking",
                    "assistant.delta",
                    "speaking",
                    "assistant.delta",
                    "complete",
                ],
            )
            assert_all_lane_messages_valid(self, websocket.sent)

            interim, final = websocket.sent[1], websocket.sent[2]
            self.assertFalse(interim["final"])
            self.assertTrue(final["final"])
            self.assertEqual(interim["turnId"], final["turnId"])
            self.assertLess(interim["sequence"], final["sequence"])

            thinking = websocket.sent[3]
            self.assertEqual(thinking["turnId"], final["turnId"])
            self.assertEqual(thinking["sourceMode"], "live")

            complete = websocket.sent[-1]
            self.assertEqual(complete["turnId"], final["turnId"])
            self.assertEqual(complete["streamSource"], "event")
            self.assertIn("totalMs", complete["latency"])
            self.assertIn("firstTranscriptMs", complete["latency"])
            self.assertIn("firstAssistantTextMs", complete["latency"])
            self.assertIn("firstAudioMs", complete["latency"])
            self.assertEqual(client.prompts, [("fork_1", "Make the test output easier to scan.")])
            # /event is directory-scoped; the turn must subscribe with the
            # fork's inherited directory or the stream stays silent.
            self.assertEqual(client.event_directories, ["/project/source-thread"])

    async def test_second_turn_gets_a_fresh_turn_id_and_sequences_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            for phrase in ("First ask.", "Second ask."):
                await connection.handle_flux_event({"type": "speech.start"})
                await connection.handle_flux_event({"type": "speech.end", "transcript": phrase})
                await asyncio.wait_for(connection.turn_task, timeout=10)
                # The real device engine stops reporting audible after drain;
                # keep the injected speaker's acoustic state equally honest.
                assert connection.native_speaker is not None
                connection.native_speaker.audible = False
                await connection.on_playback_drained()

            transcripts = [message for message in websocket.sent if message["type"] == "transcript"]
            self.assertEqual(len(transcripts), 2)
            self.assertNotEqual(transcripts[0]["turnId"], transcripts[1]["turnId"])
            self.assertEqual(transcripts[1]["sequence"], 1)
            completes = [message for message in websocket.sent if message["type"] == "complete"]
            self.assertEqual([message["turnId"] for message in completes], [t["turnId"] for t in transcripts])
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_completion_before_text_streams_via_grace_not_poll(self) -> None:
        # Reproduces the ~20% fallback: session.idle lands before the text
        # parts. The completion grace must wait for the trailing text and
        # finish on the event path instead of falling to polling.
        class IdleFirstLaneClient(LaneFakeClient):
            async def prompt_async(self, session_id: str, text: str, model: Any, agent: str) -> Any:
                self.prompts.append((session_id, text))
                message_id = "msg_reply_1"
                reply = "On it. I will tighten the summary."
                self._assistant_messages = [
                    {
                        "info": {"id": message_id, "role": "assistant", "time": {"created": 1, "completed": 2}},
                        "parts": [{"type": "text", "text": reply}],
                    }
                ]
                self._staged_events = [
                    {
                        "type": "message.updated",
                        "properties": {"info": {"id": message_id, "role": "assistant", "sessionID": session_id}},
                    },
                    # Completion arrives before any text part.
                    {"type": "session.idle", "properties": {"sessionID": session_id}},
                    {
                        "type": "message.part.updated",
                        "properties": {
                            "delta": reply,
                            "part": {
                                "id": "prt_reply_1",
                                "sessionID": session_id,
                                "messageID": message_id,
                                "type": "text",
                                "text": reply,
                            },
                        },
                    },
                ]
                self._events_staged.set()
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(tmp, client=IdleFirstLaneClient())
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_flux_event({"type": "speech.end", "transcript": "Tighten the summary."})
            await asyncio.wait_for(connection.turn_task, timeout=10)
            await connection.on_playback_drained()

            complete = next(m for m in websocket.sent if m["type"] == "complete")
            self.assertEqual(complete["streamSource"], "event")
            deltas = [m for m in websocket.sent if m["type"] == "assistant.delta"]
            self.assertTrue(deltas, "the trailing text must still stream")
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_completion_after_prefix_waits_for_and_speaks_trailing_suffix(self) -> None:
        class PartialIdleLaneClient(LaneFakeClient):
            async def prompt_async(self, session_id: str, text: str, model: Any, agent: str) -> Any:
                self.prompts.append((session_id, text))
                message_id = "msg_reply_1"
                prefix = "First sentence. "
                reply = prefix + "Trailing sentence."
                self._assistant_messages = [
                    {
                        "info": {
                            "id": message_id,
                            "role": "assistant",
                            "time": {"created": 1, "completed": 2},
                        },
                        "parts": [{"type": "text", "text": reply}],
                    }
                ]
                self._staged_events = [
                    {
                        "type": "message.updated",
                        "properties": {
                            "info": {
                                "id": message_id,
                                "role": "assistant",
                                "sessionID": session_id,
                            }
                        },
                    },
                    {
                        "type": "message.part.updated",
                        "properties": {
                            "delta": prefix,
                            "part": {
                                "id": "prt_reply_1",
                                "sessionID": session_id,
                                "messageID": message_id,
                                "type": "text",
                                "text": prefix,
                            },
                        },
                    },
                    {"type": "session.idle", "properties": {"sessionID": session_id}},
                    {
                        "type": "message.part.updated",
                        "properties": {
                            "delta": "Trailing sentence.",
                            "part": {
                                "id": "prt_reply_1",
                                "sessionID": session_id,
                                "messageID": message_id,
                                "type": "text",
                                "text": reply,
                            },
                        },
                    },
                ]
                self._events_staged.set()
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(tmp, client=PartialIdleLaneClient())
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "Give me both sentences."}
            )
            assert connection.turn_task is not None
            await asyncio.wait_for(connection.turn_task, timeout=3)
            await connection.on_playback_drained()

            deltas = [message["delta"] for message in websocket.sent if message["type"] == "assistant.delta"]
            speaker = connection.speaker
            assert isinstance(speaker, FakeSpeakSession)
            self.assertEqual("".join(deltas), "First sentence. Trailing sentence.")
            self.assertEqual(" ".join(speaker.spoken), "First sentence. Trailing sentence.")
            complete = next(message for message in websocket.sent if message["type"] == "complete")
            self.assertEqual(complete["fullSpokenText"], "First sentence. Trailing sentence.")
            assert_all_lane_messages_valid(self, websocket.sent)


class StructuredResponseLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_turn_silently_replaces_stalled_preflight(self) -> None:
        client = BlockingPreflightClient()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(
                tmp,
                client=client,
                response_mode="structured",
            )
            await connection.handle_control(START_PAYLOAD)
            if connection.compaction_task:
                await connection.compaction_task
            client.block_next = True
            await connection.handle_flux_event({"type": "speech.end", "transcript": "First question."})
            await asyncio.wait_for(client.blocked.wait(), timeout=1)
            await connection.handle_flux_event({"type": "speech.end", "transcript": "Replacement question."})
            assert connection.turn_task is not None
            await asyncio.wait_for(connection.turn_task, timeout=3)

            self.assertNotIn("interrupted", [message["type"] for message in websocket.sent])
            self.assertEqual(
                len([message for message in websocket.sent if message["type"] == "assistant.delta"]),
                1,
            )
            self.assertIn("turn.preflight.replaced", connection.logger.path.read_text(encoding="utf-8"))
            await connection.close()

    async def test_consecutive_structured_turns_use_compatible_message_projection(self) -> None:
        client = StructuredLegacyDecoderClient()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(
                tmp,
                client=client,
                response_mode="structured",
            )
            await connection.handle_control(START_PAYLOAD)
            for transcript in ("First question.", "Second question."):
                await connection.handle_flux_event({"type": "speech.end", "transcript": transcript})
                assert connection.turn_task is not None
                await asyncio.wait_for(connection.turn_task, timeout=3)
                await connection.on_playback_drained()

            deltas = [message for message in websocket.sent if message["type"] == "assistant.delta"]
            self.assertEqual(len(deltas), 2)
            self.assertIsNone(connection.active_turn_id)
            logs = connection.logger.path.read_text(encoding="utf-8")
            self.assertNotIn("turn.preflight.error", logs)
            await connection.close()

    async def test_preflight_projection_failure_is_visible_and_leaves_no_phantom_turn(self) -> None:
        client = BrokenPreflightClient()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(
                tmp,
                client=client,
                response_mode="structured",
            )
            await connection.handle_control(START_PAYLOAD)
            if connection.compaction_task:
                await connection.compaction_task
            await connection.handle_flux_event({"type": "speech.end", "transcript": "Can you hear me?"})
            assert connection.turn_task is not None
            await asyncio.wait_for(connection.turn_task, timeout=3)

            self.assertIsNone(connection.active_turn_id)
            self.assertNotIn("interrupted", [message["type"] for message in websocket.sent])
            issues = [message for message in websocket.sent if message["type"] == "voice_bridge_issue"]
            self.assertTrue(issues)
            self.assertIn("turn_failed", [issue["diagnosticCode"] for issue in issues])
            self.assertIn("turn.preflight.error", connection.logger.path.read_text(encoding="utf-8"))
            await connection.close()

    async def test_validated_display_and_spoken_fields_route_independently(self) -> None:
        client = StructuredLaneClient()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(
                tmp, client=client, response_mode="structured"
            )
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_flux_event({"type": "speech.end", "transcript": "Where is the target?"})
            assert connection.turn_task is not None
            await asyncio.wait_for(connection.turn_task, timeout=3)
            await connection.on_playback_drained()

            deltas = [message["delta"] for message in websocket.sent if message["type"] == "assistant.delta"]
            self.assertEqual(deltas, [client.response["displayText"]])
            speaker = connection.speaker
            assert isinstance(speaker, FakeSpeakSession)
            self.assertEqual(" ".join(speaker.spoken), client.response["spokenText"])
            complete = next(message for message in websocket.sent if message["type"] == "complete")
            self.assertEqual(complete["fullSpokenText"], client.response["spokenText"])
            self.assertEqual(client.prompt_payloads[0]["output_format"]["type"], "json_schema")
            self.assertIn("StructuredOutput", client.prompt_payloads[0]["system"])
            assert_all_lane_messages_valid(self, websocket.sent)
            await connection.close()

    async def test_real_tool_activity_is_observed_once_without_becoming_screen_text(self) -> None:
        client = StructuredLaneClient(with_tool=True)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(
                tmp, client=client, response_mode="structured"
            )
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_flux_event({"type": "speech.end", "transcript": "Inspect it."})
            assert connection.turn_task is not None
            await asyncio.wait_for(connection.turn_task, timeout=3)

            logs = [json.loads(line) for line in connection.logger.path.read_text(encoding="utf-8").splitlines()]
            activity = [item for item in logs if item["event"] == "opencode.tool.activity"]
            self.assertEqual([(item["tool"], item["status"]) for item in activity], [("read", "running")])
            serialized = json.dumps(websocket.sent)
            self.assertNotIn("prt_tool", serialized)
            self.assertNotIn('"tool"', serialized)
            await connection.close()

    async def test_unsafe_first_response_is_repaired_before_screen_or_speech(self) -> None:
        unsafe = {
            "displayText": "Use /Users/ana/project/src/App.tsx.",
            "spokenText": "Use slash Users slash ana slash project slash src slash App dot tsx.",
        }
        safe = {
            "displayText": "Use App.tsx.",
            "spokenText": "Use the app component.",
        }
        client = StructuredLaneClient(responses=[unsafe, safe])
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(
                tmp, client=client, response_mode="structured"
            )
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_flux_event({"type": "speech.end", "transcript": "Which file?"})
            assert connection.turn_task is not None
            await asyncio.wait_for(connection.turn_task, timeout=3)

            deltas = [message["delta"] for message in websocket.sent if message["type"] == "assistant.delta"]
            self.assertEqual(deltas, [safe["displayText"]])
            speaker = connection.speaker
            assert isinstance(speaker, FakeSpeakSession)
            self.assertEqual(" ".join(speaker.spoken), safe["spokenText"])
            self.assertNotIn("/Users/ana", json.dumps(websocket.sent))
            self.assertEqual(len(client.prompts), 2)
            self.assertTrue(all(value is False for value in client.prompt_payloads[1]["tools"].values()))
            await connection.close()

    async def test_schema_invalid_first_response_can_be_repaired(self) -> None:
        client = StructuredLaneClient(
            responses=[
                {"displayText": "Ready."},  # type: ignore[list-item]
                {"displayText": "Ready.", "spokenText": "Ready."},
            ]
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(
                tmp, client=client, response_mode="structured"
            )
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_flux_event({"type": "speech.end", "transcript": "Are we ready?"})
            assert connection.turn_task is not None
            await asyncio.wait_for(connection.turn_task, timeout=3)

            self.assertEqual(
                [message["delta"] for message in websocket.sent if message["type"] == "assistant.delta"],
                ["Ready."],
            )
            self.assertEqual(len(client.prompts), 2)
            await connection.close()


class SidepodTTSLifecycleRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def _run_turn_before_first_pcm(
        self,
        tmp: str,
    ) -> tuple[SidepodConnection, FakeWebSocket, DelayedLifecycleSpeakSession]:
        connection, websocket, _client = lane_connection(tmp)
        await connection.handle_control(START_PAYLOAD)
        await connection.handle_flux_event({"type": "speech.start"})
        await connection.handle_flux_event(
            {"type": "speech.end", "transcript": "Give me a thorough explanation.", "eager": False}
        )
        self.assertIsNotNone(connection.turn_task)
        await asyncio.wait_for(connection.turn_task, timeout=2)
        speaker = DelayedLifecycleSpeakSession.latest
        self.assertIsNotNone(speaker)
        assert speaker is not None
        return connection, websocket, speaker

    async def test_model_complete_before_first_pcm_waits_for_provider_done_and_device_drain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", DelayedLifecycleSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            DelayedLifecycleSpeakSession.latest = None
            connection, websocket, speaker = await self._run_turn_before_first_pcm(tmp)

            self.assertNotIn(
                "complete",
                [message["type"] for message in websocket.sent],
                "model EOF is not audible/provider EOF",
            )
            await speaker.release_first_audio()
            self.assertNotIn("complete", [message["type"] for message in websocket.sent])

            await speaker.emit_done()
            self.assertNotIn(
                "complete",
                [message["type"] for message in websocket.sent],
                "provider done still has a device-buffered tail",
            )

            token = speaker.active_token
            self.assertIsNotNone(token)
            assert token is not None
            assert isinstance(connection.native_speaker, FakeNativeSpeaker)
            connection.native_speaker.audible = False
            await connection.on_playback_drained(token)

            completes = [message for message in websocket.sent if message["type"] == "complete"]
            self.assertEqual(len(completes), 1)
            self.assertIn("firstAudioMs", completes[0]["latency"])
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_mid_response_tts_failure_keeps_the_model_turn_text_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FailFirstSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_flux_event({"type": "speech.start"})
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "Give me the complete response.", "eager": False}
            )
            assert connection.turn_task is not None
            await asyncio.wait_for(connection.turn_task, timeout=2)

            deltas = [message["delta"] for message in websocket.sent if message["type"] == "assistant.delta"]
            completes = [message for message in websocket.sent if message["type"] == "complete"]
            issues = [message for message in websocket.sent if message["type"] == "voice_bridge_issue"]
            self.assertEqual("".join(deltas), "On it. I will tighten the summary.")
            self.assertEqual(len(completes), 1)
            self.assertEqual(completes[0]["fullSpokenText"], "On it. I will tighten the summary.")
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["diagnosticCode"], "tts_provider_unavailable")
            self.assertIsNone(connection.active_turn_id)
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_old_ten_second_watchdog_cannot_complete_a_long_nonterminal_playback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", DelayedLifecycleSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            DelayedLifecycleSpeakSession.latest = None
            connection, websocket, speaker = await self._run_turn_before_first_pcm(tmp)
            await speaker.release_first_audio()
            self.assertTrue(connection.playback_is_audible(tail_sec=0.0))

            # Exercise the former ten-second watchdog deterministically.  A
            # real long response must stay open, regardless of elapsed wall
            # time, until provider EOF and the device-buffered tail both land.
            await connection.flush_pending_completion_after_timeout(1, delay_sec=0)
            self.assertNotIn("complete", [message["type"] for message in websocket.sent])

            await speaker.emit_done()
            self.assertNotIn("complete", [message["type"] for message in websocket.sent])
            token = speaker.active_token
            self.assertIsNotNone(token)
            assert token is not None
            assert isinstance(connection.native_speaker, FakeNativeSpeaker)
            connection.native_speaker.audible = False
            await connection.on_playback_drained(token)
            self.assertEqual([m["type"] for m in websocket.sent].count("complete"), 1)

    async def test_hybrid_observation_maps_to_frozen_v0_poll_after_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _client = lane_connection(tmp)
            await connection.send_json(
                {"type": "turn.start", "turn_id": 1, "source": "live", "text": "Explain this."}
            )
            await connection.send_json(
                {
                    "type": "turn.complete",
                    "turn_id": 1,
                    "latency_ms": 100,
                    "text": "Done.",
                    "stream_source": "hybrid",
                }
            )

            completes = [message for message in websocket.sent if message["type"] == "complete"]
            self.assertEqual(len(completes), 1)
            self.assertEqual(completes[0]["streamSource"], "poll_after_event")
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_normal_done_and_drain_leave_idle_mute_as_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", DelayedLifecycleSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            DelayedLifecycleSpeakSession.latest = None
            connection, websocket, speaker = await self._run_turn_before_first_pcm(tmp)
            await speaker.release_first_audio()
            await speaker.emit_done()
            token = speaker.active_token
            self.assertIsNotNone(token)
            assert token is not None
            assert isinstance(connection.native_speaker, FakeNativeSpeaker)
            connection.native_speaker.audible = False
            await connection.on_playback_drained(token)

            self.assertIsNone(connection.tts_turn_token)
            generation = connection.speak_generation
            interrupted_before = [m["type"] for m in websocket.sent].count("interrupted")
            await connection.barge_in("user.mute")

            self.assertEqual(connection.speak_generation, generation)
            self.assertEqual(
                [m["type"] for m in websocket.sent].count("interrupted"),
                interrupted_before,
            )

    async def test_provider_failure_flushes_spoken_remainder_and_surfaces_retryable_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", DelayedLifecycleSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            DelayedLifecycleSpeakSession.latest = None
            connection, websocket, speaker = await self._run_turn_before_first_pcm(tmp)
            await speaker.release_first_audio()
            generation = connection.speak_generation

            await speaker.emit_failed()

            self.assertEqual(connection.speak_generation, generation + 1)
            self.assertIsNone(connection.tts_turn_token)
            assert isinstance(connection.native_speaker, FakeNativeSpeaker)
            self.assertEqual(connection.native_speaker.played, [])
            issues = [m for m in websocket.sent if m["type"] == "voice_bridge_issue"]
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["diagnosticCode"], "tts_provider_unavailable")
            self.assertTrue(issues[0]["retryable"])
            self.assertEqual([m["type"] for m in websocket.sent].count("complete"), 1)

    async def test_missing_provider_done_becomes_a_safe_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", DelayedLifecycleSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            DelayedLifecycleSpeakSession.latest = None
            connection, websocket, speaker = await self._run_turn_before_first_pcm(tmp)
            await speaker.release_first_audio()
            token = speaker.active_token
            assert token is not None
            connection.tts_last_audio_at[token] = time.monotonic() - 3

            await connection.watch_tts_terminal(token)

            self.assertEqual(connection.tts_terminal_tokens[token], "failed")
            issues = [m for m in websocket.sent if m["type"] == "voice_bridge_issue"]
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["diagnosticCode"], "tts_provider_unavailable")

    async def test_stale_failure_cannot_close_the_next_generation_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _client = lane_connection(tmp)
            current_speaker = ReconnectableSpeakSession(None, None, None)
            current_speaker.connected = True
            connection.speaker = current_speaker
            connection.speak_generation = 2
            stale = PlaybackToken(1, 4)

            await connection.handle_tts_provider_event(
                {
                    "type": "tts.turn.failed",
                    "provider": "deepgram",
                    "turn_id": stale.turn_id,
                    "playback_generation": stale.generation,
                    "error_code": "websocket_closed",
                }
            )

            self.assertIs(connection.speaker, current_speaker)
            self.assertEqual(connection.speak_generation, 2)
            self.assertEqual([m for m in websocket.sent if m["type"] == "voice_bridge_issue"], [])

    async def test_prewarm_reconnects_an_existing_provider_with_a_dead_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", ReconnectableSpeakSession
        ):
            connection, _websocket, _client = lane_connection(tmp)
            speaker = connection.build_speaker()
            self.assertIsInstance(speaker, ReconnectableSpeakSession)
            connection.speaker = speaker
            self.assertFalse(speaker.connected)

            await connection.prewarm_speaker()

            self.assertTrue(speaker.connected)
            self.assertEqual(speaker.connect_calls, 1)

    async def test_provider_lifecycle_reaches_half_duplex_fallback_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, _websocket, _client = lane_connection(tmp)
            speaker = FakeNativeSpeaker(None, None, None)
            connection.native_speaker = speaker
            token = PlaybackToken(connection.speak_generation, 7)

            await connection.handle_tts_provider_event(
                {
                    "type": "tts.turn.begin",
                    "provider": "deepgram",
                    "turn_id": token.turn_id,
                    "playback_generation": token.generation,
                }
            )
            await connection.handle_tts_provider_event(
                {
                    "type": "tts.turn.done",
                    "provider": "deepgram",
                    "turn_id": token.turn_id,
                    "playback_generation": token.generation,
                }
            )

            self.assertEqual(speaker.began, [token])
            self.assertEqual(speaker.finished, [(token, "done")])


class TTSProviderSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_speaker_picks_the_configured_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.CartesiaTTSProvider", FakeCartesiaSpeakSession):
            deepgram_connection, _websocket, _client = lane_connection(tmp, tts_provider="deepgram")
            cartesia_connection, _websocket2, _client2 = lane_connection(tmp, tts_provider="cartesia")

            deepgram = deepgram_connection.build_speaker()
            cartesia = cartesia_connection.build_speaker()
            self.assertIs(type(deepgram), FakeSpeakSession)
            self.assertIs(type(cartesia), FakeCartesiaSpeakSession)
            self.assertEqual(deepgram.options.sample_rate, 16_000)
            self.assertEqual(cartesia.options.sample_rate, 16_000)

    async def test_speak_is_gated_when_the_active_providers_key_is_missing(self) -> None:
        env = {"DEEPGRAM_API_KEY": "audio-key", "INCEPTION_API_KEY": "turn-key"}  # no CARTESIA_API_KEY
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True), patch(
            # credential_issue_for reads the real project .env by default; a
            # developer's local CARTESIA_API_KEY must not leak into this
            # "key absent" scenario.
            "opencode_voice.config.load_local_dotenv",
            return_value=(),
        ):
            connection, websocket, _client = lane_connection(tmp, tts_provider="cartesia")
            await connection.speak("Hello there.", turn_id=1)

        self.assertIsNone(connection.speaker)
        issues = [message for message in websocket.sent if message.get("type") == "voice_bridge_issue"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["diagnosticCode"], "missing_cartesia_api_key")


class SidepodLaneLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_same_workspace_lane_is_rejected_until_first_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = ActiveSidepodLaneRegistry()
            first, first_ws, first_client = lane_connection(tmp, lane_registry=registry)
            second, second_ws, second_client = lane_connection(tmp, lane_registry=registry)

            await first.handle_control(START_PAYLOAD)
            await second.handle_control({**START_PAYLOAD, "clientEventId": "evt_start_2"})

            self.assertEqual(first_ws.sent[-1]["type"], "ready")
            self.assertEqual(second_ws.sent[-1]["type"], "voice_bridge_issue")
            self.assertEqual(second_ws.sent[-1]["diagnosticCode"], "voice_lane_already_active")
            self.assertEqual(first_client.fork_count, 1)
            self.assertEqual(second_client.fork_count, 0)

            await first.handle_control(
                {
                    "type": "stop",
                    "clientEventId": "evt_stop_first",
                    "sentAt": "2026-07-04T00:00:30.000Z",
                    "reason": "user.end_session",
                }
            )
            await second.handle_control({**START_PAYLOAD, "clientEventId": "evt_start_3"})

            self.assertEqual(second_ws.sent[-1]["type"], "ready")
            self.assertEqual(second_client.fork_count, 1)
            assert_all_lane_messages_valid(self, first_ws.sent)
            assert_all_lane_messages_valid(self, second_ws.sent)

    async def test_voice_tmp_source_session_is_rejected_without_forking(self) -> None:
        class VoiceTmpSourceClient(LaneFakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.listed = False

            async def get_session(self, session_id: str) -> dict[str, Any]:
                if session_id == "fork_tmp":
                    return {
                        "id": "fork_tmp",
                        "title": "[voice tmp] Old voice lane",
                        "tokens": {},
                        "directory": "/project/source-thread",
                    }
                return await super().get_session(session_id)

            async def list_sessions(self) -> list[dict[str, object]]:
                self.listed = True
                return [{"id": "fork_other", "title": "[voice tmp] Other"}]

        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, client = lane_connection(tmp, client=VoiceTmpSourceClient())
            await connection.handle_control({**START_PAYLOAD, "sourceSessionId": "fork_tmp"})

            self.assertEqual(websocket.sent[-1]["type"], "voice_bridge_issue")
            self.assertEqual(websocket.sent[-1]["diagnosticCode"], "voice_tmp_source_session")
            self.assertIn("original chat", websocket.sent[-1]["safeDetail"])
            self.assertEqual(client.fork_count, 0)
            self.assertFalse(client.listed)
            self.assertEqual(client.deleted, [])
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_stop_command_tears_down_and_acknowledges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_control(
                {
                    "type": "stop",
                    "clientEventId": "evt_stop_1",
                    "sentAt": "2026-07-04T00:01:00.000Z",
                    "reason": "user.end_session",
                }
            )

            stopped = websocket.sent[-1]
            self.assertEqual(stopped["type"], "stopped")
            self.assertEqual(stopped["reason"], "user.end_session")
            self.assertTrue(stopped["forkDeleted"])
            self.assertEqual(client.deleted, ["fork_1"])
            assert_all_lane_messages_valid(self, websocket.sent)

            websocket.sent.clear()
            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_live_1",
                    "sentAt": "2026-07-04T00:01:01.000Z",
                    "value": True,
                }
            )
            self.assertEqual(websocket.sent[-1]["diagnosticCode"], "voice_lane_not_started")

    async def test_restart_on_another_thread_reforks_and_cleans_the_old_fork(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_control(
                {**START_PAYLOAD, "clientEventId": "evt_lane_2", "sourceSessionId": "source_2"}
            )

            readies = [message for message in websocket.sent if message["type"] == "ready"]
            self.assertEqual([ready["forkSessionId"] for ready in readies], ["fork_1", "fork_2"])
            self.assertEqual(readies[1]["sourceSessionId"], "source_2")
            self.assertEqual(client.deleted, ["fork_1"])
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_start_opencode_url_rebinds_the_client(self) -> None:
        created: list[str] = []

        def factory(url: str, timeout: float) -> LaneFakeClient:
            created.append(url)
            return LaneFakeClient(base_url=url)

        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, original = lane_connection(tmp, client_factory=factory)
            await connection.handle_control({**START_PAYLOAD, "opencodeUrl": "http://127.0.0.1:4242"})

            self.assertEqual(created, ["http://127.0.0.1:4242"])
            self.assertTrue(original.closed)
            self.assertEqual(connection.client.base_url, "http://127.0.0.1:4242")
            self.assertEqual(websocket.sent[-1]["type"], "ready")

    async def test_live_set_starts_listening_and_mute_stops_capture_without_fake_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.NativeMicSession", FakeNativeMic
        ), patch("opencode_voice.server.DeepgramFluxSession", FakeFlux), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", UnavailableDeviceAudio
        ):
            FakeNativeMic.ok = True
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_live_2",
                    "sentAt": "2026-07-04T00:02:00.000Z",
                    "value": True,
                    "reason": "user.toggle",
                }
            )
            await wait_for_mic_transition(connection)
            listening = websocket.sent[-1]
            self.assertEqual(listening["type"], "listening")
            self.assertEqual(listening["mode"], "live")
            self.assertIsNotNone(connection.native_mic)

            count_before_mute = len(websocket.sent)
            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_live_3",
                    "sentAt": "2026-07-04T00:02:05.000Z",
                    "value": False,
                    "reason": "user.toggle",
                }
            )
            self.assertIsNone(connection.native_mic)
            self.assertEqual(len(websocket.sent), count_before_mute, "mute must not emit a synthetic complete")
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_duplex_mute_gates_capture_without_interrupting_or_restarting_playback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", AvailableDeviceAudio
        ):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_soft_mute_on",
                    "sentAt": "2026-07-04T00:02:10.000Z",
                    "value": True,
                }
            )
            await wait_for_mic_transition(connection)
            engine = connection.native_audio_engine
            self.assertIsNotNone(engine)
            connection.active_turn_id = 42

            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_soft_mute_off",
                    "sentAt": "2026-07-04T00:02:11.000Z",
                    "value": False,
                    "reason": "user.toggle",
                }
            )
            self.assertIs(connection.native_audio_engine, engine)
            self.assertFalse(engine.capture_enabled)
            self.assertEqual(connection.active_turn_id, 42)
            self.assertNotIn("interrupted", [message["type"] for message in websocket.sent])
            await connection.handle_flux_event(
                {
                    "type": "speech.end",
                    "transcript": "discard this muted audio",
                    "turn_index": 77,
                }
            )
            self.assertNotIn("transcript", [message["type"] for message in websocket.sent])
            self.assertEqual(connection.active_turn_id, 42)

            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_soft_mute_resume",
                    "sentAt": "2026-07-04T00:02:12.000Z",
                    "value": True,
                }
            )
            self.assertIs(connection.native_audio_engine, engine)
            self.assertTrue(engine.capture_enabled)
            self.assertEqual(websocket.sent[-1]["type"], "listening")
            connection.active_turn_id = None
            await connection.close()

    async def test_silent_mic_watchdog_reports_permission_issue_and_stops_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.NativeMicSession", FakeNativeMic
        ), patch("opencode_voice.server.DeepgramFluxSession", FakeFlux), patch(
            "opencode_voice.server.MIC_WATCHDOG_SEC", 0.01
        ), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", UnavailableDeviceAudio
        ):
            FakeNativeMic.ok = True
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_live_4",
                    "sentAt": "2026-07-04T00:03:00.000Z",
                    "value": True,
                }
            )
            await wait_for_mic_transition(connection)
            await asyncio.wait_for(connection.mic_watchdog_task, timeout=5)

            issue = websocket.sent[-1]
            self.assertEqual(issue["type"], "voice_bridge_issue")
            self.assertEqual(issue["diagnosticCode"], "mic_permission_needed")
            self.assertTrue(issue["retryable"])
            self.assertIsNone(connection.native_mic)
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_mic_start_failure_reports_issue_and_never_says_listening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.NativeMicSession", FakeNativeMic
        ), patch("opencode_voice.server.DeepgramFluxSession", FakeFlux), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", UnavailableDeviceAudio
        ):
            FakeNativeMic.ok = False
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_live_5",
                    "sentAt": "2026-07-04T00:04:00.000Z",
                    "value": True,
                }
            )
            await wait_for_mic_transition(connection)
            self.assertNotIn("listening", [message["type"] for message in websocket.sent])

    async def test_fast_mute_cancels_pending_device_start_without_stale_listening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", BlockingDeviceAudio
        ):
            BlockingDeviceAudio.started = asyncio.Event()
            BlockingDeviceAudio.release = asyncio.Event()
            BlockingDeviceAudio.instances = []
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_live_race_on",
                    "sentAt": "2026-07-04T00:05:00.000Z",
                    "value": True,
                }
            )
            await asyncio.wait_for(BlockingDeviceAudio.started.wait(), timeout=1)

            await connection.handle_control(
                {
                    "type": "live.set",
                    "clientEventId": "evt_live_race_off",
                    "sentAt": "2026-07-04T00:05:00.010Z",
                    "value": False,
                }
            )

            self.assertFalse(connection.mic_desired_live)
            self.assertIsNone(connection.native_audio_engine)
            self.assertTrue(BlockingDeviceAudio.instances[0].closed)
            self.assertNotIn("listening", [message["type"] for message in websocket.sent])
            self.assertIsNotNone(connection.flux, "mute should retain the prewarmed transport")
            await connection.close()

    async def test_slow_transport_does_not_block_fast_mute_or_publish_listening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramFluxSession", BlockingFlux
        ), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", AvailableDeviceAudio
        ):
            BlockingFlux.started = asyncio.Event()
            BlockingFlux.release = asyncio.Event()
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await asyncio.sleep(0)
            await asyncio.wait_for(BlockingFlux.started.wait(), timeout=1)

            await asyncio.wait_for(
                connection.handle_control(
                    {
                        "type": "live.set",
                        "clientEventId": "evt_slow_transport_on",
                        "sentAt": "2026-07-04T00:05:30.000Z",
                        "value": True,
                    }
                ),
                timeout=0.1,
            )
            await asyncio.wait_for(
                connection.handle_control(
                    {
                        "type": "live.set",
                        "clientEventId": "evt_slow_transport_off",
                        "sentAt": "2026-07-04T00:05:30.010Z",
                        "value": False,
                    }
                ),
                timeout=0.1,
            )
            BlockingFlux.release.set()
            self.assertTrue(await asyncio.wait_for(connection.ensure_audio_transport(), timeout=1))

            self.assertFalse(connection.mic_desired_live)
            self.assertIsNone(connection.native_audio_engine)
            self.assertNotIn("listening", [message["type"] for message in websocket.sent])
            await connection.close()

    async def test_start_prewarm_and_mute_reuse_one_flux_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.NativeMicSession", FakeNativeMic
        ), patch(
            "opencode_voice.server.DeepgramFluxSession", CountingFlux
        ), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", UnavailableDeviceAudio
        ):
            FakeNativeMic.ok = True
            CountingFlux.starts = 0
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await asyncio.sleep(0)
            self.assertTrue(await connection.ensure_audio_transport())
            self.assertEqual(CountingFlux.starts, 1)

            for suffix in ("first", "second"):
                await connection.handle_control(
                    {
                        "type": "live.set",
                        "clientEventId": f"evt_live_{suffix}_on",
                        "sentAt": "2026-07-04T00:06:00.000Z",
                        "value": True,
                    }
                )
                await wait_for_mic_transition(connection)
                await connection.handle_control(
                    {
                        "type": "live.set",
                        "clientEventId": f"evt_live_{suffix}_off",
                        "sentAt": "2026-07-04T00:06:01.000Z",
                        "value": False,
                    }
                )

            self.assertEqual(CountingFlux.starts, 1)
            records = [
                json.loads(line)
                for line in connection.logger.path.read_text(encoding="utf-8").splitlines()
            ]
            prewarm = next(record for record in records if record["event"] == "flux.prewarm")
            self.assertGreaterEqual(prewarm["latency_ms"], 0)
            mic_ready = [record for record in records if record["event"] == "native_audio.mic.ready"]
            self.assertEqual(len(mic_ready), 2)
            self.assertTrue(all(record["transport_prepared"] for record in mic_ready))
            await connection.close()


class SidepodLaneEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_and_unknown_engine_vocabulary_never_reaches_the_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _client = lane_connection(tmp)
            for internal in (
                {"type": "tokens", "context_tokens": 12000},
                {"type": "compaction.start", "session_id": "fork_1"},
                {"type": "fork.ready", "fork_session_id": "fork_1"},
                {"type": "audio.input", "status": "receiving"},
                {"type": "opencode.requested", "turn_id": 1},
                {"type": "speech.telemetry", "provider": "internal"},
            ):
                await connection.send_json(internal)
            self.assertEqual(websocket.sent, [])
            records = [
                json.loads(line)
                for line in connection.logger.path.read_text(encoding="utf-8").splitlines()
            ]
            unknown = [record for record in records if record["event"] == "sidepod.lane.unknown"]
            self.assertEqual([record["message_type"] for record in unknown], ["fork.ready", "audio.input", "speech.telemetry"])

    async def test_turn_errors_become_generic_issues_with_no_provider_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _client = lane_connection(tmp)
            await connection.send_json(
                {"type": "turn.error", "turn_id": 3, "message": "Deepgram socket sk-XYZ Inception mercury-2 blew up"}
            )
            await connection.send_json({"type": "error", "message": "Inception says no"})
            await connection.send_json({"type": "turn.timeout", "turn_id": 4})

            serialized = json.dumps(websocket.sent)
            for forbidden in ("Deepgram", "Inception", "mercury", "sk-XYZ", "blew up"):
                self.assertNotIn(forbidden, serialized)
            self.assertEqual(
                [message["diagnosticCode"] for message in websocket.sent],
                ["turn_failed", "engine_error", "turn_timeout"],
            )
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_invalid_inbound_command_answers_protocol_invalid_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control({"type": "live.set", "clientEventId": "evt_bad"})
            self.assertEqual(websocket.sent[-1]["diagnosticCode"], "protocol_invalid_message")

            websocket.sent.clear()
            await connection.handle_control(
                {"type": "mystery.command", "clientEventId": "evt_odd", "sentAt": "2026-07-04T00:05:00.000Z"}
            )
            self.assertEqual(websocket.sent, [], "unknown command types are logged and ignored")

    async def test_barge_in_command_interrupts_with_schema_valid_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            connection.active_turn_id = 7
            connection.protocol_turn_id = "turn_0007"
            await connection.handle_control(
                {
                    "type": "barge_in",
                    "clientEventId": "evt_barge",
                    "sentAt": "2026-07-04T00:06:00.000Z",
                    "reason": "user.mute",
                }
            )
            interrupted = websocket.sent[-1]
            self.assertEqual(interrupted["type"], "interrupted")
            self.assertEqual(interrupted["reason"], "user.mute")
            self.assertEqual(client.aborted, ["fork_1"])
            assert_all_lane_messages_valid(self, websocket.sent)


class FakeEchoCanceller:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.captured: list[bytes] = []
        self.rendered: list[bytes] = []
        self.delays: list[int] = []
        self.delay_error: str | None = None

    def process_capture(self, data: bytes) -> bytes:
        self.captured.append(data)
        return b"\x7f" * len(data)  # marker: processing happened

    def process_render(self, data: bytes) -> None:
        self.rendered.append(data)

    def set_stream_delay_ms(self, delay_ms: int) -> None:
        self.delays.append(delay_ms)


class SidepodEchoProtectionTests(unittest.IsolatedAsyncioTestCase):
    """Mortic must never hear itself: AEC when available, silence gate otherwise."""

    async def test_aec_mode_runs_mic_frames_through_the_canceller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ), patch("opencode_voice.server.NativeMicSession", FakeNativeMic), patch(
            "opencode_voice.server.EchoCanceller", FakeEchoCanceller
        ), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", AvailableDeviceAudio
        ):
            connection, _websocket, _client = lane_connection(
                tmp, voice_duplex="auto", device_sample_rate=16_000
            )
            self.assertTrue(await connection.start_native_audio())
            self.assertIsInstance(connection.echo_canceller, FakeEchoCanceller)

            await connection.handle_native_audio(b"\x11" * 640)

            self.assertEqual(connection.flux.audio, [b"\x7f" * 640])
            self.assertEqual(connection.echo_canceller.captured, [b"\x11" * 640])

    async def test_timed_aec_does_not_mute_stt_at_playback_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ), patch("opencode_voice.server.NativeMicSession", FakeNativeMic), patch(
            "opencode_voice.server.EchoCanceller", FakeEchoCanceller
        ), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", AvailableDeviceAudio
        ):
            connection, _websocket, _client = lane_connection(
                tmp, voice_duplex="auto", device_sample_rate=16_000
            )
            self.assertTrue(await connection.start_native_audio())
            speaker = FakeNativeSpeaker(None, None, None)
            speaker.audible = True
            speaker.startup = True
            connection.native_speaker = speaker

            await connection.handle_native_audio(b"\x11" * 640)
            speaker.startup = False
            await connection.handle_native_audio(b"\x22" * 640)

            self.assertEqual(connection.flux.audio, [b"\x7f" * 640, b"\x7f" * 640])
            self.assertEqual(connection.echo_canceller.captured, [b"\x11" * 640, b"\x22" * 640])

    async def test_half_duplex_gate_feeds_silence_while_tts_is_audible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ), patch("opencode_voice.server.NativeMicSession", FakeNativeMic), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", AvailableDeviceAudio
        ):
            connection, _websocket, _client = lane_connection(
                tmp, voice_duplex="half", device_sample_rate=16_000
            )
            self.assertTrue(await connection.start_native_audio())
            self.assertIsNone(connection.echo_canceller)
            speaker = FakeNativeSpeaker(None, None, None)
            speaker.audible = True
            connection.native_speaker = speaker

            await connection.handle_native_audio(b"\x11" * 640)
            speaker.audible = False
            await connection.handle_native_audio(b"\x22" * 640)

            self.assertEqual(connection.flux.audio, [b"\x00" * 640, b"\x22" * 640])

    async def test_full_duplex_passes_raw_frames_even_while_speaking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ), patch("opencode_voice.server.NativeMicSession", FakeNativeMic), patch(
            "opencode_voice.server.PersistentDeviceAudioEngine", AvailableDeviceAudio
        ):
            connection, _websocket, _client = lane_connection(
                tmp, voice_duplex="full", device_sample_rate=16_000
            )
            self.assertTrue(await connection.start_native_audio())
            speaker = FakeNativeSpeaker(None, None, None)
            speaker.audible = True
            connection.native_speaker = speaker

            await connection.handle_native_audio(b"\x11" * 640)

            self.assertEqual(connection.flux.audio, [b"\x11" * 640])

    async def test_gated_speech_start_does_not_interrupt_the_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, client = lane_connection(tmp, voice_duplex="half")
            await connection.handle_control(START_PAYLOAD)
            speaker = FakeNativeSpeaker(None, None, None)
            speaker.audible = True
            connection.native_speaker = speaker
            connection.active_turn_id = 7

            identity = {"flux_connection_epoch": 1, "turn_index": 33}
            await connection.handle_flux_event({"type": "speech.start", **identity})

            self.assertEqual(connection.active_turn_id, 7)
            self.assertEqual(client.aborted, [])
            self.assertNotIn("interrupted", [message["type"] for message in websocket.sent])

            # The same gated episode can finalize after the audible tail has
            # ended. It remains suppressed rather than entering QUIET recovery
            # as a phantom user turn.
            speaker.audible = False
            connection.active_turn_id = None
            await connection.handle_flux_event(
                {
                    "type": "speech.end",
                    "transcript": "echo residue",
                    "eager": False,
                    **identity,
                }
            )
            await asyncio.sleep(0)
            self.assertIsNone(connection.turn_task)
            self.assertNotIn("transcript", [message["type"] for message in websocket.sent])
            self.assertNotIn("thinking", [message["type"] for message in websocket.sent])

    async def test_episode_tombstones_are_bounded_and_reset_by_flux_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, _websocket, _client = lane_connection(tmp)
            for turn_index in range(300):
                connection.expire_interruption_episode(
                    EpisodeIdentity(1, turn_index, f"episode-{turn_index}", 0)
                )
            self.assertEqual(len(connection.expired_interruption_episodes), 256)
            self.assertEqual(len(connection.expired_interruption_episode_order), 256)

            connection.adopt_flux_connection_epoch(2)
            self.assertEqual(connection.flux_connection_epoch, 2)
            self.assertEqual(connection.expired_interruption_episodes, set())
            self.assertEqual(len(connection.expired_interruption_episode_order), 0)

    async def test_stale_tts_audio_after_barge_in_cannot_resurrect_the_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.speak("Hello there.", turn_id=1)
            stale_speaker = connection.speaker
            stale_token = connection.tts_turn_token
            self.assertIsNotNone(connection.native_speaker)

            await connection.barge_in("user.mute")
            # The device stream survives the interrupt (echo-canceller
            # convergence), but its queued audio is flushed.
            native = connection.native_speaker
            self.assertIsNotNone(native)
            self.assertEqual(native.played, [])

            # Audio still streaming from the barged-in TTS socket must be
            # dropped by the generation guard, never played.
            await stale_speaker.on_audio(stale_token, b"\x01\x02")
            self.assertEqual(native.played, [])
            self.assertEqual(connection.stale_tts_chunks, 1)

    async def test_interruption_uses_provider_native_cancel_and_keeps_prewarm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.speak("Hello there.", turn_id=1)
            speaker = connection.speaker
            token = connection.tts_turn_token
            assert token is not None
            connection.arm_tts_terminal_watchdog(token)
            watchdog = connection.tts_terminal_watchdogs[token]

            await connection.barge_in("user.mute")
            await asyncio.sleep(0)

            self.assertIs(connection.speaker, speaker)
            self.assertEqual(speaker.cancelled, [(token, "user.mute")])
            self.assertIsNone(connection.tts_turn_token)
            self.assertTrue(watchdog.cancelled())
            self.assertEqual(connection.tts_terminal_tokens[token], "cancelled")

    async def test_provider_cancel_cannot_delay_protocol_interrupted(self) -> None:
        class SlowCancelSpeaker(FakeSpeakSession):
            release_cancel = asyncio.Event()

            async def cancel_turn(self, token: PlaybackToken, reason: str) -> None:
                await self.release_cancel.wait()
                await super().cancel_turn(token, reason)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", SlowCancelSpeaker
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.speak("Hello there.", turn_id=1)

            await asyncio.wait_for(connection.barge_in("user.mute"), timeout=0.05)

            self.assertIn("interrupted", [message["type"] for message in websocket.sent])
            SlowCancelSpeaker.release_cancel.set()
            await asyncio.sleep(0)


class FluxCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_start_interim_cannot_create_lane_text_or_stale_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            identity = {"flux_connection_epoch": 1, "turn_index": 4}

            await connection.handle_flux_event(
                {"type": "speech.transcript", "transcript": "pre start", **identity}
            )

            self.assertEqual([message for message in websocket.sent if message["type"] == "transcript"], [])
            self.assertEqual(connection.pending_latency, {})
            self.assertIsNone(connection.pending_turn_id)

            await connection.handle_flux_event({"type": "speech.start", **identity})
            await connection.handle_flux_event(
                {"type": "speech.transcript", "transcript": "real words", **identity}
            )
            transcripts = [message for message in websocket.sent if message["type"] == "transcript"]
            self.assertEqual(len(transcripts), 1)
            self.assertEqual(transcripts[0]["text"], "real words")
            self.assertLess(connection.pending_latency["firstTranscriptMs"], 100)

    async def test_identical_flux_updates_are_deduplicated_but_final_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            identity = {"flux_connection_epoch": 1, "turn_index": 4}

            await connection.handle_flux_event({"type": "speech.start", **identity})
            for _ in range(4):
                await connection.handle_flux_event(
                    {"type": "speech.transcript", "transcript": "same words", **identity}
                )
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "same words", "eager": False, **identity}
            )

            transcripts = [message for message in websocket.sent if message["type"] == "transcript"]
            self.assertEqual([message["final"] for message in transcripts], [False, True])
            self.assertEqual([message["text"] for message in transcripts], ["same words", "same words"])
            self.assertLess(transcripts[0]["sequence"], transcripts[1]["sequence"])

    async def test_eager_eot_and_turn_resumed_have_no_runtime_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            episode = {"flux_connection_epoch": 1, "turn_index": 9}
            await connection.handle_flux_event({"type": "speech.start", **episode})
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "so what", "eager": True, **episode}
            )
            await connection.handle_flux_event({"type": "speech.resumed", **episode})

            self.assertEqual(started, [])
            self.assertEqual(client.aborted, [])
            self.assertNotIn("interrupted", [message["type"] for message in websocket.sent])

            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "so what exactly", "eager": False, **episode}
            )
            await asyncio.sleep(0)
            self.assertEqual(started, ["so what exactly"])


class PlaybackDrainTests(unittest.IsolatedAsyncioTestCase):
    """When a reply finishes speaking the lane returns to a listening state so
    the viewer's activity indicator stops reading 'speaking'."""

    async def test_drain_emits_listening_when_mic_live_and_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            connection.native_mic = object()  # type: ignore[assignment]  # mic live
            connection.active_turn_id = None
            websocket.sent.clear()

            await connection.on_playback_drained()

            listening = [m for m in websocket.sent if m["type"] == "listening"]
            self.assertEqual(len(listening), 1)
            assert_all_lane_messages_valid(self, websocket.sent)

    async def test_drain_is_silent_when_a_new_turn_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            connection.native_mic = object()  # type: ignore[assignment]
            connection.active_turn_id = 9  # a turn took over before playback drained
            websocket.sent.clear()

            await connection.on_playback_drained()

            self.assertEqual([m for m in websocket.sent if m["type"] == "listening"], [])

    async def test_drain_is_silent_when_mic_muted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            connection.native_mic = None  # muted
            connection.active_turn_id = None
            websocket.sent.clear()

            await connection.on_playback_drained()

            self.assertEqual([m for m in websocket.sent if m["type"] == "listening"], [])


class InterruptionControllerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_owned_pre_pcm_generation_is_interruption_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp, voice_duplex="full")
            await connection.handle_control(START_PAYLOAD)
            token = PlaybackToken(connection.speak_generation, 7)
            connection.tts_turn_token = token
            connection.turn_playback_tokens[token.turn_id] = token
            connection.active_turn_id = None

            await connection.handle_flux_event(
                {"type": "speech.start", "flux_connection_epoch": 1, "turn_index": 51}
            )

            self.assertEqual(connection.interruption_state.phase.value, "candidate")
            await connection.close()

    async def test_starved_nonterminal_generation_is_still_interruption_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp, voice_duplex="full")
            await connection.handle_control(START_PAYLOAD)
            engine = StarvedDeviceAudio()
            connection.native_audio_engine = engine  # type: ignore[assignment]
            connection.active_turn_id = None

            await connection.handle_flux_event(
                {"type": "speech.start", "flux_connection_epoch": 1, "turn_index": 52}
            )

            self.assertEqual(connection.interruption_state.phase.value, "candidate")
            self.assertTrue(engine.ducked)
            await connection.close()

    async def test_final_before_decision_commits_and_admits_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, _client = lane_connection(tmp, voice_duplex="full")
            await connection.handle_control(START_PAYLOAD)
            speaker = FakeNativeSpeaker(None, None, None)
            speaker.audible = True
            connection.native_speaker = speaker
            connection.active_turn_id = 7
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            identity = {"flux_connection_epoch": 1, "turn_index": 5}
            await connection.handle_flux_event({"type": "speech.start", **identity})
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "hello", "eager": False, **identity}
            )
            episode = connection.interruption_state.episode
            onset = connection.interruption_state.started_at_ms
            assert episode is not None and onset is not None

            await connection.reduce_interruption_event(
                InterruptionEvent.evaluate(episode, onset + 500, correlation=0.2)
            )
            await asyncio.sleep(0)

            self.assertEqual(started, ["hello"])
            self.assertEqual(
                [message["type"] for message in websocket.sent].count("interrupted"), 1
            )
            self.assertEqual(connection.interruption_state.phase.value, "quiet")

    async def test_echo_episode_ducks_once_then_owns_restart_and_turn_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, client = lane_connection(tmp, voice_duplex="full")
            await connection.handle_control(START_PAYLOAD)
            speaker = FakeNativeSpeaker(None, None, None)
            speaker.audible = True
            connection.native_speaker = speaker
            connection.active_turn_id = 7
            connection.interruption_echo_correlation = lambda: 0.91  # type: ignore[method-assign]

            first = {"flux_connection_epoch": 1, "turn_index": 40}
            await connection.handle_flux_event({"type": "speech.start", **first})
            self.assertTrue(speaker.ducked)
            await asyncio.sleep(0.55)
            self.assertFalse(speaker.ducked)
            self.assertEqual(connection.interruption_state.phase.value, "suppressed")

            restart = {"flux_connection_epoch": 1, "turn_index": 41}
            await connection.handle_flux_event({"type": "speech.start", **restart})
            await connection.handle_flux_event({"type": "speech.resumed", **restart})

            self.assertFalse(speaker.ducked)
            self.assertEqual(connection.active_turn_id, 7)
            self.assertEqual(client.aborted, [])
            self.assertNotIn("interrupted", [message["type"] for message in websocket.sent])

    async def test_priority_interruption_commits_exactly_once_and_admits_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, client = lane_connection(tmp, voice_duplex="full")
            await connection.handle_control(START_PAYLOAD)
            speaker = FakeNativeSpeaker(None, None, None)
            speaker.audible = True
            connection.native_speaker = speaker
            connection.active_turn_id = 7
            connection.interruption_echo_correlation = lambda: 0.1  # type: ignore[method-assign]
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            identity = {"flux_connection_epoch": 1, "turn_index": 3}
            await connection.handle_flux_event({"type": "speech.start", **identity})
            await connection.handle_flux_event(
                {"type": "speech.transcript", "transcript": "wait please", **identity}
            )
            await connection.handle_flux_event(
                {"type": "speech.transcript", "transcript": "wait please", **identity}
            )
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "wait please", "eager": False, **identity}
            )
            await asyncio.sleep(0)

            self.assertEqual(client.aborted, ["fork_1"])
            self.assertEqual(
                [message["type"] for message in websocket.sent].count("interrupted"), 1
            )
            self.assertEqual(started, ["wait please"])

    async def test_final_without_observed_start_is_recovered_as_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "Yes.", "eager": False, "turn_index": 1}
            )
            await asyncio.sleep(0)
            self.assertEqual(started, ["Yes."])

    async def test_expired_episode_final_cannot_resurrect_a_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp, voice_duplex="full")
            await connection.handle_control(START_PAYLOAD)
            speaker = FakeNativeSpeaker(None, None, None)
            speaker.audible = True
            connection.native_speaker = speaker
            connection.active_turn_id = 7
            connection.interruption_echo_correlation = lambda: 0.95  # type: ignore[method-assign]
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            identity = {"flux_connection_epoch": 1, "turn_index": 21}
            await connection.handle_flux_event({"type": "speech.start", **identity})
            started_at = connection.interruption_state.started_at_ms
            self.assertIsNotNone(started_at)
            assert started_at is not None
            await connection.reduce_interruption_event(InterruptionEvent.tick(started_at + 500))
            active_at = connection.interruption_state.last_provider_activity_ms
            self.assertIsNotNone(active_at)
            assert active_at is not None
            await connection.reduce_interruption_event(InterruptionEvent.tick(active_at + 2_000))

            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "late echo", "eager": False, **identity}
            )
            await asyncio.sleep(0)
            self.assertEqual(started, [])


class SilentCompletionLoggingTests(unittest.IsolatedAsyncioTestCase):
    """A completed turn with real reply text but zero speak() calls (e.g. an
    all-code, no-prose reply the speech filter strips to nothing) finishes
    normally and silently — indistinguishable from a hang unless it's
    logged. No behavior change: still no audio, just a diagnostic event."""

    async def test_speak_marks_the_turn_as_having_produced_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramTTSProvider", FakeSpeakSession
        ):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            self.assertFalse(connection.turn_spoken_any)

            await connection.speak("Hello there.", turn_id=1)

            self.assertTrue(connection.turn_spoken_any)

    async def test_silent_completion_with_real_text_logs_a_diagnostic_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)

            connection.turn_spoken_any = False
            connection.log_if_silent_completion(9, "```python\ndef foo():\n    pass\n```")

            log_lines = connection.logger.path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in log_lines]
            silent = [e for e in events if e["event"] == "tts.no_speakable_text"]
            self.assertEqual(len(silent), 1)
            self.assertEqual(silent[0]["turn_id"], 9)

    async def test_normal_completion_that_spoke_does_not_log_the_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)

            connection.turn_spoken_any = True
            connection.log_if_silent_completion(9, "A normal spoken reply.")

            log_lines = connection.logger.path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line)["event"] for line in log_lines]
            self.assertNotIn("tts.no_speakable_text", events)

    async def test_empty_completion_does_not_log_the_diagnostic(self) -> None:
        # A genuinely empty reply is a different (already-handled) case, not
        # speech-filtered-to-nothing — don't conflate the two in the logs.
        with tempfile.TemporaryDirectory() as tmp:
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)

            connection.turn_spoken_any = False
            connection.log_if_silent_completion(9, "")

            log_lines = connection.logger.path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line)["event"] for line in log_lines]
            self.assertNotIn("tts.no_speakable_text", events)


class SidepodCompactionWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_compaction_blocks_turn_before_model_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = CompactionLaneClient()
            client.summary_error = True
            connection, websocket, _client = lane_connection(
                tmp,
                client=client,
                response_mode="structured",
            )
            await connection.handle_control(START_PAYLOAD)
            if connection.compaction_task:
                await connection.compaction_task

            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "Question after failed compaction."}
            )
            assert connection.turn_task is not None
            await asyncio.wait_for(connection.turn_task, timeout=3)

            self.assertEqual(client.prompts, [])
            self.assertIsNone(connection.active_turn_id)
            self.assertNotIn("assistant.delta", [message["type"] for message in websocket.sent])
            logs = connection.logger.path.read_text(encoding="utf-8")
            self.assertIn("turn.preflight.blocked", logs)
            self.assertNotIn('"event": "turn.start"', logs)
            await connection.close()

    async def test_start_kicks_a_context_check_without_leaking_tokens_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            if connection.compaction_task:
                await connection.compaction_task

            self.assertEqual([message["type"] for message in websocket.sent], ["ready"])
            log_lines = connection.logger.path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line)["event"] for line in log_lines]
            self.assertIn("tokens.check", events)

    async def test_concurrent_triggers_share_one_summarize_and_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = CompactionLaneClient()
            client.summarize_release.clear()
            connection, _websocket, _client = lane_connection(tmp, client=client)
            connection.fork_session_id = "fork_1"

            first = asyncio.create_task(
                connection.maybe_start_compaction("speech_confirmed", run_in_background=False)
            )
            await client.summarize_started.wait()
            second = asyncio.create_task(
                connection.maybe_start_compaction("turn_complete", run_in_background=False)
            )
            client.summarize_release.set()
            first_result, second_result = await asyncio.gather(first, second)

            self.assertEqual(client.summarize_calls, 1)
            self.assertIs(first_result, second_result)
            self.assertTrue(first_result and first_result.completed)
            await connection.close()

    async def test_post_compaction_growth_guard_blocks_immediate_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = CompactionLaneClient()
            connection, _websocket, _client = lane_connection(tmp, client=client)
            connection.fork_session_id = "fork_1"

            first = await connection.maybe_start_compaction("first", run_in_background=False)
            second = await connection.maybe_start_compaction("second", run_in_background=False)

            self.assertTrue(first and first.completed)
            self.assertIsNone(second)
            self.assertEqual(client.summarize_calls, 1)
            self.assertEqual(compaction_growth_required(70_000), 7_000)
            logs = connection.logger.path.read_text(encoding="utf-8")
            self.assertIn("compaction.suppressed", logs)
            await connection.close()

    async def test_compaction_requires_event_and_validated_new_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = CompactionLaneClient()
            connection, _websocket, _client = lane_connection(tmp, client=client)
            connection.fork_session_id = "fork_1"

            outcome = await connection.maybe_start_compaction("test", run_in_background=False)

            self.assertTrue(outcome and outcome.completed)
            self.assertEqual(outcome.summary_message_id, "msg_summary_1")
            logs = connection.logger.path.read_text(encoding="utf-8")
            self.assertIn('"confirmation": "session.compacted"', logs)
            await connection.close()

    async def test_compaction_summary_error_cannot_be_reported_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = CompactionLaneClient()
            client.summary_error = True
            connection, _websocket, _client = lane_connection(tmp, client=client)
            connection.fork_session_id = "fork_1"

            outcome = await connection.maybe_start_compaction("test", run_in_background=False)

            self.assertTrue(outcome and not outcome.completed)
            logs = connection.logger.path.read_text(encoding="utf-8")
            self.assertIn('"event": "compaction.error"', logs)
            self.assertIn('"error_code": "summary_error"', logs)
            self.assertNotIn('"event": "compaction.complete"', logs)
            await connection.close()

    async def test_compaction_idle_fallback_still_validates_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = CompactionLaneClient()
            client.emit_compacted = False
            connection, _websocket, _client = lane_connection(
                tmp,
                client=client,
                compaction_wait_sec=0.01,
            )
            connection.fork_session_id = "fork_1"

            outcome = await connection.maybe_start_compaction("test", run_in_background=False)

            self.assertTrue(outcome and outcome.completed)
            self.assertEqual(client.wait_for_idle_calls, 1)
            logs = connection.logger.path.read_text(encoding="utf-8")
            self.assertIn("compaction.confirmation.fallback", logs)
            await connection.close()

    async def test_overflow_recovery_forks_before_persisted_failed_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = OverflowRecoveryClient()
            connection, _websocket, _client = lane_connection(tmp, client=client)
            connection.fork_session_id = "fork_failed"

            recovered = await connection.recover_overflow_fork("fork_failed", {"msg_before"})

            self.assertEqual(recovered, "fork_1")
            self.assertEqual(client.cutoffs, [("fork_failed", "msg_failed_user")])
            self.assertEqual(connection.fork_session_id, "fork_1")
            self.assertIn("fork_failed", client.deleted)
            await connection.close()


if __name__ == "__main__":
    unittest.main()
