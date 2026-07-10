from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets

from opencode_voice.deepgram import build_flux_url, parse_flux_message

WebSocketConnector = Callable[[str, dict[str, str]], Awaitable[Any]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FluxTransportOptions:
    api_key: str = field(repr=False)
    model: str = "flux-general-en"
    sample_rate: int = 16_000
    packet_ms: int = 80
    eot_threshold: float = 0.7
    eot_timeout_ms: int = 5_000
    eager_eot_threshold: float | None = None
    send_timeout_sec: float = 0.5
    connect_timeout_sec: float = 5.0
    max_fresh_audio_ms: int = 500
    reconnect_backoff_sec: tuple[float, ...] = (0.2, 0.5, 1.0, 2.0)


@dataclass(frozen=True, slots=True)
class FluxHealthSnapshot:
    state: str
    epoch: int
    queued_packets: int
    queued_audio_ms: int
    partial_bytes: int
    submitted_packets: int
    sent_packets: int
    dropped_stale_packets: int
    dropped_overflow_packets: int
    dropped_uncertain_packets: int
    send_failures: int
    connect_failures: int
    last_capture_at: float | None
    last_send_at: float | None
    last_receive_at: float | None
    last_error: str | None
    oldest_queue_age_ms: int


@dataclass(frozen=True, slots=True)
class _AudioPacket:
    data: bytes
    captured_at: float


async def connect_flux_websocket(url: str, headers: dict[str, str]) -> Any:
    kwargs: dict[str, Any] = {
        "open_timeout": 5,
        "close_timeout": 1,
        # Keepalive is a backstop for genuinely half-open sockets. Capture
        # traffic has its own 500 ms send deadline and watchdogs, so a tight
        # Pong deadline only turns ordinary network jitter into reconnects.
        "ping_interval": 20,
        "ping_timeout": 20,
        # Race IPv6 and IPv4 instead of trusting resolver order. Some VPNs and
        # routers advertise an IPv6 route that blackholes TCP; without Happy
        # Eyeballs, a healthy IPv4 provider can appear offline for minutes.
        "happy_eyeballs_delay": 0.25,
        "interleave": 1,
    }
    try:
        return await websockets.connect(url, additional_headers=headers, **kwargs)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers, **kwargs)


