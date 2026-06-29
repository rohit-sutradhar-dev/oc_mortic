from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    poll_interval_sec: float = 0.2
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
            "auto": True,
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
