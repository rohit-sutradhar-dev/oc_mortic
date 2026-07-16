from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import websockets

from opencode_voice.cartesia import build_tts_url as build_cartesia_tts_url
from opencode_voice.deepgram import build_tts_url as build_deepgram_tts_url
from opencode_voice.playback import PlaybackToken

WebSocketConnector = Callable[[str, dict[str, str]], Awaitable[Any]]
AudioCallback = Callable[[PlaybackToken, bytes], Awaitable[None]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
ContextIdFactory = Callable[[], str]
Clock = Callable[[], float]

_DEFAULT_DELIVERY_QUEUE_MAX_BYTES = 8 * 1024 * 1024
_DEFAULT_DELIVERY_QUEUE_MAX_CHUNKS = 8192


class TTSProviderError(RuntimeError):
    pass


class StalePlaybackToken(TTSProviderError):
    pass


class TTSProvider(Protocol):
    supports_terminal_events: bool

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def begin_turn(self, token: PlaybackToken) -> None: ...

    async def append_text(self, token: PlaybackToken, text: str) -> None: ...

    async def finish_turn(self, token: PlaybackToken) -> None: ...

    async def cancel_turn(self, token: PlaybackToken, reason: str) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DeepgramTTSOptions:
    api_key: str = field(repr=False)
    model: str = "aura-2-jupiter-en"
    sample_rate: int = 16_000
    connect_timeout_sec: float = 5.0
    send_timeout_sec: float = 0.5
    clear_timeout_sec: float = 0.5
    # 60 seconds of 48 kHz mono PCM16 is 5,760,000 bytes / 6,000 10 ms
    # frames.  Keep enough producer-ahead room for a long answer while still
    # imposing a deterministic memory bound.
    delivery_queue_max_bytes: int = _DEFAULT_DELIVERY_QUEUE_MAX_BYTES
    delivery_queue_max_chunks: int = _DEFAULT_DELIVERY_QUEUE_MAX_CHUNKS


@dataclass(frozen=True, slots=True)
class CartesiaTTSOptions:
    api_key: str = field(repr=False)
    voice_id: str
    model: str = "sonic-3.5"
    version: str = "2026-03-01"
    sample_rate: int = 16_000
    language: str = "en"
    connect_timeout_sec: float = 5.0
    send_timeout_sec: float = 0.5
    context_idle_rotate_sec: float = 0.9
    delivery_queue_max_bytes: int = _DEFAULT_DELIVERY_QUEUE_MAX_BYTES
    delivery_queue_max_chunks: int = _DEFAULT_DELIVERY_QUEUE_MAX_CHUNKS


@dataclass(frozen=True, slots=True)
class _DeliveryIdentity:
    provider: str
    token: PlaybackToken
    stream_id: int
    context_id: str | None = None


@dataclass(frozen=True, slots=True)
class _DeliveryItem:
    identity: _DeliveryIdentity
    kind: str
    data: bytes = b""
    error_code: str | None = None
    close_code: int | None = None


class _DeliveryOverflow(TTSProviderError):
    pass


TerminalCallback = Callable[
    [_DeliveryIdentity, str, str | None, int | None], Awaitable[None]
]


class _OrderedAudioDelivery:
    """Keep provider reads independent from device-clock backpressure.

    The websocket reader only performs non-blocking admissions.  A single
    actor awaits the device callback, preserving provider order.  Bytes and
    chunks are both bounded; overflow is terminal rather than a silent PCM
    drop.
    """

    _TERMINAL_RESERVE = 32
    _RETIRED_IDENTITY_LIMIT = 128

    def __init__(
        self,
        on_audio: AudioCallback,
        on_terminal: TerminalCallback,
        *,
        max_bytes: int,
        max_chunks: int,
    ) -> None:
        if max_bytes <= 0 or max_chunks <= 0:
            raise ValueError("TTS delivery queue limits must be positive")
        self._on_audio = on_audio
        self._on_terminal = on_terminal
        self._max_bytes = max_bytes
        self._max_chunks = max_chunks
        self._queue: asyncio.Queue[_DeliveryItem] = asyncio.Queue(
            maxsize=max_chunks + self._TERMINAL_RESERVE
        )
        self._task: asyncio.Task[None] | None = None
        self._buffered_bytes = 0
        self._buffered_chunks = 0
        self._terminal_queued: dict[_DeliveryIdentity, str] = {}
        self._retired: set[_DeliveryIdentity] = set()
        self._retired_order: deque[_DeliveryIdentity] = deque()
        self._terminal_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def buffered_bytes(self) -> int:
        return self._buffered_bytes

    @property
    def buffered_chunks(self) -> int:
        return self._buffered_chunks

    def enqueue_audio(self, identity: _DeliveryIdentity, data: bytes) -> bool:
        if (
            self._closed
            or identity in self._retired
            or identity in self._terminal_queued
        ):
            return False
        size = len(data)
        if (
            self._buffered_chunks >= self._max_chunks
            or self._buffered_bytes + size > self._max_bytes
        ):
            raise _DeliveryOverflow("ordered TTS audio delivery queue overflow")
        self._ensure_actor()
        try:
            self._queue.put_nowait(_DeliveryItem(identity=identity, kind="audio", data=data))
        except asyncio.QueueFull as exc:  # The terminal reserve makes this defensive.
            raise _DeliveryOverflow("ordered TTS audio delivery queue full") from exc
        self._buffered_bytes += size
        self._buffered_chunks += 1
        return True

    def enqueue_terminal(
        self,
        identity: _DeliveryIdentity,
        kind: str,
        *,
        error_code: str | None = None,
        close_code: int | None = None,
    ) -> bool:
        if kind == "failed":
            return self.fail(
                identity,
                error_code=error_code,
                close_code=close_code,
            )
        if (
            self._closed
            or identity in self._retired
            or identity in self._terminal_queued
        ):
            return False
        self._ensure_actor()
        item = _DeliveryItem(
            identity=identity,
            kind=kind,
            error_code=error_code,
            close_code=close_code,
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            return False
        self._terminal_queued[identity] = kind
        return True

    def cancel(self, identity: _DeliveryIdentity) -> None:
        # An already-entered on_audio callback cannot be preempted safely, but
        # every queued callback is fenced before it reaches the device.
        self._terminal_queued.pop(identity, None)
        self._retire(identity)
        self._purge_identity(identity)

    def fail(
        self,
        identity: _DeliveryIdentity,
        *,
        error_code: str | None = None,
        close_code: int | None = None,
    ) -> bool:
        """Fence failed PCM immediately and report failure off the audio actor.

        A provider/socket failure must not sit behind seconds of queued PCM or
        an on_audio callback blocked on device capacity.  Successful completion
        remains an ordered queue item; failure instead purges that identity and
        gets its own lifecycle task.
        """

        if self._closed or identity in self._retired:
            return False
        pending = self._terminal_queued.get(identity)
        if pending == "failed":
            return False
        self._purge_identity(identity)
        self._retire(identity)
        self._terminal_queued[identity] = "failed"
        self._ensure_failure_task(identity, error_code, close_code)
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._buffered_bytes = 0
        self._buffered_chunks = 0
        terminal_tasks = tuple(self._terminal_tasks)
        self._terminal_tasks.clear()
        for terminal_task in terminal_tasks:
            terminal_task.cancel()
        if terminal_tasks:
            await asyncio.gather(*terminal_tasks, return_exceptions=True)
        self._terminal_queued.clear()
        self._retired.clear()
        self._retired_order.clear()

    def _ensure_actor(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="tts-audio-delivery")

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item.kind == "audio":
                try:
                    if item.identity not in self._retired:
                        await self._on_audio(item.identity.token, item.data)
                except asyncio.CancelledError:
                    raise
                except Exception:  # Device failures become safe provider lifecycle failures.
                    if item.identity not in self._retired:
                        self.fail(item.identity, error_code="audio_delivery_error")
                finally:
                    self._buffered_bytes -= len(item.data)
                    self._buffered_chunks -= 1
                continue
            if self._terminal_queued.get(item.identity) != item.kind:
                continue
            self._retire(item.identity)
            try:
                await self._on_terminal(
                    item.identity,
                    item.kind,
                    item.error_code,
                    item.close_code,
                )
            finally:
                if self._terminal_queued.get(item.identity) == item.kind:
                    self._terminal_queued.pop(item.identity, None)

    def _purge_identity(self, identity: _DeliveryIdentity) -> None:
        retained: list[_DeliveryItem] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item.identity == identity:
                if item.kind == "audio":
                    self._buffered_bytes -= len(item.data)
                    self._buffered_chunks -= 1
                continue
            retained.append(item)
        for item in retained:
            self._queue.put_nowait(item)

    def _retire(self, identity: _DeliveryIdentity) -> None:
        if identity in self._retired:
            return
        self._retired.add(identity)
        self._retired_order.append(identity)
        while len(self._retired_order) > self._RETIRED_IDENTITY_LIMIT:
            expired = self._retired_order.popleft()
            self._retired.discard(expired)

    def _ensure_failure_task(
        self,
        identity: _DeliveryIdentity,
        error_code: str | None,
        close_code: int | None,
    ) -> None:
        async def emit_failure() -> None:
            try:
                await self._on_terminal(
                    identity,
                    "failed",
                    error_code,
                    close_code,
                )
            finally:
                if self._terminal_queued.get(identity) == "failed":
                    self._terminal_queued.pop(identity, None)

        task = asyncio.create_task(
            emit_failure(),
            name=f"tts-delivery-failure-{identity.stream_id}",
        )
        self._terminal_tasks.add(task)
        task.add_done_callback(self._terminal_tasks.discard)


async def connect_websocket(url: str, headers: dict[str, str]) -> Any:
    """Compatibility connector for websockets 12 through 16."""

    kwargs: dict[str, Any] = {
        "open_timeout": 5,
        "close_timeout": 1,
        # Provider-turn watchdogs own active synthesis health. Keep transport
        # heartbeats tolerant of ordinary scheduler/network jitter instead of
        # turning a transient five-second stall into a lost conversation.
        "ping_interval": 20,
        "ping_timeout": 20,
        # Do not let an advertised-but-broken IPv6 route mask a healthy IPv4
        # provider. asyncio races address families after this short delay.
        "happy_eyeballs_delay": 0.25,
        "interleave": 1,
    }
    try:
        return await websockets.connect(url, additional_headers=headers, **kwargs)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers, **kwargs)


class _ProviderSocket:
    supports_terminal_events = True

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str],
        connector: WebSocketConnector,
        on_event: EventCallback,
        connect_timeout_sec: float,
        send_timeout_sec: float,
    ) -> None:
        self._url = url
        self._headers = headers
        self._connector = connector
        self._on_event = on_event
        self._connect_timeout_sec = connect_timeout_sec
        self._send_timeout_sec = send_timeout_sec
        self._websocket: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._connection_epoch = 0

    @property
    def connection_epoch(self) -> int:
        return self._connection_epoch

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    async def connect(self) -> None:
        if self._closed:
            raise TTSProviderError("provider is closed")
        async with self._connect_lock:
            if self._websocket is not None:
                return
            try:
                websocket = await asyncio.wait_for(
                    self._connector(self._url, self._headers),
                    timeout=self._connect_timeout_sec,
                )
            except Exception as exc:
                error_code = (
                    "opening_handshake_timeout"
                    if isinstance(exc, TimeoutError)
                    else "connect_error"
                )
                await self._emit(
                    "tts.transport.connect_error",
                    stage="opening_handshake",
                    error_code=error_code,
                )
                raise TTSProviderError("could not connect TTS provider") from exc
            if self._closed:
                await self._close_websocket(websocket)
                raise TTSProviderError("provider closed while connecting")
            self._connection_epoch += 1
            epoch = self._connection_epoch
            self._websocket = websocket
            self._reader_task = asyncio.create_task(self._reader_main(websocket, epoch))
        await self._emit("tts.transport.connected", epoch=epoch)

    async def _send_payloads(self, *payloads: dict[str, Any]) -> None:
        await self.connect()
        websocket: Any = None
        try:
            async with self._send_lock:
                websocket = self._websocket
                if websocket is None:
                    raise ConnectionError("TTS websocket disconnected")
                for payload in payloads:
                    await asyncio.wait_for(
                        websocket.send(json.dumps(payload)),
                        timeout=self._send_timeout_sec,
                    )
        except Exception as exc:
            error_code, close_code = self._failure_detail(exc, websocket)
            await self._emit(
                "tts.transport.send_error",
                error_code="send_error",
                close_code=close_code,
            )
            if websocket is not None:
                await self._shutdown_socket(expected=websocket)
                await self._on_socket_lost(
                    error_code="send_error" if error_code != "delivery_overflow" else error_code,
                    close_code=close_code,
                )
            raise TTSProviderError("TTS provider send failed") from exc

    async def _reader_main(self, websocket: Any, epoch: int) -> None:
        error: Exception | None = None
        try:
            await self._consume_messages(websocket, epoch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized into provider telemetry.
            error = exc
        finally:
            current = False
            async with self._connect_lock:
                if self._websocket is websocket:
                    current = True
                    self._websocket = None
                    if self._reader_task is asyncio.current_task():
                        self._reader_task = None
            if current and not self._closed:
                error_code, close_code = self._failure_detail(error, websocket)
                await self._on_socket_lost(
                    error_code=error_code,
                    close_code=close_code,
                )
                await self._close_websocket(websocket)
                await self._emit(
                    "tts.transport.disconnected",
                    epoch=epoch,
                    error_code=error_code,
                    close_code=close_code,
                )

    async def _consume_messages(self, websocket: Any, epoch: int) -> None:
        raise NotImplementedError

    async def _on_socket_lost(
        self,
        *,
        error_code: str,
        close_code: int | None,
    ) -> None:
        """Provider-specific invalidation after an unplanned disconnect."""

        return None

    @staticmethod
    def _failure_detail(error: Exception | None, websocket: Any) -> tuple[str, int | None]:
        close_code = getattr(error, "code", None) if error is not None else None
        if not isinstance(close_code, int):
            received = getattr(error, "rcvd", None) if error is not None else None
            close_code = getattr(received, "code", None)
        if not isinstance(close_code, int):
            close_code = getattr(websocket, "close_code", None)
        if not isinstance(close_code, int):
            close_code = None
        if isinstance(error, _DeliveryOverflow):
            return "delivery_overflow", close_code
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return "transport_timeout", close_code
        if isinstance(error, (json.JSONDecodeError, UnicodeError, ValueError)):
            return "provider_protocol_error", close_code
        if error is None or close_code is not None:
            return "websocket_closed", close_code
        return "transport_error", close_code

    async def _replace_socket(self) -> None:
        await self._shutdown_socket()
        if not self._closed:
            await self.connect()

    async def _shutdown_socket(self, expected: Any = None) -> None:
        async with self._connect_lock:
            if expected is not None and self._websocket is not expected:
                return
            websocket = self._websocket
            reader = self._reader_task
            self._websocket = None
            self._reader_task = None
        current = asyncio.current_task()
        if reader is not None and reader is not current and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        if websocket is not None:
            await self._close_websocket(websocket)

    async def _close_websocket(self, websocket: Any) -> None:
        try:
            await asyncio.wait_for(websocket.close(), timeout=1.0)
        except Exception:
            pass

    async def _emit(self, event_type: str, **detail: Any) -> None:
        try:
            await self._on_event({"type": event_type, **detail})
        except Exception:
            # Diagnostics must never take down the audio transport.
            pass


@dataclass(slots=True)
class _DeepgramTurn:
    token: PlaybackToken
    delivery_identity: _DeliveryIdentity
    flush_sent: bool = False


class DeepgramTTSProvider(_ProviderSocket):
    """One conversational Deepgram socket with a Clear/Cleared turn fence."""

    def __init__(
        self,
        options: DeepgramTTSOptions,
        on_audio: AudioCallback,
        on_event: EventCallback,
        *,
        connector: WebSocketConnector = connect_websocket,
    ) -> None:
        super().__init__(
            url=build_deepgram_tts_url(options.model, options.sample_rate),
            headers={"Authorization": f"Token {options.api_key}"},
            connector=connector,
            on_event=on_event,
            connect_timeout_sec=options.connect_timeout_sec,
            send_timeout_sec=options.send_timeout_sec,
        )
        self.options = options
        self._on_audio = on_audio
        self._state_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._active_turn: _DeepgramTurn | None = None
        self._accept_audio = False
        self._clear_waiter: asyncio.Future[None] | None = None
        self._turn_sequence = 0
        self._delivery = _OrderedAudioDelivery(
            on_audio,
            self._delivery_terminal,
            max_bytes=options.delivery_queue_max_bytes,
            max_chunks=options.delivery_queue_max_chunks,
        )

    @property
    def active_token(self) -> PlaybackToken | None:
        turn = self._active_turn
        return turn.token if turn is not None else None

    async def begin_turn(self, token: PlaybackToken) -> None:
        await self.connect()
        while True:
            if self._closed:
                raise TTSProviderError("provider is closed")
            async with self._state_lock:
                waiter = self._clear_waiter
                previous_turn = self._active_turn
                previous = previous_turn.token if previous_turn is not None else None
            if waiter is not None:
                await asyncio.shield(waiter)
                continue
            if previous is not None and previous != token:
                await self.cancel_turn(previous, "superseded")
                continue
            async with self._state_lock:
                self._turn_sequence += 1
                identity = _DeliveryIdentity(
                    provider="deepgram",
                    token=token,
                    stream_id=self._turn_sequence,
                )
                self._active_turn = _DeepgramTurn(token=token, delivery_identity=identity)
                self._accept_audio = True
            await self._emit(
                "tts.turn.begin",
                provider="deepgram",
                token=token.generation,
                generation=token.generation,
                turn_id=token.turn_id,
            )
            return

    async def append_text(self, token: PlaybackToken, text: str) -> None:
        if not text:
            return
        async with self._operation_lock:
            turn = await self._require_active(token)
            if turn.flush_sent:
                raise StalePlaybackToken(f"finished playback generation {token.generation}")
            await self._send_payloads({"type": "Speak", "text": text})
        await self._emit("tts.text.append", provider="deepgram", turn_id=token.turn_id, chars=len(text))

    async def finish_turn(self, token: PlaybackToken) -> None:
        async with self._operation_lock:
            turn = await self._require_active(token)
            if turn.flush_sent:
                return
            async with self._state_lock:
                if self._active_turn is turn:
                    turn.flush_sent = True
            try:
                await self._send_payloads({"type": "Flush"})
            except Exception:
                async with self._state_lock:
                    if self._active_turn is turn:
                        turn.flush_sent = False
                raise
        await self._emit(
            "tts.turn.finish",
            provider="deepgram",
            turn_id=token.turn_id,
            generation=token.generation,
            playback_generation=token.generation,
        )

    async def cancel_turn(self, token: PlaybackToken, reason: str) -> None:
        loop = asyncio.get_running_loop()
        async with self._state_lock:
            turn = self._active_turn
            if turn is None or turn.token != token:
                return
            # Fence incoming PCM before the first network await.
            self._active_turn = None
            self._accept_audio = False
            waiter: asyncio.Future[None] = loop.create_future()
            self._clear_waiter = waiter
        self._delivery.cancel(turn.delivery_identity)
        await self._emit("tts.turn.cancel", provider="deepgram", turn_id=token.turn_id, reason=reason)
        try:
            async with self._operation_lock:
                await self._send_payloads({"type": "Clear"})
            await asyncio.wait_for(asyncio.shield(waiter), timeout=self.options.clear_timeout_sec)
        except asyncio.CancelledError:
            # A cancelled Clear waiter must not leave the next turn sharing an
            # ambiguous socket with audio generated before the fence.
            await self._shutdown_socket()
            raise
        except Exception as exc:  # timeout/send failure: a new socket is the only unambiguous boundary.
            await self._emit("tts.clear.timeout", provider="deepgram", error=repr(exc))
            try:
                await self._replace_socket()
            except Exception as reconnect_exc:
                await self._emit("tts.transport.reconnect_error", provider="deepgram", error=repr(reconnect_exc))
        finally:
            async with self._state_lock:
                if self._clear_waiter is waiter:
                    self._clear_waiter = None
                if not waiter.done():
                    waiter.set_result(None)

    async def close(self) -> None:
        if self._closed:
            return
        async with self._state_lock:
            turn = self._active_turn
            self._active_turn = None
            self._accept_audio = False
            waiter = self._clear_waiter
            self._clear_waiter = None
            if waiter is not None and not waiter.done():
                waiter.set_result(None)
        if turn is not None:
            self._delivery.cancel(turn.delivery_identity)
        if self._websocket is not None:
            try:
                await self._send_payloads({"type": "Close"})
            except TTSProviderError:
                pass
        self._closed = True
        await self._shutdown_socket()
        await self._delivery.close()

    async def _require_active(self, token: PlaybackToken) -> _DeepgramTurn:
        async with self._state_lock:
            turn = self._active_turn
            if turn is None or turn.token != token or not self._accept_audio:
                raise StalePlaybackToken(f"inactive playback generation {token.generation}")
            return turn

    async def _consume_messages(self, websocket: Any, epoch: int) -> None:
        async for message in websocket:
            if websocket is not self._websocket or epoch != self._connection_epoch:
                continue
            if isinstance(message, (bytes, bytearray)):
                async with self._state_lock:
                    turn = self._active_turn if self._accept_audio else None
                if turn is None:
                    await self._emit("tts.stale_audio.drop", provider="deepgram", bytes=len(message))
                    continue
                try:
                    accepted = self._delivery.enqueue_audio(turn.delivery_identity, bytes(message))
                except _DeliveryOverflow:
                    async with self._state_lock:
                        if self._active_turn is turn:
                            self._active_turn = None
                            self._accept_audio = False
                    self._queue_terminal_or_emit(
                        turn.delivery_identity,
                        "failed",
                        error_code="delivery_overflow",
                    )
                    raise
                if not accepted:
                    await self._emit("tts.stale_audio.drop", provider="deepgram", bytes=len(message))
                continue
            if not isinstance(message, str):
                continue
            payload = json.loads(message)
            message_type = str(payload.get("type") or "")
            if message_type == "Cleared":
                async with self._state_lock:
                    waiter = self._clear_waiter
                    self._clear_waiter = None
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
            elif message_type == "Flushed":
                async with self._state_lock:
                    turn = self._active_turn
                    if turn is not None and turn.flush_sent:
                        self._active_turn = None
                        self._accept_audio = False
                    else:
                        turn = None
                if turn is not None:
                    self._queue_terminal_or_emit(turn.delivery_identity, "done")
            elif message_type.lower() == "error":
                async with self._state_lock:
                    turn = self._active_turn
                    self._active_turn = None
                    self._accept_audio = False
                if turn is not None:
                    self._queue_terminal_or_emit(
                        turn.delivery_identity,
                        "failed",
                        error_code="provider_error",
                    )
            await self._emit("tts.provider.message", provider="deepgram", message_type=message_type, raw=payload)

    async def _on_socket_lost(
        self,
        *,
        error_code: str,
        close_code: int | None,
    ) -> None:
        async with self._state_lock:
            turn = self._active_turn
            self._active_turn = None
            self._accept_audio = False
            waiter = self._clear_waiter
            self._clear_waiter = None
            if waiter is not None and not waiter.done():
                waiter.set_result(None)
        if turn is not None:
            self._queue_terminal_or_emit(
                turn.delivery_identity,
                "failed",
                error_code=error_code,
                close_code=close_code,
            )

    def _queue_terminal_or_emit(
        self,
        identity: _DeliveryIdentity,
        kind: str,
        *,
        error_code: str | None = None,
        close_code: int | None = None,
    ) -> None:
        if self._delivery.enqueue_terminal(
            identity,
            kind,
            error_code=error_code,
            close_code=close_code,
        ):
            return
        # The queue reserves terminal capacity, so reaching this path means a
        # prior terminal/cancel already owns the lifecycle for this identity.

    async def _delivery_terminal(
        self,
        identity: _DeliveryIdentity,
        kind: str,
        error_code: str | None,
        close_code: int | None,
    ) -> None:
        async with self._state_lock:
            turn = self._active_turn
            if turn is not None and turn.delivery_identity == identity:
                self._active_turn = None
                self._accept_audio = False
        detail: dict[str, Any] = {
            "provider": "deepgram",
            "turn_id": identity.token.turn_id,
            "generation": identity.token.generation,
            "playback_generation": identity.token.generation,
        }
        if kind == "done":
            await self._emit("tts.turn.done", **detail)
            return
        detail["error_code"] = error_code or "provider_transport_error"
        if close_code is not None:
            detail["close_code"] = close_code
        await self._emit("tts.turn.failed", **detail)


@dataclass(slots=True)
class _CartesiaContext:
    context_id: str
    token: PlaybackToken
    delivery_identity: _DeliveryIdentity
    last_provider_activity_at: float | None = None
    last_text_char: str = ""
    has_input: bool = False
    finishing: bool = False
    retired: bool = False


@dataclass(slots=True)
class _StaleAudioStats:
    chunks: int = 0
    bytes: int = 0
    started_at: float = 0.0


class CartesiaTTSProvider(_ProviderSocket):
    """Turn-scoped Cartesia continuations with context-ID late-audio fencing."""

    _STALE_AUDIO_CONTEXT_LIMIT = 64

    def __init__(
        self,
        options: CartesiaTTSOptions,
        on_audio: AudioCallback,
        on_event: EventCallback,
        *,
        connector: WebSocketConnector = connect_websocket,
        context_id_factory: ContextIdFactory = lambda: str(uuid.uuid4()),
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__(
            url=build_cartesia_tts_url(options.version),
            headers={"X-API-Key": options.api_key},
            connector=connector,
            on_event=on_event,
            connect_timeout_sec=options.connect_timeout_sec,
            send_timeout_sec=options.send_timeout_sec,
        )
        self.options = options
        self._on_audio = on_audio
        self._context_id_factory = context_id_factory
        self._clock = clock
        self._state_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._contexts: dict[str, _CartesiaContext] = {}
        self._active_context: _CartesiaContext | None = None
        self._stale_audio: dict[str, _StaleAudioStats] = {}
        self._context_sequence = 0
        self._delivery = _OrderedAudioDelivery(
            on_audio,
            self._delivery_terminal,
            max_bytes=options.delivery_queue_max_bytes,
            max_chunks=options.delivery_queue_max_chunks,
        )

    @property
    def active_token(self) -> PlaybackToken | None:
        context = self._active_context
        return context.token if context and not context.retired else None

    @property
    def active_context_id(self) -> str | None:
        context = self._active_context
        return context.context_id if context and not context.retired else None

    async def begin_turn(self, token: PlaybackToken) -> None:
        await self.connect()
        old_context: _CartesiaContext | None = None
        async with self._state_lock:
            if self._active_context and self._active_context.token == token and not self._active_context.retired:
                return
            if self._active_context and not self._active_context.retired:
                old_context = self._active_context
                old_context.retired = True
                self._contexts.pop(old_context.context_id, None)
            context = self._new_context(token)
            self._active_context = context
        if old_context is not None:
            self._delivery.cancel(old_context.delivery_identity)
            await self._cancel_context_best_effort(old_context, "superseded")
        await self._emit(
            "tts.turn.begin",
            provider="cartesia",
            token=token.generation,
            generation=token.generation,
            turn_id=token.turn_id,
            context_id=context.context_id,
        )

    async def append_text(self, token: PlaybackToken, text: str) -> None:
        if not text:
            return
        async with self._operation_lock:
            context, retired = await self._context_for_append(token)
            if retired is not None:
                await self._cancel_context_best_effort(retired, "idle_expiry_guard")
                await self._emit(
                    "tts.context.rotate",
                    provider="cartesia",
                    turn_id=token.turn_id,
                    old_context_id=retired.context_id,
                    context_id=context.context_id,
                    reason="idle_expiry_guard",
                )
            joined = self._join_continuation(context, text)
            await self._send_payloads(self._generation_payload(context, joined, continue_=True))
            async with self._state_lock:
                if not context.retired:
                    context.has_input = True
                    context.last_text_char = joined[-1:] or context.last_text_char
                    context.last_provider_activity_at = self._clock()
        await self._emit(
            "tts.text.append",
            provider="cartesia",
            turn_id=token.turn_id,
            context_id=context.context_id,
            chars=len(text),
        )

    async def finish_turn(self, token: PlaybackToken) -> None:
        async with self._operation_lock:
            context = await self._require_context(token)
            if context.finishing:
                return
            await self._send_payloads(self._generation_payload(context, "", continue_=False))
            async with self._state_lock:
                if not context.retired:
                    context.finishing = True
        await self._emit(
            "tts.turn.finish",
            provider="cartesia",
            turn_id=token.turn_id,
            generation=token.generation,
            playback_generation=token.generation,
            context_id=context.context_id,
        )

    async def cancel_turn(self, token: PlaybackToken, reason: str) -> None:
        async with self._state_lock:
            context = self._active_context
            if context is None or context.token != token or context.retired:
                return
            # Context-ID routing makes this a complete local fence even though
            # Cartesia may keep streaming a request that already started.
            context.retired = True
            self._active_context = None
            self._contexts.pop(context.context_id, None)
        self._delivery.cancel(context.delivery_identity)
        await self._emit(
            "tts.turn.cancel",
            provider="cartesia",
            turn_id=token.turn_id,
            context_id=context.context_id,
            reason=reason,
        )
        async with self._operation_lock:
            await self._cancel_context_best_effort(context, reason)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._state_lock:
            contexts = list(self._contexts.values())
            for context in contexts:
                context.retired = True
            self._active_context = None
            self._contexts.clear()
            stale_context_ids = list(self._stale_audio)
        for context in contexts:
            self._delivery.cancel(context.delivery_identity)
        for context_id in stale_context_ids:
            await self._flush_stale_audio(context_id, reason="provider_close")
        await self._shutdown_socket()
        await self._delivery.close()

    def _new_context(self, token: PlaybackToken) -> _CartesiaContext:
        self._context_sequence += 1
        context_id = self._context_id_factory()
        context = _CartesiaContext(
            context_id=context_id,
            token=token,
            delivery_identity=_DeliveryIdentity(
                provider="cartesia",
                token=token,
                stream_id=self._context_sequence,
                context_id=context_id,
            ),
        )
        self._contexts[context.context_id] = context
        return context

    async def _context_for_append(
        self, token: PlaybackToken
    ) -> tuple[_CartesiaContext, _CartesiaContext | None]:
        async with self._state_lock:
            context = self._active_context
            if context is None or context.token != token or context.retired or context.finishing:
                raise StalePlaybackToken(f"inactive Cartesia generation {token.generation}")
            last_activity_at = context.last_provider_activity_at
            if (
                last_activity_at is None
                or self._clock() - last_activity_at < self.options.context_idle_rotate_sec
            ):
                return context, None
            context.retired = True
            self._contexts.pop(context.context_id, None)
            # This is a continuation of the same playback generation. Audio
            # already admitted from the expiring context remains valid and is
            # delivered before bytes from the replacement context.
            replacement = self._new_context(token)
            self._active_context = replacement
            return replacement, context

    async def _require_context(self, token: PlaybackToken) -> _CartesiaContext:
        async with self._state_lock:
            context = self._active_context
            if context is None or context.token != token or context.retired:
                raise StalePlaybackToken(f"inactive Cartesia generation {token.generation}")
            return context

    async def _cancel_context_best_effort(self, context: _CartesiaContext, reason: str) -> None:
        try:
            await self._send_payloads({"context_id": context.context_id, "cancel": True})
        except TTSProviderError as exc:
            await self._emit(
                "tts.context.cancel_error",
                provider="cartesia",
                context_id=context.context_id,
                reason=reason,
                error=repr(exc),
            )
            # Local context retirement is already authoritative. Re-establish
            # the conversation socket in the background cancellation task so
            # the next real turn does not pay a lazy reconnect penalty.
            try:
                await self.connect()
            except TTSProviderError as reconnect_exc:
                await self._emit(
                    "tts.transport.reconnect_error",
                    provider="cartesia",
                    reason=reason,
                    error=repr(reconnect_exc),
                )
            else:
                await self._emit(
                    "tts.transport.recovered",
                    provider="cartesia",
                    reason=reason,
                )

    def _generation_payload(
        self, context: _CartesiaContext, transcript: str, *, continue_: bool
    ) -> dict[str, Any]:
        return {
            "model_id": self.options.model,
            "transcript": transcript,
            "context_id": context.context_id,
            "voice": {"mode": "id", "id": self.options.voice_id},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self.options.sample_rate,
            },
            "language": self.options.language,
            "continue": continue_,
        }

    @staticmethod
    def _join_continuation(context: _CartesiaContext, text: str) -> str:
        if not context.has_input or not text:
            return text
        first = text[0]
        punctuation = ",.;:!?)]}"
        if (
            context.last_text_char
            and not context.last_text_char.isspace()
            and not first.isspace()
            and first not in punctuation
        ):
            return " " + text
        return text

    async def _consume_messages(self, websocket: Any, epoch: int) -> None:
        async for message in websocket:
            if websocket is not self._websocket or epoch != self._connection_epoch or not isinstance(message, str):
                continue
            payload = json.loads(message)
            message_type = str(payload.get("type") or "")
            context_id = str(payload.get("context_id") or "")
            async with self._state_lock:
                context = self._contexts.get(context_id)
                active = (
                    context is not None
                    and not context.retired
                    and self._active_context is context
                )
            if message_type == "chunk" and payload.get("data"):
                audio = base64.b64decode(payload["data"])
                if not active or context is None:
                    await self._record_stale_audio(context_id, len(audio))
                    continue
                async with self._state_lock:
                    if self._active_context is context and not context.retired:
                        context.last_provider_activity_at = self._clock()
                try:
                    accepted = self._delivery.enqueue_audio(context.delivery_identity, audio)
                except _DeliveryOverflow:
                    async with self._state_lock:
                        failed_context = self._contexts.pop(context_id, None)
                        if failed_context is not None:
                            failed_context.retired = True
                        if self._active_context is failed_context:
                            self._active_context = None
                    self._queue_terminal(
                        context.delivery_identity,
                        "failed",
                        error_code="delivery_overflow",
                    )
                    raise
                if not accepted:
                    await self._record_stale_audio(context_id, len(audio))
                continue
            if message_type == "done" or payload.get("done"):
                async with self._state_lock:
                    done_context = self._contexts.pop(context_id, None)
                    if done_context is not None and self._active_context is done_context:
                        self._active_context = None
                if done_context is not None and not done_context.retired:
                    self._queue_terminal(done_context.delivery_identity, "done")
                await self._flush_stale_audio(context_id, reason="context_done")
            elif message_type == "error":
                async with self._state_lock:
                    failed_context = self._contexts.pop(context_id, None)
                    if failed_context is None and not context_id:
                        failed_context = self._active_context
                        if failed_context is not None:
                            self._contexts.pop(failed_context.context_id, None)
                    should_fail = failed_context is not None and not failed_context.retired
                    if failed_context is not None:
                        failed_context.retired = True
                    if failed_context is not None and self._active_context is failed_context:
                        self._active_context = None
                if failed_context is not None and should_fail:
                    self._queue_terminal(
                        failed_context.delivery_identity,
                        "failed",
                        error_code="provider_error",
                    )
            await self._emit(
                "tts.provider.message",
                provider="cartesia",
                message_type=message_type,
                context_id=context_id,
                raw=payload,
            )

    async def _on_socket_lost(
        self,
        *,
        error_code: str,
        close_code: int | None,
    ) -> None:
        async with self._state_lock:
            failed_contexts = [context for context in self._contexts.values() if not context.retired]
            for context in self._contexts.values():
                context.retired = True
            self._active_context = None
            self._contexts.clear()
            stale_context_ids = list(self._stale_audio)
        for context in failed_contexts:
            self._queue_terminal(
                context.delivery_identity,
                "failed",
                error_code=error_code,
                close_code=close_code,
            )
        for context_id in stale_context_ids:
            await self._flush_stale_audio(context_id, reason="socket_lost")

    def _queue_terminal(
        self,
        identity: _DeliveryIdentity,
        kind: str,
        *,
        error_code: str | None = None,
        close_code: int | None = None,
    ) -> None:
        self._delivery.enqueue_terminal(
            identity,
            kind,
            error_code=error_code,
            close_code=close_code,
        )

    async def _delivery_terminal(
        self,
        identity: _DeliveryIdentity,
        kind: str,
        error_code: str | None,
        close_code: int | None,
    ) -> None:
        async with self._state_lock:
            context = self._contexts.get(identity.context_id or "")
            if context is not None and context.delivery_identity == identity:
                self._contexts.pop(context.context_id, None)
                if self._active_context is context:
                    self._active_context = None
        detail: dict[str, Any] = {
            "provider": "cartesia",
            "turn_id": identity.token.turn_id,
            "generation": identity.token.generation,
            "playback_generation": identity.token.generation,
            "context_id": identity.context_id,
        }
        if kind == "done":
            await self._emit("tts.turn.done", **detail)
            return
        detail["error_code"] = error_code or "provider_transport_error"
        if close_code is not None:
            detail["close_code"] = close_code
        await self._emit("tts.turn.failed", **detail)

    async def _flush_stale_audio(self, context_id: str, *, reason: str) -> None:
        async with self._state_lock:
            stats = self._stale_audio.pop(context_id, None)
        if stats is None:
            return
        await self._emit_stale_audio_summary(context_id, stats, reason=reason)

    async def _record_stale_audio(self, context_id: str, size: int) -> None:
        evicted: tuple[str, _StaleAudioStats] | None = None
        async with self._state_lock:
            stats = self._stale_audio.get(context_id)
            if stats is None:
                if len(self._stale_audio) >= self._STALE_AUDIO_CONTEXT_LIMIT:
                    evicted_context_id = next(iter(self._stale_audio))
                    evicted = (
                        evicted_context_id,
                        self._stale_audio.pop(evicted_context_id),
                    )
                stats = _StaleAudioStats(started_at=self._clock())
                self._stale_audio[context_id] = stats
            stats.chunks += 1
            stats.bytes += size
        if evicted is not None:
            await self._emit_stale_audio_summary(
                evicted[0],
                evicted[1],
                reason="summary_capacity",
            )

    async def _emit_stale_audio_summary(
        self,
        context_id: str,
        stats: _StaleAudioStats,
        *,
        reason: str,
    ) -> None:
        await self._emit(
            "tts.stale_audio.summary",
            provider="cartesia",
            context_id=context_id,
            chunks=stats.chunks,
            bytes=stats.bytes,
            duration_ms=max(0, int((self._clock() - stats.started_at) * 1000)),
            reason=reason,
        )
