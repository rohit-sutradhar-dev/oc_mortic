from __future__ import annotations

import asyncio
import base64
import json
import unittest
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import websockets

from opencode_voice.playback import PlaybackToken
from opencode_voice.tts_providers import (
    CartesiaTTSOptions,
    CartesiaTTSProvider,
    DeepgramTTSOptions,
    DeepgramTTSProvider,
    StalePlaybackToken,
    TTSProviderError,
    connect_websocket,
)


class FakeWebSocket:
    STOP = object()

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.send_gate = asyncio.Event()
        self.send_gate.set()
        self.send_error: Exception | None = None
        self.close_code: int | None = None

    async def send(self, raw: str) -> None:
        await self.send_gate.wait()
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(json.loads(raw))

    def __aiter__(self) -> "FakeWebSocket":
        return self

    async def __anext__(self) -> Any:
        item = await self.incoming.get()
        if item is self.STOP:
            raise StopAsyncIteration
        return item

    async def push_json(self, payload: dict[str, Any]) -> None:
        await self.incoming.put(json.dumps(payload))

    async def push_audio(self, data: bytes) -> None:
        await self.incoming.put(data)

    async def stop_reader(self) -> None:
        await self.incoming.put(self.STOP)

    async def close(self) -> None:
        self.closed = True


class SequenceConnector:
    def __init__(self, *websockets: FakeWebSocket) -> None:
        self.websockets = list(websockets)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeWebSocket:
        self.calls.append((url, headers))
        if not self.websockets:
            raise RuntimeError("no fake websocket remaining")
        return self.websockets.pop(0)


