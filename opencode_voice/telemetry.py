from __future__ import annotations

"""Privacy-safe telemetry contracts for the Mortic voice helper.

This module deliberately contains no transport or server behavior.  It defines
the stable identifiers and timing vocabulary that the audio, STT, TTS, and
interruption paths can share without logging transcript content, provider
payloads, URLs, credentials, or exception messages.
"""

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


TELEMETRY_SCHEMA_VERSION = "mortic.telemetry.v1"
BUILD_SHA_ENV = "MORTIC_BUILD_SHA"
NETWORK_PROFILE_ENV = "MORTIC_NETWORK_PROFILE"

_BUILD_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def _safe_label(value: object, *, default: str = "unknown") -> str:
    """Keep known-safe categorical labels and discard arbitrary content."""

    text = str(value or "").strip()
    return text if _SAFE_LABEL_RE.fullmatch(text) else default


def resolve_build_sha(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Return a validated build SHA without ever logging command failures.

    Release builds can set ``MORTIC_BUILD_SHA``.  Source checkouts fall back to
    ``git rev-parse`` with a short timeout.  Invalid environment values and all
    command output/errors resolve to ``unknown`` rather than entering logs.
    """

    target = environ if environ is not None else os.environ
    configured = str(target.get(BUILD_SHA_ENV) or "").strip()
    if configured:
        return configured.lower() if _BUILD_SHA_RE.fullmatch(configured) else "unknown"
    try:
        completed = runner(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=0.5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    candidate = str(completed.stdout or "").strip()
    if completed.returncode == 0 and _BUILD_SHA_RE.fullmatch(candidate):
        return candidate.lower()
    return "unknown"


def helper_version() -> str:
    try:
        return importlib.metadata.version("mortic-helper")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class TelemetryConfigSnapshot:
    """Explicit allow-list of configuration values safe to persist."""

    tts_provider: str
    duplex_mode: str
    capture_sample_rate_hz: int
    playback_sample_rate_hz: int
    stt_sample_rate_hz: int
    tts_sample_rate_hz: int
    mic_queue_blocks: int
    playback_queue_chunks: int
    jitter_buffer_target_ms: int
    network_profile: str

    def __post_init__(self) -> None:
        positive = (
            "capture_sample_rate_hz",
            "playback_sample_rate_hz",
            "stt_sample_rate_hz",
            "tts_sample_rate_hz",
            "mic_queue_blocks",
            "playback_queue_chunks",
        )
        for name in positive:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.jitter_buffer_target_ms, bool)
            or not isinstance(self.jitter_buffer_target_ms, int)
            or self.jitter_buffer_target_ms < 0
        ):
            raise ValueError("jitter_buffer_target_ms must be a non-negative integer")
        for name in ("tts_provider", "duplex_mode", "network_profile"):
            if _safe_label(getattr(self, name)) != getattr(self, name):
                raise ValueError(f"{name} must be a safe categorical label")

    def as_dict(self) -> dict[str, int | str]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def snapshot_voice_config(
    config: object,
    *,
    capture_sample_rate_hz: int | None = None,
    playback_sample_rate_hz: int | None = None,
    stt_sample_rate_hz: int | None = None,
    tts_sample_rate_hz: int | None = None,
    mic_queue_blocks: int = 64,
    playback_queue_chunks: int = 256,
    jitter_buffer_target_ms: int = 0,
    network_profile: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> TelemetryConfigSnapshot:
    """Project a VoiceConfig-like object onto the telemetry allow-list."""

    stt_rate = int(getattr(config, "deepgram_sample_rate", 16_000))
    tts_rate = int(getattr(config, "tts_sample_rate", stt_rate))
    device_rate = int(getattr(config, "device_sample_rate", stt_rate))
    target = environ if environ is not None else os.environ
    profile = network_profile if network_profile is not None else target.get(NETWORK_PROFILE_ENV, "uncontrolled")
    return TelemetryConfigSnapshot(
        tts_provider=_safe_label(getattr(config, "tts_provider", "unknown")),
        duplex_mode=_safe_label(getattr(config, "voice_duplex", "unknown")),
        capture_sample_rate_hz=device_rate if capture_sample_rate_hz is None else capture_sample_rate_hz,
        playback_sample_rate_hz=device_rate if playback_sample_rate_hz is None else playback_sample_rate_hz,
        stt_sample_rate_hz=stt_rate if stt_sample_rate_hz is None else stt_sample_rate_hz,
        tts_sample_rate_hz=tts_rate if tts_sample_rate_hz is None else tts_sample_rate_hz,
        mic_queue_blocks=mic_queue_blocks,
        playback_queue_chunks=playback_queue_chunks,
        jitter_buffer_target_ms=jitter_buffer_target_ms,
        network_profile=_safe_label(profile, default="uncontrolled"),
    )


@dataclass(frozen=True)
class RunMetadata:
    build_sha: str
    helper_version: str
    config: TelemetryConfigSnapshot
    telemetry_schema: str = TELEMETRY_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        config: TelemetryConfigSnapshot,
        *,
        build_sha: str | None = None,
        version: str | None = None,
        cwd: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "RunMetadata":
        candidate = build_sha.lower() if build_sha and _BUILD_SHA_RE.fullmatch(build_sha) else None
        return cls(
            build_sha=candidate or resolve_build_sha(environ=environ, cwd=cwd),
            helper_version=_safe_label(version or helper_version()),
            config=config,
        )

    def as_fields(self) -> dict[str, Any]:
        return {
            "telemetry_schema": self.telemetry_schema,
            "build_sha": self.build_sha,
            "helper_version": self.helper_version,
            "config_fingerprint": self.config.fingerprint,
            "voice_config": self.config.as_dict(),
        }


class RunClock:
    """Process-local monotonic clock used by every JSONL event."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._started = monotonic()
        self._last_elapsed_ms = 0
        self._lock = threading.Lock()

    def elapsed_ms(self) -> int:
        elapsed = max(0, int((self._monotonic() - self._started) * 1000))
        with self._lock:
            # Defensive clamp: monotonic clocks should not regress, but keeping
            # the log contract intact is more useful than exposing a host bug.
            self._last_elapsed_ms = max(self._last_elapsed_ms, elapsed)
            return self._last_elapsed_ms


CORRELATION_FIELDS = (
    "voice_lane_id",
    "turn_id",
    "flux_epoch",
    "flux_turn_index",
    "stt_episode_id",
    "interruption_episode_id",
    "playback_generation",
    "playback_burst_id",
    "provider_request_id",
    "provider_context_id",
)

_CORRELATION_ID_FIELDS = tuple(
    name for name in CORRELATION_FIELDS if name not in {"flux_epoch", "flux_turn_index", "playback_generation"}
)

CORRELATION_PROFILES: dict[str, tuple[str, ...]] = {
    "stt": ("flux_epoch", "flux_turn_index", "stt_episode_id"),
    "interruption": ("stt_episode_id", "interruption_episode_id", "playback_generation"),
    "playback": ("playback_generation", "playback_burst_id"),
    "provider_request": ("playback_generation", "provider_request_id"),
    "provider_context": ("playback_generation", "provider_request_id", "provider_context_id"),
}


@dataclass(frozen=True)
class CorrelationContext:
    voice_lane_id: str | None = None
    turn_id: str | None = None
    flux_epoch: int | None = None
    flux_turn_index: int | None = None
    stt_episode_id: str | None = None
    interruption_episode_id: str | None = None
    playback_generation: int | None = None
    playback_burst_id: str | None = None
    provider_request_id: str | None = None
    provider_context_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("flux_epoch", "flux_turn_index", "playback_generation"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        for name in _CORRELATION_ID_FIELDS:
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value)):
                raise ValueError(f"{name} must be an opaque safe identifier")

    def as_fields(self) -> dict[str, int | str]:
        return {
            field.name: value
            for field in fields(self)
            if (value := getattr(self, field.name)) is not None
        }

    def with_updates(self, **updates: int | str | None) -> "CorrelationContext":
        unknown = set(updates).difference(CORRELATION_FIELDS)
        if unknown:
            raise ValueError(f"unknown correlation fields: {', '.join(sorted(unknown))}")
        return replace(self, **updates)

    def missing_for(self, profile: str) -> tuple[str, ...]:
        try:
            required = CORRELATION_PROFILES[profile]
        except KeyError as exc:
            raise ValueError(f"unknown correlation profile: {profile}") from exc
        return tuple(name for name in required if getattr(self, name) is None)


