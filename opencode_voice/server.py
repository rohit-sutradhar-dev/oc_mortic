from __future__ import annotations

import asyncio
import base64
import difflib
import importlib.util
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from opencode_voice.cartesia import build_tts_url as build_cartesia_tts_url
from opencode_voice.echo_probe import PcmRingBuffer, echo_correlation
from opencode_voice.config import (
    VoiceConfig,
    iso_utc_now,
    load_voice_credentials,
    redact_secrets,
    voice_bridge_issue_payload,
)
from opencode_voice.deepgram import (
    FlushLimiter,
    SpeechTextFilter,
    TTSChunker,
    build_flux_url,
    build_tts_url,
    parse_flux_message,
)
from opencode_voice.logging import RunLogger
from opencode_voice.opencode_client import OpenCodeClient
from opencode_voice.protocol import PROTOCOL_VERSION as SIDEPOD_PROTOCOL_VERSION
from opencode_voice.protocol import check_command as check_sidepod_command
from opencode_voice.protocol import check_event as check_sidepod_event
from opencode_voice.protocol import schema_document as sidepod_schema_document
from opencode_voice.audio_processing import EchoCanceller
from opencode_voice.state import (
    AssistantTextTracker,
    OpenCodeEventTurnTracker,
    active_context_estimate,
    elapsed_ms,
    event_session_id,
    session_context_tokens,
    session_title,
    session_usage_tokens,
)

STATIC_DIR = Path(__file__).with_name("static")
EPHEMERAL_PREFIX = "[voice tmp]"
AUDIO_DEPENDENCY_MODULE = "sounddevice"
# Live capture that produces zero frames within this window is treated as a
# silently denied mic (macOS TCC denies without any error on some terminals).
MIC_WATCHDOG_SEC = 4.0
CONTEXT_OVERFLOW_MARKERS = (
    "maximum context length",
    "context length",
    "context window",
    "reduce the length of the messages",
    "too many tokens",
)


def is_context_overflow_error(exc: Exception) -> bool:
    text = repr(exc).lower()
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


