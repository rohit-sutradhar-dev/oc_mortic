from __future__ import annotations

import asyncio
import math
import struct
import unittest
from typing import Any

from opencode_voice.device_audio import DeviceAudioOptions, PersistentDeviceAudioEngine
from opencode_voice.playback import PlaybackToken


class FakeRawStream:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        self.outputs: list[bytes] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def tick(
        self,
        capture: bytes | None = None,
        *,
        status: Any = None,
        time_info: dict[str, float] | None = None,
    ) -> bytes:
        frames = int(self.kwargs["blocksize"])
        frame_bytes = frames * 2
        indata = capture if capture is not None else bytes(frame_bytes)
        outdata = bytearray(frame_bytes)
        self.kwargs["callback"](
            indata,
            outdata,
            frames,
            time_info or {"inputBufferAdcTime": 1.0, "outputBufferDacTime": 1.05},
            status,
        )
        output = bytes(outdata)
        self.outputs.append(output)
        return output


class RecordingStreamFactory:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.stream: FakeRawStream | None = None

    def __call__(self, **kwargs: Any) -> FakeRawStream:
        if self.fail:
            raise RuntimeError("device unavailable")
        self.stream = FakeRawStream(**kwargs)
        return self.stream


async def wait_until(predicate: Any, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


class PersistentDeviceAudioEngineTests(unittest.IsolatedAsyncioTestCase):
    FRAME = b"\x01\x00" * 480
    SILENCE = bytes(960)

    def make_engine(
        self,
        *,
        options: DeviceAudioOptions | None = None,
        factory: RecordingStreamFactory | None = None,
        render_events: list[bytes] | None = None,
        capture_events: list[bytes] | None = None,
        first_events: list[PlaybackToken] | None = None,
        drain_events: list[PlaybackToken] | None = None,
        playback_events: list[dict[str, Any]] | None = None,
    ) -> tuple[PersistentDeviceAudioEngine, RecordingStreamFactory]:
        stream_factory = factory or RecordingStreamFactory()
        rendered = render_events if render_events is not None else []
        captured = capture_events if capture_events is not None else []
        first = first_events if first_events is not None else []
        drained = drain_events if drain_events is not None else []
        lifecycle = playback_events if playback_events is not None else []

        def on_render(data: bytes) -> None:
            rendered.append(data)

        async def on_capture(data: bytes) -> None:
            captured.append(data)

        async def on_first_frame(token: PlaybackToken) -> None:
            first.append(token)

        async def on_drain(token: PlaybackToken) -> None:
            drained.append(token)

        async def on_event(event: dict[str, Any]) -> None:
            lifecycle.append(event)

        engine = PersistentDeviceAudioEngine(
            options or DeviceAudioOptions(),
            on_render,
            on_capture,
            on_first_frame,
            on_drain,
            on_event=on_event,
            stream_factory=stream_factory,
        )
        return engine, stream_factory

    async def test_start_opens_exact_ten_ms_synchronized_duplex_stream(self) -> None:
        engine, factory = self.make_engine()

        self.assertTrue(await engine.start())

        assert factory.stream is not None
        kwargs = factory.stream.kwargs
        self.assertEqual(kwargs["samplerate"], 48_000)
        self.assertEqual(kwargs["blocksize"], 480)
        self.assertEqual(kwargs["channels"], 1)
        self.assertEqual(kwargs["dtype"], "int16")
        self.assertTrue(factory.stream.started)
        await engine.close()
        self.assertTrue(factory.stream.stopped)
        self.assertTrue(factory.stream.closed)

    async def test_device_open_failure_returns_false_for_half_duplex_fallback(self) -> None:
        factory = RecordingStreamFactory(fail=True)
        engine, _ = self.make_engine(factory=factory)

        self.assertFalse(await engine.start())
        self.assertIn("device unavailable", engine.last_error or "")
        await engine.close()

    async def test_every_tick_renders_exact_silence_before_dispatching_paired_capture(self) -> None:
        order: list[tuple[str, bytes]] = []
        factory = RecordingStreamFactory()

        def on_render(data: bytes) -> None:
            order.append(("render", data))

        async def on_capture(data: bytes) -> None:
            order.append(("capture", data))

        engine = PersistentDeviceAudioEngine(
            DeviceAudioOptions(),
            on_render,
            on_capture,
            stream_factory=factory,
        )
        await engine.start()
        assert factory.stream is not None
        capture = b"\x22\x00" * 480

        output = factory.stream.tick(
            capture,
            time_info={"inputBufferAdcTime": 1.0, "outputBufferDacTime": 1.06},
        )
        await wait_until(lambda: len(order) == 2)

        self.assertEqual(output, self.SILENCE)
        self.assertEqual(order, [("render", self.SILENCE), ("capture", capture)])
        self.assertEqual(engine.stream_delay_ms, 60)
        await engine.close()

    async def test_capture_gate_sends_silence_without_stopping_output_stream(self) -> None:
        captured: list[bytes] = []
        engine, factory = self.make_engine(capture_events=captured)
        await engine.start()
        assert factory.stream is not None

        engine.set_capture_enabled(False)
        self.assertEqual(factory.stream.tick(b"\x22\x00" * 480), self.SILENCE)
        await wait_until(lambda: len(captured) == 1)

        self.assertEqual(captured, [self.SILENCE])
        self.assertFalse(engine.capture_enabled)
        self.assertTrue(factory.stream.started)
        self.assertFalse(factory.stream.stopped)

        engine.set_capture_enabled(True)
        factory.stream.tick(b"\x33\x00" * 480)
        await wait_until(lambda: len(captured) == 2)
        self.assertEqual(captured[-1], b"\x33\x00" * 480)
        await engine.close()

    async def test_eighty_ms_prebuffer_plays_once_and_drains_after_starvation_grace(self) -> None:
        first: list[PlaybackToken] = []
        drained: list[PlaybackToken] = []
        engine, factory = self.make_engine(first_events=first, drain_events=drained)
        await engine.start()
        assert factory.stream is not None
        token = PlaybackToken(0, 7)
        self.assertTrue(engine.begin_turn(token))
        self.assertTrue(await engine.play(self.FRAME * 8, token))
        self.assertTrue(engine.mark_turn_terminal(token))

        for _ in range(8):
            self.assertEqual(factory.stream.tick(), self.FRAME)
        await wait_until(lambda: first == [token])

        for _ in range(24):
            self.assertEqual(factory.stream.tick(), self.SILENCE)
        await asyncio.sleep(0)
        self.assertEqual(drained, [])
        self.assertEqual(factory.stream.tick(), self.SILENCE)
        await wait_until(lambda: drained == [token])

        self.assertEqual(first, [token])
        self.assertEqual(engine.state, "idle")
        await engine.close()

    async def test_short_utterance_starts_when_eighty_ms_prebuffer_deadline_elapses(self) -> None:
        engine, factory = self.make_engine()
        await engine.start()
        assert factory.stream is not None
        await engine.play(self.FRAME, PlaybackToken(0, 1))

        for _ in range(7):
            self.assertEqual(factory.stream.tick(), self.SILENCE)
        self.assertEqual(factory.stream.tick(), self.FRAME)

        await engine.close()

    async def test_starvation_emits_silence_then_resumes_after_buffer_rebuild(self) -> None:
        first: list[PlaybackToken] = []
        drained: list[PlaybackToken] = []
        lifecycle: list[dict[str, Any]] = []
        engine, factory = self.make_engine(
            first_events=first, drain_events=drained, playback_events=lifecycle
        )
        await engine.start()
        assert factory.stream is not None
        token = PlaybackToken(0, 1)
        await engine.play(self.FRAME * 8, token)
        for _ in range(8):
            factory.stream.tick()

        self.assertEqual(factory.stream.tick(), self.SILENCE)
        await engine.play(self.FRAME * 3, token)
        for _ in range(5):
            self.assertEqual(factory.stream.tick(), self.SILENCE)
        await engine.play(self.FRAME * 5, token)

        self.assertEqual(factory.stream.tick(), self.FRAME)
        await wait_until(lambda: first == [token])
        self.assertEqual(first, [token])
        self.assertEqual(drained, [])
        self.assertEqual(engine.state, "playing")
        await wait_until(lambda: len(lifecycle) >= 3)
        self.assertEqual(
            [event["type"] for event in lifecycle[:3]],
            ["playback.burst.start", "playback.starved", "playback.resumed"],
        )
        self.assertEqual(len({event["playback_burst_id"] for event in lifecycle[:3]}), 1)
        await engine.close()

    async def test_nonterminal_starvation_never_manufactures_a_new_burst(self) -> None:
        drained: list[PlaybackToken] = []
        lifecycle: list[dict[str, Any]] = []
        engine, factory = self.make_engine(drain_events=drained, playback_events=lifecycle)
        await engine.start()
        assert factory.stream is not None
        token = PlaybackToken(0, 4)
        self.assertTrue(engine.begin_turn(token))
        await engine.play(self.FRAME * 8, token)
        for _ in range(8):
            factory.stream.tick()
        for _ in range(40):
            self.assertEqual(factory.stream.tick(), self.SILENCE)
        await asyncio.sleep(0)

        self.assertEqual(drained, [])
        self.assertEqual(engine.state, "starved")
        await engine.play(self.FRAME * 3, token)
        for _ in range(10):
            self.assertEqual(factory.stream.tick(), self.SILENCE)
        self.assertEqual(engine.state, "starved")
        await engine.play(self.FRAME * 5, token)
        self.assertEqual(factory.stream.tick(), self.FRAME)
        await wait_until(lambda: any(e["type"] == "playback.resumed" for e in lifecycle))
        burst_ids = {
            event["playback_burst_id"]
            for event in lifecycle
            if event["type"] in {"playback.burst.start", "playback.starved", "playback.resumed"}
        }
        self.assertEqual(len(burst_ids), 1)

        self.assertTrue(engine.mark_turn_terminal(token))
        for _ in range(7):
            factory.stream.tick()
        for _ in range(25):
            factory.stream.tick()
        await wait_until(lambda: drained == [token])
        await engine.close()

    async def test_invalidation_emits_cancelled_for_the_active_burst(self) -> None:
        lifecycle: list[dict[str, Any]] = []
        engine, factory = self.make_engine(playback_events=lifecycle)
        await engine.start()
        assert factory.stream is not None
        token = PlaybackToken(0, 9)
        await engine.play(self.FRAME * 8, token)
        factory.stream.tick()
        await wait_until(lambda: bool(lifecycle))

        engine.invalidate_generation(1)
        await wait_until(lambda: any(event["type"] == "playback.cancelled" for event in lifecycle))

        cancelled = next(event for event in lifecycle if event["type"] == "playback.cancelled")
        self.assertEqual(cancelled["turn_id"], 9)
        self.assertEqual(cancelled["playback_generation"], 0)
        await engine.close()

    async def test_duck_ramps_to_minus_eighteen_db_without_pausing_clock(self) -> None:
        first: list[PlaybackToken] = []
        engine, factory = self.make_engine(first_events=first)
        await engine.start()
        assert factory.stream is not None
        token = PlaybackToken(0, 1)
        loud_frame = struct.pack("<h", 16_384) * 480
        await engine.play(loud_frame * 8, token)
        engine.set_ducked(True)

        first_ramp = factory.stream.tick()
        second_ramp = factory.stream.tick()
        steady = factory.stream.tick()
        self.assertNotEqual(first_ramp, self.SILENCE)
        self.assertNotEqual(second_ramp, self.SILENCE)
        self.assertAlmostEqual(struct.unpack_from("<h", steady)[0], 2_062, delta=2)
        self.assertEqual(engine.buffered_frames, 5)
        self.assertTrue(engine.is_audible())
        engine.set_ducked(False)
        self.assertNotEqual(factory.stream.tick(), self.SILENCE)
        await wait_until(lambda: first == [token])
        self.assertTrue(engine.is_audible())
        await engine.close()

    async def test_invalidation_wakes_full_buffer_and_rechecks_generation_after_wait(self) -> None:
        options = DeviceAudioOptions(max_buffer_frames=2, start_buffer_ms=20)
        engine, factory = self.make_engine(options=options)
        await engine.start()
        assert factory.stream is not None
        engine.set_ducked(True)
        stale = PlaybackToken(0, 1)
        blocked = asyncio.create_task(engine.play(self.FRAME * 3, stale))
        await wait_until(lambda: engine.buffered_frames == 2)
        self.assertFalse(blocked.done())

        self.assertEqual(engine.invalidate_generation(1), 1)
        self.assertFalse(await blocked)
        self.assertEqual(engine.buffered_frames, 0)
        self.assertFalse(await engine.play(self.FRAME, stale))

        current = PlaybackToken(1, 2)
        self.assertTrue(await engine.play(self.FRAME * 2, current))
        engine.set_ducked(False)
        self.assertEqual(factory.stream.tick(), self.FRAME)
        await engine.close()

    async def test_provider_pcm_is_resampled_to_device_clock_before_buffering(self) -> None:
        options = DeviceAudioOptions(provider_sample_rate=16_000, device_sample_rate=48_000)
        rendered: list[bytes] = []
        engine, factory = self.make_engine(options=options, render_events=rendered)
        await engine.start()
        assert factory.stream is not None
        samples = [int(math.sin(index * 2 * math.pi * 440 / 16_000) * 10_000) for index in range(1_280)]
        pcm_80ms = b"".join(struct.pack("<h", sample) for sample in samples)

        self.assertTrue(await engine.play(pcm_80ms, PlaybackToken(0, 1)))
        for _ in range(10):
            factory.stream.tick()

        self.assertTrue(any(any(frame) for frame in factory.stream.outputs))
        self.assertTrue(all(len(frame) == 960 for frame in factory.stream.outputs))
        self.assertEqual(rendered, factory.stream.outputs)
        await engine.close()

    async def test_provider_terminal_pads_partial_ten_ms_tail_instead_of_clipping_it(self) -> None:
        drained: list[PlaybackToken] = []
        engine, factory = self.make_engine(drain_events=drained)
        await engine.start()
        assert factory.stream is not None
        token = PlaybackToken(0, 12)
        self.assertTrue(engine.begin_turn(token))
        # Five milliseconds cannot form a device frame until provider EOF.
        half_frame = self.FRAME[: len(self.FRAME) // 2]
        self.assertTrue(await engine.play(half_frame, token))
        self.assertEqual(engine.buffered_frames, 0)

        self.assertTrue(await engine.finish_turn(token))
        self.assertEqual(engine.buffered_frames, 1)
        for _ in range(7):
            self.assertEqual(factory.stream.tick(), self.SILENCE)
        rendered = factory.stream.tick()
        self.assertEqual(rendered[: len(half_frame)], half_frame)
        self.assertEqual(rendered[len(half_frame) :], bytes(len(half_frame)))
        for _ in range(25):
            factory.stream.tick()
        await wait_until(lambda: drained == [token])
        await engine.close()

    async def test_close_cancels_inflight_async_capture_callbacks(self) -> None:
        factory = RecordingStreamFactory()
        callback_started = asyncio.Event()
        never = asyncio.Event()

        def on_render(_: bytes) -> None:
            return None

        async def on_capture(_: bytes) -> None:
            callback_started.set()
            await never.wait()

        engine = PersistentDeviceAudioEngine(
            DeviceAudioOptions(),
            on_render,
            on_capture,
            stream_factory=factory,
        )
        await engine.start()
        assert factory.stream is not None
        factory.stream.tick()
        await callback_started.wait()

        await asyncio.wait_for(engine.close(), timeout=0.2)

        self.assertTrue(factory.stream.closed)

    async def test_capture_actor_preserves_device_tick_order_across_awaits(self) -> None:
        factory = RecordingStreamFactory()
        release_first = asyncio.Event()
        captured: list[bytes] = []

        async def on_capture(data: bytes) -> None:
            captured.append(data)
            if len(captured) == 1:
                await release_first.wait()

        engine = PersistentDeviceAudioEngine(
            DeviceAudioOptions(),
            lambda _data: None,
            on_capture,
            stream_factory=factory,
        )
        await engine.start()
        assert factory.stream is not None
        first = b"\x01\x00" * 480
        second = b"\x02\x00" * 480
        factory.stream.tick(first)
        factory.stream.tick(second)
        await wait_until(lambda: captured == [first])
        release_first.set()
        await wait_until(lambda: captured == [first, second])

        self.assertEqual(engine.capture_dropped_frames, 0)
        await engine.close()


if __name__ == "__main__":
    unittest.main()