@dataclass(frozen=True)
class LatencyDefinition:
    name: str
    start_phase: str
    end_phase: str
    description: str


LATENCY_DEFINITIONS = (
    LatencyDefinition(
        "speech_to_first_transcript_ms",
        "speech_started",
        "first_transcript",
        "Acoustic/STT episode start to the first non-empty transcript.",
    ),
    LatencyDefinition(
        "end_of_turn_to_first_assistant_text_ms",
        "end_of_turn",
        "first_assistant_text",
        "Committed STT end-of-turn to the first assistant text delta.",
    ),
    LatencyDefinition(
        "assistant_text_to_tts_first_audio_ms",
        "first_assistant_text",
        "tts_first_audio",
        "First assistant text to the first provider audio received.",
    ),
    LatencyDefinition(
        "end_of_turn_to_playback_ms",
        "end_of_turn",
        "playback_started",
        "Committed STT end-of-turn to the first device-clock playback frame.",
    ),
    LatencyDefinition(
        "interruption_candidate_to_pause_ms",
        "interruption_candidate",
        "playback_paused",
        "Interruption candidate event to local playback pause/duck.",
    ),
    LatencyDefinition(
        "interruption_commit_to_stop_ms",
        "interruption_committed",
        "playback_stopped",
        "Committed interruption to the final stale playback frame.",
    ),
)

