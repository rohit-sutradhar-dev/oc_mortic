from __future__ import annotations

"""Config and credential loading for the Mortic helper.

V1 reads local development credentials from process environment, with optional
`.env` support for local runs. Future BYOK/proxy work should replace the
credential source behind `load_voice_credentials()` while keeping the same
capability/issue interface for helper readiness and sidepod-safe diagnostics.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, MutableMapping

VOICE_BRIDGE_USER_MESSAGE = "Voice Bridge Issue"
REDACTED = "[redacted]"
SENSITIVE_FIELD_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
}


@dataclass(frozen=True)
class CredentialSpec:
    env_var: str
    capability: str
    diagnostic_code: str
    safe_detail: str


REQUIRED_CREDENTIALS = (
    CredentialSpec(
        env_var="DEEPGRAM_API_KEY",
        capability="voice_audio",
        diagnostic_code="missing_voice_audio_key",
        safe_detail="Voice audio unavailable",
    ),
    CredentialSpec(
        env_var="INCEPTION_API_KEY",
        capability="voice_turns",
        diagnostic_code="missing_voice_turn_key",
        safe_detail="Voice turns unavailable",
    ),
)


@dataclass(frozen=True)
class VoiceCredentialIssue:
    capability: str
    diagnostic_code: str
    safe_detail: str
    retryable: bool = True

    def to_voice_bridge_issue(
        self,
        *,
        sent_at: str | None = None,
        debug_ref: str | None = None,
        voice_lane_id: str | None = None,
    ) -> dict[str, Any]:
        return voice_bridge_issue_payload(
            capability=self.capability,
            diagnostic_code=self.diagnostic_code,
            safe_detail=self.safe_detail,
            retryable=self.retryable,
            sent_at=sent_at,
            debug_ref=debug_ref,
            voice_lane_id=voice_lane_id,
        )


@dataclass(frozen=True)
class VoiceCredentials:
    has_voice_audio_key: bool
    has_voice_turn_key: bool
    issues: tuple[VoiceCredentialIssue, ...]


@dataclass(frozen=True)
class ModelRef:
    provider_id: str = "inception"
    model_id: str = "mercury-2"
    variant: str | None = "high"

    @property
    def opencode_name(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    def prompt_payload(self) -> dict[str, str]:
        payload = {"providerID": self.provider_id, "modelID": self.model_id}
        if self.variant:
            payload["variant"] = self.variant
        return payload

    def session_payload(self) -> dict[str, str]:
        payload = {"providerID": self.provider_id, "id": self.model_id}
        if self.variant:
            payload["variant"] = self.variant
        return payload


@dataclass(frozen=True)
class VoiceConfig:
    opencode_url: str
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8765
    model: ModelRef = ModelRef()
    context_threshold_tokens: int = 70_000
    compaction_wait_sec: float = 10.0
    poll_interval_sec: float = 0.1
    max_turn_sec: float = 300.0
    run_root: str = "runs/voice"
    deepgram_stt_model: str = "flux-general-en"
    deepgram_tts_model: str = "aura-2-thalia-en"
    deepgram_sample_rate: int = 16_000
    flux_eot_threshold: float = 0.7
    flux_eager_eot_threshold: float | None = None
    flux_eot_timeout_ms: int = 5_000
    opencode_agent: str = "voice-build"
    voice_agent_prompt_path: str = "opencode_voice/voice_agent.md"
    keep_fork_default: bool = False

    @property
    def browser_url(self) -> str:
        return f"http://{self.bridge_host}:{self.bridge_port}"

    @property
    def has_deepgram_key(self) -> bool:
        return bool(os.environ.get("DEEPGRAM_API_KEY"))

    @property
    def has_inception_key(self) -> bool:
        return bool(os.environ.get("INCEPTION_API_KEY"))

    @property
    def credential_issues(self) -> tuple[VoiceCredentialIssue, ...]:
        return load_voice_credentials().issues

    def credential_issue_for(self, capability: str) -> VoiceCredentialIssue | None:
        for issue in self.credential_issues:
            if issue.capability == capability:
                return issue
        return None


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def voice_bridge_issue_payload(
    *,
    capability: str,
    diagnostic_code: str,
    safe_detail: str,
    retryable: bool = True,
    sent_at: str | None = None,
    debug_ref: str | None = None,
    voice_lane_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "voice_bridge_issue",
        "sentAt": sent_at or iso_utc_now(),
        "userMessage": VOICE_BRIDGE_USER_MESSAGE,
        "safeDetail": safe_detail,
        "diagnosticCode": diagnostic_code,
        "retryable": retryable,
        "capability": capability,
    }
    if voice_lane_id:
        payload["voiceLaneId"] = voice_lane_id
    if debug_ref:
        payload["debugRef"] = debug_ref
    return payload


def load_local_dotenv(
    path: str | Path = ".env",
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    target = environ if environ is not None else os.environ
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return ()

    loaded: list[str] = []
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in target:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        target[key] = value
        loaded.append(key)
    return tuple(loaded)


def load_voice_credentials(
    *,
    environ: MutableMapping[str, str] | None = None,
    dotenv_path: str | Path = ".env",
) -> VoiceCredentials:
    target = environ if environ is not None else os.environ
    load_local_dotenv(dotenv_path, target)
    missing = [
        VoiceCredentialIssue(
            capability=spec.capability,
            diagnostic_code=spec.diagnostic_code,
            safe_detail=spec.safe_detail,
        )
        for spec in REQUIRED_CREDENTIALS
        if not target.get(spec.env_var)
    ]
    return VoiceCredentials(
        has_voice_audio_key=bool(target.get("DEEPGRAM_API_KEY")),
        has_voice_turn_key=bool(target.get("INCEPTION_API_KEY")),
        issues=tuple(missing),
    )


def secret_values(
    *,
    environ: MutableMapping[str, str] | None = None,
    extra: tuple[str, ...] = (),
) -> tuple[str, ...]:
    target = environ if environ is not None else os.environ
    values = [
        value
        for value in (target.get(spec.env_var) for spec in REQUIRED_CREDENTIALS)
        if value and len(value) > 3
    ]
    values.extend(value for value in extra if value and len(value) > 3)
    return tuple(dict.fromkeys(values))


def is_sensitive_field_name(value: str) -> bool:
    normalized = value.replace("-", "_").lower()
    return (
        normalized in SENSITIVE_FIELD_NAMES
        or normalized.endswith("_api_key")
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
    )


def redact_secrets(value: Any, *, secrets: tuple[str, ...] | None = None) -> Any:
    known_secrets = secrets if secrets is not None else secret_values()
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_field_name(str(key)):
                redacted[key] = REDACTED if item else item
            else:
                redacted[key] = redact_secrets(item, secrets=known_secrets)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item, secrets=known_secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item, secrets=known_secrets) for item in value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes redacted>"
    if isinstance(value, str):
        text = value
        for secret in known_secrets:
            text = text.replace(secret, REDACTED)
        return text
    return value


def render_opencode_config(
    model: ModelRef | None = None,
    voice_agent_prompt: str | None = None,
    voice_agent_name: str = "voice-build",
) -> dict[str, Any]:
    model_ref = model or ModelRef()
    model_name = model_ref.opencode_name
    mercury_model = {
        "id": model_ref.model_id,
        "name": "Mercury 2",
        "limit": {"context": 128000, "output": 8192},
    }
    agent_config: dict[str, Any] = {
        "compaction": {"model": model_name, "temperature": 0.5},
        "title": {"model": model_name},
        "summary": {"model": model_name},
    }
    if voice_agent_prompt:
        voice_agent_config = {
            "description": "Primary build agent tuned for spoken OpenCode voice sessions.",
            "mode": "primary",
            "model": model_name,
            "prompt": voice_agent_prompt,
        }
        if model_ref.variant:
            voice_agent_config["variant"] = model_ref.variant
        agent_config[voice_agent_name] = voice_agent_config

    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            model_ref.provider_id: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Inception Labs",
                "options": {
                    "baseURL": "https://api.inceptionlabs.ai/v1",
                    "apiKey": "{env:INCEPTION_API_KEY}",
                    "timeout": 120000,
                    "chunkTimeout": 15000,
                },
                "models": {
                    model_ref.model_id: mercury_model,
                    model_name: mercury_model,
                },
            }
        },
        "model": model_name,
        "small_model": model_name,
        "agent": agent_config,
        "compaction": {
            "auto": False,
            "prune": True,
            "tail_turns": 2,
            "preserve_recent_tokens": 8000,
            "reserved": 10000,
        },
    }


def render_opencode_config_content(
    model: ModelRef | None = None,
    voice_agent_prompt: str | None = None,
    voice_agent_name: str = "voice-build",
) -> str:
    return json.dumps(
        render_opencode_config(model, voice_agent_prompt=voice_agent_prompt, voice_agent_name=voice_agent_name),
        separators=(",", ":"),
    )


def redacted_opencode_config(model: ModelRef | None = None) -> dict[str, Any]:
    config = render_opencode_config(model)
    for provider in config.get("provider", {}).values():
        options = provider.get("options", {})
        if "apiKey" in options:
            options["apiKey"] = "{env:INCEPTION_API_KEY}"
    return config


def parse_model_ref(value: str, variant: str | None = None) -> ModelRef:
    if "/" not in value:
        raise ValueError("Model must be in provider/model form, for example inception/mercury-2.")
    provider_id, model_id = value.split("/", 1)
    if not provider_id or not model_id:
        raise ValueError("Model must include both provider and model id.")
    return ModelRef(provider_id=provider_id, model_id=model_id, variant=variant)


def load_voice_agent_prompt(path: str | None = None) -> str:
    prompt_path = Path(path or "opencode_voice/voice_agent.md")
    return prompt_path.read_text(encoding="utf-8").strip()
