from __future__ import annotations

import json
import urllib.parse
from typing import Any


def build_flux_url(
    model: str,
    sample_rate: int,
    eot_threshold: float,
    eot_timeout_ms: int,
    eager_eot_threshold: float | None = None,
) -> str:
    params: dict[str, str | int | float] = {
        "model": model,
        "encoding": "linear16",
        "sample_rate": sample_rate,
        "eot_threshold": eot_threshold,
        "eot_timeout_ms": eot_timeout_ms,
    }
    if eager_eot_threshold is not None:
        params["eager_eot_threshold"] = eager_eot_threshold
    return f"wss://api.deepgram.com/v2/listen?{urllib.parse.urlencode(params)}"


def build_tts_url(model: str, sample_rate: int) -> str:
    params = {
        "model": model,
        "encoding": "linear16",
        "sample_rate": sample_rate,
    }
    return f"wss://api.deepgram.com/v1/speak?{urllib.parse.urlencode(params)}"


def parse_flux_message(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    message_type = str(payload.get("type") or "")
    event = str(payload.get("event") or "")
    kind = event if message_type == "TurnInfo" and event else message_type or event
    transcript = str(payload.get("transcript") or "")
    channel = payload.get("channel")
    if isinstance(channel, dict):
        alternatives = channel.get("alternatives") or []
        if alternatives and isinstance(alternatives[0], dict):
            transcript = transcript or str(alternatives[0].get("transcript") or "")

    normalized = {
        "type": "deepgram.raw",
        "deepgram_type": kind,
        "transcript": transcript,
        "is_final": bool(payload.get("is_final") or payload.get("speech_final") or kind == "EndOfTurn"),
        "raw": payload,
    }
    # Flux supplies a stable index for all events belonging to one speech
    # episode.  Keep it at the normalized layer so reconnect fencing and the
    # interruption controller never have to inspect provider-shaped `raw`.
    turn_index = payload.get("turn_index")
    if isinstance(turn_index, int):
        normalized["turn_index"] = turn_index
    words = payload.get("words")
    if isinstance(words, list) and words:
        confidences = [
            float(word["confidence"])
            for word in words
            if isinstance(word, dict) and isinstance(word.get("confidence"), (int, float))
        ]
        if confidences:
            # Mean word confidence: clean speech scores high; echo the
            # canceller mangled transcribes as garbage with low scores.
            normalized["confidence"] = round(sum(confidences) / len(confidences), 3)
    lowered = kind.lower()
    if lowered == "startofturn":
        normalized["type"] = "speech.start"
    elif lowered in {"endofturn", "eagerendofturn"}:
        normalized["type"] = "speech.end"
        normalized["eager"] = lowered == "eagerendofturn"
    elif lowered == "turnresumed":
        normalized["type"] = "speech.resumed"
    elif transcript:
        normalized["type"] = "speech.transcript"
    return normalized
