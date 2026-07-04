"""Echo-cancellation unit tests: frame slicing and real APM efficacy."""

from __future__ import annotations

import math
import statistics
import unittest

from opencode_voice.audio_processing import FrameSlicer

try:
    from opencode_voice.audio_processing import EchoCanceller

    _CANCELLER_IMPORT_ERROR: Exception | None = None
    try:
        EchoCanceller(16_000)
        CANCELLER_AVAILABLE = True
    except Exception as exc:  # noqa: BLE001 - exotic platforms may lack the native module.
        CANCELLER_AVAILABLE = False
        _CANCELLER_IMPORT_ERROR = exc
except Exception as exc:  # noqa: BLE001
    CANCELLER_AVAILABLE = False
    _CANCELLER_IMPORT_ERROR = exc


SAMPLE_RATE = 16_000
FRAME_BYTES = (SAMPLE_RATE // 100) * 2  # 10 ms mono int16


def sine_frame(offset_samples: int, samples: int) -> bytes:
    out = bytearray()
    for i in range(samples):
        t = (offset_samples + i) / SAMPLE_RATE
        value = int(12_000 * math.sin(2 * math.pi * 440 * t) + 4_000 * math.sin(2 * math.pi * 950 * t))
        out += max(-32_768, min(32_767, value)).to_bytes(2, "little", signed=True)
    return bytes(out)


def rms(data: bytes) -> float:
    count = len(data) // 2
    total = 0
    for i in range(count):
        sample = int.from_bytes(data[2 * i : 2 * i + 2], "little", signed=True)
        total += sample * sample
    return (total / max(1, count)) ** 0.5


class FrameSlicerTests(unittest.TestCase):
    def test_exact_multiples_pass_straight_through(self) -> None:
        slicer = FrameSlicer(SAMPLE_RATE)

        frames = slicer.push(b"\x01\x02" * (SAMPLE_RATE // 100) * 8)  # one 80 ms mic chunk

        self.assertEqual(len(frames), 8)
        self.assertTrue(all(len(frame) == FRAME_BYTES for frame in frames))
        self.assertEqual(len(slicer.buffer), 0)

    def test_arbitrary_chunks_carry_the_remainder(self) -> None:
        slicer = FrameSlicer(SAMPLE_RATE)

        first = slicer.push(b"\x00" * (FRAME_BYTES + 100))
        second = slicer.push(b"\x00" * (FRAME_BYTES - 100))

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(len(slicer.buffer), 0)

    def test_short_chunk_stays_buffered(self) -> None:
        slicer = FrameSlicer(SAMPLE_RATE)

        self.assertEqual(slicer.push(b"\x00" * 100), [])
        self.assertEqual(len(slicer.buffer), 100)


@unittest.skipUnless(CANCELLER_AVAILABLE, f"echo canceller unavailable: {_CANCELLER_IMPORT_ERROR!r}")
class EchoCancellerTests(unittest.TestCase):
    def test_render_reference_collapses_the_echo(self) -> None:
        """Feed the same signal as render reference and mic capture: after the
        canceller converges, the capture output must be near-silent."""
        canceller = EchoCanceller(SAMPLE_RATE)
        canceller.set_stream_delay_ms(0)
        samples_per_frame = SAMPLE_RATE // 100

        in_rms: list[float] = []
        out_rms: list[float] = []
        for index in range(300):
            frame = sine_frame(index * samples_per_frame, samples_per_frame)
            canceller.process_render(frame)
            out = canceller.process_capture(frame)
            in_rms.append(rms(frame))
            out_rms.append(rms(out))

        converged_in = statistics.mean(in_rms[-50:])
        converged_out = statistics.mean(out_rms[-50:])
        self.assertGreater(converged_in, 1_000)  # the echo was loud
        self.assertLess(converged_out / converged_in, 0.05)  # and got cancelled

    def test_capture_output_preserves_length_for_frame_multiples(self) -> None:
        canceller = EchoCanceller(SAMPLE_RATE)
        chunk = b"\x00" * (FRAME_BYTES * 8)

        self.assertEqual(len(canceller.process_capture(chunk)), len(chunk))

    def test_stream_delay_is_clamped_and_applied_without_error(self) -> None:
        # set_stream_delay_ms stores the hint; process_capture re-asserts it
        # per frame, which is the cadence WebRTC accepts (a one-shot set at
        # speaker start raised on every real run — MOR-111).
        canceller = EchoCanceller(SAMPLE_RATE)

        canceller.set_stream_delay_ms(9_999)
        self.assertEqual(canceller.stream_delay_ms, 500)
        canceller.set_stream_delay_ms(-25)
        self.assertEqual(canceller.stream_delay_ms, 0)

        canceller.set_stream_delay_ms(120)
        canceller.process_render(b"\x00" * (FRAME_BYTES * 4))
        canceller.process_capture(b"\x00" * (FRAME_BYTES * 4))
        self.assertIsNone(canceller.delay_error)


if __name__ == "__main__":
    unittest.main()
