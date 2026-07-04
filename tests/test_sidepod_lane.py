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


class FakeNativeSpeaker:
    def __init__(self, config: Any, logger: Any, on_issue: Any, on_render: Any = None) -> None:
        self.played: list[bytes] = []
        self.on_render = on_render
        self.audible = False

    async def start(self) -> bool:
        return True

    async def play(self, data: bytes, turn_id: Any) -> bool:
        self.played.append(data)
        if self.on_render:
            self.on_render(data)
        return True

    def is_audible(self, tail_sec: float = 0.3) -> bool:
        return self.audible

    async def close(self) -> None:
        self.audible = False


class FakeNativeMic:
    ok = True

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
    **kwargs: Any,
) -> tuple[SidepodConnection, FakeWebSocket, LaneFakeClient]:
    websocket = FakeWebSocket()
    fake_client = client or LaneFakeClient()
    connection = SidepodConnection(
        config=VoiceConfig(opencode_url="http://opencode.test", run_root=tmp, voice_duplex=voice_duplex),
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

    def process_capture(self, data: bytes) -> bytes:
        self.captured.append(data)
        return b"\x7f" * len(data)  # marker: processing happened

    def process_render(self, data: bytes) -> None:
        self.rendered.append(data)

    def set_stream_delay_ms(self, delay_ms: int) -> None:
        return None


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
            self.assertIsNone(connection.native_speaker)

            # Audio still streaming from the barged-in TTS socket must be
            # dropped, not played, and must not recreate the native speaker.
            await stale_speaker.on_audio(b"\x01\x02", 1)
            self.assertIsNone(connection.native_speaker)
            self.assertEqual(connection.stale_tts_chunks, 1)


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
