from __future__ import annotations

"""Privacy-safe run metadata and monotonic timing for the voice helper."""

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable, Mapping


TELEMETRY_SCHEMA_VERSION = "mortic.telemetry.v1"
BUILD_SHA_ENV = "MORTIC_BUILD_SHA"
NETWORK_PROFILE_ENV = "MORTIC_NETWORK_PROFILE"

_BUILD_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


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