PHASE_ORDERS: dict[str, tuple[str, ...]] = {
    "turn": (
        "speech_started",
        "first_transcript",
        "end_of_turn",
        "turn_committed",
        "first_assistant_text",
        "tts_requested",
        "tts_first_audio",
        "playback_started",
        "playback_drained",
        "turn_completed",
    ),
    "interruption": (
        "interruption_candidate",
        "playback_paused",
        "interruption_committed",
        "playback_stopped",
    ),
}


@dataclass(frozen=True)
class PhaseOrderViolation:
    sequence: str
    earlier_phase: str
    later_phase: str
    earlier_ms: int
    later_ms: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "sequence": self.sequence,
            "earlier_phase": self.earlier_phase,
            "later_phase": self.later_phase,
            "earlier_ms": self.earlier_ms,
            "later_ms": self.later_ms,
        }


def _validated_phase_times(phase_times: Mapping[str, int]) -> dict[str, int]:
    validated: dict[str, int] = {}
    for phase, value in phase_times.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"phase {phase} must be a non-negative integer run_elapsed_ms")
        validated[str(phase)] = value
    return validated


def validate_phase_order(
    phase_times: Mapping[str, int],
    *,
    phase_orders: Mapping[str, Sequence[str]] = PHASE_ORDERS,
) -> tuple[PhaseOrderViolation, ...]:
    times = _validated_phase_times(phase_times)
    violations: list[PhaseOrderViolation] = []
    for sequence_name, ordered_phases in phase_orders.items():
        previous_phase: str | None = None
        previous_ms: int | None = None
        for phase in ordered_phases:
            if phase not in times:
                continue
            current_ms = times[phase]
            if previous_phase is not None and previous_ms is not None and current_ms < previous_ms:
                violations.append(
                    PhaseOrderViolation(
                        sequence=sequence_name,
                        earlier_phase=previous_phase,
                        later_phase=phase,
                        earlier_ms=previous_ms,
                        later_ms=current_ms,
                    )
                )
            if previous_ms is None or current_ms >= previous_ms:
                previous_phase = phase
                previous_ms = current_ms
    return tuple(violations)


def derive_latencies(
    phase_times: Mapping[str, int],
    *,
    definitions: Iterable[LatencyDefinition] = LATENCY_DEFINITIONS,
) -> dict[str, int]:
    times = _validated_phase_times(phase_times)
    violations = validate_phase_order(times)
    if violations:
        first = violations[0]
        raise ValueError(
            f"invalid phase order in {first.sequence}: "
            f"{first.later_phase} ({first.later_ms}) precedes "
            f"{first.earlier_phase} ({first.earlier_ms})"
        )
    derived: dict[str, int] = {}
    for definition in definitions:
        if definition.start_phase in times and definition.end_phase in times:
            elapsed = times[definition.end_phase] - times[definition.start_phase]
            if elapsed >= 0:
                derived[definition.name] = elapsed
    return derived


PROVIDER_STAGES = {
    "connect",
    "send",
    "receive",
    "synthesize",
    "cancel",
    "close",
    "timeout",
    "protocol",
}


def safe_provider_error(
    *,
    provider: str,
    stage: str,
    code: str,
    retryable: bool,
    correlation: CorrelationContext | None = None,
    exception: BaseException | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    """Build an actionable provider error without content-bearing fields."""

    safe_stage = stage if isinstance(stage, str) and stage in PROVIDER_STAGES else "unknown"
    safe_code = code if isinstance(code, str) and _SAFE_ERROR_CODE_RE.fullmatch(code) else "provider_error"
    record: dict[str, Any] = {
        "event": "provider.error",
        "provider": _safe_label(provider),
        "stage": safe_stage,
        "code": safe_code,
        "retryable": bool(retryable),
    }
    if exception is not None:
        record["exception_type"] = _safe_label(type(exception).__name__)
    if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599:
        record["http_status"] = http_status
    if correlation is not None:
        record.update(correlation.as_fields())
    return record