class FluxTransport:
    """Bounded, reconnecting Deepgram Flux audio transport.

    ``submit`` is deliberately synchronous and never performs network I/O. It
    must be called on the transport's asyncio loop (audio worker threads should
    use ``loop.call_soon_threadsafe``). Arbitrary PCM16 chunks are packetized
    into the 80 ms frames recommended by Flux.
    """

    BYTES_PER_SAMPLE = 2

    def __init__(
        self,
        options: FluxTransportOptions,
        on_event: EventCallback,
        *,
        connector: WebSocketConnector = connect_flux_websocket,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        samples_numerator = options.sample_rate * options.packet_ms
        if samples_numerator % 1000:
            raise ValueError("sample_rate * packet_ms must produce an integral PCM frame")
        if options.packet_ms <= 0 or options.max_fresh_audio_ms < options.packet_ms:
            raise ValueError("invalid Flux packet or freshness duration")
        if not options.reconnect_backoff_sec:
            raise ValueError("at least one reconnect backoff is required")
        self.options = options
        self._on_event = on_event
        self._connector = connector
        self._clock = clock
        self._sleep = sleep
        samples_per_packet = samples_numerator // 1000
        self.packet_bytes = samples_per_packet * self.BYTES_PER_SAMPLE
        self.max_packets = max(1, options.max_fresh_audio_ms // options.packet_ms)
        self.url = build_flux_url(
            model=options.model,
            sample_rate=options.sample_rate,
            eot_threshold=options.eot_threshold,
            eot_timeout_ms=options.eot_timeout_ms,
            eager_eot_threshold=options.eager_eot_threshold,
        )
        self.headers = {"Authorization": f"Token {options.api_key}"}

        self._partial = bytearray()
        self._packets: deque[_AudioPacket] = deque()
        self._work_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._run_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._websocket: Any = None
        self._reader_failure: tuple[int, str] | None = None
        self._closed = False
        self._state = "idle"
        self._epoch = 0
        self._backoff_index = 0
        self._needs_backoff = False
        self._epoch_has_success = False

        self._submitted_packets = 0
        self._sent_packets = 0
        self._dropped_stale_packets = 0
        self._dropped_overflow_packets = 0
        self._dropped_uncertain_packets = 0
        self._send_failures = 0
        self._connect_failures = 0
        self._last_capture_at: float | None = None
        self._last_send_at: float | None = None
        self._last_receive_at: float | None = None
        self._last_error: str | None = None

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Flux transport is closed")
        if self.running:
            return
        self._run_task = asyncio.create_task(self._run())

    async def wait_connected(self, timeout_sec: float | None = None) -> int:
        """Wait until a real Flux socket epoch exists, never actor startup."""
        waiter = self._connected_event.wait()
        if timeout_sec is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout=timeout_sec)
        if self._closed or self._websocket is None:
            raise ConnectionError("Flux transport closed before becoming ready")
        return self._epoch

    def submit(self, data: bytes, captured_at: float | None = None) -> bool:
        """Accept PCM16 without waiting; newest speech wins under congestion."""

        if self._closed:
            return False
        if len(data) % self.BYTES_PER_SAMPLE:
            raise ValueError("PCM16 data must contain complete samples")
        if not data:
            return True
        now = self._clock()
        stamp = now if captured_at is None else captured_at
        self._last_capture_at = stamp
        self._partial.extend(data)
        produced = False
        while len(self._partial) >= self.packet_bytes:
            packet_data = bytes(self._partial[: self.packet_bytes])
            del self._partial[: self.packet_bytes]
            self._prune_stale(now)
            while len(self._packets) >= self.max_packets:
                self._packets.popleft()
                self._dropped_overflow_packets += 1
            self._packets.append(_AudioPacket(packet_data, stamp))
            self._submitted_packets += 1
            produced = True
        if produced:
            self._work_event.set()
        return True

    def health_snapshot(self) -> FluxHealthSnapshot:
        now = self._clock()
        return FluxHealthSnapshot(
            state=self._state,
            epoch=self._epoch,
            queued_packets=len(self._packets),
            queued_audio_ms=len(self._packets) * self.options.packet_ms,
            partial_bytes=len(self._partial),
            submitted_packets=self._submitted_packets,
            sent_packets=self._sent_packets,
            dropped_stale_packets=self._dropped_stale_packets,
            dropped_overflow_packets=self._dropped_overflow_packets,
            dropped_uncertain_packets=self._dropped_uncertain_packets,
            send_failures=self._send_failures,
            connect_failures=self._connect_failures,
            last_capture_at=self._last_capture_at,
            last_send_at=self._last_send_at,
            last_receive_at=self._last_receive_at,
            last_error=self._last_error,
            oldest_queue_age_ms=(
                max(0, int((now - self._packets[0].captured_at) * 1000)) if self._packets else 0
            ),
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._state = "closed"
        self._connected_event.set()
        self._work_event.set()
        task = self._run_task
        self._run_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._disconnect()
        self._packets.clear()
        self._partial.clear()

    async def _run(self) -> None:
        try:
            while not self._closed:
                if self._websocket is None:
                    if self._needs_backoff:
                        self._state = "reconnecting"
                        delay = self.options.reconnect_backoff_sec[
                            min(self._backoff_index, len(self.options.reconnect_backoff_sec) - 1)
                        ]
                        self._backoff_index = min(
                            self._backoff_index + 1,
                            len(self.options.reconnect_backoff_sec) - 1,
                        )
                        await self._emit("flux.transport.reconnect_wait", delay_sec=delay)
                        await self._sleep(delay)
                        self._needs_backoff = False
                        if self._closed:
                            break
                    try:
                        await self._connect_once()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - retry loop owns recovery.
                        self._connect_failures += 1
                        self._last_error = repr(exc)
                        self._needs_backoff = True
                        response = getattr(exc, "response", None)
                        await self._emit(
                            "flux.transport.connect_error",
                            error_code=type(exc).__name__,
                            status_code=getattr(response, "status_code", None),
                        )
                        continue

                failure = self._reader_failure
                if failure is not None and failure[0] == self._epoch:
                    self._reader_failure = None
                    self._last_error = failure[1]
                    await self._emit("flux.transport.read_error", epoch=self._epoch, error=failure[1])
                    await self._disconnect()
                    self._needs_backoff = True
                    continue

                packet = self._pop_fresh()
                if packet is None:
                    self._work_event.clear()
                    # Recheck after clear so a submit/read failure cannot lose
                    # its wake-up between the first check and Event.wait().
                    if self._packets or self._reader_failure is not None:
                        self._work_event.set()
                        continue
                    await self._work_event.wait()
                    continue

                websocket = self._websocket
                if websocket is None:
                    continue
                try:
                    await asyncio.wait_for(
                        websocket.send(packet.data),
                        timeout=self.options.send_timeout_sec,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # uncertain writes are never replayed.
                    self._send_failures += 1
                    self._dropped_uncertain_packets += 1
                    self._last_error = repr(exc)
                    await self._emit(
                        "flux.transport.send_error",
                        epoch=self._epoch,
                        error_code=type(exc).__name__,
                    )
                    await self._disconnect()
                    self._needs_backoff = True
                    continue
                self._sent_packets += 1
                self._last_send_at = self._clock()
                if not self._epoch_has_success:
                    self._epoch_has_success = True
                    await self._emit("flux.transport.send_ok", epoch=self._epoch)
                # A successful audio write proves the new connection survived,
                # so a later outage starts again at the shortest backoff.
                self._backoff_index = 0
        finally:
            await self._disconnect()

    async def _connect_once(self) -> None:
        self._state = "connecting"
        websocket = await asyncio.wait_for(
            self._connector(self.url, self.headers),
            timeout=self.options.connect_timeout_sec,
        )
        if self._closed:
            await self._close_websocket(websocket)
            return
        self._epoch += 1
        epoch = self._epoch
        self._websocket = websocket
        self._epoch_has_success = False
        self._reader_failure = None
        self._reader_task = asyncio.create_task(self._read_loop(websocket, epoch))
        self._state = "connected"
        self._connected_event.set()
        await self._emit("flux.transport.connected", epoch=epoch)

    async def _read_loop(self, websocket: Any, epoch: int) -> None:
        error = "websocket_closed"
        try:
            async for message in websocket:
                if websocket is not self._websocket or epoch != self._epoch:
                    continue
                self._last_receive_at = self._clock()
                if not isinstance(message, str):
                    continue
                try:
                    event = parse_flux_message(message)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    await self._emit("flux.transport.protocol_error", epoch=epoch, error=repr(exc))
                    continue
                raw = event.get("raw")
                if isinstance(raw, dict) and isinstance(raw.get("turn_index"), int):
                    event["turn_index"] = raw["turn_index"]
                event["transport_epoch"] = epoch
                await self._emit_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - manager reconnects the socket.
            error = repr(exc)
        finally:
            if not self._closed and websocket is self._websocket and epoch == self._epoch:
                self._reader_failure = (epoch, error)
                self._work_event.set()

    async def _disconnect(self) -> None:
        websocket = self._websocket
        reader = self._reader_task
        self._websocket = None
        self._connected_event.clear()
        self._reader_task = None
        if not self._closed and self._state != "reconnecting":
            self._state = "disconnected"
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

    def _pop_fresh(self) -> _AudioPacket | None:
        self._prune_stale(self._clock())
        return self._packets.popleft() if self._packets else None

    def _prune_stale(self, now: float) -> None:
        cutoff = now - self.options.max_fresh_audio_ms / 1000
        dropped = 0
        while self._packets and self._packets[0].captured_at < cutoff:
            self._packets.popleft()
            dropped += 1
        if dropped:
            self._dropped_stale_packets += dropped

    async def _emit(self, event_type: str, **detail: Any) -> None:
        await self._emit_event({"type": event_type, **detail})

    async def _emit_event(self, event: dict[str, Any]) -> None:
        try:
            await self._on_event(event)
        except Exception:
            # Application diagnostics must not terminate capture/reconnect.
            pass
