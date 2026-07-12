from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from opencode_voice.echo_probe import PcmRingBuffer, echo_correlation
from opencode_voice.config import (
    VoiceConfig,
    iso_utc_now,
    load_voice_credentials,
    redact_secrets,
    secret_values,
    voice_bridge_issue_payload,
)
from opencode_voice.deepgram import SpeechTextFilter, TTSChunker
from opencode_voice.logging import RunLogger
from opencode_voice.interruption import (
    EpisodeIdentity,
    InterruptionActionKind,
    InterruptionEvent,
    InterruptionEventKind,
    InterruptionPhase,
    InterruptionSnapshot,
    reduce_interruption,
)
from opencode_voice.opencode_client import OpenCodeClient
from opencode_voice.protocol import PROTOCOL_VERSION as SIDEPOD_PROTOCOL_VERSION
from opencode_voice.protocol import check_command as check_sidepod_command
from opencode_voice.protocol import check_event as check_sidepod_event
from opencode_voice.protocol import schema_document as sidepod_schema_document
from opencode_voice.audio_processing import EchoCanceller, FrameSlicer, Pcm16Resampler
from opencode_voice.playback import PlaybackToken
from opencode_voice.device_audio import DeviceAudioOptions, PersistentDeviceAudioEngine
from opencode_voice.flux_transport import FluxTransport, FluxTransportOptions
from opencode_voice.tts_providers import (
    CartesiaTTSOptions,
    CartesiaTTSProvider,
    DeepgramTTSOptions,
    DeepgramTTSProvider,
    StalePlaybackToken,
    TTSProvider,
    TTSProviderError,
)
from opencode_voice.telemetry import RunMetadata, snapshot_voice_config
from opencode_voice.response_contract import (
    ResponseCase,
    evaluate_response,
    repair_prompt,
    should_admit_repair,
    should_select_repair,
)
from opencode_voice.structured_turn import StructuredTurnResult, run_structured_turn
from opencode_voice.state import (
    AssistantTextTracker,
    HybridOpenCodeTurnTracker,
    active_context_estimate,
    elapsed_ms,
    event_properties,
    event_session_id,
    latest_completed_summary,
    session_title,
    session_usage_tokens,
)

EPHEMERAL_PREFIX = "[voice tmp]"
AUDIO_DEPENDENCY_MODULE = "sounddevice"
# Live capture that produces zero frames within this window is treated as a
# silently denied mic (macOS TCC denies without any error on some terminals).
MIC_WATCHDOG_SEC = 4.0
TURN_PREFLIGHT_TIMEOUT_SEC = 3.0
CONTEXT_OVERFLOW_MARKERS = (
    "maximum context length",
    "context length",
    "context window",
    "reduce the length of the messages",
    "too many tokens",
)
INTERNAL_EVENT_HANDLED = object()
INTERNAL_ONLY_ENGINE_EVENTS = frozenset(
    {
        "tokens",
        "opencode.requested",
        "turn.context_overflow",
        "compaction.wait",
        "compaction.continuing",
        "compaction.try_again",
        "compaction.wait.timeout",
        "compaction.start",
        "compaction.complete",
        "compaction.error",
    }
)


@dataclass(frozen=True)
class CompactionOutcome:
    session_id: str
    before_tokens: int
    after_tokens: int | None
    summary_message_id: str | None
    completed: bool


