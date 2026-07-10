"""Echo cancellation for the native audio lane.

Native capture receives raw PortAudio frames, so this module applies WebRTC's
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


class Pcm16Resampler:
    """Small streaming wrapper around LiveKit's native PCM resampler."""

    def __init__(self, input_rate: int, output_rate: int) -> None:
        self.input_rate = input_rate
        self.output_rate = output_rate
        self._identity = input_rate == output_rate
        self._resampler: Any | None = None
        if not self._identity:
            from livekit import rtc  # deferred with the other native audio dependency

            self._resampler = rtc.AudioResampler(input_rate, output_rate, num_channels=1)

    def push(self, data: bytes) -> bytes:
        if self._identity:
            return data
        assert self._resampler is not None
        return b"".join(bytes(frame.data) for frame in self._resampler.push(bytearray(data)))

    def flush(self) -> bytes:
        if self._identity:
            return b""
        assert self._resampler is not None
        return b"".join(bytes(frame.data) for frame in self._resampler.flush())


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
        self.stream_delay_ms: int | None = None
        self.delay_error: str | None = None

    def set_stream_delay_ms(self, delay_ms: int) -> None:
        # WebRTC accepts 0-500ms and wants the value re-asserted alongside
        # every process_stream call, so we only store it here and apply it
        # per frame in process_capture (livekit's media_devices does the same).
        self.stream_delay_ms = min(500, max(0, int(delay_ms)))

    def process_capture(self, data: bytes) -> bytes:
        out = bytearray()
        for frame_bytes in self.capture_slicer.push(data):
            frame = self._rtc.AudioFrame(frame_bytes, self.sample_rate, 1, self.samples_per_frame)
            if self.stream_delay_ms is not None:
                try:
                    self.apm.set_stream_delay_ms(self.stream_delay_ms)
                except Exception as exc:  # noqa: BLE001 - delay hint is an optimization only.
                    if self.delay_error is None:
                        self.delay_error = repr(exc)
            self.apm.process_stream(frame)
            out.extend(frame_bytes)
        return bytes(out)

    def process_render(self, data: bytes) -> None:
        for frame_bytes in self.render_slicer.push(data):
            frame = self._rtc.AudioFrame(frame_bytes, self.sample_rate, 1, self.samples_per_frame)
            self.apm.process_reverse_stream(frame)
