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

ENV_WITH_KEYS = {"DEEPGRAM_API_KEY": "audio-key", "INCEPTION_API_KEY": "turn-key"}


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


class LaneFakeClient:
    def __init__(self, base_url: str = "http://opencode.test") -> None:
        self.base_url = base_url
        self.fork_count = 0
        self.deleted: list[str] = []
        self.aborted: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.closed = False
        self._assistant_messages: list[dict[str, Any]] = []

    async def close(self) -> None:
        self.closed = True

    async def fork_session(self, session_id: str) -> dict[str, str]:
        self.fork_count += 1
        return {"id": f"fork_{self.fork_count}"}

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return {"id": session_id, "title": "Source Thread", "tokens": {}}

    async def switch_model(self, session_id: str, model: Any) -> dict[str, bool]:
        return {"ok": True}

    async def switch_agent(self, session_id: str, agent: str) -> dict[str, bool]:
        return {"ok": True}

    async def update_session(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._assistant_messages)

    async def delete_session(self, session_id: str) -> bool:
        self.deleted.append(session_id)
        return True

    async def abort(self, session_id: str) -> bool:
        self.aborted.append(session_id)
        return True

    def events(self, on_open: Any = None) -> Any:
        raise RuntimeError("event stream disabled in tests")

    async def prompt_async(self, session_id: str, text: str, model: Any, agent: str) -> Any:
        raise RuntimeError("prompt_async disabled in tests")

    async def prompt_text(self, session_id: str, text: str, model: Any, agent: str) -> Any:
        self.prompts.append((session_id, text))
        reply_number = len(self.prompts)
        self._assistant_messages = self._assistant_messages + [
            {
                "info": {
                    "id": f"msg_reply_{reply_number}",
                    "role": "assistant",
                    "time": {"created": reply_number * 2 - 1, "completed": reply_number * 2},
                },
                "parts": [{"type": "text", "text": "On it. I will tighten the summary."}],
            }
        ]
        return {"ok": True}


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
    def __init__(self, config: Any, logger: Any, on_issue: Any) -> None:
        self.played: list[bytes] = []

    async def start(self) -> bool:
        return True

    async def play(self, data: bytes, turn_id: Any) -> bool:
        self.played.append(data)
        return True

    async def close(self) -> None:
        return None


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

    async def start(self) -> None:
        return None

    async def send_audio(self, data: bytes) -> None:
        return None

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


def lane_connection(tmp: str, client: LaneFakeClient | None = None, **kwargs: Any) -> tuple[SidepodConnection, FakeWebSocket, LaneFakeClient]:
    websocket = FakeWebSocket()
    fake_client = client or LaneFakeClient()
    connection = SidepodConnection(
        config=VoiceConfig(opencode_url="http://opencode.test", run_root=tmp),
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
                ["ready", "transcript", "transcript", "thinking", "assistant.delta", "speaking", "complete"],
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
            self.assertEqual(complete["streamSource"], "poll")
            self.assertIn("totalMs", complete["latency"])
            self.assertIn("firstTranscriptMs", complete["latency"])
            self.assertIn("firstAssistantTextMs", complete["latency"])
            self.assertIn("firstAudioMs", complete["latency"])
            self.assertEqual(client.prompts, [("fork_1", "Make the test output easier to scan.")])

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