def transcript_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def spoken_sequence_ratio(transcript: str, spoken_text: str, tail_chars: int = 400) -> float:
    """Best character-level similarity between the transcript and any
    transcript-sized window of the recently spoken text.

    The bag-of-words overlap check misses echo that STT mangled: substitute a
    third of the words and membership drops below the echo threshold even
    though the transcript is still the assistant's sentence *in order*
    (live incident 2026-07-05: 72-char echo scored 0.64 overlap and was
    confirmed as a real interrupt). SequenceMatcher on sliding windows keeps
    the word order and survives substitutions like "directories"->"directors".
    """
    words = transcript_words(transcript)
    if not words:
        return 0.0
    target = " ".join(words)
    spoken_words = transcript_words(spoken_text[-tail_chars:])
    if not spoken_words:
        return 0.0
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(target)  # seq2 is cached; slide seq1 across it
    best = 0.0
    # Windows from 70% to 130% of the transcript length, stepped one word.
    low = max(1, int(len(words) * 0.7))
    high = min(len(spoken_words), max(1, int(len(words) * 1.3)))
    for size in range(low, high + 1):
        for start in range(0, len(spoken_words) - size + 1):
            matcher.set_seq1(" ".join(spoken_words[start : start + size]))
            if matcher.real_quick_ratio() <= best or matcher.quick_ratio() <= best:
                continue
            best = max(best, matcher.ratio())
    return best


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
        blocksize = max(1, int(self.config.deepgram_sample_rate * 0.08))

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            # PortAudio realtime thread: no blocking calls here — logging does
            # file I/O and status flags fire exactly when the device is
            # already stressed, so defer everything to the loop.
            if self.closed or not self.loop:
                return
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
                samplerate=self.config.deepgram_sample_rate,
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
            sample_rate=self.config.deepgram_sample_rate,
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
    ) -> None:
        self.config = config
        self.logger = logger
        self.on_issue = on_issue
        self.on_render = on_render
        self.on_drain = on_drain
        self.queue: asyncio.Queue[bytes] | None = None
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

        # Memory bound only; play() blocks when full so chunks are never
        # dropped mid-turn (256 chunks ≈ 10s of 16kHz mono int16 audio).
        self.queue = asyncio.Queue(maxsize=256)
        try:
            self.stream = sd.RawOutputStream(
                samplerate=self.config.deepgram_sample_rate,
                channels=1,
                dtype="int16",
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
            sample_rate=self.config.deepgram_sample_rate,
            channels=1,
            dtype="int16",
        )
        return True

    async def play(self, data: bytes, turn_id: int | None) -> bool:
        if not self.queue or self.closed:
            return False
        # Backpressure instead of drops: TTS delivers faster than realtime,
        # and put() blocking here stalls the Deepgram receive loop until the
        # device catches up. close() drains the queue so this never deadlocks.
        await self.queue.put(data)
        if self.closed:
            return False
        now = time.perf_counter()
        if not self.is_audible(tail_sec=0.0):
            # Silence -> audio transition: a new playback burst begins. Reset
            # the rate counters here — the speaker now persists across turns,
            # so a session-cumulative native_tts.summary would fold the idle
            # gaps between replies into the byte/duration rate and read as
            # choppy when playback is actually realtime. Per-burst counters
            # measure this reply's playback honestly.
            self.burst_started_at = now
            self.started_at = now
            self.last_summary_log = now
            self.played_chunks = 0
            self.played_bytes = 0
            self.logger.write("native_tts.first_chunk", bytes=len(data), turn_id=turn_id)
        chunk_sec = len(data) / (self.config.deepgram_sample_rate * 2)
        self.speaking_until = max(now, self.speaking_until) + chunk_sec
        return True

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
            if self.burst_active and not self.paused:
                # Nothing queued and the played audio should be finished:
                # the burst has drained, so signal a return to listening.
                remaining = max(0.0, self.speaking_until - time.perf_counter())
                try:
                    data = await asyncio.wait_for(self.queue.get(), timeout=remaining + 0.15)
                except asyncio.TimeoutError:
                    self.burst_active = False
                    if self.started_at is not None:
                        # Final rate for the burst that just finished playing;
                        # bytes/duration_ms == sample_rate*2 means realtime.
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
                    continue
            else:
                data = await self.queue.get()
            await self.resume_event.wait()
            if self.closed:
                break
            try:
                await asyncio.to_thread(self.write_output, data)
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
            self.played_bytes += len(data)
            self.burst_active = True
            now = time.perf_counter()
            if self.started_at is not None and now - self.last_summary_log >= 5:
                self.last_summary_log = now
                self.logger.write(
                    "native_tts.summary",
                    chunks=self.played_chunks,
                    bytes=self.played_bytes,
                    dropped_chunks=self.dropped_chunks,
                    duration_ms=elapsed_ms(self.started_at),
                )

    def write_output(self, data: bytes) -> None:
        # Runs on a worker thread, so the echo-canceller reference feed (~one
        # FFI round-trip per 10ms frame) stays off the event loop that is
        # simultaneously servicing mic frames and lane sends.
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
    app = FastAPI(title="OpenCode Mercury Voice Bridge")
    load_voice_credentials(tts_provider=config.tts_provider)
    logger = RunLogger(config.run_root)

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

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/app.js")
    async def app_js() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")

    @app.get("/styles.css")
    async def styles() -> FileResponse:
        return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")

    @app.get("/api/health")
    async def health() -> JSONResponse:
        credentials = load_voice_credentials(tts_provider=config.tts_provider)
        readiness_issues = helper_readiness_issues(
            transport_ready=True, debug_ref=str(logger.run_dir), tts_provider=config.tts_provider
        )
        client = client_factory(config.opencode_url, 10)
        try:
            opencode_health = await client.health()
        finally:
            await client.close()
        return JSONResponse(
            {
                "ok": not readiness_issues,
                "ready": not readiness_issues,
                "opencode": opencode_health,
                "opencode_url": config.opencode_url,
                "run_dir": str(logger.run_dir),
                "model": config.model.opencode_name,
                "context_threshold_tokens": config.context_threshold_tokens,
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
                },
                "cartesia": {
                    "enabled": config.has_cartesia_key,
                    "tts_model": config.cartesia_tts_model,
                    "sample_rate": config.deepgram_sample_rate,
                },
            }
        )

    @app.get("/api/sessions")
    async def sessions() -> JSONResponse:
        client = client_factory(config.opencode_url, 20)
        try:
            rows = await client.list_sessions()
        finally:
            await client.close()
        rows.sort(key=lambda item: (item.get("time") or {}).get("updated") or 0, reverse=True)
        return JSONResponse(
            {
                "sessions": [
                    {
                        "id": row.get("id"),
                        "title": session_title(row),
                        "tokens": row.get("tokens") or {},
                        "context_tokens": session_context_tokens(row),
                        "usage_tokens": session_usage_tokens(row),
                        "model": row.get("model"),
                        "time": row.get("time") or {},
                        "is_voice_tmp": str(row.get("title") or "").startswith(EPHEMERAL_PREFIX),
                    }
                    for row in rows
                ]
            }
        )

    @app.websocket("/ws/voice")
    async def voice_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        client = client_factory(config.opencode_url, 60)
        connection = VoiceConnection(config=config, client=client, logger=logger, websocket=websocket)
        try:
            await connection.run()
        finally:
            await connection.close()
            await client.close()

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
        self.keep_fork = config.keep_fork_default
        self.closed = False
        self.compaction_task: asyncio.Task[None] | None = None
        self.turn_task: asyncio.Task[None] | None = None
        self.turn_seq = 0
        self.active_turn_id: int | None = None
        self.voice_lane_id: str | None = None
        self.protocol_turn_id = ""
        self.sidepod_readiness_issues: tuple[dict[str, Any], ...] = ()
        self.flux: DeepgramFluxSession | None = None
        self.speaker: DeepgramSpeakSession | CartesiaSpeakSession | None = None
        self.native_mic: NativeMicSession | None = None
        self.native_speaker: NativeSpeakerSession | None = None
        self.native_speaker_unavailable = False
        self.echo_canceller: EchoCanceller | None = None
        self.speak_generation = 0
        self.stale_tts_chunks = 0
        self.tts_unavailable_chunks = 0
        self.speaker_prewarm_task: asyncio.Task[None] | None = None
        self.final_transcript = ""
        self.eager_turn_text: str | None = None
        self.spoken_text_recent = ""
        self.dismissed_transcript: str | None = None
        self.dismissed_at = 0.0
        self.barge_pending = False
        self.barge_pending_since = 0.0
        self.pending_barge_task: asyncio.Task[None] | None = None
        self.pending_probe_task: asyncio.Task[None] | None = None
        self.aec_delay_error_logged = False
        self.tts_first_audio_seen = False
        self.turn_spoken_any = False
        self.audio_input_chunks = 0
        self.audio_input_bytes = 0
        self.audio_input_started: float | None = None
        self.audio_input_last_log = 0.0
        # Rolling audio windows for the echo probe: what the mic heard
        # (post-AEC, i.e. what STT hears) and what the speaker played.
        self.mic_audio_ring = PcmRingBuffer(config.deepgram_sample_rate, direction="ending")
        self.render_audio_ring = PcmRingBuffer(config.deepgram_sample_rate, direction="starting")

    async def run(self) -> None:
        readiness_issues = helper_readiness_issues(
            transport_ready=True,
            debug_ref=str(self.logger.run_dir),
            tts_provider=self.config.tts_provider,
        )
        if readiness_issues:
            self.logger.state_transition(
                "transport.connected",
                "voice_bridge_issue",
                diagnostic_codes=[issue["diagnosticCode"] for issue in readiness_issues],
            )
            for issue in readiness_issues:
                await self.send_json(issue)
        else:
            self.logger.state_transition("transport.connected", "ready")
            await self.send_json({"type": "ready", "run_dir": str(self.logger.run_dir)})
        while True:
            message = await self.websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if self.flux:
                    await self.flux.send_audio(message["bytes"])
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                await self.send_json({"type": "error", "message": "Invalid JSON control message."})
                continue
            await self.handle_control(payload)

    async def close(self) -> None:
        self.closed = True
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
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        if self.compaction_task and not self.compaction_task.done():
            self.compaction_task.cancel()
        if self.fork_session_id and not self.keep_fork:
            fork_id = self.fork_session_id
            try:
                await self.client.delete_session(fork_id)
                self.logger.write("fork.delete", session_id=fork_id)
            except Exception as exc:  # noqa: BLE001 - surfaced to UI/log for cleanup visibility.
                self.logger.write("fork.delete.error", session_id=fork_id, error=repr(exc))

    async def handle_control(self, payload: dict[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "start":
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                await self.send_json({"type": "error", "message": "Pick an OpenCode session first."})
                return
            try:
                await self.start(session_id=session_id, keep_fork=bool(payload.get("keep_fork")))
            except httpx.HTTPStatusError as exc:
                self.logger.write(
                    "fork.start.error",
                    source_session_id=session_id,
                    status_code=exc.response.status_code,
                    error=repr(exc),
                )
                await self.send_json({"type": "error", "message": "Could not fork that session. Refresh threads."})
        elif kind == "stop":
            await self.stop()
        elif kind == "text":
            text = str(payload.get("text") or "").strip()
            if text:
                await self.enqueue_text_turn(text, source="typed")
        elif kind == "audio.start":
            await self.start_audio()
        elif kind == "audio.stop":
            if self.flux:
                await self.flux.close()
                self.flux = None
        elif kind == "barge_in":
            await self.barge_in(reason="manual")
        elif kind == "keep_fork":
            self.keep_fork = bool(payload.get("value"))
            await self.send_json({"type": "fork.keep", "keep_fork": self.keep_fork})

    async def create_voice_fork(self, session_id: str, keep_fork: bool) -> dict[str, Any]:
        self.keep_fork = keep_fork
        self.source_session_id = session_id
        fork_started = time.perf_counter()
        fork = await self.client.fork_session(session_id)
        fork_id = str(fork.get("id") or "")
        if not fork_id:
            raise RuntimeError("OpenCode did not return a fork session id.")
        original = await self.client.get_session(session_id)
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
        self.voice_lane_id = self.voice_lane_id or f"lane_{int(time.time() * 1000)}"
        session = await self.client.get_session(fork_id)
        # Forks inherit the source thread's directory; /event subscriptions
        # are directory-scoped, so turns must subscribe with this value or the
        # stream stays silent and every turn pays the poll-fallback timeout.
        self.fork_directory = str(fork.get("directory") or session.get("directory") or "") or None
        messages = await self.client.messages(fork_id)
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

    async def start(self, session_id: str, keep_fork: bool) -> None:
        fork = await self.create_voice_fork(session_id, keep_fork)
        await self.send_json({"type": "fork.ready", **fork})
        await self.maybe_start_compaction(reason="session_start", run_in_background=True)

    async def stop(self) -> None:
        if self.fork_session_id and not self.keep_fork:
            fork_id = self.fork_session_id
            await self.client.delete_session(fork_id)
            self.logger.write("fork.delete", session_id=fork_id)
        self.fork_session_id = None
        self.voice_lane_id = None
        await self.send_json({"type": "stopped"})

    async def start_audio(self) -> None:
        issue = self.config.credential_issue_for("voice_audio")
        if issue:
            await self.send_json(issue.to_voice_bridge_issue(debug_ref=str(self.logger.run_dir)))
            return
        if self.flux:
            return
        self.reset_audio_input_counters()
        self.flux = DeepgramFluxSession(self.config, on_event=self.handle_flux_event)
        await self.flux.start()
        # Legacy browser clients render this; the sidepod seam drops it.
        await self.send_json({"type": "audio.ready"})

    async def start_native_audio(self) -> bool:
        await self.start_audio()
        if self.closed or self.flux is None or self.native_mic:
            return bool(self.native_mic)
        self.ensure_echo_canceller()
        native_mic = NativeMicSession(
            config=self.config,
            logger=self.logger,
            on_audio=self.handle_native_audio,
            on_issue=self.send_json,
        )
        if not await native_mic.start():
            if self.flux:
                await self.flux.close()
                self.flux = None
            return False
        self.native_mic = native_mic
        return True

    def ensure_echo_canceller(self) -> None:
        if self.config.voice_duplex != "auto" or self.echo_canceller is not None:
            return
        try:
            self.echo_canceller = EchoCanceller(self.config.deepgram_sample_rate)
            self.logger.write("audio.aec.start", sample_rate=self.config.deepgram_sample_rate)
        except Exception as exc:  # noqa: BLE001 - degrade to the half-duplex gate.
            self.echo_canceller = None
            self.logger.write("audio.aec.unavailable", error=repr(exc))

    def duplex_mode(self) -> str:
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
                if self.native_speaker and self.native_speaker.in_startup_window(self.config.playback_mute_sec):
                    # The canceller above still adapted on the real frames;
                    # STT just doesn't get to hear its convergence leak.
                    return b"\x00" * len(processed)
                return processed
            except Exception as exc:  # noqa: BLE001 - fall back to the gate for the rest of the lane.
                self.logger.write("audio.aec.error", error=repr(exc))
                self.echo_canceller = None
        if mode != "full" and self.native_speaker and self.native_speaker.is_audible():
            return b"\x00" * len(data)
        return data

    def update_aec_delay(self) -> None:
        # Called per mic chunk: the canceller applies the stored value right
        # before each process_stream frame, which is the cadence WebRTC
        # expects (a one-shot set at speaker start never landed — livekit's
        # media_devices re-asserts it per capture frame).
        if not self.echo_canceller:
            return
        delay_sec = self.native_mic.input_delay_sec if self.native_mic else 0.0
        speaker_latency = getattr(getattr(self.native_speaker, "stream", None), "latency", None)
        if isinstance(speaker_latency, (int, float)):
            delay_sec += float(speaker_latency)
        self.echo_canceller.set_stream_delay_ms(int(delay_sec * 1000))

    def mic_gate_active(self) -> bool:
        return (
            self.duplex_mode() == "half"
            and self.native_speaker is not None
            and self.native_speaker.is_audible()
        )

    async def stop_audio(self, reason: str) -> None:
        if self.native_mic:
            await self.native_mic.close()
            self.native_mic = None
        self.log_audio_input_summary(reason=reason)
        if self.flux:
            await self.flux.close()
            self.flux = None

    async def handle_native_audio(self, data: bytes) -> None:
        await self.record_audio_input(data)
        if not self.flux:
            return
        data = self.filter_mic_frame(data)
        if data:
            # The probe compares what STT hears against what we played, so
            # the ring gets the post-AEC frame, not the raw capture.
            self.mic_audio_ring.append(data)
            await self.flux.send_audio(data)

    def reset_audio_input_counters(self) -> None:
        self.audio_input_chunks = 0
        self.audio_input_bytes = 0
        self.audio_input_started = None
        self.audio_input_last_log = 0.0

    async def record_audio_input(self, data: bytes) -> None:
        now = time.perf_counter()
        if self.audio_input_started is None:
            self.audio_input_started = now
            self.audio_input_last_log = now
            self.logger.write("audio.input.first_chunk", bytes=len(data), flux_active=bool(self.flux))
            await self.send_json({"type": "audio.input", "status": "receiving"})
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

    async def forward_flux_event(self, event: dict[str, Any]) -> None:
        # The browser lane forwards raw STT events; the sidepod lane overrides
        # this seam to translate them into protocol v0 `transcript` events.
        await self.send_json(event)

    async def on_transcript_admitted(self, transcript: str, confidence: float | None) -> None:
        # Sidepod hook: only admitted transcripts may become durable
        # transcript entries in the UI (rejected echo must not be recorded
        # as the user's words). The browser lane shows raw events already.
        return None

    async def handle_flux_event(self, event: dict[str, Any]) -> None:
        await self.forward_flux_event(event)
        if event["type"] == "speech.start":
            self.logger.write("speech.start")
            if self.mic_gate_active():
                # Gated mic feeds STT silence; anything STT still reports
                # while TTS is audible can only be residue, never a user.
                self.logger.write("speech.gated", event_type="speech.start")
                return
            if self.barge_pending:
                return
            if self.native_speaker and self.native_speaker.is_audible():
                # Mid-playback speech could be the user or echo residue the
                # canceller missed. Pause instead of killing the turn and let
                # the transcript decide (resolve_pending_barge_in).
                self.begin_pending_barge_in()
                return
            await self.barge_in(reason="speech_start")
            await self.maybe_start_compaction(reason="speech_start", run_in_background=True)
        elif event["type"] == "speech.resumed":
            self.logger.write("speech.resumed")
            if self.mic_gate_active():
                self.logger.write("speech.gated", event_type="speech.resumed")
                return
            if self.barge_pending:
                # Speech is still going; give the transcript a fresh window.
                self.arm_pending_barge_in_deadline()
                return
            await self.barge_in(reason="speech_resumed")
        elif event["type"] == "speech.transcript" and event.get("is_final") and event.get("transcript"):
            self.final_transcript = str(event["transcript"]).strip()
            self.logger.write("speech.transcript.final", transcript_chars=len(self.final_transcript))
        elif event["type"] == "speech.end":
            transcript = str(event.get("transcript") or self.final_transcript).strip()
            self.final_transcript = ""
            eager = bool(event.get("eager"))
            confidence = event.get("confidence")
            confidence = float(confidence) if isinstance(confidence, (int, float)) else None
            self.logger.write("speech.end", transcript_chars=len(transcript), eager=eager, confidence=confidence)
            await self.handle_transcript(transcript, eager=eager, confidence=confidence)

    async def handle_transcript(self, transcript: str, eager: bool, confidence: float | None = None) -> None:
        """The single admission path for every voice transcript. The verdict
        both decides whether a turn starts and resolves any paused playback,
        so eager/final duplicates and echo cannot slip in through one code
        path while another filters them."""
        verdict, detail = self.transcript_verdict(transcript, eager, confidence)
        if verdict == "admit":
            if self.barge_pending:
                self.clear_pending_barge_in()
                self.logger.write("barge_in.confirmed", transcript_chars=len(transcript), **detail)
                await self.barge_in(reason="speech_confirmed")
                await self.maybe_start_compaction(reason="speech_confirmed", run_in_background=True)
            elif self.native_speaker and self.native_speaker.is_audible():
                # No pause in flight but stale audio is still playing (e.g. a
                # timeout resumed it): the new turn supersedes that playback.
                await self.interrupt_playback("new_turn")
            await self.on_transcript_admitted(transcript, confidence)
            await self.enqueue_text_turn(transcript, source="voice", eager=eager)
            return
        self.logger.write("transcript.rejected", verdict=verdict, transcript_chars=len(transcript), **detail)
        if verdict in ("tiny", "echo", "low_confidence", "cut_short"):
            # The confirming final speech.end repeats the same words moments
            # later; remember the dismissal so it cannot re-enter as a turn.
            self.dismissed_transcript = transcript
            self.dismissed_at = time.perf_counter()
        if verdict == "confirmed_eager":
            self.eager_turn_text = None
        if self.barge_pending:
            self.dismiss_pending_barge_in(verdict)

    # Below this mean word confidence, speech heard while the assistant is
    # audible is treated as mangled echo rather than the user. Clean speech
    # scores well above this; echo the canceller distorted transcribes as
    # low-confidence garbage that the word-overlap check cannot match.
    ECHO_CONFIDENCE_FLOOR = 0.5
    # Echo of the assistant's closing words is transcribed after playback has
    # already ended, so the content checks stay armed this long past the last
    # audible moment. (Length/one-word rules do not: a quick "Yes." right
    # after the assistant finishes is a legitimate reply.)
    ECHO_TAIL_SEC = 1.5
    # Speech that ENDS this soon after the pause began is a fragment our own
    # pause cut off: pausing silences the echo source mid-word, so Flux
    # end-of-turns it almost immediately (live run: echo fragments ended
    # 243-251ms after the pause; real interrupts ran 889-1273ms because a
    # human keeps talking whether or not the assistant went quiet). LiveKit
    # ships the same rule as min_interruption_duration.
    PENDING_MIN_SPEECH_SEC = 0.4
    # Character-level similarity (ordered window match) at or above this is
    # echo even when word membership fails: STT transcribes AEC-mangled echo
    # with substituted words, but in the assistant's word order (live
    # incident: overlap 0.64 slipped the 0.75 gate; its sequence ratio was
    # far higher). Calibrated by test against mangled vs novel transcripts.
    ECHO_SEQUENCE_RATIO = 0.72
    # The audio probe only breaks ties: below this text score the transcript
    # is clearly novel and no audio evidence may dismiss it (protects real
    # barge-ins, including the user talking over playback), above the echo
    # thresholds the text rules already decided.
    ECHO_PROBE_TEXT_FLOOR = 0.45
    # Peak mic-vs-render energy-envelope correlation at or above this means
    # the mic heard what the speaker played. Echo replays the render's
    # loudness contour (live fixture: shifted+attenuated copies score ~0.9);
    # independent speech stays well under 0.4.
    ECHO_CORRELATION_FLOOR = 0.6
    # The early probe resolves the PAUSE before any transcript exists, so it
    # has no text corroboration and demands a stronger match than the
    # tie-band check. Wrong early resumes self-correct (the transcript still
    # gets its verdict and a real interrupt still barges in), but each one
    # costs the user a restarted playback stutter — keep this conservative.
    ECHO_EARLY_CORRELATION_FLOOR = 0.75

    def transcript_verdict(
        self, transcript: str, eager: bool, confidence: float | None = None
    ) -> tuple[str, dict[str, Any]]:
        """Classify a transcript: "admit" starts a turn, anything else drops it.

        empty            nothing was said
        duplicate        repeats a transcript rejected moments ago (Flux sends
                         eager + final copies of the same utterance)
        confirmed_eager  the final EOT matching the in-flight eager turn —
                         the turn is already running, restarting adds latency
        tiny             below the length floor while the assistant is audible
        echo             mostly the assistant's own recent words while audible
                         (a length gate cannot tell echo from a user: live run
                         had 9-13 char echo fragments confirm 5 interrupts)
        low_confidence   Flux itself doubts the words, while audible — echo
                         that leaked distorted transcribes as novel garbage
                         and defeats the overlap check
        cut_short        speech that ended almost immediately after our pause
                         began — the pause silenced the echo source mid-word,
                         so mangled echo (novel words, high confidence, which
                         no content rule can catch) EOTs in ~250ms; a human
                         keeps talking well past PENDING_MIN_SPEECH_SEC

        The content rules stay armed for ECHO_TAIL_SEC past the last audible
        moment (echo of closing words transcribes after playback ends); the
        length-based rules (tiny, single-word echo) apply only while sound is
        actually on the air, so a quick short answer right after the
        assistant finishes is still admitted. In full silence none of them
        apply.
        """
        if not transcript:
            return "empty", {}
        if (
            self.dismissed_transcript == transcript
            and time.perf_counter() - self.dismissed_at < 3.0
        ):
            return "duplicate", {}
        if not eager and self.eager_turn_text == transcript:
            return "confirmed_eager", {"turn_id": self.active_turn_id}
        if self.barge_pending:
            pending_sec = time.perf_counter() - self.barge_pending_since
            if pending_sec < self.PENDING_MIN_SPEECH_SEC:
                return "cut_short", {"pending_ms": int(pending_sec * 1000)}
        speaker = self.native_speaker
        detail: dict[str, Any] = {}
        if speaker and speaker.is_audible(tail_sec=self.ECHO_TAIL_SEC):
            audible_now = speaker.is_audible()
            if audible_now and len(transcript) < self.config.barge_in_min_chars:
                return "tiny", {}
            words = transcript_words(transcript)
            spoken = set(transcript_words(self.spoken_text_recent))
            overlap = sum(word in spoken for word in words) / len(words) if words else 0.0
            seq_ratio = spoken_sequence_ratio(transcript, self.spoken_text_recent)
            # Kept on admits too: transcripts are redacted from logs, so the
            # overlap and sequence scores are what let a log reader judge
            # echo-likeness.
            detail["overlap"] = round(overlap, 2)
            detail["seq_ratio"] = round(seq_ratio, 2)
            # A single word the assistant just used ("Great." echoing back)
            # counts as echo only while sound is actually on the air; once
            # playback ends, a one-word "Yes." is a legitimate answer even
            # though the assistant's question contained the word.
            multiword_echo = len(words) >= 2 and overlap >= 0.75
            single_word_echo = audible_now and len(words) == 1 and overlap >= 1.0
            # STT-mangled echo defeats word membership but keeps word order:
            # the character-level window match catches it (>=3 words so short
            # legitimate replies never ride the fuzzy path).
            sequence_echo = len(words) >= 3 and seq_ratio >= self.ECHO_SEQUENCE_RATIO
            if multiword_echo or single_word_echo or sequence_echo:
                return "echo", detail
            if (
                self.config.echo_probe_enabled
                and self.barge_pending
                and len(words) >= 3
                and max(overlap, seq_ratio) >= self.ECHO_PROBE_TEXT_FLOOR
            ):
                # Text score is in the ambiguous band: ask the audio whether
                # the mic heard what the speaker played during this window.
                correlation = self.pending_barge_echo_correlation()
                detail["echo_corr"] = round(correlation, 2)
                if correlation >= self.ECHO_CORRELATION_FLOOR:
                    return "echo", detail
            if confidence is not None and confidence < self.ECHO_CONFIDENCE_FLOOR:
                detail["confidence"] = round(confidence, 3)
                return "low_confidence", detail
        return "admit", detail

    def pending_barge_echo_correlation(self) -> float:
        """Correlate the pending window's mic audio with the render audio
        played around it (padded for AEC/device delay error)."""
        now = time.perf_counter()
        start = self.barge_pending_since
        mic_pcm = self.mic_audio_ring.extract(start, now)
        render_pcm = self.render_audio_ring.extract(start - 0.6, now + 0.6)
        return echo_correlation(mic_pcm, render_pcm, self.config.deepgram_sample_rate)

    def begin_pending_barge_in(self) -> None:
        self.barge_pending = True
        self.barge_pending_since = time.perf_counter()
        if self.native_speaker:
            self.native_speaker.pause()
        self.logger.write("barge_in.pending")
        self.arm_pending_barge_in_deadline()
        if self.config.echo_probe_enabled:
            self.pending_probe_task = asyncio.create_task(self.early_echo_probe())

    def arm_pending_barge_in_deadline(self) -> None:
        task = self.pending_barge_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self.pending_barge_task = asyncio.create_task(self.expire_pending_barge_in())

    async def expire_pending_barge_in(self) -> None:
        await asyncio.sleep(self.config.barge_in_confirm_sec)
        if self.barge_pending:
            self.dismiss_pending_barge_in("timeout")

    async def early_echo_probe(self) -> None:
        """Resolve the pending pause by AUDIO, before any transcript exists.

        Waiting for the transcript costs the full STT round-trip while
        playback sits paused — live run 20260705T192451Z: text-resolved echo
        pauses ran 1.1-1.7s and eight more hit the whole 2s confirm deadline
        because Flux transcribed the echo only after the window. The mic and
        render rings already hold the answer; checkpoints start at 0.5s (the
        correlator needs ~0.4s of mic audio) and stay ahead of the deadline.
        A wrong dismissal self-corrects: the transcript still gets its
        verdict, so a real interrupt still barges in via the text path.
        """
        for checkpoint_sec in (0.5, 1.0, 1.5):
            remaining = self.barge_pending_since + checkpoint_sec - time.perf_counter()
            if remaining > 0:
                await asyncio.sleep(remaining)
            if not self.barge_pending:
                return
            if self.try_early_echo_dismiss():
                return

    def try_early_echo_dismiss(self) -> bool:
        """One audio check of the pending window; dismisses on a clear match.
        Always logs the correlation so live runs calibrate the floor."""
        correlation = self.pending_barge_echo_correlation()
        self.logger.write(
            "barge_in.echo_probe",
            corr=round(correlation, 2),
            at_ms=int((time.perf_counter() - self.barge_pending_since) * 1000),
        )
        if correlation >= self.ECHO_EARLY_CORRELATION_FLOOR:
            self.dismiss_pending_barge_in("echo_audio")
            return True
        return False

    def clear_pending_barge_in(self) -> None:
        self.barge_pending = False
        task = self.pending_barge_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self.pending_barge_task = None
        probe = self.pending_probe_task
        if probe and probe is not asyncio.current_task() and not probe.done():
            probe.cancel()
        self.pending_probe_task = None

    def dismiss_pending_barge_in(self, verdict: str) -> None:
        self.clear_pending_barge_in()
        self.logger.write("barge_in.false_alarm", verdict=verdict)
        if self.native_speaker:
            self.native_speaker.resume()

    async def enqueue_text_turn(self, text: str, source: str, eager: bool = False) -> None:
        issue = self.config.credential_issue_for("voice_turns")
        if issue:
            await self.send_json(issue.to_voice_bridge_issue(debug_ref=str(self.logger.run_dir)))
            return
        if not self.fork_session_id:
            await self.send_json({"type": "error", "message": "Start a voice fork before sending a prompt."})
            return
        if self.turn_task and not self.turn_task.done():
            await self.barge_in(reason="new_turn")
        # Remember the eager prompt so the confirming final speech.end can be
        # recognized (and skipped) instead of restarting the turn.
        self.eager_turn_text = text if eager else None
        self.turn_task = asyncio.create_task(self.run_text_turn(text=text, source=source, eager=eager))

    async def run_text_turn(self, text: str, source: str, eager: bool) -> None:
        if not self.fork_session_id:
            return
        self.turn_seq += 1
        turn_id = self.turn_seq
        self.active_turn_id = turn_id
        self.tts_first_audio_seen = False
        self.turn_spoken_any = False
        started = time.perf_counter()
        if self.speaker_prewarm_task is None or self.speaker_prewarm_task.done():
            self.speaker_prewarm_task = asyncio.create_task(self.prewarm_speaker())
        await self.maybe_wait_for_compaction(turn_id)
        session_id = self.fork_session_id
        before_messages = await self.client.messages(session_id)
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
            if self.active_turn_id != turn_id:
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
        while self.active_turn_id == turn_id and elapsed_ms(started) < int(self.config.max_turn_sec * 1000):
            messages = await self.client.messages(session_id)
            update = tracker.update(messages)
            if update.deltas and first_text_ms is None:
                first_text_ms = elapsed_ms(started)
                await self.send_json({"type": "assistant.first_text", "turn_id": turn_id, "latency_ms": first_text_ms})
                self.logger.write("assistant.first_text", turn_id=turn_id, latency_ms=first_text_ms)
            for delta in update.deltas:
                full_text += delta
                await self.send_json({"type": "assistant.delta", "turn_id": turn_id, "delta": delta})
                for chunk in chunker.push(speech_filter.push(delta)):
                    await self.speak(chunk, turn_id=turn_id)
            if update.completed:
                for chunk in chunker.push(speech_filter.flush()) + chunker.flush():
                    await self.speak(chunk, turn_id=turn_id)
                self.log_if_silent_completion(turn_id, update.full_text or full_text)
                if update.error:
                    await self.send_json(
                        {"type": "turn.error", "turn_id": turn_id, "message": str(update.error)[:1000]}
                    )
                    self.logger.write("turn.error", turn_id=turn_id, error=update.error)
                await self.send_json(
                    {
                        "type": "turn.complete",
                        "turn_id": turn_id,
                        "latency_ms": elapsed_ms(started),
                        "text": update.full_text or full_text,
                    }
                )
                self.logger.write(
                    "turn.complete",
                    turn_id=turn_id,
                    latency_ms=elapsed_ms(started),
                    response_chars=len(update.full_text or full_text),
                    stream_source=stream_source,
                )
                self.logger.state_transition("thinking", "ready", turn_id=turn_id)
                self.active_turn_id = None
                await self.maybe_start_compaction(reason="turn_complete", run_in_background=True)
                return
            await asyncio.sleep(self.config.poll_interval_sec)

        if self.active_turn_id == turn_id:
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

            tracker = OpenCodeEventTurnTracker(
                session_id=session_id,
                existing_message_ids={
                    str((message.get("info") or {}).get("id") or "")
                    for message in before_messages
                    if isinstance(message, dict)
                },
            )
            chunker = TTSChunker()
            speech_filter = SpeechTextFilter()
            first_text_ms: int | None = None
            full_text = ""
            stale_idle_logged = False
            # Set once completion is signalled before any text: a short window
            # to let text parts that trail the idle/completed event on the
            # subscription still land, instead of bailing straight to polling.
            completion_grace_deadline: float | None = None
            last_event_ms = elapsed_ms(started)
            while self.active_turn_id == turn_id and elapsed_ms(started) < int(self.config.max_turn_sec * 1000):
                if completion_grace_deadline is not None:
                    remaining = completion_grace_deadline - time.perf_counter()
                    if remaining <= 0:
                        raise OpenCodeEventFallback("event_stream_completed_without_text", prompt_sent=prompt_sent)
                    get_timeout: float = min(remaining, 0.2)
                else:
                    # Before the first delta, fail over to polling quickly; a
                    # stream that produced text already earns a longer stall
                    # budget (tool calls can pause deltas mid-turn).
                    get_timeout = 3 if first_text_ms is None else 8
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=get_timeout)
                except asyncio.TimeoutError as exc:
                    if completion_grace_deadline is not None:
                        continue  # keep draining for trailing text until the grace expires
                    if first_text_ms is None:
                        raise OpenCodeEventFallback("event_stream_no_initial_events", prompt_sent=prompt_sent) from exc
                    raise OpenCodeEventFallback("event_stream_stalled", prompt_sent=prompt_sent) from exc
                if event.get("type") == "_stream_error":
                    raise OpenCodeEventFallback(str(event.get("reason") or "event_stream_error"), prompt_sent=prompt_sent)
                last_event_ms = elapsed_ms(started)
                update = tracker.update(event)
                if tracker.stale_idles and not stale_idle_logged:
                    stale_idle_logged = True
                    self.logger.write("opencode.stream.stale_idle", turn_id=turn_id)
                if update.deltas and first_text_ms is None:
                    first_text_ms = elapsed_ms(started)
                    await self.send_json({"type": "assistant.first_text", "turn_id": turn_id, "latency_ms": first_text_ms})
                    self.logger.write("assistant.first_text", turn_id=turn_id, latency_ms=first_text_ms)
                    self.logger.write("opencode.stream.first_delta", turn_id=turn_id, latency_ms=first_text_ms)
                for delta in update.deltas:
                    full_text += delta
                    await self.send_json({"type": "assistant.delta", "turn_id": turn_id, "delta": delta})
                    for chunk in chunker.push(speech_filter.push(delta)):
                        await self.speak(chunk, turn_id=turn_id)
                if update.completed:
                    if not update.full_text and not full_text:
                        if completion_grace_deadline is None:
                            completion_grace_deadline = (
                                time.perf_counter() + self.config.event_completion_grace_sec
                            )
                            self.logger.write("opencode.stream.completion_grace", turn_id=turn_id)
                        continue  # wait out the grace for trailing text parts
                    await self.complete_event_text_turn(
                        session_id=session_id,
                        before_messages=before_messages,
                        turn_id=turn_id,
                        started=started,
                        chunker=chunker,
                        speech_filter=speech_filter,
                        event_text=update.full_text or full_text,
                    )
                    self.logger.write(
                        "opencode.stream.done",
                        turn_id=turn_id,
                        latency_ms=elapsed_ms(started),
                        last_event_ms=last_event_ms,
                    )
                    return

            if self.active_turn_id == turn_id:
                await self.send_json({"type": "turn.timeout", "turn_id": turn_id})
                self.logger.write("turn.timeout", turn_id=turn_id, latency_ms=elapsed_ms(started), stream_source="event")
                self.logger.state_transition("thinking", "voice_bridge_issue", turn_id=turn_id)
                self.active_turn_id = None
        finally:
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

    async def complete_event_text_turn(
        self,
        session_id: str,
        before_messages: list[dict[str, Any]],
        turn_id: int,
        started: float,
        chunker: TTSChunker,
        speech_filter: SpeechTextFilter,
        event_text: str,
    ) -> None:
        for chunk in chunker.push(speech_filter.flush()) + chunker.flush():
            await self.speak(chunk, turn_id=turn_id)
        messages = await self.client.messages(session_id)
        final_tracker = AssistantTextTracker(before_messages)
        final_update = final_tracker.update(messages)
        final_text = final_update.full_text or event_text
        self.log_if_silent_completion(turn_id, final_text)
        if final_update.error:
            await self.send_json({"type": "turn.error", "turn_id": turn_id, "message": str(final_update.error)[:1000]})
            self.logger.write("turn.error", turn_id=turn_id, error=final_update.error)
        await self.send_json(
            {
                "type": "turn.complete",
                "turn_id": turn_id,
                "latency_ms": elapsed_ms(started),
                "text": final_text,
            }
        )
        self.logger.write(
            "turn.complete",
            turn_id=turn_id,
            latency_ms=elapsed_ms(started),
            response_chars=len(final_text),
            stream_source="event",
        )
        self.logger.state_transition("thinking", "ready", turn_id=turn_id)
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
                messages = await self.client.messages(session_id)
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
                await self.compact(reason="context_overflow_error", before_tokens=before_tokens)
                tracker = AssistantTextTracker(await self.client.messages(session_id))

    def build_speaker(self) -> DeepgramSpeakSession | CartesiaSpeakSession:
        # Bind the current generation so audio still streaming from a
        # barged-in speaker cannot reach the lane or resurrect playback.
        generation = self.speak_generation

        async def deliver(data: bytes, turn_id: int | None) -> None:
            if generation != self.speak_generation:
                self.stale_tts_chunks += 1
                if self.stale_tts_chunks == 1 or self.stale_tts_chunks % 50 == 0:
                    self.logger.write("tts.stale_audio.drop", chunks=self.stale_tts_chunks, turn_id=turn_id)
                return
            await self.send_tts_audio(data, turn_id)

        # Built fresh each call (not a module-level constant) so tests can
        # patch opencode_voice.server.DeepgramSpeakSession/CartesiaSpeakSession
        # and have the swap take effect; adding a provider is one entry here
        # plus one class with the same start/speak/close(config, on_audio,
        # on_event) shape.
        tts_provider_sessions: dict[str, type] = {
            "deepgram": DeepgramSpeakSession,
            "cartesia": CartesiaSpeakSession,
        }
        session_cls = tts_provider_sessions.get(self.config.tts_provider, DeepgramSpeakSession)
        return session_cls(
            config=self.config,
            on_audio=deliver,
            on_event=self.send_json,
        )

    async def prewarm_speaker(self) -> None:
        """Open the TTS socket while the model is thinking so the handshake
        is off the first-audio path."""
        if self.speaker is not None or self.config.credential_issue_for("voice_audio"):
            return
        speaker = self.build_speaker()
        self.speaker = speaker
        try:
            await speaker.start()
        except Exception as exc:  # noqa: BLE001 - speak() retries; prewarm is best-effort.
            self.logger.write("tts.prewarm.error", error=repr(exc))
            if self.speaker is speaker:
                self.speaker = None

    async def speak(self, text: str, turn_id: int) -> None:
        issue = self.config.credential_issue_for("voice_audio")
        if issue:
            await self.send_json(issue.to_voice_bridge_issue(debug_ref=str(self.logger.run_dir)))
            return
        self.turn_spoken_any = True
        # Rolling record of what the assistant said recently; the pending
        # barge-in resolver matches transcripts against it to spot echo.
        self.spoken_text_recent = (self.spoken_text_recent + " " + text)[-1000:]
        if self.speaker is None:
            self.speaker = self.build_speaker()
        speaker = self.speaker
        await speaker.start()
        if self.speaker is not speaker:
            return
        await speaker.speak(text, turn_id=turn_id)

    def log_if_silent_completion(self, turn_id: int, response_text: str) -> None:
        """A completed turn with real reply text but zero speak() calls means
        the speech filter stripped everything (e.g. an all-code, no-prose
        reply) — the turn finishes normally and silently, which looks
        identical to a hang from the user's side. Diagnostic only; no
        fallback utterance is sent."""
        if response_text.strip() and not self.turn_spoken_any:
            self.logger.write("tts.no_speakable_text", turn_id=turn_id, response_chars=len(response_text))

    async def send_tts_audio(self, data: bytes, turn_id: int | None) -> None:
        if not self.tts_first_audio_seen:
            self.tts_first_audio_seen = True
            await self.send_json({"type": "tts.first_audio", "turn_id": turn_id})
            self.logger.write("tts.first_audio", turn_id=turn_id)
        async with self.send_lock:
            await self.websocket.send_bytes(data)

    async def interrupt_playback(self, reason: str) -> bool:
        """Stop the active turn and any speakers; the single owner of the
        speak-generation bump that keeps stale TTS audio off the wire.
        Returns True when something was actually cut off."""
        self.speak_generation += 1
        self.eager_turn_text = None
        self.clear_pending_barge_in()
        interrupted = self.active_turn_id is not None or bool(self.speaker or self.native_speaker)
        if self.active_turn_id is not None:
            self.logger.write("turn.abort", turn_id=self.active_turn_id, reason=reason)
        self.active_turn_id = None
        if self.speaker:
            # Deepgram's graceful close waits for the server to flush the
            # rest of the utterance (seen live: 4s of dead air after a
            # confirmed interrupt). Stale audio is generation-guarded, so
            # detach now and let the socket close in the background.
            speaker = self.speaker
            self.speaker = None
            self.spawn_background(self.close_speaker_quietly(speaker))
        if self.native_speaker:
            # Flush instead of close: tearing the device stream down every
            # interrupt forces the echo canceller to re-converge from scratch
            # on the next turn, and that first ~1.5s leak is exactly what
            # triggers the echo barge-ins.
            self.native_speaker.flush(reason=reason)
        self.native_speaker_unavailable = False
        return interrupted

    def spawn_background(self, coro: Awaitable[None]) -> None:
        # Retain a strong reference until the task finishes; a bare
        # create_task can be garbage-collected mid-run (CPython footgun).
        task = asyncio.ensure_future(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def close_speaker_quietly(self, speaker: DeepgramSpeakSession | CartesiaSpeakSession) -> None:
        try:
            await speaker.close()
        except Exception as exc:  # noqa: BLE001 - background close is best-effort.
            self.logger.write("tts.close.error", error=repr(exc))

    async def abort_fork_turn(self) -> None:
        if self.fork_session_id:
            try:
                await self.client.abort(self.fork_session_id)
            except Exception as exc:  # noqa: BLE001 - abort is best-effort during barge-in.
                self.logger.write("turn.abort.error", session_id=self.fork_session_id, error=repr(exc))

    async def barge_in(self, reason: str) -> None:
        await self.interrupt_playback(reason)
        await self.abort_fork_turn()
        await self.send_json({"type": "barge_in", "reason": reason})

    async def maybe_wait_for_compaction(self, turn_id: int) -> None:
        if not self.compaction_task or self.compaction_task.done():
            return
        await self.send_json({"type": "compaction.wait", "turn_id": turn_id})
        try:
            await asyncio.wait_for(asyncio.shield(self.compaction_task), timeout=self.config.compaction_wait_sec)
        except asyncio.TimeoutError:
            self.logger.write("compaction.wait.timeout", turn_id=turn_id)
            await self.send_json({"type": "compaction.wait.timeout", "turn_id": turn_id})

    async def maybe_start_compaction(self, reason: str, run_in_background: bool) -> None:
        if not self.fork_session_id:
            return
        if self.compaction_task and not self.compaction_task.done():
            return
        try:
            session = await self.client.get_session(self.fork_session_id)
            messages = await self.client.messages(self.fork_session_id)
        except Exception as exc:  # noqa: BLE001 - status should not break speech.
            self.logger.write("tokens.error", session_id=self.fork_session_id, error=repr(exc))
            return
        estimate = active_context_estimate(messages)
        usage_tokens = session_usage_tokens(session)
        await self.send_json(
            {
                "type": "tokens",
                "session_id": self.fork_session_id,
                "context_tokens": estimate.tokens,
                "context_source": estimate.source,
                "usage_tokens": usage_tokens,
                "summary_message_id": estimate.summary_message_id,
            }
        )
        self.logger.write(
            "tokens.check",
            session_id=self.fork_session_id,
            context_tokens=estimate.tokens,
            context_source=estimate.source,
            usage_tokens=usage_tokens,
            summary_message_id=estimate.summary_message_id,
            measured_message_id=estimate.measured_message_id,
            included_messages=estimate.included_messages,
            threshold=self.config.context_threshold_tokens,
        )
        if estimate.tokens < self.config.context_threshold_tokens:
            return
        self.compaction_task = asyncio.create_task(self.compact(reason=reason, before_tokens=estimate.tokens))
        if not run_in_background:
            await self.compaction_task

    async def compact(self, reason: str, before_tokens: int) -> None:
        if not self.fork_session_id:
            return
        session_id = self.fork_session_id
        started = time.perf_counter()
        self.logger.write("compaction.start", session_id=session_id, reason=reason, before_tokens=before_tokens)
        await self.send_json(
            {"type": "compaction.start", "session_id": session_id, "reason": reason, "before_tokens": before_tokens}
        )
        try:
            raw = await self.client.summarize(session_id, self.config.model, auto=False)
            after = await self.client.get_session(session_id)
            messages = await self.client.messages(session_id)
            estimate = active_context_estimate(messages)
            usage_tokens = session_usage_tokens(after)
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
                }
            )
        except Exception as exc:  # noqa: BLE001 - tell UI and keep conversation usable.
            latency = elapsed_ms(started)
            self.logger.write("compaction.error", session_id=session_id, latency_ms=latency, error=repr(exc))
            await self.send_json({"type": "compaction.error", "session_id": session_id, "latency_ms": latency})

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
    ) -> None:
        super().__init__(config=config, client=client, logger=logger, websocket=websocket)
        self.client_factory = client_factory
        self.lane_event_types = frozenset(sidepod_schema_document()["events"])
        self.mic_watchdog_task: asyncio.Task[None] | None = None
        self.lane_turn_counter = 0
        self.pending_turn_id: str | None = None
        self.transcript_seq = 0
        self.speech_started_at: float | None = None
        self.pending_latency: dict[str, Any] = {}
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
            self.logger.write("sidepod.opencode.rebind", opencode_url=opencode_url)

        if self.fork_session_id:
            await self.stop()
        self.voice_lane_id = self.voice_lane_id or f"lane_{int(time.time() * 1000)}"
        try:
            fork = await self.create_voice_fork(source_session_id, keep_fork=bool(payload.get("keepFork")))
        except Exception as exc:  # noqa: BLE001 - keep sidepod transport alive.
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

    async def handle_live_set(self, payload: dict[str, Any]) -> None:
        if not self.fork_session_id or not self.voice_lane_id:
            await self.send_protocol_issue(
                diagnostic_code="voice_lane_not_started",
                safe_detail="Voice lane unavailable",
            )
            return
        if bool(payload.get("value")):
            if await self.start_native_audio():
                self.start_mic_watchdog()
                await self.send_json(
                    {
                        "type": "listening",
                        "sentAt": iso_utc_now(),
                        "voiceLaneId": self.voice_lane_id,
                        "mode": "live",
                    }
                )
        else:
            self.cancel_mic_watchdog()
            await self.stop_audio(reason=str(payload.get("reason") or "live.set.false"))

    def start_mic_watchdog(self) -> None:
        self.cancel_mic_watchdog()
        self.mic_watchdog_task = asyncio.create_task(self.mic_watchdog())

    def cancel_mic_watchdog(self) -> None:
        if self.mic_watchdog_task and not self.mic_watchdog_task.done():
            self.mic_watchdog_task.cancel()
        self.mic_watchdog_task = None

    async def mic_watchdog(self) -> None:
        try:
            await asyncio.sleep(MIC_WATCHDOG_SEC)
        except asyncio.CancelledError:
            return
        if not self.native_mic or self.audio_input_chunks:
            return
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
        self.cancel_mic_watchdog()
        await self.stop_audio(reason="sidepod_stop")
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
        await self.interrupt_playback("sidepod_stop")
        # interrupt_playback keeps the device stream for the next turn; the
        # session is over, so actually release it here.
        if self.native_speaker:
            await self.native_speaker.close()
            self.native_speaker = None
        # The parent's legacy `stopped` message dies at the translation seam;
        # the v0 ack is handle_stop's job.
        await super().stop()
        self.protocol_turn_id = ""
        self.pending_turn_id = None

    async def close(self) -> None:
        self.cancel_mic_watchdog()
        await super().close()

    async def send_tts_audio(self, data: bytes, turn_id: int | None) -> None:
        if not self.tts_first_audio_seen:
            self.tts_first_audio_seen = True
            await self.send_json({"type": "tts.first_audio", "turn_id": turn_id})
            self.logger.write("tts.first_audio", turn_id=turn_id)
        if self.native_speaker_unavailable:
            return
        if self.native_speaker is None:
            speaker = NativeSpeakerSession(
                config=self.config,
                logger=self.logger,
                on_issue=self.send_json,
                on_render=self.feed_render_reference,
                on_drain=self.on_playback_drained,
            )
            if not await speaker.start():
                self.native_speaker_unavailable = True
                return
            self.native_speaker = speaker
        if not await self.native_speaker.play(data, turn_id):
            self.tts_unavailable_chunks += 1
            if self.tts_unavailable_chunks == 1 or self.tts_unavailable_chunks % 50 == 0:
                self.logger.write(
                    "native_tts.play.unavailable",
                    turn_id=turn_id,
                    bytes=len(data),
                    chunks=self.tts_unavailable_chunks,
                )

    def feed_render_reference(self, data: bytes) -> None:
        # Called on the playback worker thread just before the device write,
        # so the timestamp approximates when this chunk starts playing.
        self.render_audio_ring.append(data)
        if self.echo_canceller:
            self.echo_canceller.process_render(data)

    async def on_playback_drained(self) -> None:
        # Reply finished speaking: return the lane to a resting listening
        # state so the viewer's activity indicator stops reading "speaking".
        # Only while the mic is still live and no new turn has taken over.
        if self.native_mic is None or self.active_turn_id is not None or self.barge_pending:
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
        await self.send_lane_transcript(transcript, final=True, confidence=confidence)

    async def send_lane_transcript(self, text: str, final: bool, confidence: Any = None) -> None:
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
        outbound = self.translate_to_v0(payload)
        if outbound is None:
            self.logger.write("sidepod.lane.internal", message_type=payload.get("type"))
            return
        check = check_sidepod_event(outbound)
        if not check.ok:
            self.logger.write(
                "sidepod.lane.violation",
                message_type=str(outbound.get("type")),
                errors=list(check.errors),
            )
            return
        await super().send_json(outbound)

    def translate_to_v0(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        message_type = str(payload.get("type") or "")
        # v0-native payloads (lane handlers and base-class issue plumbing both
        # emit them) pass through; the discriminator against same-named legacy
        # messages is `sentAt`, which no legacy message may ever carry.
        if message_type in self.lane_event_types and "sentAt" in payload:
            return payload
        if message_type == "turn.start":
            return self.translate_turn_start(payload)
        if message_type == "assistant.delta":
            return self.translate_assistant_delta(payload)
        if message_type == "assistant.first_text":
            seam = self.turn_seams.get(int(payload.get("turn_id") or 0))
            latency_ms = payload.get("latency_ms")
            if seam and isinstance(latency_ms, (int, float)):
                seam.latency["firstAssistantTextMs"] = latency_ms
            return None
        if message_type == "tts.first_audio":
            return self.translate_tts_first_audio(payload)
        if message_type == "turn.complete":
            return self.translate_turn_complete(payload)
        if message_type in ("turn.error", "turn.timeout"):
            return self.translate_turn_failure(payload)
        if message_type == "opencode.stream.fallback":
            seam = self.turn_seams.get(int(payload.get("turn_id") or 0))
            if seam:
                seam.stream_source = "poll_after_event" if payload.get("prompt_sent") else "poll"
            return None
        if message_type == "error":
            return voice_bridge_issue_payload(
                capability="sidepod_transport",
                diagnostic_code="engine_error",
                safe_detail="Voice engine error",
                debug_ref=str(self.logger.run_dir),
                voice_lane_id=self.voice_lane_id,
            )
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
            del self.turn_seams[old_id]
        self.turn_seams[legacy_id] = TurnSeam(
            lane_id=lane_id,
            started_at=time.perf_counter(),
            latency=dict(self.pending_latency),
        )
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

    def translate_turn_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        if isinstance(total_ms, (int, float)):
            latency["totalMs"] = total_ms
        stream_source = seam.stream_source if seam else "event"
        self.logger.write("sidepod.turn.latency", turn_id=lane_id, stream_source=stream_source, **latency)
        out: dict[str, Any] = {
            "type": "complete",
            "sentAt": iso_utc_now(),
            "turnId": lane_id,
            "latency": latency,
            "streamSource": stream_source,
        }
        # Poll-fallback turns stream no assistant.delta events, so this is
        # the viewer's only copy of the reply text (the reducer falls back
        # to fullSpokenText when its delta buffer is empty).
        text = str(payload.get("text") or "")
        if text:
            out["fullSpokenText"] = text
        return out

    def translate_turn_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        timed_out = payload.get("type") == "turn.timeout"
        if timed_out:
            # A timed-out turn gets no turn.complete, so drop its seam state here.
            self.turn_seams.pop(int(payload.get("turn_id") or 0), None)
        return voice_bridge_issue_payload(
            capability="voice_turns",
            diagnostic_code="turn_timeout" if timed_out else "turn_failed",
            safe_detail="Voice turn timed out" if timed_out else "Voice turn failed",
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
        self.websocket: Any = None
        self.reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        api_key = os.environ["DEEPGRAM_API_KEY"]
        url = build_flux_url(
            model=self.config.deepgram_stt_model,
            sample_rate=self.config.deepgram_sample_rate,
            eot_threshold=self.config.flux_eot_threshold,
            eot_timeout_ms=self.config.flux_eot_timeout_ms,
            eager_eot_threshold=self.config.flux_eager_eot_threshold,
        )
        self.websocket = await connect_ws(url, {"Authorization": f"Token {api_key}"})
        self.reader_task = asyncio.create_task(self._read_loop())

    async def send_audio(self, data: bytes) -> None:
        if self.websocket:
            await self.websocket.send(data)

    async def close(self) -> None:
        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
        if self.websocket:
            await self.websocket.close()
            self.websocket = None

    async def _read_loop(self) -> None:
        try:
            async for message in self.websocket:
                if isinstance(message, str):
                    await self.on_event(parse_flux_message(message))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect is user-driven for now.
            await self.on_event({"type": "deepgram.error", "message": repr(exc)})


class DeepgramSpeakSession:
    def __init__(
        self,
        config: VoiceConfig,
        on_audio: Callable[[bytes, int | None], Awaitable[None]],
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.config = config
        self.on_audio = on_audio
        self.on_event = on_event
        self.websocket: Any = None
        self.reader_task: asyncio.Task[None] | None = None
        self.flush_limiter = FlushLimiter()
        self.current_turn_id: int | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        # Idempotent: prewarm and speak() may both race to open the socket.
        async with self._start_lock:
            if self.websocket:
                return
            api_key = os.environ["DEEPGRAM_API_KEY"]
            self.websocket = await connect_ws(
                build_tts_url(self.config.deepgram_tts_model, self.config.deepgram_sample_rate),
                {"Authorization": f"Token {api_key}"},
            )
            self.reader_task = asyncio.create_task(self._read_loop())

    async def speak(self, text: str, turn_id: int) -> None:
        if not self.websocket:
            await self.start()
        self.current_turn_id = turn_id
        await self.websocket.send(json.dumps({"type": "Speak", "text": text}))
        if self.flush_limiter.allow():
            await self.websocket.send(json.dumps({"type": "Flush"}))
            await self.on_event({"type": "tts.flush", "turn_id": turn_id, "chars": len(text)})
        else:
            await self.on_event({"type": "tts.flush.deferred", "turn_id": turn_id, "chars": len(text)})

    async def close(self) -> None:
        if self.websocket:
            try:
                await self.websocket.send(json.dumps({"type": "Close"}))
            except Exception:
                pass
        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
        if self.websocket:
            await self.websocket.close()
            self.websocket = None

    async def _read_loop(self) -> None:
        try:
            async for message in self.websocket:
                if isinstance(message, (bytes, bytearray)):
                    await self.on_audio(bytes(message), self.current_turn_id)
                elif isinstance(message, str):
                    await self.on_event({"type": "tts.metadata", "raw": json.loads(message)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI for visibility.
            await self.on_event({"type": "tts.error", "message": repr(exc)})


class CartesiaSpeakSession:
    """Same start/speak/close/on_audio surface as DeepgramSpeakSession, so
    build_speaker() can pick either without the rest of the connection caring.

    Cartesia has no single-context Flush primitive: each speak() call opens
    its own context_id, one complete non-continued request per chunk. Unlike
    Deepgram's single always-open context (inherently ordered), Cartesia
    synthesizes concurrent contexts on the same socket and interleaves their
    audio as it arrives — confirmed live: three back-to-back context_ids came
    back with chunks alternating between contexts, not in send order. Two
    contexts in flight together means two sentences' audio interleaved into
    one playback queue, which is audible as garbled/simultaneous speech.
    speak() therefore holds a lock for a context's entire lifetime — sent,
    streamed, and done — so the next call can't start a second context until
    the first one's audio has fully arrived. (An Event doesn't work here:
    Event.set() wakes every waiter at once, so two speak() calls queued
    behind one Event would both slip through together and race exactly like
    the bug this is fixing.)
    """

    def __init__(
        self,
        config: VoiceConfig,
        on_audio: Callable[[bytes, int | None], Awaitable[None]],
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.config = config
        self.on_audio = on_audio
        self.on_event = on_event
        self.websocket: Any = None
        self.reader_task: asyncio.Task[None] | None = None
        self.current_turn_id: int | None = None
        self._context_turns: dict[str, int | None] = {}
        self._context_done: dict[str, asyncio.Event] = {}
        self._context_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.closed = False

    async def start(self) -> None:
        async with self._start_lock:
            if self.websocket:
                return
            api_key = os.environ["CARTESIA_API_KEY"]
            self.websocket = await connect_ws(
                build_cartesia_tts_url(self.config.cartesia_version),
                {"X-API-Key": api_key},
            )
            self.reader_task = asyncio.create_task(self._read_loop())

    async def speak(self, text: str, turn_id: int) -> None:
        if not self.websocket:
            await self.start()
        async with self._context_lock:
            if self.closed:
                return
            self.current_turn_id = turn_id
            context_id = str(uuid.uuid4())
            done_event = asyncio.Event()
            self._context_turns[context_id] = turn_id
            self._context_done[context_id] = done_event
            await self.websocket.send(
                json.dumps(
                    {
                        "model_id": self.config.cartesia_tts_model,
                        "transcript": text,
                        "context_id": context_id,
                        "voice": {"mode": "id", "id": self.config.cartesia_voice_id},
                        "output_format": {
                            "container": "raw",
                            "encoding": "pcm_s16le",
                            "sample_rate": self.config.deepgram_sample_rate,
                        },
                        "language": "en",
                    }
                )
            )
            await self.on_event({"type": "tts.flush", "turn_id": turn_id, "chars": len(text)})
            # Held until this context's audio has fully arrived (or close()
            # force-releases it), keeping the lock so no other context opens.
            await done_event.wait()

    async def close(self) -> None:
        self.closed = True
        for done_event in self._context_done.values():
            done_event.set()
        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        self._context_turns.clear()
        self._context_done.clear()

    async def _read_loop(self) -> None:
        try:
            async for message in self.websocket:
                if not isinstance(message, str):
                    continue
                payload = json.loads(message)
                msg_type = payload.get("type")
                context_id = payload.get("context_id")
                turn_id = self._context_turns.get(context_id)
                if msg_type == "chunk" and payload.get("data"):
                    await self.on_audio(base64.b64decode(payload["data"]), turn_id)
                elif msg_type == "done" or payload.get("done"):
                    self._context_turns.pop(context_id, None)
                    done_event = self._context_done.pop(context_id, None)
                    if done_event:
                        done_event.set()
                elif msg_type == "error":
                    self._context_turns.pop(context_id, None)
                    done_event = self._context_done.pop(context_id, None)
                    if done_event:
                        done_event.set()
                    await self.on_event({"type": "tts.error", "message": payload.get("error") or repr(payload)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI for visibility.
            await self.on_event({"type": "tts.error", "message": repr(exc)})


async def connect_ws(url: str, headers: dict[str, str]) -> Any:
    try:
        return await websockets.connect(url, additional_headers=headers)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers)
