from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from opencode_voice.flux_transport import (
    FluxTransport,
    FluxTransportOptions,
    connect_flux_websocket,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeFluxWebSocket:
    STOP = object()

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.send_gate = asyncio.Event()
        self.send_gate.set()
        self.send_error: Exception | None = None
        self.closed = False

    async def send(self, data: bytes) -> None:
        await self.send_gate.wait()
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)

    def __aiter__(self) -> "FakeFluxWebSocket":
        return self

    async def __anext__(self) -> Any:
        item = await self.incoming.get()
        if item is self.STOP:
            raise StopAsyncIteration
        return item

    async def push(self, payload: dict[str, Any]) -> None:
        await self.incoming.put(json.dumps(payload))

    async def stop_reader(self) -> None:
        await self.incoming.put(self.STOP)

    async def close(self) -> None:
        self.closed = True


class SequenceConnector:
    def __init__(self, *websockets: FakeFluxWebSocket) -> None:
        self.websockets = list(websockets)
        self.calls = 0

    async def __call__(self, _url: str, _headers: dict[str, str]) -> FakeFluxWebSocket:
        self.calls += 1
        if not self.websockets:
            raise RuntimeError("no fake websocket remaining")
        return self.websockets.pop(0)


async def wait_until(predicate: Any, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


class FluxTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_options_repr_never_contains_api_key(self) -> None:
        self.assertNotIn("super-secret", repr(FluxTransportOptions(api_key="super-secret")))

    async def test_websocket_keepalive_tolerates_ordinary_network_jitter(self) -> None:
        websocket = object()
        connect = AsyncMock(return_value=websocket)
        with patch("opencode_voice.flux_transport.websockets.connect", connect):
            result = await connect_flux_websocket("wss://flux.test", {"Authorization": "Token test"})

        self.assertIs(result, websocket)
        self.assertEqual(connect.await_args.kwargs["ping_interval"], 20)
        self.assertEqual(connect.await_args.kwargs["ping_timeout"], 20)
        self.assertEqual(connect.await_args.kwargs["happy_eyeballs_delay"], 0.25)
        self.assertEqual(connect.await_args.kwargs["interleave"], 1)

    async def test_submit_packetizes_exact_eighty_ms_without_network_wait(self) -> None:
        websocket = FakeFluxWebSocket()
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        transport = FluxTransport(
            FluxTransportOptions(api_key="test"),
            on_event,
            connector=SequenceConnector(websocket),
        )
        await transport.start()
        self.assertEqual(await transport.wait_connected(timeout_sec=0.2), 1)
        await wait_until(lambda: transport.health_snapshot().state == "connected")
        half_packet = b"\x01\x02" * 640  # 40 ms at 16 kHz mono PCM16

        self.assertTrue(transport.submit(half_packet))
        await asyncio.sleep(0)
        self.assertEqual(websocket.sent, [])
        self.assertTrue(transport.submit(half_packet))
        await wait_until(lambda: len(websocket.sent) == 1)

        self.assertEqual(websocket.sent, [half_packet + half_packet])
        self.assertEqual(len(websocket.sent[0]), 2_560)
        snapshot = transport.health_snapshot()
        self.assertEqual(snapshot.submitted_packets, 1)
        self.assertEqual(snapshot.sent_packets, 1)
        await transport.close()

    async def test_queue_keeps_only_the_newest_four_hundred_eighty_ms(self) -> None:
        telemetry: list[dict[str, Any]] = []

        async def no_event(_: dict[str, Any]) -> None:
            return None

        transport = FluxTransport(
            FluxTransportOptions(api_key="test", max_fresh_audio_ms=500),
            no_event,
            on_telemetry=telemetry.append,
        )
        packet = b"\x00" * transport.packet_bytes
        for _ in range(10):
            self.assertTrue(transport.submit(packet))

        snapshot = transport.health_snapshot()
        self.assertEqual(snapshot.queued_packets, 6)
        self.assertEqual(snapshot.queued_audio_ms, 480)
        self.assertEqual(snapshot.dropped_overflow_packets, 4)
        self.assertTrue(any(item["reason"] == "queue_overflow" for item in telemetry))
        await transport.close()

    async def test_stale_audio_is_dropped_with_telemetry(self) -> None:
        clock = FakeClock()
        telemetry: list[dict[str, Any]] = []

        async def no_event(_: dict[str, Any]) -> None:
            return None

        transport = FluxTransport(
            FluxTransportOptions(api_key="test"),
            no_event,
            on_telemetry=telemetry.append,
            clock=clock,
        )
        packet = b"\x00" * transport.packet_bytes
        transport.submit(packet, captured_at=0.0)
        transport.submit(packet, captured_at=0.0)
        clock.now = 0.501
        transport.submit(packet, captured_at=clock.now)

        snapshot = transport.health_snapshot()
        self.assertEqual(snapshot.dropped_stale_packets, 2)
        self.assertEqual(snapshot.queued_packets, 1)
        self.assertTrue(any(item["reason"] == "stale" and item["packets"] == 2 for item in telemetry))
        await transport.close()

    async def test_stalled_send_is_timed_out_dropped_and_reconnected(self) -> None:
        first = FakeFluxWebSocket()
        first.send_gate.clear()
        second = FakeFluxWebSocket()
        connector = SequenceConnector(first, second)
        sleeps: list[float] = []

        async def on_event(_: dict[str, Any]) -> None:
            return None

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        transport = FluxTransport(
            FluxTransportOptions(api_key="test", send_timeout_sec=0.01),
            on_event,
            connector=connector,
            sleep=fake_sleep,
        )
        await transport.start()
        await wait_until(lambda: transport.epoch == 1)
        packet = b"\x11" * transport.packet_bytes
        transport.submit(packet)
        await wait_until(lambda: transport.epoch == 2)

        snapshot = transport.health_snapshot()
        self.assertEqual(snapshot.send_failures, 1)
        self.assertEqual(snapshot.dropped_uncertain_packets, 1)
        self.assertEqual(sleeps[0], 0.2)
        self.assertTrue(first.closed)

        transport.submit(packet)
        await wait_until(lambda: len(second.sent) == 1)
        self.assertEqual(second.sent, [packet])
        await transport.close()
        self.assertTrue(second.closed)
        self.assertFalse(transport.running)

    async def test_reader_reconnects_and_events_carry_connection_epoch(self) -> None:
        first = FakeFluxWebSocket()
        second = FakeFluxWebSocket()
        connector = SequenceConnector(first, second)
        events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            events.append(event)

        async def no_wait(_: float) -> None:
            return None

        transport = FluxTransport(
            FluxTransportOptions(api_key="test"),
            on_event,
            connector=connector,
            sleep=no_wait,
        )
        await transport.start()
        await wait_until(lambda: transport.epoch == 1)
        await first.push({"type": "StartOfTurn", "turn_index": 1})
        await wait_until(lambda: any(event.get("type") == "speech.start" for event in events))
        await first.stop_reader()
        await wait_until(lambda: transport.epoch == 2)
        await second.push({"type": "StartOfTurn", "turn_index": 2})
        await wait_until(
            lambda: len([event for event in events if event.get("type") == "speech.start"]) == 2
        )

        speech_events = [event for event in events if event.get("type") == "speech.start"]
        self.assertEqual([event["transport_epoch"] for event in speech_events], [1, 2])
        self.assertEqual([event["turn_index"] for event in speech_events], [1, 2])
        await transport.close()

    async def test_close_cancels_a_stalled_send_and_awaits_socket_tasks(self) -> None:
        websocket = FakeFluxWebSocket()
        websocket.send_gate.clear()

        async def no_event(_: dict[str, Any]) -> None:
            return None

        transport = FluxTransport(
            FluxTransportOptions(api_key="test", send_timeout_sec=10),
            no_event,
            connector=SequenceConnector(websocket),
        )
        await transport.start()
        await wait_until(lambda: transport.epoch == 1)
        transport.submit(b"\x00" * transport.packet_bytes)
        await asyncio.sleep(0.01)

        await asyncio.wait_for(transport.close(), timeout=0.2)

        self.assertTrue(websocket.closed)
        self.assertFalse(transport.running)
        self.assertEqual(transport.health_snapshot().state, "closed")

    async def test_invalid_pcm_is_rejected_and_close_is_idempotent(self) -> None:
        async def no_event(_: dict[str, Any]) -> None:
            return None

        transport = FluxTransport(FluxTransportOptions(api_key="test"), no_event)
        with self.assertRaises(ValueError):
            transport.submit(b"\x00")
        await transport.close()
        await transport.close()
        self.assertFalse(transport.submit(b"\x00\x00"))
        self.assertEqual(transport.health_snapshot().state, "closed")


if __name__ == "__main__":
    unittest.main()
