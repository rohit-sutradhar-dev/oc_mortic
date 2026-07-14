from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SpeechEvent:
    """Provider-neutral STT event. All STT adapters must emit this shape."""
    type: str  # "speech.start" | "speech.transcript" | "speech.end" | "speech.resumed"
    transcript: str = ""
    is_final: bool = False
    eager: bool = False  # True when type=="speech.end" and it was an eager EoT
    confidence: float | None = None
    turn_index: int | None = None
    transport_epoch: int | None = None
    raw: dict[str, Any] = field(default_factory=dict) # original provider payload, never spoken

class STTProvider(Protocol):
    """
    Any STT provider must implement this interface.

    submit() is synchronous and non-blocking — call it from the audio thread
    via loop.call_soon_threadsafe. It must never do network I/O.

    Events are delivered by calling the on_event callback supplied at construction.
    The callback receives SpeechEvent, never provider-specific dicts.
    """
    async def start(self) -> None: ...
    def submit(self, pcm16: bytes, captured_at: float | None = None) -> bool: ...
    async def close(self) -> None: ...