from __future__ import annotations

import asyncio
import threading
import time
from array import array
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from opencode_voice.audio_processing import Pcm16Resampler
from opencode_voice.playback import PlaybackToken

RenderCallback = Callable[[bytes], None]
CaptureCallback = Callable[[bytes], Awaitable[None]]
CaptureProcessor = Callable[[bytes], bytes]
TokenCallback = Callable[[PlaybackToken], Awaitable[None]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
StreamFactory = Callable[..., Any]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class DeviceAudioOptions:
    device_sample_rate: int = 48_000
    provider_sample_rate: int = 48_000
    frame_ms: int = 10
    max_buffer_frames: int = 100
    start_buffer_ms: int = 80
    starvation_grace_ms: int = 250
    input_device: int | str | None = None
    output_device: int | str | None = None


@dataclass(frozen=True, slots=True)
class _QueuedFrame:
    token: PlaybackToken
    data: bytes


def sounddevice_raw_stream(**kwargs: Any) -> Any:
    import sounddevice as sd  # type: ignore[import-not-found]

    return sd.RawStream(**kwargs)


class PersistentDeviceAudioEngine:
    """Persistent, device-clocked mono PCM16 duplex audio engine.

    The PortAudio callback emits one exact 10 ms render frame on every tick,
    including timed silence. Provider audio is bounded to one second by
    default; generation checks occur after every capacity wait and once more
    immediately before an acquired frame is committed to the device buffer.
    """

    BYTES_PER_SAMPLE = 2

    def __init__(
        self,
        options: DeviceAudioOptions,
        on_render: RenderCallback,
        on_capture: CaptureCallback,
        on_first_frame: TokenCallback | None = None,
        on_drain: TokenCallback | None = None,
        *,
        capture_processor: CaptureProcessor | None = None,
        on_event: EventCallback | None = None,
        stream_factory: StreamFactory = sounddevice_raw_stream,
        clock: Clock = time.monotonic,
    ) -> None:
        if options.frame_ms != 10:
            raise ValueError("the device/AEC clock requires exact 10 ms frames")
        if options.device_sample_rate <= 0 or options.provider_sample_rate <= 0:
            raise ValueError("sample rates must be positive")
        if options.device_sample_rate % 100:
            raise ValueError("device sample rate must form an integral 10 ms frame")
        if options.max_buffer_frames <= 0:
            raise ValueError("max_buffer_frames must be positive")
        if options.start_buffer_ms < options.frame_ms:
            raise ValueError("start buffer must be at least one frame")
        if options.starvation_grace_ms < options.frame_ms:
            raise ValueError("starvation grace must be at least one frame")

        self.options = options
        self._on_render = on_render
        self._on_capture = on_capture
        self._on_first_frame = on_first_frame
        self._on_drain = on_drain
        self._capture_processor = capture_processor or (lambda data: data)
        self._on_event = on_event
        self._stream_factory = stream_factory
        self._clock = clock

        self.samples_per_frame = options.device_sample_rate // 100
        self.frame_bytes = self.samples_per_frame * self.BYTES_PER_SAMPLE
        self.start_frames = max(1, options.start_buffer_ms // options.frame_ms)
        self.starvation_frames = max(1, options.starvation_grace_ms // options.frame_ms)
        self.frame_sec = options.frame_ms / 1000
        self._silence = bytes(self.frame_bytes)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: Any = None
        self._queue: deque[_QueuedFrame] = deque()
        self._cue_queue: deque[_QueuedFrame] = deque()
        self._buffer_lock = threading.Lock()
        self._capacity_event = asyncio.Event()
        self._capacity_event.set()
        self._play_lock = asyncio.Lock()
        self._callback_tasks: set[asyncio.Task[Any]] = set()
        self._capture_queue: asyncio.Queue[bytes] | None = None
        self._capture_task: asyncio.Task[None] | None = None
        self._capture_dropped_frames = 0
        self._capture_enabled = True

        self._active_generation = 0
        self._resampler_token: PlaybackToken | None = None
        self._resampler: Pcm16Resampler | None = None
        self._pending_pcm = bytearray()
        self._state = "idle"
        self._ducked = False
        self._current_gain = 1.0
        self._target_gain = 1.0
        self._gain_ramp_frames = 0
        self._duck_gain = 10 ** (-18 / 20)
        self._prebuffer_ticks = 0
        self._starved_ticks = 0
        self._burst_token: PlaybackToken | None = None
        self._burst_sequence = 0
        self._burst_id: str | None = None
        # Queue exhaustion is only an underflow signal.  A burst may become
        # terminal only after its provider confirms done or failure; otherwise
        # a slow network packet would manufacture a new acoustic edge.
        self._terminal_tokens: dict[PlaybackToken, str] = {}
        self._first_frame_tokens: set[PlaybackToken] = set()
        self._audible_until = 0.0
        self._closed = False
        self._started = False
        self._last_error: str | None = None
        self._last_status: str | None = None
        self._stream_delay_ms = 0

    @property
    def state(self) -> str:
        with self._buffer_lock:
            return "ducked" if self._ducked else self._state

    @property
    def buffered_frames(self) -> int:
        with self._buffer_lock:
            return len(self._queue)

    @property
    def active_generation(self) -> int:
        with self._buffer_lock:
            return self._active_generation

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def last_status(self) -> str | None:
        return self._last_status

    @property
    def stream_delay_ms(self) -> int:
        return self._stream_delay_ms

    @property
    def capture_dropped_frames(self) -> int:
        return self._capture_dropped_frames

    @property
    def capture_enabled(self) -> bool:
        with self._buffer_lock:
            return self._capture_enabled

    def set_capture_enabled(self, enabled: bool) -> None:
        with self._buffer_lock:
            self._capture_enabled = bool(enabled)
        if not enabled and self._capture_queue is not None:
            while not self._capture_queue.empty():
                try:
                    self._capture_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def start(self) -> bool:
        if self._closed:
            return False
        if self._started and self._stream is not None:
            return True
        self._loop = asyncio.get_running_loop()
        self._capture_queue = asyncio.Queue(maxsize=100)
        self._capture_task = asyncio.create_task(self._capture_pump())
        stream_kwargs: dict[str, Any] = {
            "samplerate": self.options.device_sample_rate,
            "blocksize": self.samples_per_frame,
            "channels": 1,
            "dtype": "int16",
            "latency": "low",
            "callback": self._audio_callback,
        }
        if self.options.input_device is not None or self.options.output_device is not None:
            stream_kwargs["device"] = (self.options.input_device, self.options.output_device)
        stream: Any = None
        try:
            stream = self._stream_factory(**stream_kwargs)
            stream.start()
        except Exception as exc:  # noqa: BLE001 - caller selects explicit half duplex fallback.
            self._last_error = repr(exc)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            self._stream = None
            self._started = False
            if self._capture_task:
                self._capture_task.cancel()
                await asyncio.gather(self._capture_task, return_exceptions=True)
            self._capture_task = None
            self._capture_queue = None
            return False
        self._stream = stream
        self._started = True
        return True

    async def play(self, data: bytes, token: PlaybackToken) -> bool:
        if len(data) % self.BYTES_PER_SAMPLE:
            raise ValueError("PCM16 data must contain complete samples")
        if not data:
            return self._token_is_current(token)
        if self._closed or not self._started:
            return False
        async with self._play_lock:
            if not self._token_is_current(token):
                return False
            frames = self._prepare_frames(data, token)
            for frame in frames:
                if not await self._enqueue_frame(_QueuedFrame(token, frame)):
                    return False
            return self._token_is_current(token)

    async def play_device_cue(self, data: bytes, token: PlaybackToken) -> bool:
        """Mix exact device-rate PCM without creating a speech burst.

        Cues share generation fencing and the final render/AEC reference, but
        never trigger first-speech or provider-drain callbacks.
        """

        if len(data) % self.BYTES_PER_SAMPLE:
            raise ValueError("PCM16 data must contain complete samples")
        if self._closed or not self._started or not self._token_is_current(token):
            return False
        frames = [data[index : index + self.frame_bytes] for index in range(0, len(data), self.frame_bytes)]
        if frames and len(frames[-1]) < self.frame_bytes:
            frames[-1] += bytes(self.frame_bytes - len(frames[-1]))
        with self._buffer_lock:
            if token.generation != self._active_generation:
                return False
            remaining = max(0, self.options.max_buffer_frames - len(self._cue_queue))
            if len(frames) > remaining:
                return False
            self._cue_queue.extend(_QueuedFrame(token, frame) for frame in frames)
        return True

    def begin_turn(self, token: PlaybackToken) -> bool:
        """Register a provider turn before its first PCM frame arrives."""

        with self._buffer_lock:
            if self._closed or token.generation != self._active_generation:
                return False
            self._terminal_tokens.pop(token, None)
            return True

    def mark_turn_terminal(self, token: PlaybackToken, outcome: str = "done") -> bool:
        """Allow a starved burst to drain once all admitted PCM is rendered."""

        with self._buffer_lock:
            if self._closed or token.generation != self._active_generation:
                return False
            self._terminal_tokens[token] = str(outcome or "done")
            return True

    async def finish_turn(self, token: PlaybackToken, outcome: str = "done") -> bool:
        """Flush resampler/frame tails before making provider EOF visible."""

        if self._closed or not self._started:
            return False
        async with self._play_lock:
            if not self._token_is_current(token):
                return False
            if self._resampler_token == token and self._resampler is not None:
                self._pending_pcm.extend(self._resampler.flush())
                while len(self._pending_pcm) >= self.frame_bytes:
                    frame = bytes(self._pending_pcm[: self.frame_bytes])
                    del self._pending_pcm[: self.frame_bytes]
                    if not await self._enqueue_frame(_QueuedFrame(token, frame)):
                        return False
                if self._pending_pcm:
                    tail = bytes(self._pending_pcm)
                    self._pending_pcm.clear()
                    padded = tail + bytes(self.frame_bytes - len(tail))
                    if not await self._enqueue_frame(_QueuedFrame(token, padded)):
                        return False
            return self.mark_turn_terminal(token, outcome)

    def turn_is_terminal(self, token: PlaybackToken) -> bool:
        with self._buffer_lock:
            return token in self._terminal_tokens

    def invalidate_generation(self, generation: int | None = None) -> int:
        """Atomically fence queued/acquired frames and wake blocked producers."""

        cancelled_event: dict[str, Any] | None = None
        with self._buffer_lock:
            target = self._active_generation + 1 if generation is None else int(generation)
            if target < self._active_generation:
                raise ValueError("playback generations must be monotonic")
            self._active_generation = target
            self._queue.clear()
            self._cue_queue.clear()
            self._state = "idle"
            self._prebuffer_ticks = 0
            self._starved_ticks = 0
            if self._burst_id is not None and self._burst_token is not None:
                cancelled_event = self._playback_event_locked(
                    "playback.cancelled", self._burst_token, self._burst_id
                )
            self._burst_token = None
            self._burst_id = None
            self._audible_until = 0.0
            self._first_frame_tokens = {
                token for token in self._first_frame_tokens if token.generation == target
            }
            self._terminal_tokens = {
                token: outcome
                for token, outcome in self._terminal_tokens.items()
                if token.generation == target
            }
            self._resampler_token = None
            self._resampler = None
            self._pending_pcm.clear()
            self._ducked = False
            self._current_gain = 1.0
            self._target_gain = 1.0
            self._gain_ramp_frames = 0
        self._signal_capacity()
        if cancelled_event is not None:
            self._schedule_event(cancelled_event)
        return target

    def set_ducked(self, ducked: bool) -> None:
        with self._buffer_lock:
            self._ducked = bool(ducked)
            self._target_gain = self._duck_gain if self._ducked else 1.0
            self._gain_ramp_frames = 2

    def is_audible(self, tail_sec: float = 0.3) -> bool:
        with self._buffer_lock:
            return (
                not self._closed
                and self._audible_until > 0
                and self._clock() < self._audible_until + max(0.0, tail_sec)
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._buffer_lock:
            self._queue.clear()
            self._cue_queue.clear()
            self._state = "closed"
            self._audible_until = 0.0
            self._pending_pcm.clear()
            self._resampler = None
            self._resampler_token = None
            self._terminal_tokens.clear()
        self._capacity_event.set()
        stream = self._stream
        self._stream = None
        self._started = False
        if stream is not None:
            try:
                stream.stop()
            except Exception as exc:  # noqa: BLE001 - teardown is best effort.
                self._last_error = repr(exc)
            try:
                stream.close()
            except Exception as exc:  # noqa: BLE001 - teardown is best effort.
                self._last_error = repr(exc)
        tasks = list(self._callback_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._callback_tasks.clear()
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
            await asyncio.gather(self._capture_task, return_exceptions=True)
        self._capture_task = None
        self._capture_queue = None

    def _prepare_frames(self, data: bytes, token: PlaybackToken) -> list[bytes]:
        if self._resampler_token != token:
            self._resampler_token = token
            self._resampler = Pcm16Resampler(
                self.options.provider_sample_rate,
                self.options.device_sample_rate,
            )
            self._pending_pcm.clear()
        assert self._resampler is not None
        self._pending_pcm.extend(self._resampler.push(data))
        frames: list[bytes] = []
        while len(self._pending_pcm) >= self.frame_bytes:
            frames.append(bytes(self._pending_pcm[: self.frame_bytes]))
            del self._pending_pcm[: self.frame_bytes]
        return frames

    async def _enqueue_frame(self, frame: _QueuedFrame) -> bool:
        while True:
            if self._closed:
                return False
            with self._buffer_lock:
                if frame.token.generation != self._active_generation:
                    return False
                if len(self._queue) < self.options.max_buffer_frames:
                    self._queue.append(frame)
                    if len(self._queue) >= self.options.max_buffer_frames:
                        self._capacity_event.clear()
                    return True
                self._capacity_event.clear()
            # Close/invalidation/callback drain all set this event. The token is
            # checked again immediately after the capacity wait and under the
            # same lock used by invalidate_generation.
            await self._capacity_event.wait()
            if not self._token_is_current(frame.token):
                return False

    def _token_is_current(self, token: PlaybackToken) -> bool:
        with self._buffer_lock:
            return not self._closed and token.generation == self._active_generation

    def _audio_callback(self, indata: Any, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            self._last_status = str(status)
        expected = self.samples_per_frame
        if frames != expected:
            self._last_error = f"unexpected_callback_frames:{frames}"
            render = bytes(frames * self.BYTES_PER_SAMPLE)
            first_token = None
            drain_token = None
            freed_capacity = False
            playback_events: list[dict[str, Any]] = []
        else:
            render, first_token, drain_token, freed_capacity, playback_events = self._next_render_frame()

        # Commit the exact PCM actually handed to PortAudio, then make that
        # same frame the AEC render reference before capture is dispatched.
        outdata[:] = render
        try:
            self._on_render(render)
        except Exception as exc:  # noqa: BLE001 - render telemetry must not break PortAudio.
            self._last_error = repr(exc)
        self._update_stream_delay(time_info)
        if not self._closed:
            try:
                capture = self._capture_processor(bytes(indata))
            except Exception as exc:  # noqa: BLE001 - fail safe to silence, never raw echo.
                self._last_error = repr(exc)
                capture = bytes(indata.nbytes if hasattr(indata, "nbytes") else len(indata))
            if not self.capture_enabled:
                capture = bytes(len(capture))
            self._schedule_capture(capture)
            if first_token is not None and self._on_first_frame is not None:
                self._schedule_async(self._on_first_frame, first_token)
            if drain_token is not None and self._on_drain is not None:
                self._schedule_async(self._on_drain, drain_token)
            for event in playback_events:
                self._schedule_event(event)
        if freed_capacity:
            self._signal_capacity()

    def _next_render_frame(
        self,
    ) -> tuple[bytes, PlaybackToken | None, PlaybackToken | None, bool, list[dict[str, Any]]]:
        candidate: _QueuedFrame | None = None
        drain_token: PlaybackToken | None = None
        freed_capacity = False
        playback_events: list[dict[str, Any]] = []
        with self._buffer_lock:
            while self._queue and self._queue[0].token.generation != self._active_generation:
                self._queue.popleft()
                freed_capacity = True

            if not self._closed:
                if self._state == "idle" and self._queue:
                    self._state = "prebuffering"
                    self._prebuffer_ticks = 0
                if self._state == "prebuffering":
                    self._prebuffer_ticks += 1
                    if len(self._queue) >= self.start_frames or self._prebuffer_ticks >= self.start_frames:
                        self._state = "playing"
                if self._state == "playing":
                    if self._queue:
                        was_full = len(self._queue) >= self.options.max_buffer_frames
                        candidate = self._queue.popleft()
                        freed_capacity = freed_capacity or was_full
                        self._burst_token = candidate.token
                        if self._burst_id is None:
                            self._burst_sequence += 1
                            self._burst_id = f"burst_{self._burst_sequence:06d}"
                            playback_events.append(
                                self._playback_event_locked(
                                    "playback.burst.start", candidate.token, self._burst_id
                                )
                            )
                    else:
                        self._state = "starved"
                        self._starved_ticks = 1
                        if (
                            self._burst_id is not None
                            and self._burst_token is not None
                            and self._burst_token not in self._terminal_tokens
                        ):
                            playback_events.append(
                                self._playback_event_locked(
                                    "playback.starved", self._burst_token, self._burst_id
                                )
                            )
                elif self._state == "starved":
                    self._starved_ticks += 1
                    rebuilt = len(self._queue) >= self.start_frames
                    grace_elapsed = self._starved_ticks >= self.starvation_frames
                    terminal_tail = (
                        self._burst_token in self._terminal_tokens and grace_elapsed
                    )
                    if self._queue and (rebuilt or terminal_tail):
                        was_full = len(self._queue) >= self.options.max_buffer_frames
                        candidate = self._queue.popleft()
                        freed_capacity = freed_capacity or was_full
                        self._state = "playing"
                        self._burst_token = candidate.token
                        if self._burst_id is not None:
                            playback_events.append(
                                self._playback_event_locked(
                                    "playback.resumed", candidate.token, self._burst_id
                                )
                            )
                    elif (
                        not self._queue
                        and grace_elapsed
                        and self._burst_token in self._terminal_tokens
                    ):
                        drain_token = self._burst_token
                        if self._burst_id is not None and self._burst_token is not None:
                            playback_events.append(
                                self._playback_event_locked(
                                    "playback.drain", self._burst_token, self._burst_id
                                )
                            )
                        self._state = "idle"
                        self._prebuffer_ticks = 0
                        self._starved_ticks = 0
                        self._burst_token = None
                        self._burst_id = None

        render = self._silence
        first_token: PlaybackToken | None = None
        if candidate is not None:
            # Separate lock acquisition gives an interrupting thread a final
            # opportunity to invalidate after dequeue and before device commit.
            with self._buffer_lock:
                if candidate.token.generation == self._active_generation:
                    render = self._apply_gain_locked(candidate.data)
                    if any(render):
                        self._audible_until = self._clock() + self.frame_sec
                        if candidate.token not in self._first_frame_tokens:
                            self._first_frame_tokens.add(candidate.token)
                            first_token = candidate.token
        cue: _QueuedFrame | None = None
        with self._buffer_lock:
            while self._cue_queue and self._cue_queue[0].token.generation != self._active_generation:
                self._cue_queue.popleft()
            if self._cue_queue:
                cue = self._cue_queue.popleft()
        if cue is not None:
            render = _mix_pcm16(render, cue.data)
            if any(cue.data):
                with self._buffer_lock:
                    self._audible_until = self._clock() + self.frame_sec
        return render, first_token, drain_token, freed_capacity, playback_events

    def _playback_event_locked(
        self, event_type: str, token: PlaybackToken, burst_id: str
    ) -> dict[str, Any]:
        return {
            "type": event_type,
            "playback_burst_id": burst_id,
            "playback_generation": token.generation,
            "turn_id": token.turn_id,
            "buffered_frames": len(self._queue),
            "starved_ms": self._starved_ticks * self.options.frame_ms,
        }

    def _apply_gain_locked(self, data: bytes) -> bytes:
        if self._current_gain == 1.0 and self._target_gain == 1.0:
            return data
        samples = array("h")
        samples.frombytes(data)
        start_gain = self._current_gain
        if self._gain_ramp_frames:
            end_gain = start_gain + (self._target_gain - start_gain) / self._gain_ramp_frames
            self._gain_ramp_frames -= 1
        else:
            end_gain = self._target_gain
        denominator = max(1, len(samples) - 1)
        for index, sample in enumerate(samples):
            gain = start_gain + (end_gain - start_gain) * (index / denominator)
            samples[index] = max(-32768, min(32767, int(sample * gain)))
        self._current_gain = end_gain
        return samples.tobytes()
    def _update_stream_delay(self, time_info: Any) -> None:
        try:
            if isinstance(time_info, dict):
                adc = float(time_info["inputBufferAdcTime"])
                dac = float(time_info["outputBufferDacTime"])
            else:
                adc = float(time_info.inputBufferAdcTime)
                dac = float(time_info.outputBufferDacTime)
            self._stream_delay_ms = min(500, max(0, int((dac - adc) * 1000)))
        except Exception:
            pass

    def _signal_capacity(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._capacity_event.set()
            return
        loop.call_soon_threadsafe(self._capacity_event.set)

    def _schedule_async(self, callback: Callable[..., Awaitable[None]], *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._start_callback_task, callback, args)

    def _schedule_event(self, event: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._schedule_async(self._on_event, event)

    def _schedule_capture(self, data: bytes) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._enqueue_capture, data)

    def _enqueue_capture(self, data: bytes) -> None:
        queue = self._capture_queue
        if self._closed or queue is None:
            return
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            self._capture_dropped_frames += 1

    async def _capture_pump(self) -> None:
        queue = self._capture_queue
        if queue is None:
            return
        try:
            while not self._closed:
                data = await queue.get()
                if not self.capture_enabled:
                    data = bytes(len(data))
                await self._on_capture(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded for the transport watchdog.
            self._last_error = repr(exc)

    def _start_callback_task(
        self, callback: Callable[..., Awaitable[None]], args: tuple[Any, ...]
    ) -> None:
        if self._closed:
            return
        try:
            task = asyncio.create_task(callback(*args))
        except Exception as exc:  # noqa: BLE001 - callback construction is diagnostic only.
            self._last_error = repr(exc)
            return
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_done)

    def _callback_done(self, task: asyncio.Task[Any]) -> None:
        self._callback_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception as exc:  # noqa: BLE001
            self._last_error = repr(exc)
            return
        if error is not None:
            self._last_error = repr(error)


def _mix_pcm16(left: bytes, right: bytes) -> bytes:
    if len(left) != len(right):
        raise ValueError("PCM frames must have equal length")
    mixed = array("h")
    left_samples = array("h")
    right_samples = array("h")
    left_samples.frombytes(left)
    right_samples.frombytes(right)
    mixed.extend(
        max(-32768, min(32767, a + b))
        for a, b in zip(left_samples, right_samples)
    )
    return mixed.tobytes()
