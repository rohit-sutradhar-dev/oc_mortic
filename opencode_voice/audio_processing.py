"""Echo cancellation for the native audio lane.

The browser lane never heard itself because getUserMedia applies WebRTC's
echo canceller by default; the native lane captures raw PortAudio frames.
This module gives the native lane the same processing: WebRTC's
AudioProcessingModule (shipped prebuilt inside the `livekit` wheel) fed with
both the mic capture path and the TTS render path. APM frames are exactly
10 ms, so both paths re-slice arbitrary chunk sizes.
"""

from __future__ import annotations

from typing import Any

BYTES_PER_SAMPLE = 2  # mono int16 everywhere in the helper


class FrameSlicer:
    """Re-buffer arbitrary byte chunks into exact 10 ms frames."""

    def __init__(self, sample_rate: int) -> None:
        self.samples_per_frame = sample_rate // 100
        self.frame_bytes = self.samples_per_frame * BYTES_PER_SAMPLE
        self.buffer = bytearray()

    def push(self, data: bytes) -> list[bytearray]:
        self.buffer.extend(data)
        frames: list[bytearray] = []
        while len(self.buffer) >= self.frame_bytes:
            frames.append(bytearray(self.buffer[: self.frame_bytes]))
            del self.buffer[: self.frame_bytes]
        return frames


class EchoCanceller:
    """Full-duplex echo cancellation around the native mic/speaker pair.

    `process_capture` takes raw mic bytes and returns echo-cancelled bytes
    (in 10 ms multiples; a partial tail stays buffered). `process_render`
    must be fed every chunk that is written to the speaker so the canceller
    knows what audio could leak back into the mic.
    """

    def __init__(self, sample_rate: int) -> None:
        from livekit import rtc  # deferred: optional native dependency

        self._rtc: Any = rtc
        self.sample_rate = sample_rate
        self.samples_per_frame = sample_rate // 100
        self.apm = rtc.AudioProcessingModule(
            echo_cancellation=True,
            noise_suppression=True,
            high_pass_filter=True,
            auto_gain_control=True,
        )
        self.capture_slicer = FrameSlicer(sample_rate)
        self.render_slicer = FrameSlicer(sample_rate)

    def process_capture(self, data: bytes) -> bytes:
        out = bytearray()
        for frame_bytes in self.capture_slicer.push(data):
            frame = self._rtc.AudioFrame(frame_bytes, self.sample_rate, 1, self.samples_per_frame)
            self.apm.process_stream(frame)
            out.extend(frame_bytes)
        return bytes(out)

    def process_render(self, data: bytes) -> None:
        for frame_bytes in self.render_slicer.push(data):
            frame = self._rtc.AudioFrame(frame_bytes, self.sample_rate, 1, self.samples_per_frame)
            self.apm.process_reverse_stream(frame)

    def set_stream_delay_ms(self, delay_ms: int) -> None:
        self.apm.set_stream_delay_ms(max(0, int(delay_ms)))
