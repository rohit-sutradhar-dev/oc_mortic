from __future__ import annotations

import os
from typing import Callable, Any, Awaitable

from opencode_voice.config import VoiceConfig
from opencode_voice.flux_transport import FluxTransport, FluxTransportOptions


class DeepgramSTTProvider:
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
