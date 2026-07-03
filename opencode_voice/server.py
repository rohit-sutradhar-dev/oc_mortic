from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from opencode_voice.config import VoiceConfig, load_voice_credentials, redact_secrets
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
from opencode_voice.state import (
    AssistantTextTracker,
    OpenCodeEventTurnTracker,
    active_context_estimate,
    elapsed_ms,
    event_properties,
    session_context_tokens,
    session_title,
    session_usage_tokens,
)

STATIC_DIR = Path(__file__).with_name("static")
EPHEMERAL_PREFIX = "[voice tmp]"
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


class OpenCodeEventFallback(Exception):
    def __init__(self, reason: str, prompt_sent: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.prompt_sent = prompt_sent


def create_app(config: VoiceConfig) -> FastAPI:
    app = FastAPI(title="OpenCode Mercury Voice Bridge")
    load_voice_credentials()
    logger = RunLogger(config.run_root)

    @app.on_event("startup")
    async def _startup() -> None:
        logger.write(
            "bridge.start",
            opencode_url=config.opencode_url,
            model=config.model.opencode_name,
            deepgram_stt_model=config.deepgram_stt_model,
            deepgram_tts_model=config.deepgram_tts_model,
            has_deepgram_key=config.has_deepgram_key,
            has_inception_key=config.has_inception_key,
            run_dir=str(logger.run_dir),
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
        credentials = load_voice_credentials()
        client = OpenCodeClient(config.opencode_url, timeout_sec=10)
        try:
            opencode_health = await client.health()
        finally:
            await client.close()
        return JSONResponse(
            {
                "ok": True,
                "opencode": opencode_health,
                "opencode_url": config.opencode_url,
                "run_dir": str(logger.run_dir),
                "model": config.model.opencode_name,
                "context_threshold_tokens": config.context_threshold_tokens,
                "credential_issues": [
                    issue.to_voice_bridge_issue(debug_ref=str(logger.run_dir)) for issue in credentials.issues
                ],
                "deepgram": {
                    "enabled": config.has_deepgram_key,
                    "stt_model": config.deepgram_stt_model,
                    "tts_model": config.deepgram_tts_model,
                    "sample_rate": config.deepgram_sample_rate,
                },
            }
        )

    @app.get("/api/sessions")
    async def sessions() -> JSONResponse:
        client = OpenCodeClient(config.opencode_url, timeout_sec=20)
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
        client = OpenCodeClient(config.opencode_url, timeout_sec=60)
        connection = VoiceConnection(config=config, client=client, logger=logger, websocket=websocket)
        try:
            await connection.run()
        finally:
            await connection.close()
            await client.close()

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
        self.source_session_id: str | None = None
        self.fork_session_id: str | None = None
        self.keep_fork = config.keep_fork_default
        self.closed = False
        self.compaction_task: asyncio.Task[None] | None = None
        self.turn_task: asyncio.Task[None] | None = None
        self.turn_seq = 0
        self.active_turn_id: int | None = None
        self.flux: DeepgramFluxSession | None = None
        self.speaker: DeepgramSpeakSession | None = None
        self.final_transcript = ""
        self.tts_first_audio_seen = False

    async def run(self) -> None:
        await self.send_json({"type": "ready", "run_dir": str(self.logger.run_dir)})
        for issue in self.config.credential_issues:
            await self.send_json(issue.to_voice_bridge_issue(debug_ref=str(self.logger.run_dir)))
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
        if self.flux:
            await self.flux.close()
        if self.speaker:
            await self.speaker.close()
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

    async def start(self, session_id: str, keep_fork: bool) -> None:
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
        session = await self.client.get_session(fork_id)
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
        await self.send_json(
            {
                "type": "fork.ready",
                "source_session_id": session_id,
                "fork_session_id": fork_id,
                "title": title,
                "context_tokens": estimate.tokens,
                "context_source": estimate.source,
                "usage_tokens": usage_tokens,
                "keep_fork": keep_fork,
            }
        )
        await self.maybe_start_compaction(reason="session_start", run_in_background=True)

    async def stop(self) -> None:
        if self.fork_session_id and not self.keep_fork:
            fork_id = self.fork_session_id
            await self.client.delete_session(fork_id)
            self.logger.write("fork.delete", session_id=fork_id)
        self.fork_session_id = None
        await self.send_json({"type": "stopped"})

    async def start_audio(self) -> None:
        issue = self.config.credential_issue_for("voice_audio")
        if issue:
            await self.send_json(issue.to_voice_bridge_issue(debug_ref=str(self.logger.run_dir)))
            return
        if self.flux:
            return
        self.flux = DeepgramFluxSession(self.config, on_event=self.handle_flux_event)
        await self.flux.start()
        await self.send_json({"type": "audio.ready"})

    async def handle_flux_event(self, event: dict[str, Any]) -> None:
        await self.send_json(event)
        if event["type"] == "speech.start":
            self.logger.write("speech.start")
            await self.barge_in(reason="speech_start")
            await self.maybe_start_compaction(reason="speech_start", run_in_background=True)
        elif event["type"] == "speech.resumed":
            self.logger.write("speech.resumed")
            await self.barge_in(reason="speech_resumed")
        elif event["type"] == "speech.transcript" and event.get("is_final") and event.get("transcript"):
            self.final_transcript = str(event["transcript"]).strip()
            self.logger.write("speech.transcript.final", transcript_chars=len(self.final_transcript))
        elif event["type"] == "speech.end":
            transcript = str(event.get("transcript") or self.final_transcript).strip()
            self.final_transcript = ""
            self.logger.write("speech.end", transcript_chars=len(transcript), eager=bool(event.get("eager")))
            if transcript:
                await self.enqueue_text_turn(transcript, source="voice", eager=bool(event.get("eager")))

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
        self.turn_task = asyncio.create_task(self.run_text_turn(text=text, source=source, eager=eager))

    async def run_text_turn(self, text: str, source: str, eager: bool) -> None:
        if not self.fork_session_id:
            return
        self.turn_seq += 1
        turn_id = self.turn_seq
        self.active_turn_id = turn_id
        self.tts_first_audio_seen = False
        started = time.perf_counter()
        await self.maybe_wait_for_compaction(turn_id)
        session_id = self.fork_session_id
        before_messages = await self.client.messages(session_id)
        tracker = AssistantTextTracker(before_messages)
        await self.send_json({"type": "turn.start", "turn_id": turn_id, "source": source, "text": text, "eager": eager})
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
                self.active_turn_id = None
                await self.maybe_start_compaction(reason="turn_complete", run_in_background=True)
                return
            await asyncio.sleep(self.config.poll_interval_sec)

        if self.active_turn_id == turn_id:
            await self.send_json({"type": "turn.timeout", "turn_id": turn_id})
            self.logger.write("turn.timeout", turn_id=turn_id, latency_ms=elapsed_ms(started))
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
            last_event_ms = elapsed_ms(started)
            while self.active_turn_id == turn_id and elapsed_ms(started) < int(self.config.max_turn_sec * 1000):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=8)
                except asyncio.TimeoutError as exc:
                    if first_text_ms is None:
                        raise OpenCodeEventFallback("event_stream_no_initial_events", prompt_sent=prompt_sent) from exc
                    raise OpenCodeEventFallback("event_stream_stalled", prompt_sent=prompt_sent) from exc
                if event.get("type") == "_stream_error":
                    raise OpenCodeEventFallback(str(event.get("reason") or "event_stream_error"), prompt_sent=prompt_sent)
                last_event_ms = elapsed_ms(started)
                update = tracker.update(event)
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
                        raise OpenCodeEventFallback("event_stream_completed_without_text", prompt_sent=prompt_sent)
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
            async for event in self.client.events(on_open=ready.set):
                properties = event_properties(event)
                if properties.get("sessionID") == session_id:
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

    async def speak(self, text: str, turn_id: int) -> None:
        issue = self.config.credential_issue_for("voice_audio")
        if issue:
            await self.send_json(issue.to_voice_bridge_issue(debug_ref=str(self.logger.run_dir)))
            return
        if self.speaker is None:
            speaker = DeepgramSpeakSession(
                config=self.config,
                on_audio=self.send_tts_audio,
                on_event=self.send_json,
            )
            self.speaker = speaker
            await speaker.start()
        else:
            speaker = self.speaker
        if self.speaker is not speaker:
            return
        await speaker.speak(text, turn_id=turn_id)

    async def send_tts_audio(self, data: bytes, turn_id: int | None) -> None:
        if not self.tts_first_audio_seen:
            self.tts_first_audio_seen = True
            await self.send_json({"type": "tts.first_audio", "turn_id": turn_id})
            self.logger.write("tts.first_audio", turn_id=turn_id)
        async with self.send_lock:
            await self.websocket.send_bytes(data)

    async def barge_in(self, reason: str) -> None:
        if self.active_turn_id is not None:
            self.logger.write("turn.abort", turn_id=self.active_turn_id, reason=reason)
        self.active_turn_id = None
        if self.speaker:
            await self.speaker.close()
            self.speaker = None
        if self.fork_session_id:
            try:
                await self.client.abort(self.fork_session_id)
            except Exception as exc:  # noqa: BLE001 - abort is best-effort during barge-in.
                self.logger.write("turn.abort.error", session_id=self.fork_session_id, error=repr(exc))
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
            return
        async with self.send_lock:
            try:
                await self.websocket.send_text(json.dumps(redact_secrets(payload), ensure_ascii=False))
            except WebSocketDisconnect:
                self.closed = True


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

    async def start(self) -> None:
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


async def connect_ws(url: str, headers: dict[str, str]) -> Any:
    try:
        return await websockets.connect(url, additional_headers=headers)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers)