class CompactionConfirmationError(RuntimeError):
    """A compaction request was accepted but not proven successful."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def compaction_growth_required(threshold: int) -> int:
    return max(4_096, int(threshold * 0.10))


def message_identity(message: dict[str, Any]) -> tuple[str, str]:
    info = message.get("info")
    if isinstance(info, dict):
        return str(info.get("id") or ""), str(info.get("role") or "")
    return str(message.get("id") or ""), str(message.get("type") or message.get("role") or "")


def message_ids(messages: list[dict[str, Any]]) -> set[str]:
    return {message_id for message_id, _role in map(message_identity, messages) if message_id}


def validate_compaction_summary(
    messages: list[dict[str, Any]],
    previous_summary_message_id: str | None,
) -> tuple[str, dict[str, Any]]:
    summary = latest_completed_summary(messages)
    if summary is None:
        raise CompactionConfirmationError("summary_missing")
    info = summary.get("info")
    if not isinstance(info, dict):
        raise CompactionConfirmationError("summary_invalid")
    summary_id = str(info.get("id") or "")
    if not summary_id or summary_id == previous_summary_message_id:
        raise CompactionConfirmationError("summary_not_advanced")
    if info.get("error") or str(info.get("finish") or "").lower() == "error":
        raise CompactionConfirmationError("summary_error")
    time_info = info.get("time")
    completed_at = time_info.get("completed") if isinstance(time_info, dict) else None
    if completed_at is None and not info.get("finish"):
        raise CompactionConfirmationError("summary_incomplete")
    has_text = any(
        isinstance(part, dict)
        and part.get("type") == "text"
        and str(part.get("text") or "").strip()
        for part in summary.get("parts") or []
    )
    if not has_text:
        raise CompactionConfirmationError("summary_empty")
    return summary_id, summary


def synthesize_tool_cue(sample_rate: int, *, hold: bool) -> bytes:
    duration_ms = 70 if hold else 100
    sample_count = max(1, sample_rate * duration_ms // 1000)
    fade_samples = max(1, sample_rate // 100)
    frequencies = (523.25,) if hold else (659.25, 880.0)
    samples = array("h")
    for index in range(sample_count):
        progress = index / max(1, sample_count - 1)
        frequency = frequencies[min(len(frequencies) - 1, int(progress * len(frequencies)))]
        envelope = min(1.0, index / fade_samples, (sample_count - 1 - index) / fade_samples)
        value = math.sin(2 * math.pi * frequency * index / sample_rate)
        samples.append(int(32767 * 0.07 * max(0.0, envelope) * value))
    return samples.tobytes()


def is_context_overflow_error(exc: Exception) -> bool:
    return is_context_overflow_value(exc)


def is_context_overflow_value(value: Any) -> bool:
    text = repr(value).lower()
    return any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS)


def audio_dependency_available(module_name: str = AUDIO_DEPENDENCY_MODULE) -> bool:
    return importlib.util.find_spec(module_name) is not None


def helper_readiness_issues(
    transport_ready: bool,
    *,
    audio_ready: bool | None = None,
    debug_ref: str | None = None,
    dotenv_path: str | Path = ".env",
    tts_provider: str = "deepgram",
) -> tuple[dict[str, Any], ...]:
    issues: list[dict[str, Any]] = []
    if not transport_ready:
        issues.append(
            voice_bridge_issue_payload(
                capability="helper_transport",
                diagnostic_code="transport_unavailable",
                safe_detail="Helper transport unavailable",
                debug_ref=debug_ref,
            )
        )
    if not (audio_dependency_available() if audio_ready is None else audio_ready):
        issues.append(
            voice_bridge_issue_payload(
                capability="voice_audio",
                diagnostic_code="audio_dependency_unavailable",
                safe_detail="Audio capture unavailable",
                debug_ref=debug_ref,
            )
        )
    credentials = load_voice_credentials(dotenv_path=dotenv_path, tts_provider=tts_provider)
    issues.extend(issue.to_voice_bridge_issue(debug_ref=debug_ref) for issue in credentials.issues)
    return tuple(issues)


# Maps a turn-failure reason to the lane's (diagnostic_code, safe_detail,
# retryable). Everything here is provider-agnostic and text-safe: the raw
# model-provider error never reaches the wire, only the bucket it fell into.
# "language model" stays generic on purpose — no provider name leaks.
TURN_FAILURE_DETAILS: dict[str, tuple[str, str, bool]] = {
    "provider_quota": (
        "model_provider_quota",
        "Language-model provider quota reached — check the model plan or billing",
        False,
    ),
    "provider_auth": (
        "model_provider_auth",
        "Language-model provider rejected the API key — check the model credentials",
        False,
    ),
    "content_policy": (
        "model_content_policy",
        "Language-model provider blocked this voice turn for safety policy",
        False,
    ),
    "turn_timeout": ("turn_timeout", "Voice turn timed out", True),
    "failed": ("turn_failed", "Voice turn failed", True),
}


def classify_turn_failure(error: Any) -> str:
    """Bucket an OpenCode turn error so the lane can show a useful cause
    without carrying any provider text. Digs the HTTP status and error code
    out of the (possibly nested) error object; an unknown shape falls through
    to "failed", so a new error class degrades to the generic message rather
    than leaking raw detail."""
    data = error.get("data") if isinstance(error, dict) else None
    if not isinstance(data, dict):
        data = error if isinstance(error, dict) else {}
    status = data.get("statusCode")
    status = status if isinstance(status, int) else None
    code = str(data.get("code") or "").lower()
    message = str(data.get("message") or "").lower()
    if status == 402 or "quota" in code or "billing" in code:
        return "provider_quota"
    if (
        "content_policy" in code
        or "content policy" in message
        or "policy violation" in message
        or "filtered" in message
        or "safety" in code
        or "safety" in message
    ):
        return "content_policy"
    if status in (401, 403) or "auth" in code or "api_key" in code or "access_denied" in code:
        return "provider_auth"
    return "failed"


class OpenCodeEventFallback(Exception):
    def __init__(self, reason: str, prompt_sent: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.prompt_sent = prompt_sent


class NativeMicSession:
    def __init__(
        self,
        config: VoiceConfig,
        logger: RunLogger,
        on_audio: Callable[[bytes], Awaitable[None]],
        on_issue: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.config = config
        self.logger = logger
        self.on_audio = on_audio
        self.on_issue = on_issue
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue[bytes] | None = None
        self.stream: Any | None = None
        self.pump_task: asyncio.Task[None] | None = None
        self.closed = False
        self.dropped_chunks = 0
        self.input_delay_sec = 0.0
        self.last_callback_at = 0.0

    async def start(self) -> bool:
        try:
            import sounddevice as sd  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
            self.logger.write("native_audio.start.error", error=repr(exc), reason="import_failed")
            await self.on_issue(
                voice_bridge_issue_payload(
                    capability="voice_audio",
                    diagnostic_code="audio_dependency_unavailable",
                    safe_detail="Audio capture unavailable",
                    debug_ref=str(self.logger.run_dir),
                )
            )
            return False

        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=64)
        # Device/AEC clock: exact 10 ms capture frames.  Flux packetization is
        # owned by its sender actor and must never dictate PortAudio cadence.
        blocksize = max(1, int(self.config.device_sample_rate * 0.01))

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            # PortAudio realtime thread: no blocking calls here — logging does
            # file I/O and status flags fire exactly when the device is
            # already stressed, so defer everything to the loop.
            if self.closed or not self.loop:
                return
            self.last_callback_at = time.perf_counter()
            try:
                # Capture-side delay for the echo canceller, from PortAudio
                # timing the way livekit's media_devices measures it. Plain
                # float attribute write: safe from this thread.
                adc_delay = float(time_info.currentTime) - float(time_info.inputBufferAdcTime)
                if adc_delay > 0:
                    self.input_delay_sec = adc_delay
            except Exception:  # noqa: BLE001, S110 - some hosts omit timing info.
                pass
            if status:
                self.loop.call_soon_threadsafe(log_status, str(status))
            self.loop.call_soon_threadsafe(self.enqueue_audio, bytes(indata))

        def log_status(status: str) -> None:
            self.logger.write("native_audio.status", status=status)

        try:
            self.stream = sd.RawInputStream(
                samplerate=self.config.device_sample_rate,
                channels=1,
                dtype="int16",
                blocksize=blocksize,
                callback=callback,
            )
            self.stream.start()
        except Exception as exc:  # noqa: BLE001 - device/permission failures become safe UI issues.
            self.logger.write("native_audio.start.error", error=repr(exc), reason="stream_start_failed")
            await self.on_issue(
                voice_bridge_issue_payload(
                    capability="voice_audio",
                    diagnostic_code="audio_capture_unavailable",
                    safe_detail="Audio capture unavailable",
                    debug_ref=str(self.logger.run_dir),
                )
            )
            return False

        self.pump_task = asyncio.create_task(self.pump())
        self.logger.write(
            "native_audio.start",
            sample_rate=self.config.device_sample_rate,
            channels=1,
            dtype="int16",
            blocksize=blocksize,
        )
        return True

    def enqueue_audio(self, data: bytes) -> None:
        if not self.queue:
            return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.dropped_chunks += 1
            if self.dropped_chunks == 1 or self.dropped_chunks % 50 == 0:
                self.logger.write("native_audio.drop", dropped_chunks=self.dropped_chunks)

    async def pump(self) -> None:
        if not self.queue:
            return
        while not self.closed:
            data = await self.queue.get()
            await self.on_audio(data)

    async def close(self) -> None:
        self.closed = True
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as exc:  # noqa: BLE001 - close is best-effort.
                self.logger.write("native_audio.close.error", error=repr(exc))
            self.stream = None
        if self.pump_task and not self.pump_task.done():
            self.pump_task.cancel()
            try:
                await self.pump_task
            except asyncio.CancelledError:
                pass
        self.logger.write("native_audio.stop", dropped_chunks=self.dropped_chunks)


class NativeSpeakerSession:
    def __init__(
        self,
        config: VoiceConfig,
        logger: RunLogger,
        on_issue: Callable[[dict[str, Any]], Awaitable[None]],
        on_render: Callable[[bytes], None] | None = None,
        on_drain: Callable[[], Awaitable[None]] | None = None,
        on_first_frame: Callable[[PlaybackToken], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.on_issue = on_issue
        self.on_render = on_render
        self.on_drain = on_drain
        self.on_first_frame = on_first_frame
        self.queue: asyncio.Queue[tuple[PlaybackToken, int, bytes]] | None = None
        self.stream: Any | None = None
        self.pump_task: asyncio.Task[None] | None = None
        self.closed = False
        self.dropped_chunks = 0
        self.played_chunks = 0
        self.played_bytes = 0
        self.started_at: float | None = None
        self.last_summary_log = 0.0
        self.speaking_until = 0.0
        self.resume_event = asyncio.Event()
        self.resume_event.set()
        self.paused = False
        self.paused_at = 0.0
        self.burst_started_at = 0.0
        self.burst_active = False
        self.playback_generation = 0
        self.sequence = 0
        self.play_lock = asyncio.Lock()
        self.device_write_lock = asyncio.Lock()
        self.resampler_token: PlaybackToken | None = None
        self.frame_slicer = FrameSlicer(config.device_sample_rate)
        self.resampler = Pcm16Resampler(config.tts_sample_rate, config.device_sample_rate)
        # Queue exhaustion is an underflow, not provider EOF.  A turn owns its
        # acoustic burst until the provider explicitly reports a terminal
        # marker, matching the persistent duplex engine's semantics.
        self.terminal_tokens: dict[PlaybackToken, str] = {}
        self.jitter_target_frames = 8
        self.max_starvation_sec = 0.25
        self.duck_gain = 10 ** (-18 / 20)
        self.current_gain = 1.0
        self.target_gain = 1.0
        self.gain_ramp_frames = 0
        self.first_frame_tokens: set[PlaybackToken] = set()

    async def start(self) -> bool:
        try:
            import sounddevice as sd  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 - optional runtime dependency.
            self.logger.write("native_tts.start.error", error=repr(exc), reason="import_failed")
            await self.on_issue(
                voice_bridge_issue_payload(
                    capability="voice_audio",
                    diagnostic_code="audio_dependency_unavailable",
                    safe_detail="Audio playback unavailable",
                    debug_ref=str(self.logger.run_dir),
                )
            )
            return False

        # Exactly one second of 10 ms device frames.  Provider timing cannot
        # grow memory without bound or become the device clock.
        self.queue = asyncio.Queue(maxsize=100)
        try:
            self.stream = sd.RawOutputStream(
                samplerate=self.config.device_sample_rate,
                channels=1,
                dtype="int16",
                blocksize=max(1, self.config.device_sample_rate // 100),
            )
            self.stream.start()
        except Exception as exc:  # noqa: BLE001 - device/permission failures become safe UI issues.
            self.logger.write("native_tts.start.error", error=repr(exc), reason="stream_start_failed")
            await self.on_issue(
                voice_bridge_issue_payload(
                    capability="voice_audio",
                    diagnostic_code="audio_playback_unavailable",
                    safe_detail="Audio playback unavailable",
                    debug_ref=str(self.logger.run_dir),
                )
            )
            return False

        self.pump_task = asyncio.create_task(self.pump())
        self.logger.write(
            "native_tts.start",
            sample_rate=self.config.device_sample_rate,
            provider_sample_rate=self.config.tts_sample_rate,
            channels=1,
            dtype="int16",
        )
        return True

    async def play(self, data: bytes, turn_id: int | PlaybackToken | None) -> bool:
        if not self.queue or self.closed:
            return False
        token = (
            turn_id
            if isinstance(turn_id, PlaybackToken)
            else PlaybackToken(self.playback_generation, int(turn_id or 0))
        )
        if not self.token_is_current(token):
            return False
        async with self.play_lock:
            if not self.token_is_current(token):
                return False
            if self.resampler_token != token:
                self._reset_pcm_pipeline(token)
            converted = self.resampler.push(data)
            frames = self.frame_slicer.push(converted)
            for frame in frames:
                if not self.token_is_current(token):
                    return False
                self.sequence += 1
                await self.queue.put((token, self.sequence, bytes(frame)))
                # Cancellation may have advanced while capacity was awaited.
                if not self.token_is_current(token):
                    return False
            return True

    async def play_device_cue(self, data: bytes, token: PlaybackToken) -> bool:
        if self.closed or self.stream is None or not self.token_is_current(token):
            return False
        frame_bytes = max(1, self.config.device_sample_rate // 100) * 2
        frames = [data[index : index + frame_bytes] for index in range(0, len(data), frame_bytes)]
        if frames and len(frames[-1]) < frame_bytes:
            frames[-1] += bytes(frame_bytes - len(frames[-1]))
        async with self.device_write_lock:
            for frame in frames:
                if not self.token_is_current(token):
                    return False
                await asyncio.to_thread(self.write_output, frame)
        return True

    def begin_turn(self, token: PlaybackToken) -> bool:
        """Register provider ownership before its first PCM frame arrives."""

        if not self.token_is_current(token):
            return False
        self.terminal_tokens.pop(token, None)
        if self.resampler_token != token:
            self._reset_pcm_pipeline(token)
        return True

    def mark_turn_terminal(self, token: PlaybackToken, outcome: str = "done") -> bool:
        if not self.token_is_current(token):
            return False
        self.terminal_tokens[token] = str(outcome or "done")
        return True

    async def finish_turn(self, token: PlaybackToken, outcome: str = "done") -> bool:
        """Admit every resampler/slicer tail before making EOF visible."""

        if not self.queue or self.closed:
            return False
        async with self.play_lock:
            if not self.token_is_current(token):
                return False
            if self.resampler_token == token:
                converted_tail = self.resampler.flush()
                frames = self.frame_slicer.push(converted_tail)
                if self.frame_slicer.buffer:
                    tail = bytes(self.frame_slicer.buffer)
                    self.frame_slicer.buffer.clear()
                    frames.append(bytearray(tail + bytes(self.frame_slicer.frame_bytes - len(tail))))
                for frame in frames:
                    if not self.token_is_current(token):
                        return False
                    self.sequence += 1
                    await self.queue.put((token, self.sequence, bytes(frame)))
                    if not self.token_is_current(token):
                        return False
            return self.mark_turn_terminal(token, outcome)

    def turn_is_terminal(self, token: PlaybackToken) -> bool:
        return token in self.terminal_tokens

    def _reset_pcm_pipeline(self, token: PlaybackToken | None = None) -> None:
        # Native resamplers carry delay and FrameSlicer carries a sub-frame
        # tail. Neither may cross a turn/generation fence.
        self.resampler_token = token
        self.resampler = Pcm16Resampler(self.config.tts_sample_rate, self.config.device_sample_rate)
        self.frame_slicer = FrameSlicer(self.config.device_sample_rate)

    def token_is_current(self, token: PlaybackToken) -> bool:
        return not self.closed and token.generation == self.playback_generation

    def invalidate_generation(self, generation: int, reason: str) -> None:
        """Fence producer/dequeue/device stages before external cancellation."""
        self.playback_generation = generation
        self.flush(reason=reason)
        self._reset_pcm_pipeline()
        self.terminal_tokens = {
            token: outcome
            for token, outcome in self.terminal_tokens.items()
            if token.generation == generation
        }
        self.first_frame_tokens = {
            token for token in self.first_frame_tokens if token.generation == generation
        }

    def set_ducked(self, ducked: bool) -> None:
        target = self.duck_gain if ducked else 1.0
        if target == self.target_gain:
            return
        self.target_gain = target
        self.gain_ramp_frames = 2  # 20 ms at the device clock
        self.logger.write("native_tts.duck", ducked=ducked, gain_db=-18 if ducked else 0)

    def is_audible(self, tail_sec: float = 0.3) -> bool:
        return not self.closed and time.perf_counter() < self.speaking_until + tail_sec

    def in_startup_window(self, window_sec: float) -> bool:
        """True during the first moments of a playback burst — the canceller's
        convergence window, when echo leaks are all but guaranteed."""
        if window_sec <= 0 or self.paused or not self.is_audible(tail_sec=0.0):
            return False
        return time.perf_counter() - self.burst_started_at < window_sec

    def pause(self) -> None:
        """Hold playback between chunks; the queue keeps its audio."""
        if self.closed or self.paused:
            return
        self.paused = True
        self.paused_at = time.perf_counter()
        self.resume_event.clear()
        self.logger.write("native_tts.pause")

    def resume(self) -> None:
        if self.closed or not self.paused:
            return
        # The playback clock stood still while paused.
        self.speaking_until += time.perf_counter() - self.paused_at
        self.paused = False
        self.resume_event.set()
        self.logger.write("native_tts.resume")

    def flush(self, reason: str) -> None:
        """Drop queued audio but keep the device stream open — closing it
        would make the echo canceller re-converge on the next turn."""
        discarded = 0
        if self.queue:
            while not self.queue.empty():
                self.queue.get_nowait()
                discarded += 1
        if discarded:
            self.dropped_chunks += discarded
            self.logger.write("native_tts.drop", dropped_chunks=discarded, reason=reason)
        self.speaking_until = 0.0
        # A flush precedes a barge-in teardown or a new turn; that path owns the
        # UI state, so suppress the drain->listening signal for this burst.
        self.burst_active = False
        if self.paused:
            self.paused = False
            self.resume_event.set()

    async def pump(self) -> None:
        if not self.queue:
            return
        while not self.closed:
            token, sequence, data = await self.queue.get()
            # A queued item can outlive cancellation even if flush raced its
            # dequeue.  This is the first post-dequeue fence.
            if not self.token_is_current(token):
                self.dropped_chunks += 1
                continue

            if not self.burst_active:
                # Build the 80 ms target before exposing a new acoustic edge.
                deadline = time.perf_counter() + self.max_starvation_sec
                while (
                    self.queue.qsize() + 1 < self.jitter_target_frames
                    and time.perf_counter() < deadline
                    and self.token_is_current(token)
                ):
                    await asyncio.sleep(0.005)
                now = time.perf_counter()
                self.burst_started_at = now
                self.started_at = now
                self.last_summary_log = now
                self.played_chunks = 0
                self.played_bytes = 0
                self.burst_active = True
                self.logger.write(
                    "native_tts.burst.start",
                    buffered_frames=self.queue.qsize() + 1,
                    playback_generation=token.generation,
                    turn_id=token.turn_id,
                    sequence=sequence,
                )

            await self.resume_event.wait()
            # Pause/capacity/cancellation can all interleave; fence again
            # immediately before acquiring the device frame.
            if not self.token_is_current(token):
                self.dropped_chunks += 1
                continue
            frame = self.apply_gain(data)
            if frame.strip(b"\x00") and token not in self.first_frame_tokens:
                self.first_frame_tokens.add(token)
                if self.on_first_frame:
                    await self.on_first_frame(token)
            try:
                async with self.device_write_lock:
                    await asyncio.to_thread(self.write_output, frame)
            except Exception as exc:  # noqa: BLE001 - surface playback failures but keep transport alive.
                self.logger.write("native_tts.write.error", error=repr(exc))
                await self.on_issue(
                    voice_bridge_issue_payload(
                        capability="voice_audio",
                        diagnostic_code="audio_playback_unavailable",
                        safe_detail="Audio playback unavailable",
                        debug_ref=str(self.logger.run_dir),
                    )
                )
                self.closed = True
                break
            self.played_chunks += 1
            self.played_bytes += len(frame)
            now = time.perf_counter()
            self.speaking_until = now + 0.01
            if self.started_at is not None and now - self.last_summary_log >= 5:
                self.last_summary_log = now
                self.logger.write(
                    "native_tts.summary",
                    chunks=self.played_chunks,
                    bytes=self.played_bytes,
                    dropped_chunks=self.dropped_chunks,
                    duration_ms=elapsed_ms(self.started_at),
                )

            if self.queue.empty() and self.token_is_current(token):
                await self.hold_starvation(token)

    async def hold_starvation(self, token: PlaybackToken) -> None:
        """Keep one device/AEC clock until provider EOF or a full refill.

        Provider/network gaps can be arbitrarily longer than the 250 ms
        grace.  They remain the same acoustic burst and emit timed silence;
        only explicit provider terminal ownership may drain the burst.
        """
        if not self.queue:
            return
        started = time.perf_counter()
        silence = b"\x00" * (self.config.device_sample_rate // 100 * 2)
        if not self.turn_is_terminal(token):
            self.logger.write(
                "native_tts.starved",
                playback_generation=token.generation,
                turn_id=token.turn_id,
            )
        target_frames = min(
            self.jitter_target_frames,
            self.queue.maxsize if self.queue.maxsize > 0 else self.jitter_target_frames,
        )
        while self.token_is_current(token):
            elapsed = time.perf_counter() - started
            buffered = self.queue.qsize()
            terminal = self.turn_is_terminal(token)
            refill_ready = buffered >= target_frames
            terminal_tail_ready = terminal and buffered > 0 and elapsed >= self.max_starvation_sec
            terminal_drain_ready = terminal and buffered == 0 and elapsed >= self.max_starvation_sec
            if refill_ready or terminal_tail_ready:
                self.logger.write(
                    "native_tts.starved.recovered",
                    duration_ms=int(elapsed * 1000),
                    buffered_frames=buffered,
                    playback_generation=token.generation,
                    turn_id=token.turn_id,
                    terminal_tail=terminal_tail_ready,
                )
                return
            if terminal_drain_ready:
                break
            async with self.device_write_lock:
                await asyncio.to_thread(self.write_output, silence)
            if self.stream is None:
                await asyncio.sleep(0.01)
        if not self.token_is_current(token):
            return

        self.burst_active = False
        self.speaking_until = 0.0
        if self.started_at is not None:
            self.logger.write(
                "native_tts.summary",
                chunks=self.played_chunks,
                bytes=self.played_bytes,
                dropped_chunks=self.dropped_chunks,
                duration_ms=elapsed_ms(self.started_at),
                reason="drained",
            )
        if self.on_drain:
            await self.on_drain()

    def apply_gain(self, data: bytes) -> bytes:
        if self.current_gain == 1.0 and self.target_gain == 1.0:
            return data
        samples = array("h")
        samples.frombytes(data)
        start_gain = self.current_gain
        if self.gain_ramp_frames:
            end_gain = start_gain + (self.target_gain - start_gain) / self.gain_ramp_frames
            self.gain_ramp_frames -= 1
        else:
            end_gain = self.target_gain
        count = max(1, len(samples) - 1)
        for index, sample in enumerate(samples):
            gain = start_gain + (end_gain - start_gain) * (index / count)
            samples[index] = max(-32768, min(32767, int(sample * gain)))
        self.current_gain = end_gain
        return samples.tobytes()

    def write_output(self, data: bytes) -> None:
        # One actual 10 ms device frame at a time.  Timed silence during
        # starvation follows this same path, preserving the AEC render clock.
        if self.on_render:
            try:
                self.on_render(data)
            except Exception as exc:  # noqa: BLE001 - reference feed must not stop playback.
                self.logger.write("native_tts.render_ref.error", error=repr(exc))
                self.on_render = None
        if self.stream:
            self.stream.write(data)

    async def close(self) -> None:
        self.closed = True
        self.speaking_until = 0.0
        self.resume_event.set()
        if self.pump_task and not self.pump_task.done():
            self.pump_task.cancel()
            try:
                await self.pump_task
            except asyncio.CancelledError:
                pass
        if self.queue is not None:
            # Frees queue slots so a play() blocked on put() wakes up (and
            # then sees closed); anything still buffered is teardown discard.
            discarded = 0
            while not self.queue.empty():
                self.queue.get_nowait()
                discarded += 1
            if discarded:
                self.dropped_chunks += discarded
                self.logger.write("native_tts.drop", dropped_chunks=discarded, reason="teardown")
        self._reset_pcm_pipeline()
        self.terminal_tokens.clear()
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as exc:  # noqa: BLE001 - close is best-effort.
                self.logger.write("native_tts.close.error", error=repr(exc))
            self.stream = None
        self.logger.write(
            "native_tts.stop",
            chunks=self.played_chunks,
            bytes=self.played_bytes,
            dropped_chunks=self.dropped_chunks,
        )


def create_app(
    config: VoiceConfig,
    *,
    client_factory: Callable[[str, float], OpenCodeClient] = OpenCodeClient,
) -> FastAPI:
    app = FastAPI(
        title="Mortic Voice Helper",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    load_voice_credentials(tts_provider=config.tts_provider)
    logger = RunLogger(config.run_root)
    run_metadata = RunMetadata.create(
        snapshot_voice_config(
            config,
            mic_queue_blocks=64,
            playback_queue_chunks=100,
            jitter_buffer_target_ms=80,
        ),
        # Build identity belongs to the helper artifact, not the user's active
        # OpenCode workspace (which may be outside a Git checkout entirely).
        cwd=Path(__file__).resolve().parents[1],
    )
    logger.write("run.metadata", **run_metadata.as_fields())
    sidepod_lane_registry = ActiveSidepodLaneRegistry()

    @app.on_event("startup")
    async def _startup() -> None:
        readiness_issues = helper_readiness_issues(
            transport_ready=True, debug_ref=str(logger.run_dir), tts_provider=config.tts_provider
        )
        logger.write(
            "bridge.start",
            opencode_url=config.opencode_url,
            model=config.model.opencode_name,
            deepgram_stt_model=config.deepgram_stt_model,
            tts_provider=config.tts_provider,
            deepgram_tts_model=config.deepgram_tts_model,
            cartesia_tts_model=config.cartesia_tts_model,
            has_deepgram_key=config.has_deepgram_key,
            has_inception_key=config.has_inception_key,
            has_cartesia_key=config.has_cartesia_key,
            ready=not readiness_issues,
            diagnostic_codes=[issue["diagnosticCode"] for issue in readiness_issues],
            run_dir=str(logger.run_dir),
        )
        logger.state_transition(
            "starting",
            "ready" if not readiness_issues else "voice_bridge_issue",
            diagnostic_codes=[issue["diagnosticCode"] for issue in readiness_issues],
        )

    async def _shutdown() -> None:
        logger.close()
    app.router.add_event_handler("shutdown", _shutdown)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        credentials = load_voice_credentials(tts_provider=config.tts_provider)
        readiness_issues = list(
            helper_readiness_issues(
                transport_ready=True, debug_ref=str(logger.run_dir), tts_provider=config.tts_provider
            )
        )
        client = client_factory(config.opencode_url, 10)
        try:
            opencode_health = await client.health()
        except Exception as exc:  # noqa: BLE001 - health must describe dependency failures, not 500.
            opencode_health = {"healthy": False, "reachable": False, "error": type(exc).__name__}
            readiness_issues.append(
                voice_bridge_issue_payload(
                    capability="opencode_server",
                    diagnostic_code="opencode_unreachable",
                    safe_detail="Mortic could not reach its OpenCode voice server.",
                    debug_ref=str(logger.run_dir),
                )
            )
        else:
            try:
                agents = await client.agents()
            except Exception as exc:  # noqa: BLE001 - agent inspection failures must surface before mic start.
                readiness_issues.append(
                    voice_bridge_issue_payload(
                        capability="opencode_agent",
                        diagnostic_code="opencode_agent_check_failed",
                        safe_detail="Mortic could not inspect its OpenCode voice agent.",
                        debug_ref=str(logger.run_dir),
                    )
                )
                opencode_health = {**opencode_health, "agent_check_error": type(exc).__name__}
            else:
                if config.opencode_agent not in agents:
                    readiness_issues.append(
                        voice_bridge_issue_payload(
                            capability="opencode_agent",
                            diagnostic_code="opencode_agent_missing",
                            safe_detail="Mortic voice agent is missing from the OpenCode voice server.",
                            debug_ref=str(logger.run_dir),
                        )
                    )
                opencode_health = {**opencode_health, "agent_present": config.opencode_agent in agents}
        finally:
            await client.close()
        return JSONResponse(
            {
                "ok": not readiness_issues,
                "ready": not readiness_issues,
                "opencode": opencode_health,
                "opencode_url": config.opencode_url,
                "workspace_dir": config.workspace_dir,
                "run_dir": str(logger.run_dir),
                "model": config.model.opencode_name,
                "context_threshold_tokens": config.context_threshold_tokens,
                "response_mode": config.response_mode,
                "credential_issues": [
                    issue.to_voice_bridge_issue(debug_ref=str(logger.run_dir)) for issue in credentials.issues
                ],
                "issues": list(readiness_issues),
                "tts_provider": config.tts_provider,
                "deepgram": {
                    "enabled": config.has_deepgram_key,
                    "stt_model": config.deepgram_stt_model,
                    "tts_model": config.deepgram_tts_model,
                    "sample_rate": config.deepgram_sample_rate,
                    "tts_sample_rate": config.tts_sample_rate,
                },
                "cartesia": {
                    "enabled": config.has_cartesia_key,
                    "tts_model": config.cartesia_tts_model,
                    "sample_rate": config.tts_sample_rate,
                },
            }
        )

    @app.websocket("/ws/sidepod")
    async def sidepod_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        client = client_factory(config.opencode_url, 60)
        connection = SidepodConnection(
            config=config,
            client=client,
            logger=logger,
            websocket=websocket,
            client_factory=client_factory,
            lane_registry=sidepod_lane_registry,
        )
        try:
            await connection.run()
        finally:
            await connection.close()
            # start.opencodeUrl may have rebound the client; close the live one.
            await connection.client.close()

    return app


class VoiceConnection:
    def __init__(
        self,
        config: VoiceConfig,
        client: OpenCodeClient,
        logger: RunLogger,
        websocket: WebSocket,
    ) -> None:
        self.config = config
        self.client = client
        self.logger = logger
        self.websocket = websocket
        self.send_lock = asyncio.Lock()
        self.dropped_sends = 0
        # Fire-and-forget tasks (e.g. background speaker close) kept referenced
        # so the loop can't garbage-collect them mid-run.
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self.source_session_id: str | None = None
        self.fork_session_id: str | None = None
        self.fork_directory: str | None = None
        self.message_cache: dict[str, list[dict[str, Any]]] = {}
        self.keep_fork = config.keep_fork_default
        self.closed = False
        self.compaction_task: asyncio.Task[CompactionOutcome | None] | None = None
        self.compaction_lock = asyncio.Lock()
        self.compaction_reasons: set[str] = set()
        self.compaction_force_requested = False
        self.compaction_decision_event = asyncio.Event()
        self.compaction_decision_event.set()
        self.compaction_running = False
        self.compaction_after_tokens: int | None = None
        self.compaction_summary_message_id: str | None = None
        self.tool_cue_turns: set[int] = set()
        self.tool_hold_tasks: dict[int, asyncio.Task[None]] = {}
        self.turn_task: asyncio.Task[None] | None = None
        self.turn_seq = 0
        self.active_turn_id: int | None = None
        self.turn_playback_tokens: dict[int, PlaybackToken] = {}
        self.voice_lane_id: str | None = None
        self.protocol_turn_id = ""
        self.sidepod_readiness_issues: tuple[dict[str, Any], ...] = ()
        self.flux: DeepgramFluxSession | None = None
        self.flux_connection_epoch: int | None = None
        self.stt_transport_healthy = True
        self.stt_unhealthy_reported = False
        self.flux_watchdog_task: asyncio.Task[None] | None = None
        self.audio_transport_task: asyncio.Task[bool] | None = None
        self.mic_start_task: asyncio.Task[None] | None = None
        self.mic_start_generation = 0
        self.mic_desired_live = False
        self.mic_capture_gated = False
        self.speaker: TTSProvider | None = None
        self.tts_turn_token: PlaybackToken | None = None
        self.tts_failed_turns: set[int] = set()
        self.tts_terminal_tokens: dict[PlaybackToken, str] = {}
        self.tts_last_audio_at: dict[PlaybackToken, float] = {}
        self.tts_terminal_watchdogs: dict[PlaybackToken, asyncio.Task[None]] = {}
        self.tts_failure_reported: set[PlaybackToken] = set()
        self.native_mic: NativeMicSession | None = None
        self.native_speaker: NativeSpeakerSession | None = None
        self.native_audio_engine: PersistentDeviceAudioEngine | None = None
        self.force_half_duplex = False
        self.native_speaker_unavailable = False
        self.echo_canceller: EchoCanceller | None = None
        self.capture_to_flux_resampler: Pcm16Resampler | None = None
        self.render_to_probe_resampler: Pcm16Resampler | None = None
        self.speak_generation = 0
        self.stale_tts_chunks = 0
        self.tts_unavailable_chunks = 0
        self.speaker_prewarm_task: asyncio.Task[None] | None = None
        self.final_transcript = ""
        self.aec_delay_error_logged = False
        self.tts_first_audio_seen = False
        self.turn_spoken_any = False
        self.audio_input_chunks = 0
        self.audio_input_bytes = 0
        self.audio_input_started: float | None = None
        self.audio_input_last_log = 0.0
        self.audio_input_last_at = 0.0
        self.device_capture_drops_seen = 0
        # Rolling audio windows for the echo probe: what the mic heard
        # (post-AEC, i.e. what STT hears) and what the speaker played.
        self.mic_audio_ring = PcmRingBuffer(config.deepgram_sample_rate, direction="ending")
        self.render_audio_ring = PcmRingBuffer(config.deepgram_sample_rate, direction="starting")
        self.interruption_state = InterruptionSnapshot()
        self.interruption_lock = asyncio.Lock()
        self.interruption_clock_started = time.perf_counter()
        self.interruption_group_seq = 0
        self.interruption_episode: EpisodeIdentity | None = None
        self.interruption_episodes: dict[tuple[int, int], EpisodeIdentity] = {}
        self.expired_interruption_episodes: set[tuple[int, int]] = set()
        self.expired_interruption_episode_order: deque[tuple[int, int]] = deque()
        self.interruption_decision_task: asyncio.Task[None] | None = None
        self.interruption_expiry_task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        self.closed = True
        now_ms = self.interruption_elapsed_ms()
        self.interruption_state = reduce_interruption(
            self.interruption_state, InterruptionEvent.close(now_ms)
        ).state
        owned_tasks = [
            self.speaker_prewarm_task,
            self.turn_task,
            self.compaction_task,
            self.interruption_decision_task,
            self.interruption_expiry_task,
            self.flux_watchdog_task,
            self.audio_transport_task,
            self.mic_start_task,
            getattr(self, "mic_watchdog_task", None),
            *self.tts_terminal_watchdogs.values(),
            *self.tool_hold_tasks.values(),
            *self.background_tasks,
        ]
        current = asyncio.current_task()
        pending_tasks = list({task for task in owned_tasks if task and task is not current and not task.done()})
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            try:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                # ASGI websocket shutdown can cancel the lane handler while
                # its owned prewarm tasks are draining. They have already
                # been cancelled; finish deterministic resource cleanup and
                # let the disconnect complete normally.
                await asyncio.gather(*pending_tasks, return_exceptions=True)
        self.background_tasks.clear()
        self.tts_terminal_watchdogs.clear()
        self.tool_hold_tasks.clear()
        self.tool_cue_turns.clear()
        if self.native_audio_engine:
            await self.native_audio_engine.close()
            self.native_audio_engine = None
        if self.native_mic:
            await self.native_mic.close()
            self.native_mic = None
        if self.flux:
            await self.flux.close()
        if self.speaker:
            await self.speaker.close()
        if self.native_speaker:
            await self.native_speaker.close()
            self.native_speaker = None
        if self.fork_session_id and not self.keep_fork:
            fork_id = self.fork_session_id
            try:
                await self.client.delete_session(fork_id)
                self.logger.write("fork.delete", session_id=fork_id)
            except Exception as exc:  # noqa: BLE001 - surfaced to UI/log for cleanup visibility.
                self.logger.write("fork.delete.error", session_id=fork_id, error=repr(exc))
            finally:
                self.message_cache.pop(fork_id, None)

    def interruption_elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.interruption_clock_started) * 1000)

    async def create_voice_fork(
        self, session_id: str, keep_fork: bool, original: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.keep_fork = keep_fork
        self.source_session_id = session_id
        fork_started = time.perf_counter()
        fork = await self.client.fork_session(session_id)
        fork_id = str(fork.get("id") or "")
        if not fork_id:
            raise RuntimeError("OpenCode did not return a fork session id.")
        original = original or await self.client.get_session(session_id)
        title = f"{EPHEMERAL_PREFIX} {session_title(original)}"
        try:
            await self.client.switch_model(fork_id, self.config.model)
            await self.client.switch_agent(fork_id, self.config.opencode_agent)
            self.logger.write(
                "fork.configure",
                fork_session_id=fork_id,
                model=self.config.model.opencode_name,
                agent=self.config.opencode_agent,
            )
        except httpx.HTTPStatusError as exc:
            self.logger.write(
                "fork.configure.warning",
                fork_session_id=fork_id,
                status_code=exc.response.status_code,
                message="Falling back to fork's existing OpenCode model/agent.",
            )
        try:
            await self.client.update_session(fork_id, {"title": title, "metadata": {"opencode_voice": True}})
        except httpx.HTTPStatusError:
            await self.client.update_session(fork_id, {"title": title})
        self.fork_session_id = fork_id
        self.reset_compaction_guard()
        self.voice_lane_id = self.voice_lane_id or f"lane_{int(time.time() * 1000)}"
        session = await self.client.get_session(fork_id)
        # Forks inherit the source thread's directory; /event subscriptions
        # are directory-scoped, so turns must subscribe with this value or the
        # stream stays silent and every turn pays the poll-fallback timeout.
        self.fork_directory = str(fork.get("directory") or session.get("directory") or "") or None
        messages = await self.read_messages(fork_id)
        estimate = active_context_estimate(messages)
        usage_tokens = session_usage_tokens(session)
        self.logger.write(
            "fork.create",
            source_session_id=session_id,
            fork_session_id=fork_id,
            latency_ms=elapsed_ms(fork_started),
            context_tokens=estimate.tokens,
            context_source=estimate.source,
            usage_tokens=usage_tokens,
            keep_fork=keep_fork,
        )
        return {
            "source_session_id": session_id,
            "fork_session_id": fork_id,
            "title": title,
            "context_tokens": estimate.tokens,
            "context_source": estimate.source,
            "usage_tokens": usage_tokens,
            "keep_fork": keep_fork,
        }

    async def delete_voice_fork(self) -> None:
        if self.fork_session_id and not self.keep_fork:
            fork_id = self.fork_session_id
            await self.client.delete_session(fork_id)
            self.logger.write("fork.delete", session_id=fork_id)
            self.message_cache.pop(fork_id, None)
        self.fork_session_id = None
        self.reset_compaction_guard()
        self.voice_lane_id = None

    async def start_audio(self) -> bool:
        issue = self.config.credential_issue_for("voice_audio")
        if issue:
            await self.send_json(issue.to_voice_bridge_issue(debug_ref=str(self.logger.run_dir)))
            return False
        if self.flux:
            return True
        flux = DeepgramFluxSession(self.config, on_event=self.handle_flux_event)
        try:
            await flux.start()
        except asyncio.CancelledError:
            await flux.close()
            raise
        except Exception:
            await flux.close()
            raise
        self.flux = flux
        epoch = getattr(flux, "connection_epoch", None)
        if isinstance(epoch, int):
            self.adopt_flux_connection_epoch(epoch)
        else:
            self.flux_connection_epoch = None
        self.logger.write("flux.connection.ready", flux_connection_epoch=self.flux_connection_epoch)
        if self.flux_watchdog_task is None or self.flux_watchdog_task.done():
            self.flux_watchdog_task = asyncio.create_task(self.watch_flux_transport())
        return True

    async def ensure_audio_transport(self) -> bool:
        if self.flux is not None:
            return True
        task = self.audio_transport_task
        if task is None or task.done():
            task = asyncio.create_task(self.start_audio())
            self.audio_transport_task = task
        try:
            # Mic startup may be cancelled by a newer M/off request. The
            # transport prewarm remains useful and must finish independently.
            return bool(await asyncio.shield(task))
        finally:
            if task.done() and self.audio_transport_task is task:
                self.audio_transport_task = None

    async def prewarm_audio_transport(self) -> None:
        if self.config.credential_issue_for("voice_audio") or self.closed:
            return
        started_at = time.perf_counter()
        try:
            ready = await self.ensure_audio_transport()
            self.logger.write("flux.prewarm", ready=ready, latency_ms=elapsed_ms(started_at))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - M retries on demand.
            self.logger.write("flux.prewarm.error", error_code=type(exc).__name__)

    def schedule_audio_prewarm(self) -> None:
        if self.flux is not None or self.config.credential_issue_for("voice_audio"):
            return
        self.spawn_background(self.prewarm_audio_transport())

    async def start_native_audio(self) -> bool:
        if not await self.ensure_audio_transport():
            return False
        if self.closed or self.flux is None or self.native_mic or self.native_audio_engine:
            if self.native_audio_engine:
                self.native_audio_engine.set_capture_enabled(True)
            return bool(self.native_mic or self.native_audio_engine)
        self.reset_audio_input_counters()
        self.ensure_echo_canceller()
        self.capture_to_flux_resampler = Pcm16Resampler(
            self.config.device_sample_rate, self.config.deepgram_sample_rate
        )
        self.render_to_probe_resampler = Pcm16Resampler(
            self.config.device_sample_rate, self.config.deepgram_sample_rate
        )
        async def on_engine_drain(_token: PlaybackToken) -> None:
            callback = getattr(self, "on_playback_drained", None)
            if callback:
                await callback(_token)

        engine = PersistentDeviceAudioEngine(
            DeviceAudioOptions(
                device_sample_rate=self.config.device_sample_rate,
                provider_sample_rate=self.config.tts_sample_rate,
            ),
            on_render=self.feed_render_reference,
            on_capture=self.handle_processed_native_audio,
            on_first_frame=getattr(self, "on_first_playback_frame", None),
            on_drain=on_engine_drain,
            capture_processor=self.filter_mic_frame,
            on_event=self.on_playback_event,
        )
        try:
            engine_started = await engine.start()
        except asyncio.CancelledError:
            await engine.close()
            raise
        except Exception:
            await engine.close()
            raise
        if engine_started:
            engine.invalidate_generation(self.speak_generation)
            self.native_audio_engine = engine
            self.force_half_duplex = False
            self.logger.write(
                "native_audio.duplex.start",
                sample_rate=self.config.device_sample_rate,
                frame_ms=10,
                jitter_target_ms=80,
            )
            return True
        await engine.close()

        # A synchronized stream is the only supported automatic full-duplex
        # mode.  Separate device streams are retained solely as the explicit
        # half-duplex safety fallback.
        self.force_half_duplex = True
        self.logger.write(
            "native_audio.duplex.fallback",
            mode="half",
            error_code="duplex_stream_unavailable",
        )
        native_mic = NativeMicSession(
            config=self.config,
            logger=self.logger,
            on_audio=self.handle_native_audio,
            on_issue=self.send_json,
        )
        try:
            mic_started = await native_mic.start()
        except asyncio.CancelledError:
            await native_mic.close()
            raise
        except Exception:
            await native_mic.close()
            raise
        if not mic_started:
            await native_mic.close()
            return False
        self.native_mic = native_mic
        return True

    def ensure_echo_canceller(self) -> None:
        if self.config.voice_duplex != "auto" or self.echo_canceller is not None:
            return
        try:
            self.echo_canceller = EchoCanceller(self.config.device_sample_rate)
            self.logger.write("audio.aec.start", sample_rate=self.config.device_sample_rate)
        except Exception as exc:  # noqa: BLE001 - degrade to the half-duplex gate.
            self.echo_canceller = None
            self.logger.write("audio.aec.unavailable", error=repr(exc))

    def duplex_mode(self) -> str:
        if self.force_half_duplex or not self.stt_transport_healthy:
            return "half"
        mode = self.config.voice_duplex
        if mode == "auto":
            return "aec" if self.echo_canceller else "half"
        return mode

    def filter_mic_frame(self, data: bytes) -> bytes:
        """Keep the assistant's own voice out of STT.

        aec: run WebRTC echo cancellation (voice barge-in stays usable).
        half: substitute silence while TTS is audible so STT physically
        cannot hear the assistant; equal length keeps the STT timeline
        continuous. full: raw passthrough for headphone users.
        """
        mode = self.duplex_mode()
        if mode == "aec" and self.echo_canceller:
            try:
                self.update_aec_delay()
                processed = self.echo_canceller.process_capture(data)
                if self.echo_canceller.delay_error and not self.aec_delay_error_logged:
                    self.aec_delay_error_logged = True
                    self.logger.write("audio.aec.delay.error", error=self.echo_canceller.delay_error)
                return processed
            except Exception as exc:  # noqa: BLE001 - fall back to the gate for the rest of the lane.
                self.logger.write("audio.aec.error", error=repr(exc))
                self.echo_canceller = None
        if mode != "full" and self.playback_is_audible():
            return b"\x00" * len(data)
        return data

    def playback_is_audible(self, tail_sec: float = 0.3) -> bool:
        if self.native_audio_engine and self.native_audio_engine.is_audible(tail_sec=tail_sec):
            return True
        return bool(self.native_speaker and self.native_speaker.is_audible(tail_sec=tail_sec))

    def playback_has_pending_audio(self) -> bool:
        if self.native_audio_engine and (
            self.native_audio_engine.buffered_frames > 0
            or self.native_audio_engine.state not in {"idle", "closed"}
        ):
            return True
        if self.native_speaker:
            queue = getattr(self.native_speaker, "queue", None)
            return bool(getattr(self.native_speaker, "burst_active", False)) or bool(
                queue and not queue.empty()
            ) or self.native_speaker.is_audible(tail_sec=0.0)
        return self.playback_is_audible(tail_sec=0.0)

    def playback_is_exposed(self) -> bool:
        """True while an assistant generation can still reach the device."""

        return bool(
            self.active_turn_id is not None
            # Provider ownership starts before the first PCM frame.  Treat
            # that interval as exposed too: a user utterance must cancel the
            # pending synthesis generation instead of being admitted beside
            # it as an unrelated turn.
            or self.tts_turn_token is not None
            or self.playback_is_audible()
            or self.playback_has_pending_audio()
        )

    def update_aec_delay(self) -> None:
        # Called per mic chunk: the canceller applies the stored value right
        # before each process_stream frame, which is the cadence WebRTC
        # expects (a one-shot set at speaker start never landed — livekit's
        # media_devices re-asserts it per capture frame).
        if not self.echo_canceller:
            return
        if self.native_audio_engine:
            self.echo_canceller.set_stream_delay_ms(self.native_audio_engine.stream_delay_ms)
            return
        delay_sec = self.native_mic.input_delay_sec if self.native_mic else 0.0
        speaker_latency = getattr(getattr(self.native_speaker, "stream", None), "latency", None)
        if isinstance(speaker_latency, (int, float)):
            delay_sec += float(speaker_latency)
        self.echo_canceller.set_stream_delay_ms(int(delay_sec * 1000))

    def mic_gate_active(self) -> bool:
        return (
            self.duplex_mode() == "half"
            and self.playback_is_audible()
        )

    async def stop_audio(self, reason: str, *, keep_transport: bool = False) -> None:
        if self.native_audio_engine:
            await self.native_audio_engine.close()
            self.native_audio_engine = None
        if self.native_mic:
            await self.native_mic.close()
            self.native_mic = None
        self.log_audio_input_summary(reason=reason)
        if not keep_transport:
            transport_task = self.audio_transport_task
            if transport_task and transport_task is not asyncio.current_task() and not transport_task.done():
                transport_task.cancel()
                await asyncio.gather(transport_task, return_exceptions=True)
            self.audio_transport_task = None
            if self.flux_watchdog_task and not self.flux_watchdog_task.done():
                self.flux_watchdog_task.cancel()
                try:
                    await self.flux_watchdog_task
                except asyncio.CancelledError:
                    pass
            self.flux_watchdog_task = None
            if self.flux:
                await self.flux.close()
                self.flux = None
            self.flux_connection_epoch = None
            self.stt_transport_healthy = True
            self.stt_unhealthy_reported = False
        self.force_half_duplex = False
        if not keep_transport:
            self.mic_capture_gated = False
        self.reset_capture_interruption_state()

    def reset_capture_interruption_state(self) -> None:
        self.interruption_state = InterruptionSnapshot(updated_at_ms=self.interruption_elapsed_ms())
        self.interruption_episode = None
        self.interruption_episodes.clear()
        self.expired_interruption_episodes.clear()
        self.expired_interruption_episode_order.clear()
        self.arm_interruption_timers()

    async def set_stt_transport_health(self, healthy: bool, reason: str) -> None:
        if self.stt_transport_healthy == healthy:
            return
        self.stt_transport_healthy = healthy
        self.logger.write("flux.transport.health", healthy=healthy, reason=reason)
        if healthy:
            self.stt_unhealthy_reported = False
            return
        if self.interruption_state.phase is InterruptionPhase.CANDIDATE:
            if self.native_audio_engine:
                self.native_audio_engine.set_ducked(False)
            if self.native_speaker:
                set_ducked = getattr(self.native_speaker, "set_ducked", None)
                if set_ducked:
                    set_ducked(False)
            self.interruption_state = InterruptionSnapshot(updated_at_ms=self.interruption_elapsed_ms())
            self.arm_interruption_timers()
            self.logger.write("interruption.candidate.cancelled", reason="stt_transport_unhealthy")
        if not self.stt_unhealthy_reported:
            self.stt_unhealthy_reported = True
            await self.send_json(
                voice_bridge_issue_payload(
                    capability="voice_audio",
                    diagnostic_code="stt_transport_unhealthy",
                    safe_detail="Voice recognition reconnecting",
                    retryable=True,
                    debug_ref=str(self.logger.run_dir),
                    voice_lane_id=self.voice_lane_id,
                )
            )

    async def watch_flux_transport(self) -> None:
        try:
            while not self.closed and self.flux is not None:
                await asyncio.sleep(0.25)
                snapshot_fn = getattr(self.flux, "health_snapshot", None)
                snapshot = snapshot_fn() if snapshot_fn else None
                if snapshot is None:
                    continue
                now = time.monotonic()
                if self.native_audio_engine:
                    drops = self.native_audio_engine.capture_dropped_frames
                    if drops > self.device_capture_drops_seen:
                        self.logger.write(
                            "native_audio.capture.drop",
                            dropped_frames=drops - self.device_capture_drops_seen,
                            total_dropped_frames=drops,
                        )
                        self.device_capture_drops_seen = drops
                    cadence_stalled = (
                        self.audio_input_started is not None and now - self.audio_input_last_at > 0.5
                    )
                    if cadence_stalled:
                        await self.set_stt_transport_health(False, "capture_cadence_stalled")
                else:
                    cadence_stalled = bool(
                        self.native_mic
                        and self.native_mic.last_callback_at
                        and now - self.native_mic.last_callback_at > 0.5
                    )
                    if cadence_stalled:
                        await self.set_stt_transport_health(False, "capture_cadence_stalled")
                capture_recent = (
                    snapshot.last_capture_at is not None and now - snapshot.last_capture_at <= 1.0
                )
                stalled = capture_recent and (
                    snapshot.oldest_queue_age_ms >= 500
                    or (
                        snapshot.queued_packets > 0
                        and snapshot.state in {"disconnected", "reconnecting"}
                    )
                )
                if stalled:
                    await self.set_stt_transport_health(False, "sender_stalled")
                elif (
                    not cadence_stalled
                    and snapshot.state == "connected"
                    and snapshot.last_send_at is not None
                    and now - snapshot.last_send_at <= 1.0
                ):
                    await self.set_stt_transport_health(True, "watchdog_recovered")
        except asyncio.CancelledError:
            raise

    async def handle_native_audio(self, data: bytes) -> None:
        await self.record_audio_input(data)
        if not self.flux:
            return
        data = self.filter_mic_frame(data)
        await self.handle_processed_native_audio(data, recorded=True)

    async def handle_processed_native_audio(self, data: bytes, *, recorded: bool = False) -> None:
        if not recorded:
            await self.record_audio_input(data)
        if not self.flux:
            return
        if self.capture_to_flux_resampler:
            data = self.capture_to_flux_resampler.push(data)
        if data:
            # The probe compares what STT hears against what we played, so
            # the ring gets the post-AEC frame, not the raw capture.
            self.mic_audio_ring.append(data)
            submit = getattr(self.flux, "submit_audio", None)
            if submit:
                submit(data)
            else:  # compatibility for injected transports
                await self.flux.send_audio(data)

    def reset_audio_input_counters(self) -> None:
        self.audio_input_chunks = 0
        self.audio_input_bytes = 0
        self.audio_input_started = None
        self.audio_input_last_log = 0.0
        self.audio_input_last_at = 0.0
        self.device_capture_drops_seen = 0

    async def record_audio_input(self, data: bytes) -> None:
        now = time.perf_counter()
        self.audio_input_last_at = now
        if self.audio_input_started is None:
            self.audio_input_started = now
            self.audio_input_last_log = now
            self.logger.write("audio.input.first_chunk", bytes=len(data), flux_active=bool(self.flux))
        self.audio_input_chunks += 1
        self.audio_input_bytes += len(data)
        if now - self.audio_input_last_log >= 5:
            self.audio_input_last_log = now
            self.logger.write(
                "audio.input.summary",
                chunks=self.audio_input_chunks,
                bytes=self.audio_input_bytes,
                duration_ms=elapsed_ms(self.audio_input_started),
                flux_active=bool(self.flux),
            )

    def log_audio_input_summary(self, reason: str) -> None:
        if not self.audio_input_chunks or self.audio_input_started is None:
            self.logger.write("audio.input.none", reason=reason, flux_active=bool(self.flux))
            return
        self.logger.write(
            "audio.input.summary",
            reason=reason,
            chunks=self.audio_input_chunks,
            bytes=self.audio_input_bytes,
            duration_ms=elapsed_ms(self.audio_input_started),
            flux_active=bool(self.flux),
        )

    def adopt_flux_connection_epoch(self, epoch: int) -> None:
        """Fence all episode identities created by the replaced socket."""

        if self.flux_connection_epoch == epoch:
            return
        self.flux_connection_epoch = epoch
        self.interruption_episode = None
        self.interruption_episodes.clear()
        self.expired_interruption_episodes.clear()
        self.expired_interruption_episode_order.clear()

    def expire_interruption_episode(self, episode: EpisodeIdentity) -> None:
        """Suppress one provider episode with bounded same-epoch tombstones."""

        key = (episode.flux_epoch, episode.turn_index)
        if key in self.expired_interruption_episodes:
            return
        self.expired_interruption_episodes.add(key)
        self.expired_interruption_episode_order.append(key)
        self.interruption_episodes.pop(key, None)
        while len(self.expired_interruption_episode_order) > 256:
            expired = self.expired_interruption_episode_order.popleft()
            self.expired_interruption_episodes.discard(expired)

    def interruption_episode_for(self, event: dict[str, Any], *, started: bool = False) -> EpisodeIdentity:
        epoch_value = event.get("flux_connection_epoch")
        epoch = int(epoch_value) if isinstance(epoch_value, int) else int(self.flux_connection_epoch or 0)
        turn_value = event.get("turn_index")
        if isinstance(turn_value, int):
            turn_index = turn_value
        elif not started and self.interruption_episode is not None:
            return self.interruption_episode
        else:
            self.interruption_group_seq += 1
            turn_index = self.interruption_group_seq

        key = (epoch, turn_index)
        episode = self.interruption_episodes.get(key)
        if episode is None:
            if not started:
                # A provider transcript with no observed Start is intentionally
                # unknown to the reducer and cannot resurrect an expired turn.
                self.interruption_group_seq += 1
            else:
                self.interruption_group_seq += 1
            episode = EpisodeIdentity(
                flux_epoch=epoch,
                turn_index=turn_index,
                acoustic_group_id=f"acoustic-{epoch}-{turn_index}-{self.interruption_group_seq}",
                playback_generation=self.speak_generation,
            )
            self.interruption_episodes[key] = episode
        if started:
            self.interruption_episode = episode
        return episode

    def interruption_echo_correlation(self) -> float:
        started_at_ms = self.interruption_state.started_at_ms
        if started_at_ms is None:
            return 0.0
        start = self.interruption_clock_started + (started_at_ms / 1000)
        now = time.perf_counter()
        mic_pcm = self.mic_audio_ring.extract(start, now)
        render_pcm = self.render_audio_ring.extract(start - 0.6, now + 0.6)
        return echo_correlation(mic_pcm, render_pcm, self.config.deepgram_sample_rate)

    async def reduce_interruption_event(self, event: InterruptionEvent) -> None:
        async with self.interruption_lock:
            await self._reduce_interruption_event_locked(event)

    async def _reduce_interruption_event_locked(self, event: InterruptionEvent) -> None:
        previous = self.interruption_state
        reduction = reduce_interruption(previous, event)
        self.interruption_state = reduction.state
        episode = reduction.state.episode or event.episode
        if previous.phase is not reduction.state.phase or reduction.actions or event.kind in {
            InterruptionEventKind.EPISODE_STARTED,
            InterruptionEventKind.FINAL_EOT,
            InterruptionEventKind.CANDIDATE_EVALUATION,
            InterruptionEventKind.TURN_RESUMED,
            InterruptionEventKind.MANUAL_INTERRUPT,
            InterruptionEventKind.CLOSE,
        }:
            self.logger.write(
                "interruption.transition",
                from_state=previous.phase.value,
                to_state=reduction.state.phase.value,
                event_kind=event.kind.value,
                flux_connection_epoch=episode.flux_epoch if episode else None,
                turn_index=episode.turn_index if episode else None,
                interruption_episode_id=episode.acoustic_group_id if episode else None,
                playback_generation=episode.playback_generation if episode else self.speak_generation,
            )
        for action in reduction.actions:
            await self.execute_interruption_action(action)
        self.arm_interruption_timers()

    async def execute_interruption_action(self, action: Any) -> None:
        kind = action.kind
        episode = action.episode
        detail = {
            "reason": action.reason,
            "flux_connection_epoch": episode.flux_epoch if episode else None,
            "turn_index": episode.turn_index if episode else None,
            "interruption_episode_id": episode.acoustic_group_id if episode else None,
            "playback_generation": episode.playback_generation if episode else self.speak_generation,
            "echo_correlation": (
                round(self.interruption_state.latest_correlation, 3)
                if self.interruption_state.latest_correlation is not None
                else None
            ),
            "decision_latency_ms": (
                max(0, self.interruption_state.updated_at_ms - self.interruption_state.started_at_ms)
                if self.interruption_state.started_at_ms is not None
                else None
            ),
        }
        if kind is InterruptionActionKind.HOLD_PLAYBACK:
            # "Hold" is logical ownership: acoustically we duck by 18 dB and
            # keep the device/AEC clock continuous.
            if self.native_audio_engine:
                self.native_audio_engine.set_ducked(True)
            if self.native_speaker:
                set_ducked = getattr(self.native_speaker, "set_ducked", None)
                if set_ducked:
                    set_ducked(True)
            self.logger.write("barge_in.candidate", **detail)
        elif kind is InterruptionActionKind.RESUME_PLAYBACK:
            if self.native_audio_engine:
                self.native_audio_engine.set_ducked(False)
            if self.native_speaker:
                set_ducked = getattr(self.native_speaker, "set_ducked", None)
                if set_ducked:
                    set_ducked(False)
            self.logger.write("barge_in.gain_restored", **detail)
        elif kind is InterruptionActionKind.SUPPRESS_EPISODE:
            self.logger.write("barge_in.suppressed", **detail)
        elif kind is InterruptionActionKind.COMMIT_INTERRUPT:
            self.logger.write("barge_in.committed", transcript_chars=len(action.text), **detail)
            await self.barge_in(reason=action.reason)
            await self.maybe_start_compaction(reason="speech_confirmed", run_in_background=True)
        elif kind is InterruptionActionKind.ADMIT_TRANSCRIPT:
            transcript = str(action.text or "").strip()
            if not transcript:
                return
            await self.on_transcript_admitted(transcript, None)
            await self.enqueue_text_turn(transcript, source="voice", eager=False)
        elif kind is InterruptionActionKind.CANCEL_SPECULATION:
            self.logger.write("speech.turn_resumed.compat", action="no_side_effect", **detail)
        elif kind is InterruptionActionKind.EPISODE_EXPIRED:
            if episode is not None:
                self.expire_interruption_episode(episode)
            self.logger.write("interruption.episode.expired", **detail)

    def arm_interruption_timers(self) -> None:
        current = asyncio.current_task()
        for task in (self.interruption_decision_task, self.interruption_expiry_task):
            if task and task is not current and not task.done():
                task.cancel()
        self.interruption_decision_task = None
        self.interruption_expiry_task = None
        state = self.interruption_state
        now_ms = self.interruption_elapsed_ms()
        if state.phase is InterruptionPhase.CANDIDATE and state.started_at_ms is not None:
            self.interruption_decision_task = asyncio.create_task(
                self.interruption_tick_after(max(0, state.started_at_ms + 500 - now_ms))
            )
        deadline: int | None = None
        if state.phase is InterruptionPhase.SUPPRESSED and state.suppression_guard_until_ms is not None:
            deadline = state.suppression_guard_until_ms
        elif state.last_provider_activity_ms is not None and state.final_eot_at_ms is None:
            deadline = state.last_provider_activity_ms + 2_000
        if deadline is not None:
            self.interruption_expiry_task = asyncio.create_task(
                self.interruption_tick_after(max(0, deadline - now_ms))
            )

    async def interruption_tick_after(self, delay_ms: int) -> None:
        try:
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            # At the decision boundary capture fresh acoustic evidence before
            # evaluating the candidate.
            state = self.interruption_state
            now_ms = max(self.interruption_elapsed_ms(), state.updated_at_ms)
            if state.phase is InterruptionPhase.CANDIDATE and state.episode is not None:
                evidence = InterruptionEvent.evaluate(
                    state.episode,
                    now_ms,
                    correlation=self.interruption_echo_correlation(),
                )
                await self.reduce_interruption_event(evidence)
                return
            await self.reduce_interruption_event(InterruptionEvent.tick(now_ms))
        except asyncio.CancelledError:
            raise

    async def handle_flux_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type.startswith("flux.transport."):
            epoch = event.get("flux_connection_epoch") or event.get("epoch")
            if isinstance(epoch, int) and event_type in {
                "flux.transport.connected",
                "flux.transport.send_ok",
            }:
                self.adopt_flux_connection_epoch(epoch)
            self.logger.write(
                "flux.transport.event",
                transport_event=event_type,
                flux_connection_epoch=epoch,
                stage=event_type.rsplit(".", 1)[-1],
                error_code=(
                    event.get("error_code")
                    or (
                        "stt_transport_failure"
                        if event_type.endswith(("error", "timeout"))
                        else None
                    )
                ),
                status_code=event.get("status_code"),
            )
            if event_type in {
                "flux.transport.send_error",
                "flux.transport.read_error",
                "flux.transport.connect_error",
            }:
                await self.set_stt_transport_health(False, event_type.rsplit(".", 1)[-1])
            elif event_type == "flux.transport.send_ok":
                await self.set_stt_transport_health(True, "send_ok")
            return
        if self.mic_capture_gated:
            episode = self.interruption_episode_for(event, started=event_type == "speech.start")
            self.expire_interruption_episode(episode)
            self.final_transcript = ""
            self.logger.write(
                "speech.muted.drop",
                event_type=event_type,
                flux_connection_epoch=episode.flux_epoch,
                turn_index=episode.turn_index,
            )
            return
        event_epoch = event.get("flux_connection_epoch")
        if (
            isinstance(event_epoch, int)
            and self.flux_connection_epoch is not None
            and event_epoch != self.flux_connection_epoch
        ):
            self.logger.write(
                "speech.stale_epoch.drop",
                event_type=str(event.get("type") or ""),
                flux_connection_epoch=event_epoch,
                active_flux_connection_epoch=self.flux_connection_epoch,
                turn_index=event.get("turn_index"),
            )
            return
        await self.forward_flux_event(event)
        if event["type"] == "speech.start":
            episode = self.interruption_episode_for(event, started=True)
            if (episode.flux_epoch, episode.turn_index) in self.expired_interruption_episodes:
                self.logger.write(
                    "speech.expired_episode.drop",
                    flux_connection_epoch=episode.flux_epoch,
                    turn_index=episode.turn_index,
                )
                return
            self.logger.write(
                "speech.start",
                flux_connection_epoch=episode.flux_epoch,
                turn_index=episode.turn_index,
                stt_episode_id=episode.acoustic_group_id,
            )
            if self.mic_gate_active():
                # Gated mic feeds STT silence; anything STT still reports
                # while TTS is audible can only be residue, never a user.
                # Own the entire Flux episode through final EOT; merely
                # ignoring Start lets its delayed final transcript re-enter
                # through QUIET recovery once playback becomes inaudible.
                self.expire_interruption_episode(episode)
                self.logger.write(
                    "speech.gated",
                    event_type="speech.start",
                    flux_connection_epoch=episode.flux_epoch,
                    turn_index=episode.turn_index,
                )
                return
            playback_exposed = self.playback_is_exposed()
            await self.reduce_interruption_event(
                InterruptionEvent.start(
                    episode,
                    self.interruption_elapsed_ms(),
                    playback_exposed=playback_exposed,
                )
            )
        elif event["type"] == "speech.resumed":
            # Flux TurnResumed means an *eager* EOT prediction was revoked.
            # It is not fresh acoustic evidence and must never flush real
            # playback or abort OpenCode.  We keep it for correlation only.
            self.logger.write(
                "speech.resumed",
                action="ignored",
                flux_connection_epoch=event_epoch,
                turn_index=event.get("turn_index"),
            )
            episode = self.interruption_episode_for(event)
            await self.reduce_interruption_event(
                InterruptionEvent.turn_resumed(episode, self.interruption_elapsed_ms())
            )
        elif event["type"] == "speech.transcript":
            transcript = str(event.get("transcript") or "").strip()
            if event.get("is_final") and transcript:
                self.final_transcript = transcript
                self.logger.write("speech.transcript.final", transcript_chars=len(self.final_transcript))
            if transcript:
                episode = self.interruption_episode_for(event)
                if (episode.flux_epoch, episode.turn_index) in self.expired_interruption_episodes:
                    self.logger.write(
                        "speech.expired_episode.drop",
                        flux_connection_epoch=episode.flux_epoch,
                        turn_index=episode.turn_index,
                    )
                    return
                correlation = (
                    self.interruption_echo_correlation()
                    if self.interruption_state.phase
                    in {InterruptionPhase.CANDIDATE, InterruptionPhase.SUPPRESSED}
                    else None
                )
                await self.reduce_interruption_event(
                    InterruptionEvent.interim(
                        episode,
                        self.interruption_elapsed_ms(),
                        transcript,
                        correlation=correlation,
                        playback_exposed=self.playback_is_exposed(),
                    )
                )
        elif event["type"] == "speech.end":
            transcript = str(event.get("transcript") or self.final_transcript).strip()
            self.final_transcript = ""
            eager = bool(event.get("eager"))
            confidence = event.get("confidence")
            confidence = float(confidence) if isinstance(confidence, (int, float)) else None
            episode = self.interruption_episode_for(event)
            expired_episode = (episode.flux_epoch, episode.turn_index) in self.expired_interruption_episodes
            if expired_episode:
                self.logger.write(
                    "speech.expired_episode.drop",
                    flux_connection_epoch=episode.flux_epoch,
                    turn_index=episode.turn_index,
                )
                return
            if self.interruption_state.phase is InterruptionPhase.QUIET:
                # Defensive recovery for an EndOfTurn whose StartOfTurn was
                # lost during a connection boundary.  It still enters the
                # same controller and uses current playback exposure.
                self.interruption_episode = episode
                await self.reduce_interruption_event(
                    InterruptionEvent.start(
                        episode,
                        self.interruption_elapsed_ms(),
                        playback_exposed=self.playback_is_exposed(),
                    )
                )
            correlation = (
                self.interruption_echo_correlation()
                if self.interruption_state.phase in {InterruptionPhase.CANDIDATE, InterruptionPhase.SUPPRESSED}
                else None
            )
            self.logger.write(
                "speech.end",
                transcript_chars=len(transcript),
                eager=eager,
                confidence=confidence,
                flux_connection_epoch=episode.flux_epoch,
                turn_index=episode.turn_index,
                stt_episode_id=episode.acoustic_group_id,
            )
            if eager:
                self.logger.write(
                    "speech.eager_eot.ignored",
                    transcript_chars=len(transcript),
                    flux_connection_epoch=event_epoch,
                    turn_index=event.get("turn_index"),
                )
                await self.reduce_interruption_event(
                    InterruptionEvent.eager_eot(
                        episode,
                        self.interruption_elapsed_ms(),
                        transcript,
                        correlation=correlation,
                    )
                )
                return
            await self.reduce_interruption_event(
                InterruptionEvent.final_eot(
                    episode,
                    self.interruption_elapsed_ms(),
                    transcript,
                    correlation=correlation,
                    playback_exposed=self.playback_is_exposed(),
                )
            )

    async def enqueue_text_turn(self, text: str, source: str, eager: bool = False) -> None:
        issue = self.config.credential_issue_for("voice_turns")
        if issue:
            await self.send_json(issue.to_voice_bridge_issue(debug_ref=str(self.logger.run_dir)))
            return
        if not self.fork_session_id:
            await self.send_json({"type": "error", "message": "Start a voice fork before sending a prompt."})
            return
        if self.turn_task and not self.turn_task.done():
            if self.active_turn_id is None and not self.playback_is_exposed():
                # The previous task has not crossed turn.start yet. Replacing
                # local setup is not an interruption and must not emit the
                # protocol event or abort a completed/audible assistant turn.
                pending = self.turn_task
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                compaction = self.compaction_task
                if compaction and not compaction.done() and not self.compaction_running:
                    compaction.cancel()
                    await asyncio.gather(compaction, return_exceptions=True)
                    async with self.compaction_lock:
                        if self.compaction_task is compaction:
                            self.compaction_task = None
                            self.compaction_reasons.clear()
                            self.compaction_force_requested = False
                            self.compaction_decision_event.set()
                self.logger.write("turn.preflight.replaced", reason="new_turn")
            else:
                await self.barge_in(reason="new_turn")
        self.turn_task = asyncio.create_task(self.run_text_turn(text=text, source=source, eager=eager))

    def turn_is_active(self, turn_id: int) -> bool:
        # Model ownership and playback ownership are independent. A provider
        # failure advances the playback generation to fence PCM, but the model
        # turn must keep streaming its remaining screen text. Real turn
        # cancellation clears active_turn_id synchronously before any await.
        return self.active_turn_id == turn_id

    async def timeout_silent_turn(self, turn_id: int, started: float, stream_source: str) -> None:
        """Fail a provider connection stall without shortening long responses."""

        if not self.turn_is_active(turn_id):
            return
        self.active_turn_id = None
        latency_ms = elapsed_ms(started)
        self.logger.write(
            "turn.timeout",
            turn_id=turn_id,
            latency_ms=latency_ms,
            stream_source=stream_source,
            reason="first_text_timeout",
        )
        self.logger.state_transition("thinking", "voice_bridge_issue", turn_id=turn_id)
        await self.send_json(
            {
                "type": "turn.timeout",
                "turn_id": turn_id,
                "reason": "first_text_timeout",
            }
        )
        self.spawn_background(self.abort_fork_turn())

    async def run_text_turn(self, text: str, source: str, eager: bool) -> None:
        if not self.fork_session_id:
            return
        self.turn_seq += 1
        turn_id = self.turn_seq
        started = time.perf_counter()
        if self.speaker_prewarm_task is None or self.speaker_prewarm_task.done():
            self.speaker_prewarm_task = asyncio.create_task(self.prewarm_speaker())
        try:
            decision = await self.maybe_start_compaction(
                reason="turn_preflight",
                run_in_background=True,
            )
            if decision is not None and not decision.completed:
                await self.send_json({"type": "compaction.try_again", "turn_id": turn_id})
                self.logger.write("turn.preflight.blocked", turn_id=turn_id, reason="compaction_failed")
                return
            if not await self.maybe_wait_for_compaction(turn_id):
                self.logger.write("turn.preflight.blocked", turn_id=turn_id, reason="compaction_unconfirmed")
                return
            session_id = self.fork_session_id
            if not session_id:
                return
            before_messages = await asyncio.wait_for(
                self.read_messages(session_id),
                timeout=TURN_PREFLIGHT_TIMEOUT_SEC,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - preflight must never strand an active turn.
            self.logger.write(
                "turn.preflight.error",
                turn_id=turn_id,
                error_code=type(exc).__name__,
            )
            self.logger.state_transition("thinking", "voice_bridge_issue", turn_id=turn_id)
            await self.send_json(
                {
                    "type": "turn.error",
                    "turn_id": turn_id,
                    "message": "turn_preflight_failed",
                    "failure": "turn_preflight_failed",
                }
            )
            return
        self.active_turn_id = turn_id
        self.turn_playback_tokens[turn_id] = PlaybackToken(self.speak_generation, turn_id)
        self.tts_first_audio_seen = False
        self.turn_spoken_any = False
        tracker = AssistantTextTracker(before_messages)
        await self.send_json({"type": "turn.start", "turn_id": turn_id, "source": source, "text": text, "eager": eager})
        self.logger.state_transition("ready", "thinking", turn_id=turn_id, source=source)
        self.logger.write(
            "turn.start",
            turn_id=turn_id,
            source=source,
            eager=eager,
            session_id=session_id,
            stream_source="event",
        )
        if self.config.response_mode == "structured":
            try:
                await self.run_structured_text_turn(
                    session_id=session_id,
                    text=text,
                    turn_id=turn_id,
                    started=started,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the lane alive and fail closed.
                if self.turn_is_active(turn_id):
                    await self.fail_structured_turn(
                        turn_id,
                        started,
                        error=exc,
                        safety_codes=(),
                    )
            return
        try:
            await self.run_event_text_turn(
                session_id=session_id,
                text=text,
                before_messages=before_messages,
                turn_id=turn_id,
                started=started,
            )
            return
        except OpenCodeEventFallback as exc:
            if not self.turn_is_active(turn_id):
                return
            self.logger.write(
                "opencode.stream.fallback",
                turn_id=turn_id,
                reason=exc.reason,
                prompt_sent=exc.prompt_sent,
            )
            await self.send_json(
                {
                    "type": "opencode.stream.fallback",
                    "turn_id": turn_id,
                    "reason": exc.reason,
                    "prompt_sent": exc.prompt_sent,
                }
            )
            if exc.prompt_sent:
                await self.poll_text_turn(
                    session_id=session_id,
                    tracker=tracker,
                    turn_id=turn_id,
                    started=started,
                    stream_source="poll_after_event",
                )
                return

        try:
            tracker = await self.prompt_with_overflow_retry(session_id, text, tracker, turn_id)
        except Exception as exc:  # noqa: BLE001 - keep the WebSocket alive and make the failure visible.
            self.active_turn_id = None
            self.logger.state_transition("thinking", "voice_bridge_issue", turn_id=turn_id)
            self.logger.write("turn.request.error", turn_id=turn_id, error=repr(exc))
            await self.send_json({"type": "turn.error", "turn_id": turn_id, "message": repr(exc)})
            return
        await self.send_json({"type": "opencode.requested", "turn_id": turn_id})
        await self.poll_text_turn(
            session_id=session_id,
            tracker=tracker,
            turn_id=turn_id,
            started=started,
            stream_source="poll",
        )

    async def read_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Read messages through the OpenCode 1.17 structured-format shim.

        Injected test clients predating the compatibility method retain the
        legacy fallback, while the production client uses the v2 projection
        when the legacy decoder rejects ``format.retryCount``.
        """

        compatible = getattr(self.client, "messages_for_tracking", None)
        if compatible is not None:
            incoming = await compatible(session_id)
        else:
            incoming = await self.client.messages(session_id)
        cached = self.message_cache.get(session_id)
        if not cached:
            self.message_cache[session_id] = list(incoming)
            return list(incoming)
        merged = list(cached)
        positions = {
            message_identity(message)[0]: index
            for index, message in enumerate(merged)
            if message_identity(message)[0]
        }
        for message in incoming:
            identity = message_identity(message)[0]
            if identity and identity in positions:
                merged[positions[identity]] = message
                continue
            if identity:
                positions[identity] = len(merged)
            merged.append(message)
        self.message_cache[session_id] = merged
        return list(merged)

    async def run_structured_text_turn(
        self,
        *,
        session_id: str,
        text: str,
        turn_id: int,
        started: float,
    ) -> None:
        before_messages = await self.read_messages(session_id)
        before_ids = message_ids(before_messages)
        repaired = False
        result = await self.observe_structured_response(session_id, text, turn_id)
        if result.response is None and is_context_overflow_value(result.error):
            recovered_session = await self.recover_overflow_fork(session_id, before_ids)
            outcome = await self.maybe_start_compaction(
                reason="context_overflow_error",
                run_in_background=False,
                force=True,
            )
            if outcome is not None and outcome.completed and self.turn_is_active(turn_id):
                session_id = recovered_session
                result = await self.observe_structured_response(session_id, text, turn_id)

        selected = result.response
        evaluation = evaluate_response(
            result.raw,
            ResponseCase(
                case_id=f"production-{turn_id}",
                category="production",
                prompt=text,
                secret_sentinels=secret_values(),
            ),
            workspace_root=self.config.workspace_dir,
        )
        admitted, repair_reason = should_admit_repair(evaluation)
        if result.raw is not None and admitted and self.turn_is_active(turn_id):
            repair = await self.observe_structured_response(
                session_id,
                repair_prompt(text, result.raw, list(evaluation.violations)),
                turn_id,
                tools={
                    name: False
                    for name in (
                        "read",
                        "glob",
                        "grep",
                        "list",
                        "edit",
                        "bash",
                        "task",
                        "webfetch",
                        "websearch",
                    )
                },
                emit_tool_cues=False,
            )
            repaired_evaluation = evaluate_response(
                repair.raw,
                ResponseCase(
                    case_id=f"production-repair-{turn_id}",
                    category="production",
                    prompt=text,
                    secret_sentinels=secret_values(),
                ),
                workspace_root=self.config.workspace_dir,
            )
            use_repair, selection_reason = should_select_repair(evaluation, repaired_evaluation)
            self.logger.write(
                "structured.repair",
                turn_id=turn_id,
                admitted_reason=repair_reason,
                selected=use_repair,
                selection_reason=selection_reason,
            )
            if use_repair:
                selected = repair.response
                evaluation = repaired_evaluation
                result = repair
                repaired = True

        if selected is None or evaluation.safety_violations:
            await self.fail_structured_turn(
                turn_id,
                started,
                error=result.error or "structured_response_rejected",
                safety_codes=tuple(sorted({item.code for item in evaluation.safety_violations})),
            )
            return
        if not self.turn_is_active(turn_id):
            return

        first_text_ms = elapsed_ms(started)
        await self.send_json({"type": "assistant.first_text", "turn_id": turn_id, "latency_ms": first_text_ms})
        await self.send_json(
            {"type": "assistant.delta", "turn_id": turn_id, "delta": selected.display_text}
        )
        chunker = TTSChunker()
        for chunk in chunker.push(selected.spoken_text) + chunker.flush():
            await self.speak(chunk, turn_id=turn_id)
            if not self.turn_is_active(turn_id):
                return
        await self.finish_speaking_turn(turn_id)
        if not self.turn_is_active(turn_id):
            return
        await self.send_json(
            {
                "type": "turn.complete",
                "turn_id": turn_id,
                "latency_ms": elapsed_ms(started),
                "text": selected.display_text,
                "spoken_text": selected.spoken_text,
                "stream_source": result.stream_source,
            }
        )
        self.logger.write(
            "turn.complete",
            turn_id=turn_id,
            latency_ms=elapsed_ms(started),
            response_chars=len(selected.display_text),
            spoken_chars=len(selected.spoken_text),
            stream_source=result.stream_source,
            structured=True,
            repaired=repaired,
        )
        self.logger.state_transition(
            "thinking",
            "awaiting_playback" if self.turn_spoken_any else "ready",
            turn_id=turn_id,
        )
        self.cancel_tool_hold(turn_id)
        self.active_turn_id = None
        await self.maybe_start_compaction(reason="turn_complete", run_in_background=True)

    async def observe_structured_response(
        self,
        session_id: str,
        prompt: str,
        turn_id: int,
        *,
        tools: dict[str, bool] | None = None,
        emit_tool_cues: bool = True,
    ) -> StructuredTurnResult:
        observed_tools: set[tuple[str, str]] = set()

        async def on_tool(activity: Any) -> None:
            key = (activity.part_id, activity.status)
            if key in observed_tools:
                return
            observed_tools.add(key)
            self.logger.write(
                "opencode.tool.activity",
                turn_id=turn_id,
                tool=activity.tool,
                status=activity.status,
                part_id=activity.part_id,
            )
            if emit_tool_cues:
                await self.on_structured_tool_activity(turn_id, activity)

        return await run_structured_turn(
            self.client,
            session_id=session_id,
            directory=self.fork_directory,
            prompt=prompt,
            model=self.config.model,
            agent=self.config.opencode_agent,
            max_turn_sec=self.config.max_turn_sec,
            final_grace_sec=max(0.1, self.config.event_completion_grace_sec),
            tools=tools,
            on_tool_activity=on_tool,
        )

    async def fail_structured_turn(
        self,
        turn_id: int,
        started: float,
        *,
        error: Any,
        safety_codes: tuple[str, ...],
    ) -> None:
        self.logger.write(
            "structured.reject",
            turn_id=turn_id,
            latency_ms=elapsed_ms(started),
            error_code=type(error).__name__ if not isinstance(error, str) else error,
            safety_codes=safety_codes,
        )
        self.cancel_tool_hold(turn_id)
        self.speak_generation += 1
        if self.native_audio_engine:
            self.native_audio_engine.invalidate_generation(self.speak_generation)
        if self.native_speaker:
            invalidate = getattr(self.native_speaker, "invalidate_generation", None)
            if invalidate:
                invalidate(self.speak_generation, "structured_rejected")
        self.active_turn_id = None
        await self.send_json(
            {
                "type": "turn.error",
                "turn_id": turn_id,
                "message": "structured_response_unavailable",
                "failure": "structured_response_unavailable",
            }
        )

    async def on_structured_tool_activity(self, turn_id: int, activity: Any) -> None:
        if activity.tool == "StructuredOutput" or activity.status not in {"pending", "running"}:
            return
        if turn_id in self.tool_cue_turns:
            return
        self.tool_cue_turns.add(turn_id)
        await self.play_tool_cue(turn_id, hold=False)
        task = asyncio.create_task(self.play_tool_hold_after_delay(turn_id), name=f"tool-hold-{turn_id}")
        self.tool_hold_tasks[turn_id] = task

    async def play_tool_hold_after_delay(self, turn_id: int) -> None:
        try:
            await asyncio.sleep(4.0)
            if self.turn_is_active(turn_id):
                await self.play_tool_cue(turn_id, hold=True)
        except asyncio.CancelledError:
            raise
        finally:
            if self.tool_hold_tasks.get(turn_id) is asyncio.current_task():
                self.tool_hold_tasks.pop(turn_id, None)

    def cancel_tool_hold(self, turn_id: int) -> None:
        task = self.tool_hold_tasks.pop(turn_id, None)
        if task and not task.done():
            task.cancel()
        self.tool_cue_turns.discard(turn_id)

    async def play_tool_cue(self, turn_id: int, *, hold: bool) -> None:
        engine = self.native_audio_engine
        fallback = self.native_speaker
        token = self.turn_playback_tokens.get(turn_id)
        if (engine is None and fallback is None) or token is None or not self.turn_is_active(turn_id):
            self.logger.write("tool.cue.skipped", turn_id=turn_id, hold=hold, reason="device_clock_unavailable")
            return
        pcm = synthesize_tool_cue(self.config.device_sample_rate, hold=hold)
        if engine is not None:
            admitted = await engine.play_device_cue(pcm, token)
        else:
            cue = getattr(fallback, "play_device_cue", None)
            admitted = bool(await cue(pcm, token)) if cue is not None else False
        self.logger.write(
            "tool.cue",
            turn_id=turn_id,
            hold=hold,
            admitted=admitted,
            sample_rate=self.config.device_sample_rate,
            duration_ms=int(len(pcm) / 2 / self.config.device_sample_rate * 1000),
        )

    async def recover_overflow_fork(self, session_id: str, before_ids: set[str]) -> str:
        messages = await self.client.messages_for_tracking(session_id)
        new_user_id = next(
            (
                message_identity(message)[0]
                for message in messages
                if message_identity(message)[0] not in before_ids
                and message_identity(message)[1] == "user"
            ),
            None,
        )
        if not new_user_id:
            return session_id
        failed_session = session_id
        fork = await self.client.fork_session(failed_session, message_id=new_user_id)
        recovered_id = str(fork.get("id") or "")
        if not recovered_id:
            raise RuntimeError("OpenCode did not return an overflow recovery fork id.")
        await self.client.switch_model(recovered_id, self.config.model)
        await self.client.switch_agent(recovered_id, self.config.opencode_agent)
        self.fork_session_id = recovered_id
        failed_cache = self.message_cache.get(failed_session, [])
        cutoff = next(
            (
                index
                for index, message in enumerate(failed_cache)
                if message_identity(message)[0] == new_user_id
            ),
            len(failed_cache),
        )
        self.message_cache[recovered_id] = list(failed_cache[:cutoff])
        session = await self.client.get_session(recovered_id)
        self.fork_directory = str(fork.get("directory") or session.get("directory") or "") or self.fork_directory
        self.reset_compaction_guard()
        self.logger.write(
            "turn.context_overflow.recovery_fork",
            failed_session_id=failed_session,
            recovered_session_id=recovered_id,
            cutoff_message_id=new_user_id,
        )
        if not self.keep_fork:
            await self.client.delete_session(failed_session)
            self.message_cache.pop(failed_session, None)
        return recovered_id

    async def poll_text_turn(
        self,
        session_id: str,
        tracker: AssistantTextTracker,
        turn_id: int,
        started: float,
        stream_source: str,
    ) -> None:
        chunker = TTSChunker()
        speech_filter = SpeechTextFilter()
        first_text_ms: int | None = None
        full_text = ""
        while self.turn_is_active(turn_id) and elapsed_ms(started) < int(self.config.max_turn_sec * 1000):
            if first_text_ms is None and elapsed_ms(started) >= int(self.config.first_text_timeout_sec * 1000):
                await self.timeout_silent_turn(turn_id, started, stream_source)
                return
            try:
                messages = await asyncio.wait_for(
                    self.client.messages(session_id),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                self.logger.write(
                    "opencode.poll.timeout",
                    turn_id=turn_id,
                    timeout_ms=1_000,
                    stream_source=stream_source,
                )
                await asyncio.sleep(self.config.poll_interval_sec)
                continue
            except Exception as exc:  # noqa: BLE001 - keep polling until the turn deadline.
                self.logger.write(
                    "opencode.poll.error",
                    turn_id=turn_id,
                    error=repr(exc),
                    stream_source=stream_source,
                )
                await asyncio.sleep(self.config.poll_interval_sec)
                continue
            if not self.turn_is_active(turn_id):
                return
            update = tracker.update(messages)
            if update.deltas and first_text_ms is None:
                first_text_ms = elapsed_ms(started)
                await self.send_json({"type": "assistant.first_text", "turn_id": turn_id, "latency_ms": first_text_ms})
                self.logger.write("assistant.first_text", turn_id=turn_id, latency_ms=first_text_ms)
            for delta in update.deltas:
                if not self.turn_is_active(turn_id):
                    return
                full_text += delta
                await self.send_json({"type": "assistant.delta", "turn_id": turn_id, "delta": delta})
                for chunk in chunker.push(speech_filter.push(delta)):
                    await self.speak(chunk, turn_id=turn_id)
                    if not self.turn_is_active(turn_id):
                        return
            if update.completed:
                if not self.turn_is_active(turn_id):
                    return
                for chunk in chunker.push(speech_filter.flush()) + chunker.flush():
                    await self.speak(chunk, turn_id=turn_id)
                    if not self.turn_is_active(turn_id):
                        return
                await self.finish_speaking_turn(turn_id)
                self.log_if_silent_completion(turn_id, update.full_text or full_text)
                if update.error:
                    await self.send_json(
                        {
                            "type": "turn.error",
                            "turn_id": turn_id,
                            "message": str(update.error)[:1000],
                            "failure": classify_turn_failure(update.error),
                        }
                    )
                    self.logger.write("turn.error", turn_id=turn_id, error=update.error)
                await self.send_json(
                    {
                        "type": "turn.complete",
                        "turn_id": turn_id,
                        "latency_ms": elapsed_ms(started),
                        "text": update.full_text or full_text,
                        "stream_source": stream_source,
                    }
                )
                self.logger.write(
                    "turn.complete",
                    turn_id=turn_id,
                    latency_ms=elapsed_ms(started),
                    response_chars=len(update.full_text or full_text),
                    stream_source=stream_source,
                )
                self.logger.state_transition(
                    "thinking",
                    "awaiting_playback" if self.turn_spoken_any else "ready",
                    turn_id=turn_id,
                )
                self.active_turn_id = None
                await self.maybe_start_compaction(reason="turn_complete", run_in_background=True)
                return
            await asyncio.sleep(self.config.poll_interval_sec)

        if self.turn_is_active(turn_id):
            await self.send_json({"type": "turn.timeout", "turn_id": turn_id})
            self.logger.write("turn.timeout", turn_id=turn_id, latency_ms=elapsed_ms(started))
            self.logger.state_transition("thinking", "voice_bridge_issue", turn_id=turn_id)
            self.active_turn_id = None

    async def run_event_text_turn(
        self,
        session_id: str,
        text: str,
        before_messages: list[dict[str, Any]],
        turn_id: int,
        started: float,
    ) -> None:
        self.turn_playback_tokens.setdefault(turn_id, PlaybackToken(self.speak_generation, turn_id))
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        ready = asyncio.Event()
        reader_task = asyncio.create_task(self.read_opencode_events(session_id, queue, ready))
        prompt_sent = False
        try:
            try:
                await asyncio.wait_for(ready.wait(), timeout=3)
            except asyncio.TimeoutError as exc:
                raise OpenCodeEventFallback("event_stream_open_timeout", prompt_sent=False) from exc
            self.logger.write("opencode.stream.start", turn_id=turn_id, session_id=session_id)
            try:
                await self.client.prompt_async(session_id, text, self.config.model, agent=self.config.opencode_agent)
            except Exception as exc:  # noqa: BLE001 - prompt_async is optional; fallback preserves old behavior.
                raise OpenCodeEventFallback(f"prompt_async_error:{type(exc).__name__}", prompt_sent=False) from exc
            prompt_sent = True
            await self.send_json({"type": "opencode.requested", "turn_id": turn_id})

            tracker = HybridOpenCodeTurnTracker(session_id=session_id, before_messages=before_messages)
            chunker = TTSChunker()
            speech_filter = SpeechTextFilter()
            first_text_ms: int | None = None
            full_text = ""
            stale_idle_logged = False
            completion_grace_deadline: float | None = None
            completion_observed = False
            last_event_ms = elapsed_ms(started)
            hedge_active = False
            event_healthy = True
            hedge_deadline = time.perf_counter() + 3.0
            poll_interval = max(0.5, self.config.poll_interval_sec * 5)
            poll_task: asyncio.Task[None] | None = None
            no_first_text_logged = False
            text_sources: set[str] = set()

            def ensure_polling() -> None:
                nonlocal poll_task
                if poll_task is None or poll_task.done():
                    poll_task = asyncio.create_task(
                        self.poll_opencode_messages(
                            session_id=session_id,
                            queue=queue,
                            turn_id=turn_id,
                            interval_sec=poll_interval,
                        ),
                        name=f"opencode-poll-{turn_id}",
                    )

            async def consume_update(update: Any, source: str) -> bool:
                nonlocal first_text_ms, full_text, completion_grace_deadline
                nonlocal completion_observed, hedge_deadline
                if not self.turn_is_active(turn_id):
                    return False
                if update.deltas:
                    text_sources.add(source)
                    if first_text_ms is None:
                        first_text_ms = elapsed_ms(started)
                        await self.send_json(
                            {"type": "assistant.first_text", "turn_id": turn_id, "latency_ms": first_text_ms}
                        )
                        self.logger.write("assistant.first_text", turn_id=turn_id, latency_ms=first_text_ms)
                        self.logger.write(
                            "opencode.stream.first_delta",
                            turn_id=turn_id,
                            latency_ms=first_text_ms,
                            source=source,
                        )
                    for delta in update.deltas:
                        if not self.turn_is_active(turn_id):
                            return False
                        full_text += delta
                        await self.send_json({"type": "assistant.delta", "turn_id": turn_id, "delta": delta})
                        for chunk in chunker.push(speech_filter.push(delta)):
                            await self.speak(chunk, turn_id=turn_id)
                            if not self.turn_is_active(turn_id):
                                return False
                    if not hedge_active:
                        hedge_deadline = time.perf_counter() + 3.0
                    if completion_observed and self.config.event_completion_grace_sec > 0:
                        # Completion can arrive before one or more part events.
                        # Debounce from the latest unseen text, not the first
                        # premature session.idle signal.
                        completion_grace_deadline = (
                            time.perf_counter() + self.config.event_completion_grace_sec
                        )

                if not update.completed:
                    return False
                # A completed messages snapshot is already the canonical
                # polling observation. Event completion, however, is only a
                # signal: part events can still be queued behind session.idle.
                if source == "poll" and not completion_observed:
                    return bool(update.full_text or full_text)
                completion_observed = True
                if self.config.event_completion_grace_sec <= 0:
                    return bool(update.full_text or full_text)
                if completion_grace_deadline is None:
                    completion_grace_deadline = (
                        time.perf_counter() + self.config.event_completion_grace_sec
                    )
                    self.logger.write("opencode.stream.completion_grace", turn_id=turn_id)
                return False

            while self.turn_is_active(turn_id) and elapsed_ms(started) < int(self.config.max_turn_sec * 1000):
                now = time.perf_counter()
                if first_text_ms is None and elapsed_ms(started) >= int(
                    self.config.first_text_timeout_sec * 1000
                ):
                    await self.timeout_silent_turn(turn_id, started, "event")
                    return
                if first_text_ms is None and not no_first_text_logged and elapsed_ms(started) >= 10_000:
                    no_first_text_logged = True
                    self.logger.write(
                        "opencode.stream.first_delta.delayed",
                        turn_id=turn_id,
                        latency_ms=elapsed_ms(started),
                        event_healthy=event_healthy,
                        poll_active=bool(poll_task and not poll_task.done()),
                    )
                if completion_grace_deadline is not None and now >= completion_grace_deadline:
                    # Reconcile through the same message-id tracker before
                    # closing TTS, so a suffix observed only by polling still
                    # reaches both the screen and speech exactly once.
                    try:
                        messages = await asyncio.wait_for(
                            self.client.messages(session_id),
                            timeout=1.0,
                        )
                    except asyncio.TimeoutError:
                        self.logger.write(
                            "opencode.stream.completion_reconcile.timeout",
                            turn_id=turn_id,
                            timeout_ms=1_000,
                        )
                        if full_text:
                            stream_source = (
                                "hybrid"
                                if text_sources == {"event", "poll"}
                                else next(iter(text_sources), "event")
                            )
                            await self.complete_event_text_turn(
                                session_id=session_id,
                                before_messages=before_messages,
                                turn_id=turn_id,
                                started=started,
                                chunker=chunker,
                                speech_filter=speech_filter,
                                event_text=full_text,
                                stream_source=stream_source,
                            )
                            self.logger.write(
                                "opencode.stream.done",
                                turn_id=turn_id,
                                latency_ms=elapsed_ms(started),
                                last_event_ms=last_event_ms,
                                final_fetch="timeout",
                            )
                            return
                        completion_grace_deadline = time.perf_counter() + poll_interval
                        continue
                    except Exception as exc:  # noqa: BLE001 - keep SSE alive and retry the grace fetch.
                        self.logger.write(
                            "opencode.stream.completion_reconcile.error",
                            turn_id=turn_id,
                            error=repr(exc),
                        )
                        if full_text:
                            stream_source = (
                                "hybrid"
                                if text_sources == {"event", "poll"}
                                else next(iter(text_sources), "event")
                            )
                            await self.complete_event_text_turn(
                                session_id=session_id,
                                before_messages=before_messages,
                                turn_id=turn_id,
                                started=started,
                                chunker=chunker,
                                speech_filter=speech_filter,
                                event_text=full_text,
                                stream_source=stream_source,
                            )
                            self.logger.write(
                                "opencode.stream.done",
                                turn_id=turn_id,
                                latency_ms=elapsed_ms(started),
                                last_event_ms=last_event_ms,
                                final_fetch="error",
                            )
                            return
                        completion_grace_deadline = time.perf_counter() + poll_interval
                        continue
                    update = tracker.update_messages(messages)
                    if not self.turn_is_active(turn_id):
                        return
                    await consume_update(update, "poll")
                    if not self.turn_is_active(turn_id):
                        return
                    if not update.full_text and not full_text:
                        hedge_active = True
                        ensure_polling()
                        completion_grace_deadline = time.perf_counter() + poll_interval
                        continue
                    stream_source = (
                        "hybrid" if text_sources == {"event", "poll"} else next(iter(text_sources), "event")
                    )
                    await self.complete_event_text_turn(
                        session_id=session_id,
                        before_messages=before_messages,
                        turn_id=turn_id,
                        started=started,
                        chunker=chunker,
                        speech_filter=speech_filter,
                        event_text=full_text,
                        stream_source=stream_source,
                    )
                    self.logger.write(
                        "opencode.stream.done",
                        turn_id=turn_id,
                        latency_ms=elapsed_ms(started),
                        last_event_ms=last_event_ms,
                    )
                    return
                if not hedge_active and now >= hedge_deadline:
                    hedge_active = True
                    ensure_polling()
                    self.logger.write("opencode.stream.poll_hedge.start", turn_id=turn_id, after_ms=3000)

                source: str
                turn_deadline = started + self.config.max_turn_sec
                deadline = turn_deadline if hedge_active else min(turn_deadline, hedge_deadline)
                if first_text_ms is None:
                    deadline = min(deadline, started + self.config.first_text_timeout_sec)
                if first_text_ms is None and not no_first_text_logged:
                    deadline = min(deadline, started + 10.0)
                if completion_grace_deadline is not None:
                    deadline = min(deadline, completion_grace_deadline)
                timeout = max(0.01, deadline - now)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue
                event_type = str(event.get("type") or "")
                if event_type == "_poll_error":
                    continue
                if event_type == "_poll_messages":
                    messages = event.get("messages")
                    update = tracker.update_messages(messages if isinstance(messages, list) else [])
                    source = "poll"
                else:
                    if event_type == "_stream_error":
                        # This is an actual connection/read/parser failure, not
                        # model silence. Keep the turn alive through an
                        # independent polling producer.
                        event_healthy = False
                        hedge_active = True
                        ensure_polling()
                        self.logger.write(
                            "opencode.stream.error",
                            turn_id=turn_id,
                            reason=str(event.get("reason") or "event_stream_error"),
                            prompt_sent=prompt_sent,
                        )
                        continue
                    last_event_ms = elapsed_ms(started)
                    update = tracker.update_event(event)
                    source = "event"
                if not self.turn_is_active(turn_id):
                    return
                if tracker.stale_idles and not stale_idle_logged:
                    stale_idle_logged = True
                    self.logger.write("opencode.stream.stale_idle", turn_id=turn_id)
                completed = await consume_update(update, source)
                if completed:
                    if not self.turn_is_active(turn_id):
                        return
                    stream_source = (
                        "hybrid" if text_sources == {"event", "poll"} else next(iter(text_sources), source)
                    )
                    await self.complete_event_text_turn(
                        session_id=session_id,
                        before_messages=before_messages,
                        turn_id=turn_id,
                        started=started,
                        chunker=chunker,
                        speech_filter=speech_filter,
                        event_text=full_text,
                        stream_source=stream_source,
                    )
                    self.logger.write(
                        "opencode.stream.done",
                        turn_id=turn_id,
                        latency_ms=elapsed_ms(started),
                        last_event_ms=last_event_ms,
                    )
                    return

            if self.turn_is_active(turn_id):
                await self.send_json({"type": "turn.timeout", "turn_id": turn_id})
                self.logger.write("turn.timeout", turn_id=turn_id, latency_ms=elapsed_ms(started), stream_source="event")
                self.logger.state_transition("thinking", "voice_bridge_issue", turn_id=turn_id)
                self.active_turn_id = None
        finally:
            if "poll_task" in locals() and poll_task is not None:
                poll_task.cancel()
                await asyncio.gather(poll_task, return_exceptions=True)
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

    async def read_opencode_events(
        self,
        session_id: str,
        queue: asyncio.Queue[dict[str, Any]],
        ready: asyncio.Event,
    ) -> None:
        try:
            async for event in self.client.events(on_open=ready.set, directory=self.fork_directory):
                if event_session_id(event) == session_id:
                    await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - event stream is an optimization; caller falls back.
            if not ready.is_set():
                ready.set()
            await queue.put({"type": "_stream_error", "reason": repr(exc)})

    async def poll_opencode_messages(
        self,
        *,
        session_id: str,
        queue: asyncio.Queue[dict[str, Any]],
        turn_id: int,
        interval_sec: float,
        request_timeout_sec: float = 1.0,
    ) -> None:
        """Produce bounded polling observations without blocking SSE consumption."""

        attempt = 0
        try:
            while self.turn_is_active(turn_id):
                attempt += 1
                started = time.perf_counter()
                try:
                    messages = await asyncio.wait_for(
                        self.client.messages(session_id),
                        timeout=request_timeout_sec,
                    )
                except asyncio.TimeoutError:
                    self.logger.write(
                        "opencode.stream.poll_hedge.timeout",
                        turn_id=turn_id,
                        attempt=attempt,
                        timeout_ms=int(request_timeout_sec * 1000),
                    )
                    await queue.put({"type": "_poll_error", "reason": "timeout"})
                except Exception as exc:  # noqa: BLE001 - SSE remains authoritative.
                    self.logger.write(
                        "opencode.stream.poll_hedge.error",
                        turn_id=turn_id,
                        attempt=attempt,
                        error=repr(exc),
                    )
                    await queue.put({"type": "_poll_error", "reason": type(exc).__name__})
                else:
                    if attempt == 1 or attempt % 10 == 0:
                        self.logger.write(
                            "opencode.stream.poll_hedge.observation",
                            turn_id=turn_id,
                            attempt=attempt,
                            latency_ms=elapsed_ms(started),
                            message_count=len(messages),
                        )
                    await queue.put({"type": "_poll_messages", "messages": messages})
                await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise

    async def complete_event_text_turn(
        self,
        session_id: str,
        before_messages: list[dict[str, Any]],
        turn_id: int,
        started: float,
        chunker: TTSChunker,
        speech_filter: SpeechTextFilter,
        event_text: str,
        stream_source: str = "event",
    ) -> None:
        # Fetch the canonical message before closing TTS. OpenCode can expose
        # session completion before its last part event reaches our SSE queue;
        # any prefix-compatible suffix must pass through the ordinary delta
        # and speech pipeline before provider EOF.
        final_error: Any | None = None
        try:
            messages = await asyncio.wait_for(
                self.client.messages(session_id),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            self.logger.write(
                "opencode.stream.final_fetch.timeout",
                turn_id=turn_id,
                timeout_ms=1_000,
            )
            final_text = event_text
        except Exception as exc:  # noqa: BLE001 - streamed text is already authoritative enough to finish.
            self.logger.write(
                "opencode.stream.final_fetch.error",
                turn_id=turn_id,
                error=repr(exc),
            )
            final_text = event_text
        else:
            if not self.turn_is_active(turn_id):
                return
            final_tracker = AssistantTextTracker(before_messages)
            final_update = final_tracker.update(messages)
            final_text = final_update.full_text or event_text
            final_error = final_update.error
        if final_text.startswith(event_text):
            final_suffix = final_text[len(event_text) :]
            if final_suffix:
                await self.send_json(
                    {"type": "assistant.delta", "turn_id": turn_id, "delta": final_suffix}
                )
                for chunk in chunker.push(speech_filter.push(final_suffix)):
                    await self.speak(chunk, turn_id=turn_id)
                    if not self.turn_is_active(turn_id):
                        return
                if stream_source == "event":
                    stream_source = "hybrid"
        elif final_text != event_text:
            # Protocol v0 has append-only deltas, so a rewrite cannot retract
            # already rendered text. Keep the canonical complete payload and
            # avoid speaking a corrupt partial replacement.
            self.logger.write(
                "opencode.stream.final_rewrite",
                turn_id=turn_id,
                streamed_chars=len(event_text),
                final_chars=len(final_text),
            )
        for chunk in chunker.push(speech_filter.flush()) + chunker.flush():
            await self.speak(chunk, turn_id=turn_id)
            if not self.turn_is_active(turn_id):
                return
        await self.finish_speaking_turn(turn_id)
        if not self.turn_is_active(turn_id):
            return
        self.log_if_silent_completion(turn_id, final_text)
        if final_error:
            await self.send_json(
                {
                    "type": "turn.error",
                    "turn_id": turn_id,
                    "message": str(final_error)[:1000],
                    "failure": classify_turn_failure(final_error),
                }
            )
            self.logger.write("turn.error", turn_id=turn_id, error=final_error)
        await self.send_json(
            {
                "type": "turn.complete",
                "turn_id": turn_id,
                "latency_ms": elapsed_ms(started),
                "text": final_text,
                "stream_source": stream_source,
            }
        )
        self.logger.write(
            "turn.complete",
            turn_id=turn_id,
            latency_ms=elapsed_ms(started),
            response_chars=len(final_text),
            stream_source=stream_source,
        )
        self.logger.state_transition(
            "thinking",
            "awaiting_playback" if self.turn_spoken_any else "ready",
            turn_id=turn_id,
        )
        self.active_turn_id = None
        await self.maybe_start_compaction(reason="turn_complete", run_in_background=True)

    async def prompt_with_overflow_retry(
        self,
        session_id: str,
        text: str,
        tracker: AssistantTextTracker,
        turn_id: int,
    ) -> AssistantTextTracker:
        overflow_compacted = False
        while True:
            try:
                await self.client.prompt_text(session_id, text, self.config.model, agent=self.config.opencode_agent)
                return tracker
            except Exception as exc:  # noqa: BLE001 - only retry known context overflow failures.
                if overflow_compacted or not is_context_overflow_error(exc):
                    raise
                overflow_compacted = True
                messages = await self.read_messages(session_id)
                update = tracker.update(messages)
                before_tokens = max(active_context_estimate(messages).tokens, self.config.context_threshold_tokens)
                self.logger.write(
                    "turn.context_overflow",
                    turn_id=turn_id,
                    error=repr(exc),
                    before_tokens=before_tokens,
                    response_chars=len(update.full_text),
                )
                await self.send_json(
                    {
                        "type": "turn.context_overflow",
                        "turn_id": turn_id,
                        "before_tokens": before_tokens,
                    }
                )
                outcome = await self.maybe_start_compaction(
                    reason="context_overflow_error",
                    run_in_background=False,
                    force=True,
                )
                if outcome is None or not outcome.completed:
                    raise exc
                tracker = AssistantTextTracker(await self.read_messages(session_id))

    def build_speaker(self) -> TTSProvider:
        async def deliver(token: PlaybackToken, data: bytes) -> None:
            if (
                token.generation != self.speak_generation
                or self.turn_playback_tokens.get(token.turn_id) != token
            ):
                self.stale_tts_chunks += 1
                if self.stale_tts_chunks == 1 or self.stale_tts_chunks % 50 == 0:
                    self.logger.write(
                        "tts.stale_audio.drop",
                        chunks=self.stale_tts_chunks,
                        turn_id=token.turn_id,
                        playback_generation=token.generation,
                    )
                return
            await self.send_tts_audio(data, token)

        if self.config.tts_provider == "cartesia":
            return CartesiaTTSProvider(
                CartesiaTTSOptions(
                    api_key=os.environ.get("CARTESIA_API_KEY", ""),
                    voice_id=self.config.cartesia_voice_id,
                    model=self.config.cartesia_tts_model,
                    version=self.config.cartesia_version,
                    sample_rate=self.config.tts_sample_rate,
                ),
                deliver,
                self.handle_tts_provider_event,
            )
        return DeepgramTTSProvider(
            DeepgramTTSOptions(
                api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
                model=self.config.deepgram_tts_model,
                sample_rate=self.config.tts_sample_rate,
            ),
            deliver,
            self.handle_tts_provider_event,
        )

    async def handle_tts_provider_event(self, event: dict[str, Any]) -> None:
        # Provider detail belongs in local diagnostics, never normal UI. Keep
        # only categorical lifecycle/error fields; raw provider text remains
        # excluded while terminal ownership is preserved end-to-end.
        event_type = str(event.get("type") or "tts.provider.event")
        token = self.tts_token_from_provider_event(event)
        error_code = str(event.get("error_code") or "") or (
            "provider_transport_failure"
            if event_type in {"tts.turn.failed", "tts.transport.disconnected"}
            else None
        )
        self.logger.write(
            "tts.provider.event",
            provider_event=event_type,
            provider=str(event.get("provider") or self.config.tts_provider),
            stage=event_type.rsplit(".", 1)[-1],
            error_code=error_code,
            provider_context_id=event.get("context_id"),
            provider_request_id=event.get("request_id"),
            provider_connection_epoch=event.get("epoch"),
            provider_message_type=event.get("message_type"),
            close_code=event.get("close_code"),
            turn_id=token.turn_id if token else event.get("turn_id"),
            playback_generation=token.generation if token else None,
            text_chars=event.get("chars"),
        )

        if token is None:
            return
        if event_type == "tts.turn.begin":
            self.tts_terminal_tokens.pop(token, None)
            self.tts_last_audio_at.pop(token, None)
            if self.native_audio_engine:
                self.native_audio_engine.begin_turn(token)
            elif self.native_speaker:
                begin_turn = getattr(self.native_speaker, "begin_turn", None)
                if begin_turn:
                    begin_turn(token)
            await self.on_tts_turn_began(token)
        elif event_type == "tts.turn.finish":
            self.arm_tts_terminal_watchdog(token)
        elif event_type in {"tts.turn.done", "tts.turn.failed"}:
            outcome = "done" if event_type == "tts.turn.done" else "failed"
            await self.on_tts_turn_terminal(token, outcome, error_code=error_code)

    @staticmethod
    def tts_token_from_provider_event(event: dict[str, Any]) -> PlaybackToken | None:
        turn_id = event.get("turn_id")
        generation = event.get("playback_generation")
        if generation is None:
            generation = event.get("generation", event.get("token"))
        if isinstance(turn_id, bool) or not isinstance(turn_id, int):
            return None
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        return PlaybackToken(generation, turn_id)

    async def on_tts_turn_began(self, token: PlaybackToken) -> None:
        """Subclass hook for protocol-specific per-turn lifecycle state."""

        return None

    def arm_tts_terminal_watchdog(self, token: PlaybackToken) -> None:
        previous = self.tts_terminal_watchdogs.pop(token, None)
        if previous and not previous.done():
            previous.cancel()
        task = asyncio.create_task(self.watch_tts_terminal(token))
        self.tts_terminal_watchdogs[token] = task

        def discard(done: asyncio.Task[None]) -> None:
            if self.tts_terminal_watchdogs.get(token) is done:
                self.tts_terminal_watchdogs.pop(token, None)

        task.add_done_callback(discard)

    async def watch_tts_terminal(self, token: PlaybackToken) -> None:
        """Fail a context that stops producing PCM but never reports done."""

        started_at = time.monotonic()
        try:
            while not self.closed and token not in self.tts_terminal_tokens:
                last_audio_at = self.tts_last_audio_at.get(token)
                deadline = (last_audio_at + 2.0) if last_audio_at is not None else (started_at + 5.0)
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(min(0.25, remaining))
                    continue
                await self.handle_tts_provider_event(
                    {
                        "type": "tts.turn.failed",
                        "provider": self.config.tts_provider,
                        "turn_id": token.turn_id,
                        "playback_generation": token.generation,
                        "error_code": "provider_done_timeout",
                    }
                )
                return
        except asyncio.CancelledError:
            raise

    async def on_tts_turn_terminal(
        self,
        token: PlaybackToken,
        outcome: str,
        *,
        error_code: str | None = None,
    ) -> None:
        if token in self.tts_terminal_tokens:
            return
        self.tts_terminal_tokens[token] = outcome
        failure_was_current = outcome == "failed" and token.generation == self.speak_generation
        watchdog = self.tts_terminal_watchdogs.pop(token, None)
        current = asyncio.current_task()
        if watchdog and watchdog is not current and not watchdog.done():
            watchdog.cancel()
        if failure_was_current:
            # A failed provider turn must stop its already-buffered spoken
            # remainder while preserving the completed screen text.
            self.speak_generation += 1
            if self.native_audio_engine:
                self.native_audio_engine.invalidate_generation(self.speak_generation)
            if self.native_speaker:
                invalidate = getattr(self.native_speaker, "invalidate_generation", None)
                if invalidate:
                    invalidate(self.speak_generation, "provider_failed")
                else:
                    self.native_speaker.flush(reason="provider_failed")
        elif self.native_audio_engine:
            await self.native_audio_engine.finish_turn(token, outcome)
        elif self.native_speaker:
            finish_turn = getattr(self.native_speaker, "finish_turn", None)
            if finish_turn:
                await finish_turn(token, outcome)
        if self.tts_turn_token == token:
            self.tts_turn_token = None

        if outcome != "failed":
            return
        if not failure_was_current:
            # A delayed failure marker from a generation already interrupted
            # must not close a newly reconnected provider or alarm the user.
            self.logger.write(
                "tts.turn.failure.stale",
                turn_id=token.turn_id,
                playback_generation=token.generation,
                active_playback_generation=self.speak_generation,
            )
            return
        self.tts_failed_turns.add(token.turn_id)
        if token not in self.tts_failure_reported:
            self.tts_failure_reported.add(token)
            await self.send_json(
                voice_bridge_issue_payload(
                    capability="voice_audio",
                    diagnostic_code="tts_provider_unavailable",
                    safe_detail="Voice playback interrupted",
                    retryable=True,
                    debug_ref=str(self.logger.run_dir),
                    voice_lane_id=self.voice_lane_id,
                )
            )
        failed_speaker = self.speaker
        if failed_speaker is not None:
            self.speaker = None
            self.spawn_background(self.recover_speaker_after_failure(failed_speaker))

    async def recover_speaker_after_failure(self, failed_speaker: TTSProvider) -> None:
        await self.close_speaker_quietly(failed_speaker)
        if not self.closed and self.speaker is None:
            await self.prewarm_speaker()

    async def prewarm_speaker(self) -> None:
        """Open the TTS socket while the model is thinking so the handshake
        is off the first-audio path."""
        if self.config.credential_issue_for("voice_audio"):
            return
        if self.speaker is not None:
            if bool(getattr(self.speaker, "connected", True)):
                return
            try:
                await self.speaker.connect()
            except Exception as exc:  # noqa: BLE001 - speak() retries; prewarm is best-effort.
                self.logger.write(
                    "tts.prewarm.error",
                    stage="reconnect",
                    error_code=type(exc).__name__,
                )
            return
        speaker = self.build_speaker()
        self.speaker = speaker
        try:
            await speaker.connect()
        except Exception as exc:  # noqa: BLE001 - speak() retries; prewarm is best-effort.
            self.logger.write(
                "tts.prewarm.error",
                stage="connect",
                error_code=type(exc).__name__,
            )
            if self.speaker is speaker:
                self.speaker = None

    async def speak(self, text: str, turn_id: int) -> None:
        issue = self.config.credential_issue_for("voice_audio")
        if issue:
            await self.send_json(issue.to_voice_bridge_issue(debug_ref=str(self.logger.run_dir)))
            return
        if self.active_turn_id is not None and self.active_turn_id != turn_id:
            return
        if turn_id in self.tts_failed_turns:
            return
        if self.speaker is None:
            self.speaker = self.build_speaker()
        speaker = self.speaker
        token = self.turn_playback_tokens.setdefault(turn_id, PlaybackToken(self.speak_generation, turn_id))
        if token.generation != self.speak_generation:
            return
        try:
            await speaker.connect()
            if (
                self.speaker is not speaker
                or (self.active_turn_id is not None and self.active_turn_id != turn_id)
                or token.generation != self.speak_generation
            ):
                return
            if self.tts_turn_token != token:
                await speaker.begin_turn(token)
                self.tts_turn_token = token
            if (
                (self.active_turn_id is not None and self.active_turn_id != turn_id)
                or token.generation != self.speak_generation
            ):
                return
            await speaker.append_text(token, text)
            self.turn_spoken_any = True
        except StalePlaybackToken:
            return
        except (TTSProviderError, OSError, asyncio.TimeoutError) as exc:
            self.logger.write(
                "tts.turn.error",
                turn_id=turn_id,
                stage="append_text",
                error_code=type(exc).__name__,
            )
            await self.handle_tts_provider_event(
                {
                    "type": "tts.turn.failed",
                    "provider": self.config.tts_provider,
                    "turn_id": token.turn_id,
                    "playback_generation": token.generation,
                    "error_code": "append_failed",
                }
            )

    async def finish_speaking_turn(self, turn_id: int) -> None:
        token = self.tts_turn_token
        speaker = self.speaker
        if not speaker or not token or token.turn_id != turn_id or turn_id in self.tts_failed_turns:
            return
        try:
            await speaker.finish_turn(token)
        except StalePlaybackToken:
            return
        except (TTSProviderError, OSError, asyncio.TimeoutError) as exc:
            self.logger.write(
                "tts.turn.error",
                turn_id=turn_id,
                stage="finish_turn",
                error_code=type(exc).__name__,
            )
            await self.handle_tts_provider_event(
                {
                    "type": "tts.turn.failed",
                    "provider": self.config.tts_provider,
                    "turn_id": token.turn_id,
                    "playback_generation": token.generation,
                    "error_code": "finish_failed",
                }
            )

    def log_if_silent_completion(self, turn_id: int, response_text: str) -> None:
        """A completed turn with real reply text but zero speak() calls means
        the speech filter stripped everything (e.g. an all-code, no-prose
        reply) — the turn finishes normally and silently, which looks
        identical to a hang from the user's side. Diagnostic only; no
        fallback utterance is sent."""
        if response_text.strip() and not self.turn_spoken_any:
            self.logger.write("tts.no_speakable_text", turn_id=turn_id, response_chars=len(response_text))

    async def interrupt_playback(self, reason: str) -> bool:
        """Stop the active turn and any speakers; the single owner of the
        speak-generation bump that keeps stale TTS audio off the wire.
        Returns True when something was actually cut off."""
        was_audible = self.playback_is_audible(tail_sec=0.0)
        cancelled_token = self.tts_turn_token
        running_turn = self.turn_task
        interrupted = bool(
            self.active_turn_id is not None
            or was_audible
            or self.playback_has_pending_audio()
            or cancelled_token is not None
            or (running_turn and not running_turn.done())
        )
        if not interrupted:
            return False
        if running_turn and running_turn is not asyncio.current_task() and not running_turn.done():
            running_turn.cancel()
        self.speak_generation += 1
        if self.native_audio_engine:
            self.native_audio_engine.invalidate_generation(self.speak_generation)
        if self.native_speaker:
            # Invalidate and clear local PCM synchronously before provider
            # cancellation or OpenCode abort can block.
            invalidate = getattr(self.native_speaker, "invalidate_generation", None)
            if invalidate:
                invalidate(self.speak_generation, reason)
            else:  # compatibility for injected device fakes
                self.native_speaker.flush(reason=reason)
        interrupted_turn_id = self.active_turn_id
        if interrupted_turn_id is not None:
            self.logger.write("turn.abort", turn_id=interrupted_turn_id, reason=reason)
            self.cancel_tool_hold(interrupted_turn_id)
        self.active_turn_id = None
        if self.speaker and cancelled_token:
            # The generation fence is already complete. Provider cancellation
            # is tracked independently so a Clear timeout/reconnect can never
            # delay OpenCode abort or the protocol interruption acknowledgement.
            self.tts_terminal_tokens.setdefault(cancelled_token, "cancelled")
            watchdog = self.tts_terminal_watchdogs.pop(cancelled_token, None)
            if watchdog and watchdog is not asyncio.current_task() and not watchdog.done():
                watchdog.cancel()
            self.spawn_background(self.cancel_speaker_turn(self.speaker, cancelled_token, reason))
        self.tts_turn_token = None
        # The native device stream stays alive across interruption; generation
        # invalidation above already flushed it without resetting AEC.
        self.native_speaker_unavailable = False
        return True

    async def cancel_speaker_turn(self, speaker: TTSProvider, token: PlaybackToken, reason: str) -> None:
        try:
            await speaker.cancel_turn(token, reason)
        except Exception as exc:  # noqa: BLE001 - local fencing remains authoritative.
            self.logger.write(
                "tts.cancel.error",
                stage="provider_cancel",
                error_code=type(exc).__name__,
                turn_id=token.turn_id,
                playback_generation=token.generation,
            )
            if self.speaker is speaker:
                self.speaker = None
            await self.close_speaker_quietly(speaker)

    def spawn_background(self, coro: Awaitable[None]) -> None:
        # Retain a strong reference until the task finishes; a bare
        # create_task can be garbage-collected mid-run (CPython footgun).
        task = asyncio.ensure_future(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def close_speaker_quietly(self, speaker: TTSProvider) -> None:
        try:
            await speaker.close()
        except Exception as exc:  # noqa: BLE001 - background close is best-effort.
            self.logger.write("tts.close.error", stage="close", error_code=type(exc).__name__)

    async def abort_fork_turn(self) -> None:
        if self.fork_session_id:
            try:
                await self.client.abort(self.fork_session_id)
            except Exception as exc:  # noqa: BLE001 - abort is best-effort during barge-in.
                self.logger.write("turn.abort.error", session_id=self.fork_session_id, error=repr(exc))

    async def maybe_wait_for_compaction(self, turn_id: int) -> bool:
        task = self.compaction_task
        if not task:
            return True
        if task.done():
            try:
                outcome = task.result()
            except Exception:  # noqa: BLE001 - coordinator failures fail closed here.
                outcome = CompactionOutcome(
                    session_id=self.fork_session_id or "",
                    before_tokens=0,
                    after_tokens=None,
                    summary_message_id=None,
                    completed=False,
                )
            if outcome is not None and not outcome.completed:
                await self.send_json({"type": "compaction.try_again", "turn_id": turn_id})
                return False
            return True
        if not self.compaction_running:
            return True
        await self.send_json({"type": "compaction.wait", "turn_id": turn_id})
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(task), timeout=self.config.compaction_wait_sec
            )
            await self.send_json(
                {
                    "type": "compaction.continuing" if outcome is None or outcome.completed else "compaction.try_again",
                    "turn_id": turn_id,
                }
            )
            return outcome is None or outcome.completed
        except asyncio.TimeoutError:
            self.logger.write("compaction.wait.timeout", turn_id=turn_id)
            await self.send_json({"type": "compaction.wait.timeout", "turn_id": turn_id})
            return False

    def reset_compaction_guard(self) -> None:
        self.compaction_reasons.clear()
        self.compaction_force_requested = False
        self.compaction_running = False
        self.compaction_decision_event.set()
        self.compaction_after_tokens = None
        self.compaction_summary_message_id = None

    async def maybe_start_compaction(
        self,
        reason: str,
        run_in_background: bool,
        *,
        force: bool = False,
    ) -> CompactionOutcome | None:
        if not self.fork_session_id:
            return None
        joined_existing = False
        async with self.compaction_lock:
            self.compaction_reasons.add(reason)
            self.compaction_force_requested = self.compaction_force_requested or force
            task = self.compaction_task
            if task is None or task.done():
                self.compaction_decision_event.clear()
                task = asyncio.create_task(self.coordinate_compaction(), name="mortic-compaction")
                self.compaction_task = task
            else:
                joined_existing = True
                self.logger.write(
                    "compaction.coalesced",
                    session_id=self.fork_session_id,
                    reason=reason,
                    force=force,
                )
        if run_in_background:
            await self.compaction_decision_event.wait()
            if task.done():
                return task.result()
            return None
        outcome = await asyncio.shield(task)
        if force and joined_existing and outcome is None and self.fork_session_id:
            # A forced overflow request can arrive after an ordinary context
            # check took its decision snapshot. Start one fresh coordinator
            # rather than silently losing the force request.
            async with self.compaction_lock:
                stale = self.compaction_task
                if stale is task:
                    self.compaction_task = None
            return await self.maybe_start_compaction(
                reason=reason,
                run_in_background=False,
                force=True,
            )
        return outcome

    async def coordinate_compaction(self) -> CompactionOutcome | None:
        task = asyncio.current_task()
        try:
            session_id = self.fork_session_id
            if not session_id:
                return None
            session, messages = await asyncio.gather(
                self.client.get_session(session_id),
                self.read_messages(session_id),
            )
        except Exception as exc:  # noqa: BLE001 - status should not break speech.
            self.logger.write("tokens.error", session_id=self.fork_session_id, error=repr(exc))
            async with self.compaction_lock:
                if self.compaction_task is task:
                    self.compaction_task = None
                    self.compaction_reasons.clear()
                    self.compaction_force_requested = False
                    self.compaction_running = False
                    self.compaction_decision_event.set()
            return None
        try:
            estimate = active_context_estimate(messages)
            usage_tokens = session_usage_tokens(session)
            async with self.compaction_lock:
                reasons = tuple(sorted(self.compaction_reasons))
                force = self.compaction_force_requested
            await self.send_json(
                {
                    "type": "tokens",
                    "session_id": session_id,
                    "context_tokens": estimate.tokens,
                    "context_source": estimate.source,
                    "usage_tokens": usage_tokens,
                    "summary_message_id": estimate.summary_message_id,
                }
            )
            growth = (
                max(0, estimate.tokens - self.compaction_after_tokens)
                if self.compaction_after_tokens is not None
                else None
            )
            growth_required = compaction_growth_required(self.config.context_threshold_tokens)
            self.logger.write(
                "tokens.check",
                session_id=session_id,
                context_tokens=estimate.tokens,
                context_source=estimate.source,
                usage_tokens=usage_tokens,
                summary_message_id=estimate.summary_message_id,
                measured_message_id=estimate.measured_message_id,
                included_messages=estimate.included_messages,
                threshold=self.config.context_threshold_tokens,
                compaction_reasons=reasons,
                force=force,
                growth_since_compaction=growth,
                growth_required=growth_required,
            )
            if not force and estimate.tokens < self.config.context_threshold_tokens:
                self.compaction_decision_event.set()
                return None
            if not force and growth is not None and growth < growth_required:
                self.logger.write(
                    "compaction.suppressed",
                    session_id=session_id,
                    reason="insufficient_growth",
                    context_tokens=estimate.tokens,
                    growth_tokens=growth,
                    growth_required=growth_required,
                    summary_message_id=self.compaction_summary_message_id,
                )
                self.compaction_decision_event.set()
                return None
            self.compaction_running = True
            self.compaction_decision_event.set()
            return await self.compact(
                reason="+".join(reasons) or "unspecified",
                before_tokens=estimate.tokens,
                previous_summary_message_id=estimate.summary_message_id,
            )
        finally:
            async with self.compaction_lock:
                if self.compaction_task is task:
                    self.compaction_task = None
                    self.compaction_reasons.clear()
                    self.compaction_force_requested = False
                    self.compaction_running = False
                    self.compaction_decision_event.set()

    async def wait_for_compaction_confirmation(
        self,
        session_id: str,
        opened: asyncio.Event,
    ) -> str:
        async for event in self.client.events(
            on_open=opened.set,
            directory=self.fork_directory,
        ):
            if event_session_id(event) != session_id:
                continue
            event_type = str(event.get("type") or "")
            if event_type == "session.compacted":
                return event_type
            if event_type == "session.error":
                raise CompactionConfirmationError("session_error")
            if event_type != "message.updated":
                continue
            info = event_properties(event).get("info")
            if not isinstance(info, dict) or info.get("summary") is not True:
                continue
            if info.get("error") or str(info.get("finish") or "").lower() == "error":
                raise CompactionConfirmationError("summary_error")
        raise CompactionConfirmationError("event_stream_closed")

    async def compact(
        self,
        reason: str,
        before_tokens: int,
        previous_summary_message_id: str | None = None,
    ) -> CompactionOutcome | None:
        if not self.fork_session_id:
            return None
        session_id = self.fork_session_id
        started = time.perf_counter()
        self.logger.write("compaction.start", session_id=session_id, reason=reason, before_tokens=before_tokens)
        await self.send_json(
            {"type": "compaction.start", "session_id": session_id, "reason": reason, "before_tokens": before_tokens}
        )
        confirmation_task: asyncio.Task[str] | None = None
        confirmation = "session.compacted"
        try:
            opened = asyncio.Event()
            confirmation_task = asyncio.create_task(
                self.wait_for_compaction_confirmation(session_id, opened),
                name=f"mortic-compaction-events-{session_id}",
            )
            try:
                await asyncio.wait_for(
                    opened.wait(),
                    timeout=max(0.1, min(2.0, self.config.compaction_wait_sec)),
                )
            except asyncio.TimeoutError:
                confirmation = "idle_fallback:event_open"
                confirmation_task.cancel()
                await asyncio.gather(confirmation_task, return_exceptions=True)
                confirmation_task = None

            raw = await self.client.summarize(session_id, self.config.model, auto=False)
            if raw is False:
                raise CompactionConfirmationError("request_rejected")
            if confirmation_task is not None:
                try:
                    confirmation = await asyncio.wait_for(
                        asyncio.shield(confirmation_task),
                        timeout=max(0.1, self.config.compaction_wait_sec),
                    )
                except asyncio.TimeoutError:
                    confirmation = "idle_fallback:event_timeout"
                    confirmation_task.cancel()
                    await asyncio.gather(confirmation_task, return_exceptions=True)
                    confirmation_task = None
            if confirmation.startswith("idle_fallback"):
                wait_for_idle = getattr(self.client, "wait_for_idle", None)
                if wait_for_idle is None:
                    raise CompactionConfirmationError("idle_fallback_unavailable")
                self.logger.write(
                    "compaction.confirmation.fallback",
                    session_id=session_id,
                    reason=confirmation,
                )
                await wait_for_idle(session_id)
            after = await self.client.get_session(session_id)
            messages = await self.read_messages(session_id)
            summary_message_id, _summary = validate_compaction_summary(
                messages,
                previous_summary_message_id,
            )
            estimate = active_context_estimate(messages)
            if estimate.summary_message_id != summary_message_id:
                raise CompactionConfirmationError("summary_not_authoritative")
            usage_tokens = session_usage_tokens(after)
            if self.fork_session_id == session_id:
                self.compaction_after_tokens = estimate.tokens
                self.compaction_summary_message_id = estimate.summary_message_id
            latency = elapsed_ms(started)
            self.logger.write(
                "compaction.complete",
                session_id=session_id,
                latency_ms=latency,
                before_tokens=before_tokens,
                after_tokens=estimate.tokens,
                context_source=estimate.source,
                usage_tokens=usage_tokens,
                summary_message_id=estimate.summary_message_id,
                measured_message_id=estimate.measured_message_id,
                raw=raw,
                confirmation=confirmation,
            )
            await self.send_json(
                {
                    "type": "compaction.complete",
                    "session_id": session_id,
                    "latency_ms": latency,
                    "before_tokens": before_tokens,
                    "after_tokens": estimate.tokens,
                    "context_source": estimate.source,
                    "usage_tokens": usage_tokens,
                    "summary_message_id": estimate.summary_message_id,
                    "confirmation": confirmation,
                }
            )
            return CompactionOutcome(
                session_id=session_id,
                before_tokens=before_tokens,
                after_tokens=estimate.tokens,
                summary_message_id=estimate.summary_message_id,
                completed=True,
            )
        except Exception as exc:  # noqa: BLE001 - tell UI and keep conversation usable.
            latency = elapsed_ms(started)
            self.logger.write(
                "compaction.error",
                session_id=session_id,
                latency_ms=latency,
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            await self.send_json({"type": "compaction.error", "session_id": session_id, "latency_ms": latency})
            return CompactionOutcome(
                session_id=session_id,
                before_tokens=before_tokens,
                after_tokens=None,
                summary_message_id=None,
                completed=False,
            )
        finally:
            if confirmation_task is not None and not confirmation_task.done():
                confirmation_task.cancel()
                await asyncio.gather(confirmation_task, return_exceptions=True)

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.closed:
            # A frozen viewer with working audio means sends are dying here;
            # leave a trace instead of vanishing (rate-limited).
            self.dropped_sends += 1
            if self.dropped_sends == 1 or self.dropped_sends % 50 == 0:
                self.logger.write(
                    "lane.send.dropped",
                    message_type=str(payload.get("type")),
                    dropped=self.dropped_sends,
                )
            return
        async with self.send_lock:
            try:
                await self.websocket.send_text(json.dumps(redact_secrets(payload), ensure_ascii=False))
            except WebSocketDisconnect:
                self.closed = True
                self.logger.write("lane.send.disconnect", message_type=str(payload.get("type")))
            except Exception as exc:  # noqa: BLE001 - a dying lane socket must not kill the voice engine.
                self.closed = True
                self.logger.write(
                    "lane.send.error",
                    message_type=str(payload.get("type")),
                    error=repr(exc),
                )


async def reap_stale_voice_forks(
    client: OpenCodeClient, logger: RunLogger, exclude_ids: set[str] | None = None
) -> int:
    exclude_ids = exclude_ids or set()
    try:
        rows = await client.list_sessions()
    except Exception as exc:  # noqa: BLE001 - stale cleanup must not block helper startup.
        logger.write("fork.reap.error", error=repr(exc), stage="list")
        return 0
    deleted = 0
    for row in rows:
        title = str(row.get("title") or session_title(row))
        session_id = str(row.get("id") or "")
        if not session_id or session_id in exclude_ids or not title.startswith(EPHEMERAL_PREFIX):
            continue
        try:
            await client.delete_session(session_id)
        except Exception as exc:  # noqa: BLE001 - best-effort stale cleanup.
            logger.write("fork.reap.error", session_id=session_id, error=repr(exc), stage="delete")
            continue
        deleted += 1
        logger.write("fork.reap.delete", session_id=session_id, title=title)
    if deleted:
        logger.write("fork.reap.complete", deleted=deleted)
    return deleted


@dataclass
class TurnSeam:
    """Per-turn bookkeeping for translating one legacy engine turn to v0."""

    lane_id: str
    started_at: float
    latency: dict[str, Any] = field(default_factory=dict)
    stream_source: str = "event"
    assistant_seq: int = 0
    # Kept (not popped) after turn.complete so a first TTS chunk arriving
    # after the text finished can still report firstAudioMs.
    completed: bool = False
    pending_complete: dict[str, Any] | None = None
    tts_expected: bool = False
    provider_terminal: bool = False
    provider_outcome: str | None = None
    playback_started: bool = False
    playback_drained: bool = False


@dataclass
class ActiveLaneRecord:
    owner_id: str
    source_session_id: str
    voice_lane_id: str
    acquired_at: float


class ActiveSidepodLaneRegistry:
    """Process-local guard for the single managed helper/workspace.

    V1 deliberately rejects a second active lane instead of trying to merge or
    take over audio state across TUI windows.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: ActiveLaneRecord | None = None

    async def acquire(self, *, owner_id: str, source_session_id: str, voice_lane_id: str) -> ActiveLaneRecord | None:
        async with self._lock:
            if self._active is not None and self._active.owner_id != owner_id:
                return self._active
            self._active = ActiveLaneRecord(
                owner_id=owner_id,
                source_session_id=source_session_id,
                voice_lane_id=voice_lane_id,
                acquired_at=time.time(),
            )
            return None

    async def release(self, owner_id: str) -> None:
        async with self._lock:
            if self._active is not None and self._active.owner_id == owner_id:
                self._active = None


class SidepodConnection(VoiceConnection):
    """Protocol v0 lane. Every outbound message funnels through the send_json
    override: legacy engine vocabulary is translated to v0, schema-validated,
    and anything off-contract is kept off the wire (fail closed)."""

    def __init__(
        self,
        config: VoiceConfig,
        client: OpenCodeClient,
        logger: RunLogger,
        websocket: WebSocket,
        client_factory: Callable[[str, float], OpenCodeClient] = OpenCodeClient,
        lane_registry: ActiveSidepodLaneRegistry | None = None,
    ) -> None:
        super().__init__(config=config, client=client, logger=logger, websocket=websocket)
        self.client_factory = client_factory
        self.connection_id = f"sidepod_{id(self)}"
        self.lane_registry = lane_registry or ActiveSidepodLaneRegistry()
        self.lane_registered = False
        self.lane_event_types = frozenset(sidepod_schema_document()["events"])
        self.mic_watchdog_task: asyncio.Task[None] | None = None
        self.lane_turn_counter = 0
        self.pending_turn_id: str | None = None
        self.transcript_seq = 0
        self.last_interim_transcript = ""
        self.speech_started_at: float | None = None
        self.pending_latency: dict[str, Any] = {}
        self.admitted_eot_started_at: float | None = None
        # One turn runs at a time; the dict form only tolerates stragglers
        # from an aborted turn arriving after the next one starts.
        self.turn_seams: dict[int, TurnSeam] = {}

    async def run(self) -> None:
        readiness_issues = helper_readiness_issues(
            transport_ready=True,
            debug_ref=str(self.logger.run_dir),
            tts_provider=self.config.tts_provider,
        )
        self.sidepod_readiness_issues = readiness_issues
        if readiness_issues:
            self.logger.state_transition(
                "sidepod.connected",
                "voice_bridge_issue",
                diagnostic_codes=[issue["diagnosticCode"] for issue in readiness_issues],
            )
            for issue in readiness_issues:
                await self.send_json(issue)
        else:
            self.logger.state_transition("sidepod.connected", "waiting_for_start")

        while True:
            message = await self.websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                await self.send_protocol_issue(
                    diagnostic_code="protocol_invalid_message",
                    safe_detail="Invalid sidepod message",
                )
                continue
            await self.handle_control(payload)

    async def handle_control(self, payload: Any) -> None:
        # Lane negotiation outranks shape validation: a start for a version we
        # do not speak must answer protocol_version_unsupported, not invalid.
        if isinstance(payload, dict) and payload.get("type") == "start":
            version = payload.get("protocolVersion")
            if isinstance(version, str) and version != SIDEPOD_PROTOCOL_VERSION:
                await self.send_protocol_issue(
                    diagnostic_code="protocol_version_unsupported",
                    safe_detail="Sidepod protocol unsupported",
                    retryable=False,
                )
                return
        check = check_sidepod_command(payload)
        if check.unknown_type:
            # Compatibility rule: unknown message types are logged and ignored.
            self.logger.write("sidepod.command.unknown", message_type=payload.get("type"))
            return
        if not check.ok:
            self.logger.write(
                "sidepod.command.invalid",
                message_type=payload.get("type") if isinstance(payload, dict) else None,
                errors=list(check.errors),
            )
            await self.send_protocol_issue(
                diagnostic_code="protocol_invalid_message",
                safe_detail="Invalid sidepod command",
            )
            return
        kind = payload["type"]
        if kind == "start":
            await self.handle_start(payload)
        elif kind == "stop":
            await self.handle_stop(payload)
        elif kind == "refresh":
            await self.handle_refresh(payload)
        elif kind == "ptt.start":
            await self.handle_ptt_start(payload)
        elif kind == "ptt.stop":
            await self.handle_ptt_stop(payload)
        elif kind == "live.set":
            await self.handle_live_set(payload)
        elif kind == "barge_in":
            await self.barge_in(reason=str(payload.get("reason") or "sidepod"))
        elif kind == "confirm.response":
            self.logger.write(
                "sidepod.confirm.response",
                prompt_id=payload.get("promptId"),
                action_id=payload.get("actionId"),
                confirmed=bool(payload.get("confirmed")),
                voice_lane_id=self.voice_lane_id,
            )

    async def handle_start(self, payload: dict[str, Any]) -> None:
        # Version and shape are already enforced by handle_control before any
        # wire message reaches here; handle_refresh builds a conforming payload.
        source_session_id = str(payload.get("sourceSessionId") or "")
        if self.sidepod_readiness_issues:
            for issue in self.sidepod_readiness_issues:
                await self.send_json(issue)
            return

        opencode_url = str(payload.get("opencodeUrl") or "").strip().rstrip("/")
        if opencode_url and opencode_url != getattr(self.client, "base_url", None):
            await self.client.close()
            self.client = self.client_factory(opencode_url, 60)
            self.message_cache.clear()
            self.logger.write("sidepod.opencode.rebind", opencode_url=opencode_url)

        if self.fork_session_id:
            await self.stop()
        self.voice_lane_id = self.voice_lane_id or f"lane_{int(time.time() * 1000)}"
        try:
            source_session = await self.client.get_session(source_session_id)
        except Exception as exc:  # noqa: BLE001 - keep sidepod transport alive.
            self.logger.write("sidepod.start.source_error", source_session_id=source_session_id, error=repr(exc))
            await self.send_protocol_issue(
                diagnostic_code="voice_lane_start_failed",
                safe_detail="Voice lane unavailable",
            )
            return
        source_title = str(source_session.get("title") or session_title(source_session))
        if source_title.startswith(EPHEMERAL_PREFIX):
            self.logger.write(
                "sidepod.start.voice_tmp_source",
                source_session_id=source_session_id,
                source_title=source_title,
            )
            await self.send_protocol_issue(
                diagnostic_code="voice_tmp_source_session",
                safe_detail="Switch to the original chat before starting Mortic voice.",
                retryable=True,
            )
            return
        blocking_lane = await self.lane_registry.acquire(
            owner_id=self.connection_id,
            source_session_id=source_session_id,
            voice_lane_id=self.voice_lane_id,
        )
        if blocking_lane is not None:
            self.logger.write(
                "sidepod.lane.busy",
                source_session_id=source_session_id,
                active_source_session_id=blocking_lane.source_session_id,
                active_voice_lane_id=blocking_lane.voice_lane_id,
            )
            await self.send_protocol_issue(
                diagnostic_code="voice_lane_already_active",
                safe_detail="Mortic voice is already active in this workspace.",
                retryable=True,
            )
            return
        self.lane_registered = True
        # Open Flux while the fork is being prepared. This removes the
        # provider handshake from the first M press without prompting for or
        # opening the microphone before the user asks.
        self.schedule_audio_prewarm()
        if not self.config.keep_fork_default:
            await reap_stale_voice_forks(self.client, self.logger, exclude_ids={source_session_id})
        try:
            fork = await self.create_voice_fork(
                source_session_id,
                keep_fork=bool(payload.get("keepFork")),
                original=source_session,
            )
        except Exception as exc:  # noqa: BLE001 - keep sidepod transport alive.
            self.lane_registered = False
            await self.lane_registry.release(self.connection_id)
            await self.stop_audio(reason="lane_start_failed")
            self.logger.write("sidepod.start.error", source_session_id=source_session_id, error=repr(exc))
            await self.send_protocol_issue(
                diagnostic_code="voice_lane_start_failed",
                safe_detail="Voice lane unavailable",
            )
            return

        self.logger.state_transition("waiting_for_start", "ready", voice_lane_id=self.voice_lane_id)
        await self.send_json(
            {
                "type": "ready",
                "sentAt": iso_utc_now(),
                "protocolVersion": SIDEPOD_PROTOCOL_VERSION,
                "voiceLaneId": self.voice_lane_id,
                "state": "ready",
                "sourceSessionId": fork["source_session_id"],
                "forkSessionId": fork["fork_session_id"],
            }
        )
        await self.maybe_start_compaction(reason="session_start", run_in_background=True)

    async def handle_refresh(self, payload: dict[str, Any]) -> None:
        source_session_id = str(payload.get("sourceSessionId") or self.source_session_id or "")
        if not source_session_id:
            await self.send_protocol_issue(
                diagnostic_code="voice_lane_not_started",
                safe_detail="Voice lane unavailable",
            )
            return
        await self.stop()
        await self.handle_start(
            {
                "type": "start",
                "protocolVersion": SIDEPOD_PROTOCOL_VERSION,
                "sourceSessionId": source_session_id,
                "keepFork": self.keep_fork,
            }
        )

    def schedule_mic_start(self) -> None:
        self.mic_desired_live = True
        if self.native_mic or self.native_audio_engine:
            return
        if self.mic_start_task and not self.mic_start_task.done():
            return
        self.mic_start_generation += 1
        generation = self.mic_start_generation
        transport_prepared = self.flux is not None
        task = asyncio.create_task(self.start_mic_generation(generation, transport_prepared))
        self.mic_start_task = task

        def clear_finished(done: asyncio.Task[None]) -> None:
            if self.mic_start_task is done:
                self.mic_start_task = None

        task.add_done_callback(clear_finished)

    async def cancel_pending_mic_start(self, reason: str) -> None:
        task = self.mic_start_task
        was_pending = self.mic_desired_live or bool(task and not task.done())
        self.mic_desired_live = False
        self.mic_start_generation += 1
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.mic_start_task is task:
            self.mic_start_task = None
        if was_pending:
            self.logger.write("native_audio.start.cancel", reason=reason)

    async def start_mic_generation(self, generation: int, transport_prepared: bool) -> None:
        started_at = time.perf_counter()
        try:
            started = await self.start_native_audio()
        except asyncio.CancelledError:
            await self.stop_audio(reason="mic_start_cancelled", keep_transport=True)
            raise
        except Exception as exc:  # noqa: BLE001 - keep the v0 lane alive while STT reconnects.
            if generation != self.mic_start_generation or not self.mic_desired_live:
                return
            self.mic_desired_live = False
            self.logger.write(
                "flux.connection.error",
                stage="initial_connect",
                error_code=type(exc).__name__,
            )
            await self.stop_audio(reason="stt_initial_connect_failed")
            await self.send_json(
                voice_bridge_issue_payload(
                    capability="voice_audio",
                    diagnostic_code="stt_transport_unhealthy",
                    safe_detail="Voice recognition reconnecting",
                    retryable=True,
                    debug_ref=str(self.logger.run_dir),
                    voice_lane_id=self.voice_lane_id,
                )
            )
            return

        if generation != self.mic_start_generation or not self.mic_desired_live:
            await self.stop_audio(reason="mic_start_superseded", keep_transport=True)
            return
        if not started:
            self.mic_desired_live = False
            return
        self.mic_capture_gated = False
        self.start_mic_watchdog()
        # Recheck after every awaited startup operation. A fast M/off must
        # never be followed by a stale listening acknowledgement.
        if generation != self.mic_start_generation or not self.mic_desired_live:
            await self.shutdown_mic_watchdog()
            await self.stop_audio(reason="mic_start_superseded", keep_transport=True)
            return
        self.logger.write(
            "native_audio.mic.ready",
            latency_ms=elapsed_ms(started_at),
            transport_prepared=transport_prepared,
            mode=self.duplex_mode(),
        )
        await self.send_json(
            {
                "type": "listening",
                "sentAt": iso_utc_now(),
                "voiceLaneId": self.voice_lane_id,
                "mode": "live",
            }
        )

    async def handle_live_set(self, payload: dict[str, Any]) -> None:
        if not self.fork_session_id or not self.voice_lane_id:
            await self.send_protocol_issue(
                diagnostic_code="voice_lane_not_started",
                safe_detail="Voice lane unavailable",
            )
            return
        if bool(payload.get("value")):
            if self.native_audio_engine and self.flux:
                self.mic_desired_live = True
                self.mic_capture_gated = False
                self.mic_start_generation += 1
                self.native_audio_engine.set_capture_enabled(True)
                self.reset_audio_input_counters()
                self.start_mic_watchdog()
                self.logger.write("native_audio.capture_gate", enabled=True, reason="live.set.true")
                await self.send_json(
                    {
                        "type": "listening",
                        "sentAt": iso_utc_now(),
                        "voiceLaneId": self.voice_lane_id,
                        "mode": "live",
                    }
                )
            else:
                self.schedule_mic_start()
        else:
            reason = str(payload.get("reason") or "live.set.false")
            self.mic_capture_gated = True
            if self.native_audio_engine:
                # Apply the privacy gate before any cancellation await.
                self.native_audio_engine.set_capture_enabled(False)
            await self.cancel_pending_mic_start(reason)
            await self.shutdown_mic_watchdog()
            if self.native_audio_engine:
                # Soft mute: the synchronized device/AEC/output clock keeps
                # running, while real capture is replaced with timed silence
                # before it can reach Flux.
                self.log_audio_input_summary(reason=reason)
                self.reset_audio_input_counters()
                active_episode = self.interruption_episode
                self.reset_capture_interruption_state()
                if active_episode is not None:
                    self.expire_interruption_episode(active_episode)
                self.logger.write("native_audio.capture_gate", enabled=False, reason=reason)
            else:
                # Explicit half-duplex fallback owns a separate mic stream,
                # so closing it cannot disturb speaker playback.
                await self.stop_audio(reason=reason, keep_transport=True)

    def start_mic_watchdog(self) -> None:
        self.cancel_mic_watchdog()
        self.mic_watchdog_task = asyncio.create_task(self.mic_watchdog())

    def cancel_mic_watchdog(self) -> asyncio.Task[None] | None:
        task = self.mic_watchdog_task
        if task and not task.done():
            task.cancel()
        self.mic_watchdog_task = None
        return task

    async def shutdown_mic_watchdog(self) -> None:
        task = self.cancel_mic_watchdog()
        if task and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    async def mic_watchdog(self) -> None:
        try:
            await asyncio.sleep(MIC_WATCHDOG_SEC)
        except asyncio.CancelledError:
            return
        if not (self.native_mic or self.native_audio_engine) or self.audio_input_chunks:
            return
        self.mic_desired_live = False
        self.logger.write("native_audio.silent", window_sec=MIC_WATCHDOG_SEC)
        await self.stop_audio(reason="mic_watchdog_silent")
        await self.send_json(
            voice_bridge_issue_payload(
                capability="voice_audio",
                diagnostic_code="mic_permission_needed",
                safe_detail="Mic permission needed",
                retryable=True,
                debug_ref=str(self.logger.run_dir),
                voice_lane_id=self.voice_lane_id,
            )
        )

    async def handle_ptt_start(self, payload: dict[str, Any]) -> None:
        if not self.fork_session_id or not self.voice_lane_id:
            await self.send_protocol_issue(
                diagnostic_code="voice_lane_not_started",
                safe_detail="Voice lane unavailable",
            )
            return
        self.active_turn_id = None
        self.protocol_turn_id = str(payload.get("turnId") or "")
        await self.send_protocol_issue(
            diagnostic_code="native_audio_capture_unavailable",
            safe_detail="Audio capture unavailable",
        )

    async def handle_ptt_stop(self, payload: dict[str, Any]) -> None:
        turn_id = str(payload.get("turnId") or "")
        if turn_id and self.protocol_turn_id == turn_id:
            await self.barge_in(reason=str(payload.get("reason") or "ptt.stop"))
            self.protocol_turn_id = ""

    async def handle_stop(self, payload: dict[str, Any]) -> None:
        reason = str(payload.get("reason") or "user.end_session")
        lane_id = self.voice_lane_id
        fork_deleted = bool(self.fork_session_id and not self.keep_fork)
        await self.stop()
        stopped: dict[str, Any] = {
            "type": "stopped",
            "sentAt": iso_utc_now(),
            "reason": reason,
            "forkDeleted": fork_deleted,
        }
        if lane_id:
            stopped["voiceLaneId"] = lane_id
        await self.send_json(stopped)

    async def stop(self) -> None:
        await self.cancel_pending_mic_start("sidepod_stop")
        await self.shutdown_mic_watchdog()
        await self.stop_audio(reason="sidepod_stop")
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        self.clear_pending_completions()
        await self.interrupt_playback("sidepod_stop")
        # interrupt_playback keeps the device stream for the next turn; the
        # session is over, so actually release it here.
        if self.native_speaker:
            await self.native_speaker.close()
            self.native_speaker = None
        if self.native_audio_engine:
            await self.native_audio_engine.close()
            self.native_audio_engine = None
        await self.delete_voice_fork()
        self.protocol_turn_id = ""
        self.pending_turn_id = None
        self.last_interim_transcript = ""
        self.lane_registered = False
        await self.lane_registry.release(self.connection_id)

    async def close(self) -> None:
        await self.cancel_pending_mic_start("sidepod_close")
        await self.shutdown_mic_watchdog()
        try:
            await super().close()
        finally:
            self.lane_registered = False
            await self.lane_registry.release(self.connection_id)

    async def send_tts_audio(self, data: bytes, turn_id: int | PlaybackToken | None) -> None:
        if self.native_speaker_unavailable:
            return
        token = turn_id if isinstance(turn_id, PlaybackToken) else PlaybackToken(self.speak_generation, int(turn_id or 0))
        self.tts_last_audio_at[token] = time.monotonic()
        if self.native_audio_engine is not None:
            if not await self.native_audio_engine.play(data, token):
                if token.generation != self.speak_generation:
                    self.stale_tts_chunks += 1
                    self.logger.write(
                        "native_tts.stale_generation.drop",
                        turn_id=token.turn_id,
                        bytes=len(data),
                        playback_generation=token.generation,
                        active_playback_generation=self.speak_generation,
                    )
                    return
                self.tts_unavailable_chunks += 1
                if self.tts_unavailable_chunks == 1 or self.tts_unavailable_chunks % 50 == 0:
                    self.logger.write(
                        "native_tts.play.unavailable",
                        turn_id=token.turn_id,
                        bytes=len(data),
                        chunks=self.tts_unavailable_chunks,
                        playback_generation=token.generation,
                    )
            return
        if self.native_speaker is None:
            speaker = NativeSpeakerSession(
                config=self.config,
                logger=self.logger,
                on_issue=self.send_json,
                on_render=self.feed_render_reference,
                on_drain=self.on_playback_drained,
                on_first_frame=self.on_first_playback_frame,
            )
            if not await speaker.start():
                self.native_speaker_unavailable = True
                return
            speaker.playback_generation = self.speak_generation
            speaker.begin_turn(token)
            self.native_speaker = speaker
        if not await self.native_speaker.play(data, token):
            if token.generation != self.speak_generation:
                self.stale_tts_chunks += 1
                self.logger.write(
                    "native_tts.stale_generation.drop",
                    turn_id=token.turn_id,
                    bytes=len(data),
                    playback_generation=token.generation,
                    active_playback_generation=self.speak_generation,
                )
                return
            self.tts_unavailable_chunks += 1
            if self.tts_unavailable_chunks == 1 or self.tts_unavailable_chunks % 50 == 0:
                self.logger.write(
                    "native_tts.play.unavailable",
                    turn_id=token.turn_id,
                    bytes=len(data),
                    chunks=self.tts_unavailable_chunks,
                )

    async def on_first_playback_frame(self, token: PlaybackToken) -> None:
        """`speaking` begins at device exposure, never provider arrival."""
        if token.generation != self.speak_generation or self.tts_first_audio_seen:
            return
        self.tts_first_audio_seen = True
        await self.send_json({"type": "tts.first_audio", "turn_id": token.turn_id})
        self.logger.write(
            "tts.first_audio",
            turn_id=token.turn_id,
            playback_generation=token.generation,
        )
        self.logger.state_transition("awaiting_playback", "speaking", turn_id=token.turn_id)

    async def on_tts_turn_began(self, token: PlaybackToken) -> None:
        await super().on_tts_turn_began(token)
        seam = self.turn_seams.get(token.turn_id)
        if seam is not None:
            seam.provider_terminal = False
            seam.provider_outcome = None
            seam.playback_drained = False

    async def on_tts_turn_terminal(
        self,
        token: PlaybackToken,
        outcome: str,
        *,
        error_code: str | None = None,
    ) -> None:
        already_terminal = token in self.tts_terminal_tokens
        await super().on_tts_turn_terminal(token, outcome, error_code=error_code)
        if already_terminal:
            return
        seam = self.turn_seams.get(token.turn_id)
        if seam is None:
            return
        seam.provider_terminal = True
        seam.provider_outcome = outcome
        if not self.playback_has_pending_audio():
            # A provider can legitimately finish without a non-silent frame
            # after speech filtering. There will be no device drain callback.
            seam.playback_drained = True
        if seam.completed and seam.pending_complete is not None and seam.playback_drained:
            await self.flush_pending_completions(
                reason="provider_failed" if outcome == "failed" else "provider_done",
                legacy_id=token.turn_id,
            )

    def feed_render_reference(self, data: bytes) -> None:
        # Called on the playback worker thread just before the device write,
        # so the timestamp approximates when this chunk starts playing.
        probe_data = self.render_to_probe_resampler.push(data) if self.render_to_probe_resampler else data
        self.render_audio_ring.append(probe_data)
        if self.echo_canceller:
            self.echo_canceller.process_render(data)

    async def on_playback_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "playback.event")
        detail = {key: value for key, value in event.items() if key != "type"}
        self.logger.write(event_type, **detail)

    async def on_playback_drained(self, token: PlaybackToken | None = None) -> None:
        legacy_id = token.turn_id if token is not None else None
        if legacy_id is None:
            pending_ids = [item_id for item_id, seam in self.turn_seams.items() if seam.pending_complete]
            if len(pending_ids) == 1:
                legacy_id = pending_ids[0]
                token = self.turn_playback_tokens.get(legacy_id)
        seam = self.turn_seams.get(legacy_id) if legacy_id is not None else None
        if seam is not None:
            seam.playback_drained = True
            # Older injected speakers predate provider lifecycle events. Real
            # providers must explicitly own EOF; keep compatibility confined
            # to fakes/adapters that don't advertise the terminal contract.
            if not bool(getattr(self.speaker, "supports_terminal_events", False)):
                seam.provider_terminal = True
                seam.provider_outcome = seam.provider_outcome or "done"
                if token is not None:
                    self.tts_terminal_tokens.setdefault(token, "done")
                    if self.tts_turn_token == token:
                        self.tts_turn_token = None
            if seam.provider_terminal:
                await self.flush_pending_completions(
                    reason="playback_drained",
                    legacy_id=legacy_id,
                )
                if seam.completed:
                    self.logger.state_transition("speaking", "ready", turn_id=legacy_id)
        # Reply finished speaking: return the lane to a resting listening
        # state so the viewer's activity indicator stops reading "speaking".
        # Only while the mic is still live and no new turn has taken over.
        if (
            self.native_mic is None and self.native_audio_engine is None
            or self.active_turn_id is not None
            or self.interruption_state.phase in {InterruptionPhase.CANDIDATE, InterruptionPhase.INTERRUPTED}
        ):
            return
        if not self.voice_lane_id:
            return
        await self.send_json(
            {
                "type": "listening",
                "sentAt": iso_utc_now(),
                "voiceLaneId": self.voice_lane_id,
                "mode": "live",
            }
        )

    async def barge_in(self, reason: str) -> None:
        turn_id = self.protocol_turn_id or None
        # `interrupted` means something was actually cut off; a speech.start
        # with no active turn or speech is just the user starting to talk.
        if not await self.interrupt_playback(reason):
            return
        for seam in self.turn_seams.values():
            if seam.lane_id != turn_id:
                continue
            interrupted_latency = {
                key: value for key, value in seam.latency.items() if isinstance(value, (int, float))
            }
            interrupted_latency["totalMs"] = elapsed_ms(seam.started_at)
            self.logger.write(
                "sidepod.turn.latency.interrupted",
                turn_id=seam.lane_id,
                stream_source=seam.stream_source,
                interruption_reason=reason,
                **interrupted_latency,
            )
            break
        self.clear_pending_completions()
        await self.abort_fork_turn()
        payload: dict[str, Any] = {"type": "interrupted", "sentAt": iso_utc_now(), "reason": reason}
        if self.voice_lane_id:
            payload["voiceLaneId"] = self.voice_lane_id
        if turn_id:
            payload["turnId"] = turn_id
        await self.send_json(payload)

    async def forward_flux_event(self, event: dict[str, Any]) -> None:
        etype = str(event.get("type") or "")
        if etype == "speech.start":
            self.speech_started_at = time.perf_counter()
            return
        if etype == "speech.transcript":
            # Flux can send interim text before StartOfTurn. Only an episode
            # already owned as an ordinary user turn may create provisional
            # sidepod text; candidates and pre-start text remain private until
            # the controller admits their final EOT.
            if self.interruption_state.phase is not InterruptionPhase.USER_TURN:
                return
            text = str(event.get("transcript") or "")
            if text.strip():
                await self.send_lane_transcript(text, final=False, confidence=event.get("confidence"))
            return
        # speech.end is deliberately NOT forwarded here: the durable
        # (final) transcript entry is emitted from on_transcript_admitted
        # only after the admission gate accepts it — rejected echo used to
        # render in the transcript as words the user never said.
        # Other raw STT events (speech.resumed, socket notices) stay off the lane.

    async def on_transcript_admitted(self, transcript: str, confidence: float | None) -> None:
        self.admitted_eot_started_at = time.perf_counter()
        await self.send_lane_transcript(transcript, final=True, confidence=confidence)

    async def send_lane_transcript(self, text: str, final: bool, confidence: Any = None) -> None:
        # Flux emits Update about four times per second even if its current
        # turn transcript hasn't changed. Repainting the same provisional text
        # wastes protocol/TUI work and made live sessions look noisy. The final
        # event is never deduplicated because it closes the transcript turn.
        if not final and text == self.last_interim_transcript:
            return
        self.last_interim_transcript = "" if final else text
        turn_id = self.ensure_pending_turn()
        if "firstTranscriptMs" not in self.pending_latency and self.speech_started_at is not None:
            self.pending_latency["firstTranscriptMs"] = elapsed_ms(self.speech_started_at)
        self.transcript_seq += 1
        payload: dict[str, Any] = {
            "type": "transcript",
            "sentAt": iso_utc_now(),
            "turnId": turn_id,
            "sequence": self.transcript_seq,
            "text": text,
            "final": final,
        }
        if isinstance(confidence, (int, float)):
            payload["confidence"] = float(confidence)
        await self.send_json(payload)

    def ensure_pending_turn(self) -> str:
        if not self.pending_turn_id:
            self.lane_turn_counter += 1
            self.pending_turn_id = f"turn_{self.lane_turn_counter:04d}"
            self.transcript_seq = 0
            self.pending_latency = {}
        return self.pending_turn_id

    async def send_json(self, payload: dict[str, Any]) -> None:
        if str(payload.get("type") or "") == "turn.start":
            # A new turn is not evidence that the previous reply finished
            # speaking.  Only release an old completion that already owns
            # both provider EOF and device drain; an interrupted/nonterminal
            # reply must never be reported as successfully complete.
            await self.flush_pending_completions(reason="new_turn", ready_only=True)
        outbound = self.translate_to_v0(payload)
        if outbound is INTERNAL_EVENT_HANDLED:
            return
        if outbound is None:
            self.logger.write("sidepod.lane.unknown", message_type=payload.get("type"))
            return
        assert isinstance(outbound, dict)
        check = check_sidepod_event(outbound)
        if not check.ok:
            self.logger.write(
                "sidepod.lane.violation",
                message_type=str(outbound.get("type")),
                errors=list(check.errors),
            )
            return
        await super().send_json(outbound)

    def translate_to_v0(self, payload: dict[str, Any]) -> dict[str, Any] | object | None:
        message_type = str(payload.get("type") or "")
        # v0-native payloads (lane handlers and base-class issue plumbing both
        # emit them) pass through; the discriminator against same-named legacy
        # messages is `sentAt`, which no legacy message may ever carry.
        if message_type in self.lane_event_types and "sentAt" in payload:
            return payload
        if message_type == "turn.start":
            return self.translate_turn_start(payload)
        if message_type in {"compaction.wait", "compaction.continuing", "compaction.try_again", "compaction.wait.timeout"}:
            phase = {
                "compaction.wait": "preparing_context",
                "compaction.continuing": "continuing",
                "compaction.try_again": "try_again",
                "compaction.wait.timeout": "try_again",
            }[message_type]
            return self.translate_thinking_phase(payload, phase)
        if message_type == "assistant.delta":
            return self.translate_assistant_delta(payload)
        if message_type == "assistant.first_text":
            seam = self.turn_seams.get(int(payload.get("turn_id") or 0))
            latency_ms = payload.get("latency_ms")
            if seam and isinstance(latency_ms, (int, float)):
                seam.latency["firstAssistantTextMs"] = latency_ms
            return INTERNAL_EVENT_HANDLED
        if message_type == "tts.first_audio":
            return self.translate_tts_first_audio(payload)
        if message_type == "turn.complete":
            return self.translate_turn_complete(payload) or INTERNAL_EVENT_HANDLED
        if message_type in ("turn.error", "turn.timeout"):
            return self.translate_turn_failure(payload)
        if message_type == "opencode.stream.fallback":
            seam = self.turn_seams.get(int(payload.get("turn_id") or 0))
            if seam:
                seam.stream_source = "poll_after_event" if payload.get("prompt_sent") else "poll"
            return INTERNAL_EVENT_HANDLED
        if message_type == "error":
            return voice_bridge_issue_payload(
                capability="sidepod_transport",
                diagnostic_code="engine_error",
                safe_detail="Voice engine error",
                debug_ref=str(self.logger.run_dir),
                voice_lane_id=self.voice_lane_id,
            )
        if message_type in INTERNAL_ONLY_ENGINE_EVENTS:
            return INTERNAL_EVENT_HANDLED
        return None

    def lane_turn_id(self, legacy_id: int) -> str:
        seam = self.turn_seams.get(legacy_id)
        return seam.lane_id if seam else f"turn_legacy_{legacy_id}"

    def translate_turn_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        legacy_id = int(payload.get("turn_id") or 0)
        lane_id = self.ensure_pending_turn()
        self.pending_turn_id = None
        self.protocol_turn_id = lane_id
        # Completed seams were only kept for late first-audio; a new turn
        # supersedes them.
        for old_id in [k for k, v in self.turn_seams.items() if v.completed]:
            old_seam = self.turn_seams[old_id]
            if old_seam.pending_complete is not None:
                self.logger.write(
                    "sidepod.turn.complete.superseded",
                    turn_id=old_seam.lane_id,
                    provider_terminal=old_seam.provider_terminal,
                    playback_drained=old_seam.playback_drained,
                )
            del self.turn_seams[old_id]
        self.turn_seams[legacy_id] = TurnSeam(
            lane_id=lane_id,
            started_at=self.admitted_eot_started_at or time.perf_counter(),
            latency=dict(self.pending_latency),
        )
        self.admitted_eot_started_at = None
        self.pending_latency = {}
        out: dict[str, Any] = {
            "type": "thinking",
            "sentAt": iso_utc_now(),
            "turnId": lane_id,
            "sourceMode": "live",
            "submittedTextChars": len(str(payload.get("text") or "")),
        }
        if self.voice_lane_id:
            out["voiceLaneId"] = self.voice_lane_id
        return out

    def translate_thinking_phase(self, payload: dict[str, Any], phase: str) -> dict[str, Any]:
        legacy_id = int(payload.get("turn_id") or 0)
        lane_id = self.lane_turn_id(legacy_id) if legacy_id in self.turn_seams else self.ensure_pending_turn()
        out: dict[str, Any] = {
            "type": "thinking",
            "sentAt": iso_utc_now(),
            "turnId": lane_id,
            "sourceMode": "live",
            "phase": phase,
        }
        if self.voice_lane_id:
            out["voiceLaneId"] = self.voice_lane_id
        return out

    def translate_assistant_delta(self, payload: dict[str, Any]) -> dict[str, Any]:
        legacy_id = int(payload.get("turn_id") or 0)
        seam = self.turn_seams.get(legacy_id)
        if seam:
            seam.assistant_seq += 1
        return {
            "type": "assistant.delta",
            "sentAt": iso_utc_now(),
            "turnId": self.lane_turn_id(legacy_id),
            "sequence": seam.assistant_seq if seam else 1,
            "delta": str(payload.get("delta") or ""),
        }

    def translate_tts_first_audio(self, payload: dict[str, Any]) -> dict[str, Any]:
        legacy_id = int(payload.get("turn_id") or 0)
        seam = self.turn_seams.get(legacy_id)
        out: dict[str, Any] = {
            "type": "speaking",
            "sentAt": iso_utc_now(),
            "turnId": self.lane_turn_id(legacy_id),
        }
        if self.voice_lane_id:
            out["voiceLaneId"] = self.voice_lane_id
        if seam:
            first_audio_ms = elapsed_ms(seam.started_at)
            seam.latency["firstAudioMs"] = first_audio_ms
            seam.playback_started = True
            seam.playback_drained = False
            if seam.pending_complete is not None:
                seam.pending_complete["latency"] = {
                    key: value for key, value in seam.latency.items() if isinstance(value, (int, float))
                }
            out["firstAudioLatencyMs"] = first_audio_ms
            if seam.completed:
                # The turn's latency record already went out at text-complete;
                # streamed turns usually reach first audio only afterwards.
                self.logger.write(
                    "sidepod.turn.latency.audio",
                    turn_id=seam.lane_id,
                    firstAudioMs=first_audio_ms,
                )
        return out

    def translate_turn_complete(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        legacy_id = int(payload.get("turn_id") or 0)
        seam = self.turn_seams.get(legacy_id)
        if seam:
            seam.completed = True
        lane_id = seam.lane_id if seam else f"turn_legacy_{legacy_id}"
        latency = {
            key: value
            for key, value in (seam.latency if seam else {}).items()
            if isinstance(value, (int, float))
        }
        total_ms = payload.get("latency_ms")
        model_complete_ms = total_ms if isinstance(total_ms, (int, float)) else None
        payload_stream_source = str(payload.get("stream_source") or "")
        if seam and payload_stream_source in {"event", "poll", "hybrid", "poll_after_event"}:
            seam.stream_source = payload_stream_source
        stream_source = seam.stream_source if seam else (payload_stream_source or "event")
        wire_stream_source = "poll_after_event" if stream_source == "hybrid" else stream_source
        self.logger.write(
            "sidepod.turn.latency.text",
            turn_id=lane_id,
            stream_source=stream_source,
            modelCompleteMs=model_complete_ms,
            **latency,
        )
        out: dict[str, Any] = {
            "type": "complete",
            "sentAt": iso_utc_now(),
            "turnId": lane_id,
            "latency": latency,
            "streamSource": wire_stream_source,
        }
        # Poll-fallback turns stream no assistant.delta events, so this is
        # the viewer's only copy of the reply text (the reducer falls back
        # to fullSpokenText when its delta buffer is empty).
        spoken_text = str(payload.get("spoken_text") or payload.get("text") or "")
        if spoken_text:
            out["fullSpokenText"] = spoken_text
        if seam:
            seam.tts_expected = bool(self.turn_spoken_any and not self.native_speaker_unavailable)
        if seam and seam.tts_expected and not (seam.provider_terminal and seam.playback_drained):
            seam.pending_complete = out
            self.logger.write("sidepod.turn.complete.pending_playback", turn_id=lane_id)
            return None
        latency["totalMs"] = elapsed_ms(seam.started_at) if seam else int(model_complete_ms or 0)
        self.logger.write("sidepod.turn.latency", turn_id=lane_id, stream_source=stream_source, **latency)
        return out

    async def flush_pending_completion_after_timeout(self, legacy_id: int, delay_sec: float = 10.0) -> None:
        await asyncio.sleep(delay_sec)
        seam = self.turn_seams.get(legacy_id)
        if not seam or seam.pending_complete is None:
            return
        # Kept as a compatibility hook for older callers. Elapsed wall time is
        # never evidence that provider output or device playout is complete.
        if seam.provider_terminal and seam.playback_drained:
            await self.flush_pending_completions(reason="terminal_watchdog", legacy_id=legacy_id)
        else:
            self.logger.write(
                "sidepod.turn.complete.still_pending",
                turn_id=seam.lane_id,
                provider_terminal=seam.provider_terminal,
                playback_drained=seam.playback_drained,
            )

    async def flush_pending_completions(
        self,
        *,
        reason: str,
        legacy_id: int | None = None,
        ready_only: bool = False,
    ) -> None:
        for item_id, seam in list(self.turn_seams.items()):
            if legacy_id is not None and item_id != legacy_id:
                continue
            if seam.pending_complete is None:
                continue
            if ready_only and not (seam.provider_terminal and seam.playback_drained):
                continue
            complete = seam.pending_complete
            seam.pending_complete = None
            latency = complete.setdefault("latency", {})
            if isinstance(latency, dict):
                latency["totalMs"] = elapsed_ms(seam.started_at)
            self.logger.write(
                "sidepod.turn.latency",
                turn_id=seam.lane_id,
                stream_source=seam.stream_source,
                completion_reason=reason,
                **(latency if isinstance(latency, dict) else {}),
            )
            self.logger.write("sidepod.turn.complete.flush", turn_id=seam.lane_id, reason=reason)
            await self.send_json(complete)

    def clear_pending_completions(self) -> None:
        for seam in self.turn_seams.values():
            seam.pending_complete = None

    def translate_turn_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        timed_out = payload.get("type") == "turn.timeout"
        if timed_out:
            # A timed-out turn gets no turn.complete, so drop its seam state here.
            self.turn_seams.pop(int(payload.get("turn_id") or 0), None)
            reason = "turn_timeout"
        else:
            reason = str(payload.get("failure") or "failed")
        diagnostic_code, safe_detail, retryable = TURN_FAILURE_DETAILS.get(
            reason, TURN_FAILURE_DETAILS["failed"]
        )
        return voice_bridge_issue_payload(
            capability="voice_turns",
            diagnostic_code=diagnostic_code,
            safe_detail=safe_detail,
            retryable=retryable,
            debug_ref=str(self.logger.run_dir),
            voice_lane_id=self.voice_lane_id,
        )

    async def send_protocol_issue(
        self,
        *,
        diagnostic_code: str,
        safe_detail: str,
        retryable: bool = True,
    ) -> None:
        await self.send_json(
            voice_bridge_issue_payload(
                capability="sidepod_transport",
                diagnostic_code=diagnostic_code,
                safe_detail=safe_detail,
                retryable=retryable,
                debug_ref=str(self.logger.run_dir),
                voice_lane_id=self.voice_lane_id,
            )
        )


class DeepgramFluxSession:
    def __init__(self, config: VoiceConfig, on_event: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self.config = config
        self.on_event = on_event
        self.transport: FluxTransport | None = None
        self.connection_epoch = 0

    async def start(self) -> None:
        options = FluxTransportOptions(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            model=self.config.deepgram_stt_model,
            sample_rate=self.config.deepgram_sample_rate,
            eot_threshold=self.config.flux_eot_threshold,
            eot_timeout_ms=self.config.flux_eot_timeout_ms,
            eager_eot_threshold=self.config.flux_eager_eot_threshold,
        )
        self.transport = FluxTransport(options, self._handle_transport_event)
        await self.transport.start()
        try:
            self.connection_epoch = await self.transport.wait_connected(
                timeout_sec=options.connect_timeout_sec + 0.5
            )
        except Exception:
            await self.transport.close()
            self.transport = None
            raise

    async def close(self) -> None:
        if self.transport:
            await self.transport.close()
            self.transport = None

    def submit_audio(self, data: bytes) -> bool:
        return bool(self.transport and self.transport.submit(data))

    def health_snapshot(self) -> Any:
        return self.transport.health_snapshot() if self.transport else None

    async def _handle_transport_event(self, event: dict[str, Any]) -> None:
        epoch = event.get("transport_epoch") or event.get("epoch")
        if isinstance(epoch, int):
            self.connection_epoch = epoch
            event["flux_connection_epoch"] = epoch
        await self.on_event(event)
