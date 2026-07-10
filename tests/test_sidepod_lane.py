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
from opencode_voice.interruption import EpisodeIdentity, InterruptionEvent
from opencode_voice.logging import RunLogger
from opencode_voice.playback import PlaybackToken
from opencode_voice.protocol import check_event
from opencode_voice.server import (
    ActiveSidepodLaneRegistry,
    SIDEPOD_PROTOCOL_VERSION,
    SidepodConnection,
    classify_turn_failure,
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

    async def start(self) -> bool:
        return True

    def is_audible(self, tail_sec: float = 0.3) -> bool:
        return False

    def invalidate_generation(self, generation: int | None = None) -> int:
        return int(generation or 0)

    def set_ducked(self, ducked: bool) -> None:
        return None


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


class LegacyPendingBargeInScenarios:
    """Historical, non-collected scenarios for the deleted heuristic path.

    The authoritative episode reducer is covered below and in
    tests/test_interruption.py.  These methods remain temporarily as readable
    incident provenance until the two rollout cohorts are complete.
    """

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

    async def test_pause_holds_through_a_long_utterance_then_dismisses_when_quiet(self) -> None:
        # The confirm deadline is measured from the last speech signal, so a
        # >confirm_sec utterance (interim transcripts still arriving) keeps the
        # pause held instead of releasing playback mid-sentence.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.config = dataclasses.replace(
                connection.config, barge_in_confirm_sec=0.15, barge_in_max_sec=5.0, echo_probe_enabled=False
            )
            await connection.handle_flux_event({"type": "speech.start"})
            self.assertTrue(connection.barge_pending)

            # Interim transcripts keep arriving for ~0.4s (well past confirm_sec).
            for _ in range(4):
                await asyncio.sleep(0.1)
                await connection.handle_flux_event(
                    {"type": "speech.transcript", "transcript": "still talking", "is_final": False}
                )
                self.assertTrue(connection.barge_pending, "pause released mid-utterance")
            self.assertTrue(speaker.paused)

            # Speaker goes quiet: dismiss ~confirm_sec later.
            await asyncio.sleep(0.35)
            self.assertFalse(connection.barge_pending)
            self.assertFalse(speaker.paused)

    async def test_hard_cap_dismisses_even_while_speech_keeps_coming(self) -> None:
        # Leaked echo that STT keeps re-transcribing must not freeze playback:
        # the pause dismisses barge_in_max_sec after it began regardless.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, ENV_WITH_KEYS, clear=True):
            connection, _websocket, _client = lane_connection(tmp)
            await connection.handle_control(START_PAYLOAD)
            speaker = self.audible_speaker(connection)
            connection.active_turn_id = 7
            connection.config = dataclasses.replace(
                connection.config, barge_in_confirm_sec=0.2, barge_in_max_sec=0.4, echo_probe_enabled=False
            )
            await connection.handle_flux_event({"type": "speech.start"})

            # Never stop "talking": feed interim transcripts past the hard cap.
            for _ in range(10):
                await asyncio.sleep(0.06)
                await connection.handle_flux_event(
                    {"type": "speech.transcript", "transcript": "x", "is_final": False}
                )
                if not connection.barge_pending:
                    break

            self.assertFalse(connection.barge_pending)
            self.assertFalse(speaker.paused)
            events = [json.loads(l) for l in connection.logger.path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(
                any(e["event"] == "barge_in.false_alarm" and e["verdict"] == "timeout_max" for e in events)
            )

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