async def wait_until(predicate: Any, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


class DeepgramTTSProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_websocket_uses_happy_eyeballs(self) -> None:
        websocket = object()
        connect = AsyncMock(return_value=websocket)
        with patch("opencode_voice.tts_providers.websockets.connect", connect):
            result = await connect_websocket("wss://tts.test", {"X-API-Key": "test"})

        self.assertIs(result, websocket)
        self.assertEqual(connect.await_args.kwargs["happy_eyeballs_delay"], 0.25)
        self.assertEqual(connect.await_args.kwargs["interleave"], 1)

    def test_options_repr_never_contains_api_key(self) -> None:
        deepgram = DeepgramTTSOptions(api_key="super-secret")
        cartesia = CartesiaTTSOptions(api_key="super-secret", voice_id="voice")
        self.assertEqual(deepgram.sample_rate, 16_000)
        self.assertEqual(cartesia.sample_rate, 16_000)
        self.assertNotIn("super-secret", repr(deepgram))
        self.assertNotIn(
            "super-secret",
            repr(cartesia),
        )
        pcm_60_seconds = 60 * 48_000 * 2
        ten_ms_frames_60_seconds = 60 * 100
        for options in (deepgram, cartesia):
            self.assertGreaterEqual(options.delivery_queue_max_bytes, pcm_60_seconds)
            self.assertGreaterEqual(options.delivery_queue_max_chunks, ten_ms_frames_60_seconds)

    async def test_clear_fences_late_audio_and_reuses_the_socket(self) -> None:
        websocket = FakeWebSocket()
        connector = SequenceConnector(websocket)
        received: list[tuple[PlaybackToken, bytes]] = []
        events: list[dict[str, Any]] = []

        async def on_audio(token: PlaybackToken, data: bytes) -> None:
            received.append((token, data))

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = DeepgramTTSProvider(
            DeepgramTTSOptions(api_key="test", clear_timeout_sec=0.2),
            on_audio,
            on_event,
            connector=connector,
        )
        first = PlaybackToken(1, 11)
        second = PlaybackToken(2, 12)
        await provider.begin_turn(first)
        await provider.append_text(first, "First response.")

        cancel = asyncio.create_task(provider.cancel_turn(first, "barge_in"))
        await wait_until(lambda: any(item.get("type") == "Clear" for item in websocket.sent))
        await websocket.push_audio(b"old")
        await websocket.push_json({"type": "Cleared", "sequence_id": 0})
        await cancel

        await provider.begin_turn(second)
        await websocket.push_audio(b"new")
        await wait_until(lambda: len(received) == 1)

        self.assertEqual(received, [(second, b"new")])
        self.assertEqual(len(connector.calls), 1)
        self.assertTrue(any(event["type"] == "tts.stale_audio.drop" for event in events))
        await provider.close()

    async def test_clear_timeout_reconnects_before_next_turn(self) -> None:
        first_socket = FakeWebSocket()
        second_socket = FakeWebSocket()
        connector = SequenceConnector(first_socket, second_socket)
        received: list[tuple[PlaybackToken, bytes]] = []

        async def on_audio(token: PlaybackToken, data: bytes) -> None:
            received.append((token, data))

        async def on_event(_: dict[str, Any]) -> None:
            return None

        provider = DeepgramTTSProvider(
            DeepgramTTSOptions(api_key="test", clear_timeout_sec=0.01),
            on_audio,
            on_event,
            connector=connector,
        )
        first = PlaybackToken(1, 1)
        second = PlaybackToken(2, 2)
        await provider.begin_turn(first)
        await provider.cancel_turn(first, "barge_in")

        self.assertTrue(first_socket.closed)
        self.assertEqual(len(connector.calls), 2)
        self.assertEqual(provider.connection_epoch, 2)

        await provider.begin_turn(second)
        await second_socket.push_audio(b"second")
        await wait_until(lambda: bool(received))
        self.assertEqual(received, [(second, b"second")])
        await provider.close()

    async def test_stale_token_cannot_append_text(self) -> None:
        websocket = FakeWebSocket()

        async def no_audio(_: PlaybackToken, __: bytes) -> None:
            return None

        async def no_event(_: dict[str, Any]) -> None:
            return None

        provider = DeepgramTTSProvider(
            DeepgramTTSOptions(api_key="test"),
            no_audio,
            no_event,
            connector=SequenceConnector(websocket),
        )
        active = PlaybackToken(2, 2)
        await provider.begin_turn(active)
        with self.assertRaises(StalePlaybackToken):
            await provider.append_text(PlaybackToken(1, 1), "stale")
        await provider.close()

    async def test_cancelling_clear_waiter_discards_the_ambiguous_socket(self) -> None:
        first_socket = FakeWebSocket()
        second_socket = FakeWebSocket()
        connector = SequenceConnector(first_socket, second_socket)

        async def no_audio(_: PlaybackToken, __: bytes) -> None:
            return None

        async def no_event(_: dict[str, Any]) -> None:
            return None

        provider = DeepgramTTSProvider(
            DeepgramTTSOptions(api_key="test", clear_timeout_sec=10),
            no_audio,
            no_event,
            connector=connector,
        )
        first = PlaybackToken(1, 1)
        await provider.begin_turn(first)
        cancelling = asyncio.create_task(provider.cancel_turn(first, "barge_in"))
        await wait_until(lambda: any(item.get("type") == "Clear" for item in first_socket.sent))
        cancelling.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelling

        self.assertTrue(first_socket.closed)
        await provider.begin_turn(PlaybackToken(2, 2))
        self.assertEqual(len(connector.calls), 2)
        await provider.close()

    async def test_append_sends_speak_and_finish_sends_one_final_flush(self) -> None:
        websocket = FakeWebSocket()
        timeline: list[str] = []
        events: list[dict[str, Any]] = []

        async def on_audio(_: PlaybackToken, data: bytes) -> None:
            timeline.append(f"audio:{data.decode()}")

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)
            if event["type"] == "tts.turn.done":
                timeline.append("done")

        provider = DeepgramTTSProvider(
            DeepgramTTSOptions(api_key="test"),
            on_audio,
            on_event,
            connector=SequenceConnector(websocket),
        )
        token = PlaybackToken(7, 41)
        await provider.begin_turn(token)
        await provider.append_text(token, "First")
        await provider.append_text(token, " second")
        await provider.finish_turn(token)
        await provider.finish_turn(token)

        self.assertEqual(
            websocket.sent,
            [
                {"type": "Speak", "text": "First"},
                {"type": "Speak", "text": " second"},
                {"type": "Flush"},
            ],
        )
        await websocket.push_audio(b"ordered")
        await websocket.push_json({"type": "Flushed", "sequence_id": 1})
        await wait_until(lambda: timeline == ["audio:ordered", "done"])

        done = next(event for event in events if event["type"] == "tts.turn.done")
        self.assertEqual(done["turn_id"], 41)
        self.assertEqual(done["generation"], 7)
        self.assertEqual(done["playback_generation"], 7)
        self.assertIsNone(provider.active_token)
        await provider.close()

    async def test_cleared_is_processed_while_audio_delivery_is_blocked(self) -> None:
        websocket = FakeWebSocket()
        audio_started = asyncio.Event()
        release_audio = asyncio.Event()

        async def blocked_audio(_: PlaybackToken, __: bytes) -> None:
            audio_started.set()
            await release_audio.wait()

        async def no_event(_: dict[str, Any]) -> None:
            return None

        provider = DeepgramTTSProvider(
            DeepgramTTSOptions(api_key="test", clear_timeout_sec=0.2),
            blocked_audio,
            no_event,
            connector=SequenceConnector(websocket),
        )
        token = PlaybackToken(1, 1)
        await provider.begin_turn(token)
        await websocket.push_audio(b"audio")
        await asyncio.wait_for(audio_started.wait(), timeout=1)

        cancelling = asyncio.create_task(provider.cancel_turn(token, "barge_in"))
        await wait_until(lambda: any(item.get("type") == "Clear" for item in websocket.sent))
        await websocket.push_json({"type": "Cleared"})
        await asyncio.wait_for(cancelling, timeout=0.1)

        release_audio.set()
        await provider.close()

    async def test_unexpected_socket_loss_fails_the_finishing_turn(self) -> None:
        websocket = FakeWebSocket()
        websocket.close_code = 1006
        events: list[dict[str, Any]] = []

        async def no_audio(_: PlaybackToken, __: bytes) -> None:
            return None

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = DeepgramTTSProvider(
            DeepgramTTSOptions(api_key="test"),
            no_audio,
            on_event,
            connector=SequenceConnector(websocket),
        )
        token = PlaybackToken(9, 27)
        await provider.begin_turn(token)
        await provider.append_text(token, "Response")
        await provider.finish_turn(token)
        await websocket.stop_reader()
        await wait_until(lambda: any(event["type"] == "tts.turn.failed" for event in events))

        failed = next(event for event in events if event["type"] == "tts.turn.failed")
        self.assertEqual(failed["turn_id"], 27)
        self.assertEqual(failed["generation"], 9)
        self.assertEqual(failed["error_code"], "websocket_closed")
        self.assertEqual(failed["close_code"], 1006)
        await provider.close()


class MutableClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class CartesiaTTSProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_opening_handshake_timeout_has_a_safe_specific_error_code(self) -> None:
        events: list[dict[str, Any]] = []

        async def timeout_connector(_url: str, _headers: dict[str, str]) -> Any:
            raise TimeoutError("opening handshake timed out")

        async def no_audio(_: PlaybackToken, __: bytes) -> None:
            return None

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            no_audio,
            on_event,
            connector=timeout_connector,
        )

        with self.assertRaises(TTSProviderError):
            await provider.connect()

        self.assertEqual(events[0]["type"], "tts.transport.connect_error")
        self.assertEqual(events[0]["stage"], "opening_handshake")
        self.assertEqual(events[0]["error_code"], "opening_handshake_timeout")
        await provider.close()

    def make_provider(
        self,
        websocket: FakeWebSocket,
        context_ids: Iterator[str],
        received: list[tuple[PlaybackToken, bytes]],
        events: list[dict[str, Any]],
        *,
        clock: MutableClock | None = None,
    ) -> CartesiaTTSProvider:
        async def on_audio(token: PlaybackToken, data: bytes) -> None:
            received.append((token, data))

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        return CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            on_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=lambda: next(context_ids),
            clock=clock or MutableClock(),
        )

    async def test_turn_uses_continuations_and_explicit_finish(self) -> None:
        websocket = FakeWebSocket()
        received: list[tuple[PlaybackToken, bytes]] = []
        events: list[dict[str, Any]] = []
        provider = self.make_provider(websocket, iter(["ctx-1"]), received, events)
        token = PlaybackToken(1, 10)

        await provider.begin_turn(token)
        await provider.append_text(token, "Hello")
        await provider.append_text(token, "world.")
        await provider.finish_turn(token)

        generations = [item for item in websocket.sent if not item.get("cancel")]
        self.assertEqual([item["context_id"] for item in generations], ["ctx-1"] * 3)
        self.assertEqual([item["transcript"] for item in generations], ["Hello", " world.", ""])
        self.assertEqual([item["continue"] for item in generations], [True, True, False])

        encoded = base64.b64encode(b"audio").decode()
        await websocket.push_json({"type": "chunk", "context_id": "ctx-1", "data": encoded})
        await websocket.push_json({"type": "done", "context_id": "ctx-1", "done": True})
        await wait_until(lambda: bool(received))
        self.assertEqual(received, [(token, b"audio")])
        await provider.close()

    async def test_cancelled_context_can_never_feed_the_next_turn(self) -> None:
        websocket = FakeWebSocket()
        received: list[tuple[PlaybackToken, bytes]] = []
        events: list[dict[str, Any]] = []
        provider = self.make_provider(websocket, iter(["old", "new"]), received, events)
        old = PlaybackToken(1, 1)
        new = PlaybackToken(2, 2)

        await provider.begin_turn(old)
        await provider.append_text(old, "Old response")
        await provider.cancel_turn(old, "barge_in")
        self.assertIn({"context_id": "old", "cancel": True}, websocket.sent)

        await provider.begin_turn(new)
        await provider.append_text(new, "New response")
        old_audio = base64.b64encode(b"old").decode()
        new_audio = base64.b64encode(b"new").decode()
        await websocket.push_json({"type": "chunk", "context_id": "old", "data": old_audio})
        await websocket.push_json({"type": "chunk", "context_id": "new", "data": new_audio})
        await websocket.push_json({"type": "done", "context_id": "old", "done": True})
        await wait_until(lambda: bool(received))
        await wait_until(lambda: any(event["type"] == "tts.stale_audio.summary" for event in events))

        self.assertEqual(received, [(new, b"new")])
        summary = next(event for event in events if event["type"] == "tts.stale_audio.summary")
        self.assertEqual(summary["chunks"], 1)
        self.assertEqual(summary["bytes"], 3)
        self.assertFalse(any(event["type"] == "tts.stale_audio.drop" for event in events))
        await provider.close()

    async def test_cancel_send_failure_reconnects_before_the_next_turn(self) -> None:
        first_socket = FakeWebSocket()
        second_socket = FakeWebSocket()
        connector = SequenceConnector(first_socket, second_socket)
        events: list[dict[str, Any]] = []

        async def no_audio(_: PlaybackToken, __: bytes) -> None:
            return None

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            no_audio,
            on_event,
            connector=connector,
            context_id_factory=iter(["old", "new"]).__next__,
        )
        old = PlaybackToken(1, 1)
        await provider.begin_turn(old)
        await provider.append_text(old, "Old response")
        first_socket.send_error = ConnectionError("socket gone")

        await provider.cancel_turn(old, "barge_in")

        self.assertTrue(first_socket.closed)
        self.assertTrue(provider.connected)
        self.assertEqual(provider.connection_epoch, 2)
        self.assertTrue(any(event["type"] == "tts.transport.recovered" for event in events))
        await provider.begin_turn(PlaybackToken(2, 2))
        self.assertEqual(len(connector.calls), 2)
        await provider.close()

    async def test_context_rotates_only_after_nine_hundred_ms_without_provider_activity(self) -> None:
        websocket = FakeWebSocket()
        received: list[tuple[PlaybackToken, bytes]] = []
        events: list[dict[str, Any]] = []
        clock = MutableClock()
        provider = self.make_provider(websocket, iter(["ctx-1", "ctx-2"]), received, events, clock=clock)
        token = PlaybackToken(1, 1)

        await provider.begin_turn(token)
        await provider.append_text(token, "First")
        clock.now = 0.8
        encoded = base64.b64encode(b"audio").decode()
        await websocket.push_json({"type": "chunk", "context_id": "ctx-1", "data": encoded})
        await wait_until(lambda: bool(received))

        # Incoming audio is genuine provider activity, so this append remains
        # on the original context even though the first send is 1s old.
        clock.now = 1.0
        await provider.append_text(token, "Second")
        self.assertEqual(provider.active_context_id, "ctx-1")
        # A successful append/send also resets the idle-expiry guard.
        clock.now = 1.899
        await provider.append_text(token, "Third")
        self.assertEqual(provider.active_context_id, "ctx-1")
        clock.now = 2.8
        await provider.append_text(token, "Fourth")

        requests = [item for item in websocket.sent if "transcript" in item]
        self.assertEqual(
            [item["context_id"] for item in requests],
            ["ctx-1", "ctx-1", "ctx-1", "ctx-2"],
        )
        self.assertIn({"context_id": "ctx-1", "cancel": True}, websocket.sent)
        self.assertTrue(any(event["type"] == "tts.context.rotate" for event in events))
        await provider.close()

    async def test_idle_rotation_preserves_already_received_audio_behind_blocked_playout(self) -> None:
        websocket = FakeWebSocket()
        clock = MutableClock()
        audio_started = asyncio.Event()
        release_audio = asyncio.Event()
        received: list[bytes] = []
        events: list[dict[str, Any]] = []

        async def blocked_audio(_: PlaybackToken, data: bytes) -> None:
            audio_started.set()
            await release_audio.wait()
            received.append(data)

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            blocked_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=iter(["ctx-old", "ctx-new"]).__next__,
            clock=clock,
        )
        token = PlaybackToken(2, 20)
        await provider.begin_turn(token)
        await provider.append_text(token, "First")
        for data in (b"old-1", b"old-2"):
            await websocket.push_json(
                {
                    "type": "chunk",
                    "context_id": "ctx-old",
                    "data": base64.b64encode(data).decode(),
                }
            )
        await asyncio.wait_for(audio_started.wait(), timeout=1)

        clock.now = 0.901
        await provider.append_text(token, "Second")
        self.assertEqual(provider.active_context_id, "ctx-new")
        await websocket.push_json(
            {
                "type": "chunk",
                "context_id": "ctx-new",
                "data": base64.b64encode(b"new").decode(),
            }
        )
        await websocket.push_json({"type": "done", "context_id": "ctx-new", "done": True})

        release_audio.set()
        await wait_until(lambda: received == [b"old-1", b"old-2", b"new"])
        await wait_until(lambda: any(event["type"] == "tts.turn.done" for event in events))
        await provider.close()

    async def test_socket_loss_retires_context_instead_of_continuing_it_on_a_new_socket(self) -> None:
        first_socket = FakeWebSocket()
        second_socket = FakeWebSocket()
        received: list[tuple[PlaybackToken, bytes]] = []
        events: list[dict[str, Any]] = []

        async def on_audio(token: PlaybackToken, data: bytes) -> None:
            received.append((token, data))

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            on_audio,
            on_event,
            connector=SequenceConnector(first_socket, second_socket),
            context_id_factory=iter(["ctx-old", "ctx-new"]).__next__,
        )
        token = PlaybackToken(1, 1)
        await provider.begin_turn(token)
        await provider.append_text(token, "Before disconnect")
        await first_socket.stop_reader()
        await wait_until(lambda: provider.active_context_id is None)

        with self.assertRaises(StalePlaybackToken):
            await provider.append_text(token, "must not cross sockets")
        await provider.begin_turn(token)
        await provider.append_text(token, "After reconnect")

        self.assertEqual(second_socket.sent[-1]["context_id"], "ctx-new")
        self.assertEqual(second_socket.sent[-1]["transcript"], "After reconnect")
        await provider.close()

    async def test_done_is_read_while_audio_delivery_is_blocked_but_emitted_in_order(self) -> None:
        websocket = FakeWebSocket()
        audio_started = asyncio.Event()
        release_audio = asyncio.Event()
        timeline: list[str] = []
        events: list[dict[str, Any]] = []

        async def blocked_audio(_: PlaybackToken, data: bytes) -> None:
            audio_started.set()
            await release_audio.wait()
            timeline.append(f"audio:{data.decode()}")

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)
            if event["type"] == "tts.turn.done":
                timeline.append("done")

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            blocked_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=lambda: "ctx-blocked",
        )
        token = PlaybackToken(3, 9)
        await provider.begin_turn(token)
        await provider.append_text(token, "A long response")
        await provider.finish_turn(token)
        encoded = base64.b64encode(b"first").decode()
        await websocket.push_json({"type": "chunk", "context_id": "ctx-blocked", "data": encoded})
        await asyncio.wait_for(audio_started.wait(), timeout=1)
        await websocket.push_json({"type": "done", "context_id": "ctx-blocked", "done": True})

        await wait_until(
            lambda: any(
                event["type"] == "tts.provider.message" and event.get("message_type") == "done"
                for event in events
            )
        )
        self.assertIsNone(provider.active_context_id)
        self.assertNotIn("done", timeline)

        release_audio.set()
        await wait_until(lambda: timeline == ["audio:first", "done"])
        await provider.close()

    async def test_real_websocket_heartbeat_survives_blocked_device_playout(self) -> None:
        release_audio = asyncio.Event()
        server_release = asyncio.Event()
        events: list[dict[str, Any]] = []

        async def handler(websocket: Any) -> None:
            # Consume generation requests concurrently so the test socket has
            # the same bidirectional shape as Cartesia.
            async def consume_requests() -> None:
                async for _ in websocket:
                    pass

            consumer = asyncio.create_task(consume_requests())
            encoded = base64.b64encode(b"\x01\x00" * 480).decode()
            try:
                for _ in range(64):
                    await websocket.send(
                        json.dumps(
                            {"type": "chunk", "context_id": "ctx-heartbeat", "data": encoded}
                        )
                    )
                await websocket.send(
                    json.dumps(
                        {"type": "done", "context_id": "ctx-heartbeat", "done": True}
                    )
                )
                await server_release.wait()
            finally:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def connector(_: str, __: dict[str, str]) -> Any:
            return await websockets.connect(
                f"ws://127.0.0.1:{port}",
                ping_interval=0.05,
                ping_timeout=0.05,
                max_queue=2,
            )

        audio_calls = 0

        async def blocked_audio(_: PlaybackToken, __: bytes) -> None:
            nonlocal audio_calls
            audio_calls += 1
            if audio_calls == 1:
                await release_audio.wait()

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            blocked_audio,
            on_event,
            connector=connector,
            context_id_factory=lambda: "ctx-heartbeat",
        )
        try:
            await provider.begin_turn(PlaybackToken(7, 19))
            await wait_until(
                lambda: any(
                    event["type"] == "tts.provider.message"
                    and event.get("message_type") == "done"
                    for event in events
                )
            )
            # Two heartbeat periods elapse while the device callback is still
            # blocked. The socket reader must remain alive and consume pong.
            await asyncio.sleep(0.15)
            self.assertTrue(provider.connected)
            self.assertFalse(any(event["type"] == "tts.turn.failed" for event in events))

            release_audio.set()
            await wait_until(lambda: audio_calls == 64)
            await wait_until(lambda: any(event["type"] == "tts.turn.done" for event in events))
        finally:
            server_release.set()
            await provider.close()
            server.close()
            await server.wait_closed()

    async def test_delivery_overflow_fails_the_turn_instead_of_dropping_audio(self) -> None:
        websocket = FakeWebSocket()
        audio_started = asyncio.Event()
        release_audio = asyncio.Event()
        received: list[bytes] = []
        events: list[dict[str, Any]] = []

        async def blocked_audio(_: PlaybackToken, data: bytes) -> None:
            audio_started.set()
            await release_audio.wait()
            received.append(data)

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(
                api_key="test",
                voice_id="voice",
                delivery_queue_max_bytes=1024,
                delivery_queue_max_chunks=2,
            ),
            blocked_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=lambda: "ctx-overflow",
        )
        token = PlaybackToken(5, 17)
        await provider.begin_turn(token)
        encoded = base64.b64encode(b"chunk").decode()
        await websocket.push_json({"type": "chunk", "context_id": "ctx-overflow", "data": encoded})
        await asyncio.wait_for(audio_started.wait(), timeout=1)
        await websocket.push_json({"type": "chunk", "context_id": "ctx-overflow", "data": encoded})
        await websocket.push_json({"type": "chunk", "context_id": "ctx-overflow", "data": encoded})
        await wait_until(
            lambda: any(
                event["type"] == "tts.transport.disconnected"
                and event.get("error_code") == "delivery_overflow"
                for event in events
            )
        )
        self.assertTrue(websocket.closed)
        # Failure lifecycle is independent of the blocked audio actor and the
        # queued tail is purged before device capacity returns.
        await wait_until(lambda: any(event["type"] == "tts.turn.failed" for event in events))
        self.assertEqual(received, [])

        release_audio.set()
        await wait_until(lambda: received == [b"chunk"])
        failed = next(event for event in events if event["type"] == "tts.turn.failed")
        self.assertEqual(failed["error_code"], "delivery_overflow")
        self.assertEqual(failed["turn_id"], 17)
        self.assertEqual(failed["generation"], 5)
        self.assertEqual(failed["context_id"], "ctx-overflow")
        self.assertEqual(received, [b"chunk"])
        await provider.close()

    async def test_provider_failure_bypasses_blocked_audio_and_purges_queued_pcm(self) -> None:
        websocket = FakeWebSocket()
        audio_started = asyncio.Event()
        release_audio = asyncio.Event()
        received: list[bytes] = []
        events: list[dict[str, Any]] = []

        async def blocked_audio(_: PlaybackToken, data: bytes) -> None:
            audio_started.set()
            await release_audio.wait()
            received.append(data)

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            blocked_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=lambda: "ctx-failure-fence",
        )
        token = PlaybackToken(21, 34)
        await provider.begin_turn(token)
        for data in (b"entered", b"must-be-purged"):
            await websocket.push_json(
                {
                    "type": "chunk",
                    "context_id": "ctx-failure-fence",
                    "data": base64.b64encode(data).decode(),
                }
            )
        await asyncio.wait_for(audio_started.wait(), timeout=1)
        await websocket.push_json({"type": "error", "context_id": "ctx-failure-fence"})

        await asyncio.wait_for(
            wait_until(lambda: any(event["type"] == "tts.turn.failed" for event in events)),
            timeout=0.1,
        )
        self.assertEqual(received, [])
        self.assertEqual(provider._delivery.buffered_chunks, 1)

        release_audio.set()
        await wait_until(lambda: received == [b"entered"])
        await asyncio.sleep(0)
        self.assertEqual(received, [b"entered"])
        await provider.close()

    async def test_long_burst_is_fully_read_before_blocked_playout_catches_up(self) -> None:
        websocket = FakeWebSocket()
        audio_started = asyncio.Event()
        release_audio = asyncio.Event()
        received: list[bytes] = []
        events: list[dict[str, Any]] = []

        async def blocked_audio(_: PlaybackToken, data: bytes) -> None:
            audio_started.set()
            await release_audio.wait()
            received.append(data)

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(
                api_key="test",
                voice_id="voice",
                delivery_queue_max_bytes=4096,
                delivery_queue_max_chunks=80,
            ),
            blocked_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=lambda: "ctx-long",
        )
        token = PlaybackToken(6, 18)
        await provider.begin_turn(token)
        first = base64.b64encode(b"0").decode()
        await websocket.push_json({"type": "chunk", "context_id": "ctx-long", "data": first})
        await asyncio.wait_for(audio_started.wait(), timeout=1)
        for index in range(1, 64):
            encoded = base64.b64encode(str(index).encode()).decode()
            await websocket.push_json({"type": "chunk", "context_id": "ctx-long", "data": encoded})
        await websocket.push_json({"type": "done", "context_id": "ctx-long", "done": True})
        await wait_until(
            lambda: any(
                event["type"] == "tts.provider.message" and event.get("message_type") == "done"
                for event in events
            )
        )
        self.assertFalse(any(event["type"] == "tts.turn.failed" for event in events))

        release_audio.set()
        await wait_until(lambda: len(received) == 64)
        await wait_until(lambda: any(event["type"] == "tts.turn.done" for event in events))
        self.assertEqual(received, [str(index).encode() for index in range(64)])
        await provider.close()

    async def test_default_queue_absorbs_sixty_seconds_of_pcm_while_playout_is_blocked(self) -> None:
        websocket = FakeWebSocket()
        audio_started = asyncio.Event()
        release_audio = asyncio.Event()
        events: list[dict[str, Any]] = []
        audio_calls = 0
        delivered_bytes = 0

        async def blocked_audio(_: PlaybackToken, data: bytes) -> None:
            nonlocal audio_calls, delivered_bytes
            audio_started.set()
            await release_audio.wait()
            audio_calls += 1
            delivered_bytes += len(data)

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            blocked_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=lambda: "ctx-sixty-seconds",
        )
        token = PlaybackToken(22, 35)
        await provider.begin_turn(token)
        await provider.append_text(token, "Long answer")
        ten_ms_pcm48_mono16 = b"\x01\x00" * 480
        encoded = base64.b64encode(ten_ms_pcm48_mono16).decode()
        payload = {
            "type": "chunk",
            "context_id": "ctx-sixty-seconds",
            "data": encoded,
        }
        await websocket.push_json(payload)
        await asyncio.wait_for(audio_started.wait(), timeout=1)
        for _ in range(5_999):
            await websocket.push_json(payload)
        await websocket.push_json(
            {"type": "done", "context_id": "ctx-sixty-seconds", "done": True}
        )
        await wait_until(
            lambda: any(
                event["type"] == "tts.provider.message"
                and event.get("message_type") == "done"
                for event in events
            ),
            timeout=5,
        )
        self.assertEqual(provider._delivery.buffered_chunks, 6_000)
        self.assertEqual(provider._delivery.buffered_bytes, 60 * 48_000 * 2)
        self.assertFalse(any(event["type"] == "tts.turn.failed" for event in events))

        release_audio.set()
        await wait_until(lambda: audio_calls == 6_000, timeout=5)
        await wait_until(lambda: any(event["type"] == "tts.turn.done" for event in events))
        self.assertEqual(delivered_bytes, 60 * 48_000 * 2)
        await provider.close()

    async def test_cancel_soak_keeps_context_and_delivery_tombstones_bounded(self) -> None:
        websocket = FakeWebSocket()
        events: list[dict[str, Any]] = []

        async def no_audio(_: PlaybackToken, __: bytes) -> None:
            return None

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        context_ids = iter(f"ctx-{index}" for index in range(300))
        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            no_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=context_ids.__next__,
        )
        for index in range(300):
            token = PlaybackToken(index + 1, index + 1)
            await provider.begin_turn(token)
            await provider.cancel_turn(token, "soak")

        self.assertEqual(provider._contexts, {})
        self.assertIsNone(provider.active_context_id)
        self.assertLessEqual(
            len(provider._delivery._retired),
            provider._delivery._RETIRED_IDENTITY_LIMIT,
        )
        self.assertEqual(provider._delivery._terminal_queued, {})

        late_audio = base64.b64encode(b"late").decode()
        for index in range(100):
            await websocket.push_json(
                {"type": "chunk", "context_id": f"retired-{index}", "data": late_audio}
            )
        await wait_until(
            lambda: sum(
                event.get("reason") == "summary_capacity" for event in events
            )
            == 36
        )
        self.assertEqual(len(provider._stale_audio), provider._STALE_AUDIO_CONTEXT_LIMIT)
        await provider.close()

    async def test_socket_loss_emits_safe_failure_with_active_turn_identity(self) -> None:
        websocket = FakeWebSocket()
        websocket.close_code = 1011
        events: list[dict[str, Any]] = []

        async def no_audio(_: PlaybackToken, __: bytes) -> None:
            return None

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            no_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=lambda: "ctx-failed",
        )
        await provider.begin_turn(PlaybackToken(12, 88))
        await websocket.stop_reader()
        await wait_until(lambda: any(event["type"] == "tts.turn.failed" for event in events))

        failed = next(event for event in events if event["type"] == "tts.turn.failed")
        self.assertEqual(
            {key: failed[key] for key in ("turn_id", "generation", "context_id", "error_code", "close_code")},
            {
                "turn_id": 88,
                "generation": 12,
                "context_id": "ctx-failed",
                "error_code": "websocket_closed",
                "close_code": 1011,
            },
        )
        self.assertNotIn("error", failed)
        await provider.close()

    async def test_contextless_provider_error_fails_the_active_cartesia_turn(self) -> None:
        websocket = FakeWebSocket()
        events: list[dict[str, Any]] = []

        async def no_audio(_: PlaybackToken, __: bytes) -> None:
            return None

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        provider = CartesiaTTSProvider(
            CartesiaTTSOptions(api_key="test", voice_id="voice"),
            no_audio,
            on_event,
            connector=SequenceConnector(websocket),
            context_id_factory=lambda: "ctx-provider-error",
        )
        await provider.begin_turn(PlaybackToken(13, 89))
        await websocket.push_json({"type": "error", "message": "safe-redacted-by-caller"})
        await wait_until(lambda: any(event["type"] == "tts.turn.failed" for event in events))

        failed = next(event for event in events if event["type"] == "tts.turn.failed")
        self.assertEqual(failed["error_code"], "provider_error")
        self.assertEqual(failed["context_id"], "ctx-provider-error")
        self.assertIsNone(provider.active_context_id)
        await provider.close()


if __name__ == "__main__":
    unittest.main()
