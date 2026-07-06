"""Protocol v0 lane tests for SidepodConnection.

These drive the connection object directly with fake transport/client/audio so
the full voice turn (STT transcript -> fork turn -> TTS -> complete) can be
machine-verified without devices, keys, or network. Every outbound assertion
runs through opencode_voice.protocol.check_event, so these tests double as
runtime-contract conformance proof for the engine side.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from opencode_voice.config import VoiceConfig
from opencode_voice.logging import RunLogger
from opencode_voice.protocol import check_event
from opencode_voice.server import SIDEPOD_PROTOCOL_VERSION, SidepodConnection
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

    async def fork_session(self, session_id: str) -> dict[str, str]:
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


class FakeSpeakSession:
    """Stands in for the streaming TTS socket: first speak() reports audio."""

    def __init__(self, config: Any, on_audio: Any, on_event: Any) -> None:
        self.on_audio = on_audio
        self.spoken: list[str] = []

    async def start(self) -> None:
        return None

    async def speak(self, text: str, turn_id: int) -> None:
        self.spoken.append(text)
        if len(self.spoken) == 1:
            await self.on_audio(b"\x00\x00", turn_id)

    async def close(self) -> None:
        return None


class FakeCartesiaSpeakSession(FakeSpeakSession):
    """Distinct type from FakeSpeakSession so provider-selection tests can
    prove build_speaker() picked the right class, not just *a* class."""


class FakeNativeSpeaker:
    def __init__(self, config: Any, logger: Any, on_issue: Any, on_render: Any = None, on_drain: Any = None) -> None:
        self.played: list[bytes] = []
        self.on_render = on_render
        self.on_drain = on_drain
        self.audible = False
        self.silent_for: float | None = None  # seconds since playback ended
        self.startup = False  # inside the post-start convergence window
        self.paused = False

    async def start(self) -> bool:
        return True

    async def play(self, data: bytes, turn_id: Any) -> bool:
        self.played.append(data)
        if self.on_render:
            self.on_render(data)
        return True

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
        ),
        client=fake_client,  # type: ignore[arg-type]
        logger=RunLogger(root=tmp),
        websocket=websocket,  # type: ignore[arg-type]
        **kwargs,
    )
    return connection, websocket, fake_client


def assert_all_lane_messages_valid(test: unittest.TestCase, sent: list[dict[str, Any]]) -> None:
    for message in sent:
        check = check_event(message)
        test.assertTrue(check.ok, f"{message.get('type')}: {check.errors}")


class SidepodLaneTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_voice_turn_emits_the_v0_sequence_with_real_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramSpeakSession", FakeSpeakSession
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
            "opencode_voice.server.DeepgramSpeakSession", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            for phrase in ("First ask.", "Second ask."):
                await connection.handle_flux_event({"type": "speech.start"})
                await connection.handle_flux_event({"type": "speech.end", "transcript": phrase})
                await asyncio.wait_for(connection.turn_task, timeout=10)

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
            "opencode_voice.server.DeepgramSpeakSession", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ):
            connection, websocket, _client = lane_connection(tmp, client=IdleFirstLaneClient())
            await connection.handle_control(START_PAYLOAD)
            await connection.handle_flux_event({"type": "speech.end", "transcript": "Tighten the summary."})
            await asyncio.wait_for(connection.turn_task, timeout=10)

            complete = next(m for m in websocket.sent if m["type"] == "complete")
            self.assertEqual(complete["streamSource"], "event")
            deltas = [m for m in websocket.sent if m["type"] == "assistant.delta"]
            self.assertTrue(deltas, "the trailing text must still stream")
            assert_all_lane_messages_valid(self, websocket.sent)


class TTSProviderSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_speaker_picks_the_configured_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramSpeakSession", FakeSpeakSession
        ), patch("opencode_voice.server.CartesiaSpeakSession", FakeCartesiaSpeakSession):
            deepgram_connection, _websocket, _client = lane_connection(tmp, tts_provider="deepgram")
            cartesia_connection, _websocket2, _client2 = lane_connection(tmp, tts_provider="cartesia")

            self.assertIs(type(deepgram_connection.build_speaker()), FakeSpeakSession)
            self.assertIs(type(cartesia_connection.build_speaker()), FakeCartesiaSpeakSession)

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
        ), patch("opencode_voice.server.DeepgramFluxSession", FakeFlux):
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

    async def test_silent_mic_watchdog_reports_permission_issue_and_stops_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.NativeMicSession", FakeNativeMic
        ), patch("opencode_voice.server.DeepgramFluxSession", FakeFlux), patch(
            "opencode_voice.server.MIC_WATCHDOG_SEC", 0.01
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
        ), patch("opencode_voice.server.DeepgramFluxSession", FakeFlux):
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
            self.assertNotIn("listening", [message["type"] for message in websocket.sent])


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
        ):
            connection, _websocket, _client = lane_connection(tmp, voice_duplex="auto")
            self.assertTrue(await connection.start_native_audio())
            self.assertIsInstance(connection.echo_canceller, FakeEchoCanceller)

            await connection.handle_native_audio(b"\x11" * 640)

            self.assertEqual(connection.flux.audio, [b"\x7f" * 640])
            self.assertEqual(connection.echo_canceller.captured, [b"\x11" * 640])

    async def test_startup_mute_window_feeds_stt_silence_but_canceller_real_audio(self) -> None:
        # First ~0.6s of each playback burst: the canceller is converging and
        # leaks echo, so STT hears silence while the canceller keeps adapting
        # on the real frames.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ), patch("opencode_voice.server.NativeMicSession", FakeNativeMic), patch(
            "opencode_voice.server.EchoCanceller", FakeEchoCanceller
        ):
            connection, _websocket, _client = lane_connection(tmp, voice_duplex="auto")
            self.assertTrue(await connection.start_native_audio())
            speaker = FakeNativeSpeaker(None, None, None)
            speaker.audible = True
            speaker.startup = True
            connection.native_speaker = speaker

            await connection.handle_native_audio(b"\x11" * 640)
            speaker.startup = False
            await connection.handle_native_audio(b"\x22" * 640)

            self.assertEqual(connection.flux.audio, [b"\x00" * 640, b"\x7f" * 640])
            self.assertEqual(connection.echo_canceller.captured, [b"\x11" * 640, b"\x22" * 640])

    async def test_half_duplex_gate_feeds_silence_while_tts_is_audible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramFluxSession", FakeFlux
        ), patch("opencode_voice.server.NativeMicSession", FakeNativeMic):
            connection, _websocket, _client = lane_connection(tmp, voice_duplex="half")
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
        ), patch("opencode_voice.server.NativeMicSession", FakeNativeMic):
            connection, _websocket, _client = lane_connection(tmp, voice_duplex="full")
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

            await connection.handle_flux_event({"type": "speech.start"})

            self.assertEqual(connection.active_turn_id, 7)
            self.assertEqual(client.aborted, [])
            self.assertNotIn("interrupted", [message["type"] for message in websocket.sent])

    async def test_stale_tts_audio_after_barge_in_cannot_resurrect_the_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramSpeakSession", FakeSpeakSession
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.speak("Hello there.", turn_id=1)
            stale_speaker = connection.speaker
            self.assertIsNotNone(connection.native_speaker)

            await connection.barge_in("user.mute")
            # The device stream survives the interrupt (echo-canceller
            # convergence), but its queued audio is flushed.
            native = connection.native_speaker
            self.assertIsNotNone(native)
            self.assertEqual(native.played, [])

            # Audio still streaming from the barged-in TTS socket must be
            # dropped by the generation guard, never played.
            await stale_speaker.on_audio(b"\x01\x02", 1)
            self.assertEqual(native.played, [])
            self.assertEqual(connection.stale_tts_chunks, 1)

    async def test_background_speaker_close_is_retained_and_runs_to_completion(self) -> None:
        # The detached TTS-socket close must keep a strong task reference or
        # the loop can garbage-collect it mid-run, leaking the Deepgram socket.
        class SlowCloseSpeaker(FakeSpeakSession):
            def __init__(self, config: Any, on_audio: Any, on_event: Any) -> None:
                super().__init__(config, on_audio, on_event)
                self.gate = asyncio.Event()
                self.closed = False

            async def close(self) -> None:
                await self.gate.wait()
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramSpeakSession", SlowCloseSpeaker
        ), patch("opencode_voice.server.NativeSpeakerSession", FakeNativeSpeaker):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            await connection.speak("Hello there.", turn_id=1)
            speaker = connection.speaker

            await connection.barge_in("user.mute")
            # Close is still pending (gated) and must be tracked, not orphaned.
            self.assertEqual(len(connection.background_tasks), 1)
            self.assertFalse(speaker.closed)

            speaker.gate.set()
            await asyncio.wait_for(asyncio.gather(*connection.background_tasks), timeout=5)
            self.assertTrue(speaker.closed)
            self.assertEqual(connection.background_tasks, set())


class EagerTurnConfirmTests(unittest.IsolatedAsyncioTestCase):
    """Flux fires an eager end-of-turn and then a confirming final one for the
    same utterance milliseconds later; the final must confirm the running
    turn, not restart it (24 new_turn aborts per session before this)."""

    def hold_turns(self, connection: SidepodConnection) -> tuple[list[str], asyncio.Event]:
        started: list[str] = []
        release = asyncio.Event()

        async def fake_turn(text: str, source: str, eager: bool) -> None:
            started.append(text)
            await release.wait()

        connection.run_text_turn = fake_turn  # type: ignore[method-assign]
        return started, release

    async def test_final_speech_end_with_same_transcript_confirms_the_eager_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            started, release = self.hold_turns(connection)

            await connection.handle_flux_event({"type": "speech.end", "transcript": "what time is it", "eager": True})
            await asyncio.sleep(0)
            await connection.handle_flux_event({"type": "speech.end", "transcript": "what time is it", "eager": False})
            await asyncio.sleep(0)

            self.assertEqual(started, ["what time is it"])
            self.assertEqual(client.aborted, [])
            release.set()

    async def test_final_speech_end_with_longer_transcript_restarts_the_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            started, release = self.hold_turns(connection)

            await connection.handle_flux_event({"type": "speech.end", "transcript": "what time", "eager": True})
            await asyncio.sleep(0)
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "what time is it in tokyo", "eager": False}
            )
            await asyncio.sleep(0)

            self.assertEqual(started, ["what time", "what time is it in tokyo"])
            release.set()

    async def test_speech_resumed_still_cancels_the_eager_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            started, release = self.hold_turns(connection)

            await connection.handle_flux_event({"type": "speech.end", "transcript": "so what", "eager": True})
            await asyncio.sleep(0)
            connection.active_turn_id = 1
            await connection.handle_flux_event({"type": "speech.resumed"})
            # The eager prompt must not be treated as confirmable afterwards.
            self.assertIsNone(connection.eager_turn_text)
            await connection.handle_flux_event({"type": "speech.end", "transcript": "so what", "eager": False})
            await asyncio.sleep(0)

            self.assertEqual(started, ["so what", "so what"])
            release.set()


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


class PendingBargeInTests(unittest.IsolatedAsyncioTestCase):
    """speech.start during audible playback pauses instead of killing the
    turn; the transcript decides interrupt vs false alarm."""

    def audible_speaker(self, connection: SidepodConnection) -> FakeNativeSpeaker:
        # A canceller must be present or duplex "auto" resolves to the
        # half-duplex gate, which swallows speech events while audible.
        connection.echo_canceller = FakeEchoCanceller(16_000)  # type: ignore[assignment]
        speaker = FakeNativeSpeaker(None, None, None)
        speaker.audible = True
        connection.native_speaker = speaker
        return speaker

    async def test_speech_start_during_playback_pauses_without_aborting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7

            await connection.handle_flux_event({"type": "speech.start"})

            self.assertTrue(speaker.paused)
            self.assertTrue(connection.barge_pending)
            self.assertEqual(connection.active_turn_id, 7)
            self.assertEqual(client.aborted, [])
            self.assertNotIn("interrupted", [message["type"] for message in websocket.sent])
            connection.clear_pending_barge_in()

    async def test_tiny_transcript_resumes_playback_and_never_becomes_a_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            await connection.handle_flux_event({"type": "speech.end", "transcript": "uh", "eager": False})
            await asyncio.sleep(0)

            self.assertFalse(speaker.paused)
            self.assertFalse(connection.barge_pending)
            self.assertEqual(connection.active_turn_id, 7)
            self.assertEqual(started, [])
            self.assertEqual(client.aborted, [])

    async def test_real_transcript_commits_the_interrupt_and_starts_the_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            self.audible_speaker(connection)
            connection.active_turn_id = 7
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0  # the user has been talking a while
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "wait, try the other file", "eager": False}
            )
            await asyncio.sleep(0)

            self.assertIsNone(connection.active_turn_id)
            self.assertEqual(len(client.aborted), 1)
            self.assertEqual(started, ["wait, try the other file"])
            self.assertFalse(connection.barge_pending)

    async def test_transcript_of_the_assistants_own_words_is_echo_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.spoken_text_recent = "Sure, I can walk you through the config file changes now."
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "I can walk you through", "eager": True}
            )
            await asyncio.sleep(0)

            self.assertFalse(speaker.paused)
            self.assertEqual(connection.active_turn_id, 7)
            self.assertEqual(started, [])
            self.assertEqual(client.aborted, [])

    async def test_novel_words_during_playback_still_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.spoken_text_recent = "Sure, I can walk you through the config file changes now."
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0  # the user has been talking a while
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "no wait, look at the tests instead", "eager": False}
            )
            await asyncio.sleep(0)

            self.assertIsNone(connection.active_turn_id)
            self.assertEqual(started, ["no wait, look at the tests instead"])

    async def test_mangled_in_order_echo_is_dismissed_despite_low_word_overlap(self) -> None:
        # Replay of run 20260705T174112Z 17:58:45: 21s into a long reply the
        # AEC leaked, STT transcribed the echo with ~1/3 of the words
        # substituted, and the bag-of-words overlap (0.64) slipped under the
        # 0.75 gate — Mortic's own words came back as a confirmed interrupt.
        # The words changed but the order didn't: the sequence gate must
        # classify this as echo and resume playback.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.spoken_text_recent = (
                "We just explored the repository, looking at its overall architecture, "
                "key directories, the eight agents and their services, configuration "
                "files, and the current development status."
            )
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0  # long enough to defeat cut_short
            await connection.handle_flux_event(
                {
                    "type": "speech.end",
                    # In-order echo, mangled: word overlap ~0.69 (< 0.75).
                    "transcript": "the eight agents in their servaces configuration file and the currents development status",
                    "eager": False,
                    "confidence": 0.9,
                }
            )
            await asyncio.sleep(0)

            self.assertEqual(started, [])
            self.assertEqual(connection.active_turn_id, 7)
            self.assertFalse(speaker.paused)
            self.assertEqual(client.aborted, [])

    async def test_long_novel_interrupt_still_admits_with_sequence_gate_armed(self) -> None:
        # A real interrupt of the same length as the incident echo must not
        # be swallowed by the sequence gate.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.spoken_text_recent = (
                "We just explored the repository, looking at its overall architecture, "
                "key directories, the eight agents and their services, configuration "
                "files, and the current development status."
            )
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0
            transcript = "actually stop for a second I want you to focus on the failing tests instead"
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": transcript, "eager": False, "confidence": 0.95}
            )
            await asyncio.sleep(0)

            self.assertIsNone(connection.active_turn_id)
            self.assertEqual(started, [transcript])

    async def test_mangled_closing_words_echo_in_the_tail_window_is_rejected(self) -> None:
        # Echo of the reply's closing words transcribes after playback has
        # ended; the sequence gate stays armed for ECHO_TAIL_SEC like the
        # other content rules.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            speaker.audible = False
            speaker.silent_for = 0.5  # inside ECHO_TAIL_SEC, past the audible-now rules
            connection.spoken_text_recent = (
                "That should give you a solid picture of where things stand right now."
            )
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event(
                {
                    "type": "speech.end",
                    "transcript": "a solid pitcher of where things stands right now",
                    "eager": False,
                    "confidence": 0.9,
                }
            )
            await asyncio.sleep(0)

            self.assertEqual(started, [])

    TIE_BAND_SPOKEN = (
        "We just explored the repository, looking at its overall architecture, "
        "key directories, the eight agents and their services, configuration "
        "files, and the current development status."
    )
    # Word overlap ~0.55, sequence ratio ~0.60: between ECHO_PROBE_TEXT_FLOOR
    # (0.45) and the echo thresholds — the band where the audio probe decides.
    TIE_BAND_TRANSCRIPT = "you said the eight agents share services is that actually true"

    def fill_echo_rings(self, connection: SidepodConnection, correlated: bool) -> None:
        from tests.test_opencode_voice import EchoProbeTests

        import numpy as np

        rate = connection.config.deepgram_sample_rate
        start = connection.barge_pending_since
        render = EchoProbeTests.speechlike_pcm(2.0, rate, seed=3)
        connection.render_audio_ring.append(render, at=start - 0.2)
        if correlated:
            samples = np.frombuffer(render, dtype=np.int16).astype(np.float32)
            delay = int(0.2 * rate)
            echoed = np.concatenate([np.zeros(delay, dtype=np.float32), samples * 0.3])
            mic = echoed[: int(1.2 * rate)].astype(np.int16).tobytes()
        else:
            mic = EchoProbeTests.speechlike_pcm(1.2, rate, seed=42)
        connection.mic_audio_ring.append(mic, at=start + 1.2)

    async def test_tie_band_transcript_with_correlated_audio_dismisses_as_echo(self) -> None:
        # Text alone can't decide (score in the ambiguous band); the mic
        # signal matching the render reference is what settles it as echo.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.spoken_text_recent = self.TIE_BAND_SPOKEN
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0
            self.fill_echo_rings(connection, correlated=True)
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": self.TIE_BAND_TRANSCRIPT, "eager": False, "confidence": 0.9}
            )
            await asyncio.sleep(0)

            self.assertEqual(started, [])
            self.assertEqual(connection.active_turn_id, 7)
            self.assertFalse(speaker.paused)

    async def test_early_probe_dismisses_clear_echo_before_any_transcript(self) -> None:
        # The pause must not wait out the STT round-trip when the audio
        # already matches the render (live run 192451Z: 8 pauses burned the
        # full 2s confirm deadline waiting for a transcript of pure echo).
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            await connection.handle_flux_event({"type": "speech.start"})
            self.assertTrue(speaker.paused)
            connection.barge_pending_since -= 1.0  # window already has audio
            self.fill_echo_rings(connection, correlated=True)

            dismissed = connection.try_early_echo_dismiss()

            self.assertTrue(dismissed)
            self.assertFalse(connection.barge_pending)
            self.assertFalse(speaker.paused)
            self.assertEqual(connection.active_turn_id, 7)
            self.assertEqual(client.aborted, [])
            log_lines = connection.logger.path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in log_lines]
            self.assertTrue(any(e["event"] == "barge_in.echo_probe" for e in events))
            self.assertTrue(
                any(e["event"] == "barge_in.false_alarm" and e["verdict"] == "echo_audio" for e in events)
            )

    async def test_early_probe_keeps_waiting_when_audio_is_novel(self) -> None:
        # A real interrupt must NOT be resumed over: low correlation leaves
        # the pending barge in place for the transcript to decide.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0
            self.fill_echo_rings(connection, correlated=False)

            dismissed = connection.try_early_echo_dismiss()

            self.assertFalse(dismissed)
            self.assertTrue(connection.barge_pending)
            self.assertTrue(speaker.paused)
            log_lines = connection.logger.path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in log_lines]
            # The correlation is still logged for live calibration.
            self.assertTrue(any(e["event"] == "barge_in.echo_probe" for e in events))

    async def test_probe_task_starts_with_pending_and_dies_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            self.audible_speaker(connection)
            connection.active_turn_id = 7

            await connection.handle_flux_event({"type": "speech.start"})
            probe = connection.pending_probe_task
            self.assertIsNotNone(probe)
            self.assertFalse(probe.done())

            connection.clear_pending_barge_in()
            await asyncio.sleep(0)
            self.assertTrue(probe.cancelled() or probe.done())
            self.assertIsNone(connection.pending_probe_task)

    async def test_early_probe_repolls_until_correlation_crosses(self) -> None:
        # Frame-driven: the probe rechecks on a fine cadence rather than
        # firing a fixed set of ticks, so echo that only correlates once
        # enough mic frames have arrived still resolves the pause.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.ECHO_PROBE_MIN_SEC = 0.0  # type: ignore[attr-defined]
            connection.ECHO_PROBE_POLL_SEC = 0.0  # type: ignore[attr-defined]
            connection.barge_pending = True
            connection.barge_pending_since = time.perf_counter() - 1.0
            self.fill_echo_rings(connection, correlated=True)

            calls = {"n": 0}
            real = connection.try_early_echo_dismiss

            def gated() -> bool:
                calls["n"] += 1
                # The audio only becomes conclusive after a few frames.
                return real() if calls["n"] >= 3 else False

            connection.try_early_echo_dismiss = gated  # type: ignore[method-assign]

            await connection.early_echo_probe()

            self.assertGreaterEqual(calls["n"], 3)
            self.assertFalse(connection.barge_pending)
            self.assertFalse(speaker.paused)

    async def test_pending_pcm_capture_writes_replayable_window_when_enabled(self) -> None:
        # With capture on, a resolved pending barge-in dumps the mic + render
        # PCM so the decision can be replayed offline; off by default, nothing
        # is written (raw audio stays only in memory).
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.config = dataclasses.replace(connection.config, echo_capture_enabled=True)
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0
            self.fill_echo_rings(connection, correlated=True)

            connection.dismiss_pending_barge_in("timeout")

            capture_dir = connection.logger.run_dir / "barge_pcm"
            self.assertTrue(capture_dir.is_dir())
            mics = list(capture_dir.glob("*.mic.pcm"))
            renders = list(capture_dir.glob("*.render.pcm"))
            metas = list(capture_dir.glob("*.json"))
            self.assertEqual((len(mics), len(renders), len(metas)), (1, 1, 1))
            self.assertGreater(mics[0].stat().st_size, 0)
            meta = json.loads(metas[0].read_text())
            self.assertEqual(meta["verdict"], "timeout")
            self.assertEqual(meta["sample_rate"], connection.config.deepgram_sample_rate)

    async def test_pending_pcm_capture_is_off_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            self.audible_speaker(connection)
            connection.active_turn_id = 7
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0
            self.fill_echo_rings(connection, correlated=True)

            connection.dismiss_pending_barge_in("timeout")

            self.assertFalse((connection.logger.run_dir / "barge_pcm").exists())

    async def test_tie_band_transcript_with_uncorrelated_audio_still_interrupts(self) -> None:
        # Same borderline transcript, but the mic audio does NOT match the
        # render: a real user quoting the assistant must still barge in.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.spoken_text_recent = self.TIE_BAND_SPOKEN
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0
            self.fill_echo_rings(connection, correlated=False)
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": self.TIE_BAND_TRANSCRIPT, "eager": False, "confidence": 0.9}
            )
            await asyncio.sleep(0)

            self.assertIsNone(connection.active_turn_id)
            self.assertEqual(started, [self.TIE_BAND_TRANSCRIPT])

    async def test_dismissed_transcripts_final_copy_never_becomes_a_turn(self) -> None:
        # The regression from run 20260704T093645Z: the eager copy of an echo
        # was correctly dismissed, then its confirming final speech.end
        # arrived 30ms later on the ordinary path and became a ghost turn.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.spoken_text_recent = "Sure, I can walk you through the config file changes now."
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "walk you through", "eager": True}
            )
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "walk you through", "eager": False}
            )
            await asyncio.sleep(0)

            self.assertEqual(started, [])
            self.assertEqual(connection.active_turn_id, 7)
            self.assertFalse(speaker.paused)

    async def test_low_confidence_speech_during_playback_is_dropped_as_echo(self) -> None:
        # Garbled echo transcribes as novel words (defeating the overlap
        # check) but with low Flux word confidence — run 20260704T100207Z had
        # three 5-8 char low-overlap fragments confirm interrupts in a row.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "grblx nar", "eager": True, "confidence": 0.31}
            )
            await asyncio.sleep(0)

            self.assertEqual(started, [])
            self.assertEqual(connection.active_turn_id, 7)
            self.assertFalse(speaker.paused)
            self.assertEqual(client.aborted, [])

    async def test_confident_novel_speech_during_playback_still_interrupts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            self.audible_speaker(connection)
            connection.active_turn_id = 7
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0  # the user has been talking a while
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "hold on a second", "eager": False, "confidence": 0.94}
            )
            await asyncio.sleep(0)

            self.assertEqual(started, ["hold on a second"])
            self.assertIsNone(connection.active_turn_id)

    async def test_rejected_transcripts_never_render_as_the_users_words(self) -> None:
        # Echo of the assistant saying "3" was gate-rejected as a turn but
        # still appeared in the transcript pane as user speech: the final
        # lane transcript used to be emitted before the verdict ran.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.spoken_text_recent = "The answer is 3 in this case."
            connection.barge_pending_since = 0.0

            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "is 3 in this case", "eager": False, "confidence": 1.0}
            )
            await asyncio.sleep(0)

            finals = [m for m in websocket.sent if m.get("type") == "transcript" and m.get("final")]
            self.assertEqual(finals, [])
            self.assertFalse(speaker.paused)

            # A real interrupt is still recorded exactly once.
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            connection.barge_pending_since -= 1.0
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "now use 4 instead", "eager": False, "confidence": 1.0}
            )
            await asyncio.sleep(0)

            finals = [m for m in websocket.sent if m.get("type") == "transcript" and m.get("final")]
            self.assertEqual([m["text"] for m in finals], ["now use 4 instead"])
            self.assertEqual(started, ["now use 4 instead"])

    async def test_speech_cut_short_by_our_own_pause_is_rejected(self) -> None:
        # Mangled echo (novel words, high confidence — no content rule can
        # catch it) ends ~250ms after the pause because the pause silenced
        # its source; a human keeps talking well past 400ms.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            # speech.end arrives almost immediately: the fragment was cut off.
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "blorp nax", "eager": True, "confidence": 0.99}
            )
            await asyncio.sleep(0)

            self.assertEqual(started, [])
            self.assertEqual(connection.active_turn_id, 7)
            self.assertFalse(speaker.paused)
            self.assertEqual(client.aborted, [])

    async def test_one_word_echo_during_playback_is_rejected(self) -> None:
        # The "great" loop: the assistant opens with "Great," and its echo
        # comes back as a single confident word — the old >=2-word guard let
        # it through and it became the next prompt.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.spoken_text_recent = "Great, I will update the config file now."
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.start"})
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "Great.", "eager": True, "confidence": 1.0}
            )
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "Great.", "eager": False, "confidence": 1.0}
            )
            await asyncio.sleep(0)

            self.assertEqual(started, [])
            self.assertEqual(connection.active_turn_id, 7)
            self.assertFalse(speaker.paused)

    async def test_closing_words_echo_in_the_tail_is_rejected(self) -> None:
        # Echo of the assistant's final words transcribes after playback has
        # ended; the content check stays armed through the tail window.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            speaker.audible = False
            speaker.silent_for = 0.8
            connection.spoken_text_recent = "I pushed the branch, let me know how it looks."
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "let me know how it looks", "eager": False, "confidence": 0.97}
            )
            await asyncio.sleep(0)

            self.assertEqual(started, [])

    async def test_one_word_answer_right_after_playback_is_admitted(self) -> None:
        # "Yes." moments after the assistant asked "yes or no?" is a real
        # reply — the single-word echo rule must not apply once sound stops.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            speaker.audible = False
            speaker.silent_for = 0.5
            connection.spoken_text_recent = "Should I apply the change? Yes or no."
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event(
                {"type": "speech.end", "transcript": "Yes.", "eager": False, "confidence": 0.99}
            )
            await asyncio.sleep(0)

            self.assertEqual(started, ["Yes."])

    async def test_short_reply_in_silence_is_admitted(self) -> None:
        # The tiny/echo floors only apply while the assistant is audible;
        # "Yes." in silence is a legitimate turn.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            connection.spoken_text_recent = "Should I apply the change? Yes or no."
            started: list[str] = []

            async def fake_turn(text: str, source: str, eager: bool) -> None:
                started.append(text)

            connection.run_text_turn = fake_turn  # type: ignore[method-assign]
            await connection.handle_flux_event({"type": "speech.end", "transcript": "Yes.", "eager": False})
            await asyncio.sleep(0)

            self.assertEqual(started, ["Yes."])

    async def test_confirm_deadline_expiry_resumes_playback(self) -> None:
        import dataclasses

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, client = lane_connection(tmp)
            connection.config = dataclasses.replace(connection.config, barge_in_confirm_sec=0.02)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7

            await connection.handle_flux_event({"type": "speech.start"})
            self.assertTrue(speaker.paused)
            await asyncio.sleep(0.08)

            self.assertFalse(speaker.paused)
            self.assertFalse(connection.barge_pending)
            self.assertEqual(connection.active_turn_id, 7)
            self.assertEqual(client.aborted, [])


class SilentCompletionLoggingTests(unittest.IsolatedAsyncioTestCase):
    """A completed turn with real reply text but zero speak() calls (e.g. an
    all-code, no-prose reply the speech filter strips to nothing) finishes
    normally and silently — indistinguishable from a hang unless it's
    logged. No behavior change: still no audio, just a diagnostic event."""

    async def test_speak_marks_the_turn_as_having_produced_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True), patch(
            "opencode_voice.server.DeepgramSpeakSession", FakeSpeakSession
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
    async def test_start_kicks_a_context_check_without_leaking_tokens_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connection, websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)

            self.assertEqual([message["type"] for message in websocket.sent], ["ready"])
            log_lines = connection.logger.path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line)["event"] for line in log_lines]
            self.assertIn("tokens.check", events)


if __name__ == "__main__":
    unittest.main()
