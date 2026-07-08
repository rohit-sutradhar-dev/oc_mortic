from __future__ import annotations

"""End-to-end install gate for the voice helper.

Every startup failure this class of bug produced was silent: a turn sent with
an agent the OpenCode server does not have is accepted (HTTP 204) and then
never runs, so the helper hangs with no error. The doctor turns that — and the
neighbouring credential/reachability failures — into a loud, specific report
BEFORE a turn is attempted. It does not repair or mutate OpenCode global
config; managed `/mortic` supplies its voice agent through
OPENCODE_CONFIG_CONTENT.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from opencode_voice.config import ModelRef, VoiceConfig

PASS = "pass"
WARN = "warn"
FAIL = "fail"

# The dotenv files the helper loads, most-specific last (matches the launch
# chain: ~/.mortic/.env is the BYOK home, a checkout .env may add to it).
DOTENV_PATHS = ("~/.mortic/.env", ".env")

@dataclass
class DoctorResult:
    name: str
    status: str  # PASS / WARN / FAIL
    detail: str

    @property
    def ok(self) -> bool:
        return self.status != FAIL


def _dotenv_defines(key: str) -> list[str]:
    """Which loadable .env files define `key` (for reporting the key source)."""
    found: list[str] = []
    for raw in DOTENV_PATHS:
        path = Path(raw).expanduser()
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().removeprefix("export ").lstrip()
            if stripped.split("=", 1)[0].strip() == key:
                found.append(raw)
                break
    return found


def key_source(key: str) -> str:
    """Human description of where `key` resolves from, for the report."""
    present = bool(os.environ.get(key))
    files = _dotenv_defines(key)
    if not present and not files:
        return "not set anywhere"
    parts: list[str] = []
    if present:
        parts.append("process environment")
    if files:
        parts.append("defined in " + ", ".join(files))
    return "; ".join(parts)


def check_credentials(config: "VoiceConfig") -> list[DoctorResult]:
    results: list[DoctorResult] = []
    inception = bool(os.environ.get("INCEPTION_API_KEY"))
    results.append(
        DoctorResult(
            "LLM key (INCEPTION_API_KEY)",
            PASS if inception else FAIL,
            key_source("INCEPTION_API_KEY"),
        )
    )
    results.append(
        DoctorResult(
            "STT key (DEEPGRAM_API_KEY)",
            PASS if os.environ.get("DEEPGRAM_API_KEY") else FAIL,
            key_source("DEEPGRAM_API_KEY"),
        )
    )
    if config.tts_provider == "cartesia":
        results.append(
            DoctorResult(
                "TTS key (CARTESIA_API_KEY)",
                PASS if os.environ.get("CARTESIA_API_KEY") else FAIL,
                key_source("CARTESIA_API_KEY"),
            )
        )
    return results


async def check_opencode(
    config: "VoiceConfig", model: "ModelRef", agent: str, round_trip: bool = True
) -> list[DoctorResult]:
    """Reachability, the voice agent, and a real model round-trip — the three
    that were silently broken. The round-trip is skipped (WARN) if a prior
    check already failed, since it would only fail again with less signal;
    round_trip=False also skips it for the cheap boot-time preflight."""
    from opencode_voice.opencode_client import OpenCodeClient

    results: list[DoctorResult] = []
    client = OpenCodeClient(config.opencode_url, timeout_sec=30.0)
    try:
        try:
            await client.health()
            results.append(DoctorResult("OpenCode reachable", PASS, config.opencode_url))
        except Exception as exc:  # noqa: BLE001 - any failure to reach is a hard stop.
            results.append(
                DoctorResult("OpenCode reachable", FAIL, f"{config.opencode_url}: {type(exc).__name__}")
            )
            return results

        try:
            agents = await client.agents()
        except Exception as exc:  # noqa: BLE001
            results.append(DoctorResult("Voice agent present", FAIL, f"could not list agents: {type(exc).__name__}"))
            return results
        if agent in agents:
            results.append(DoctorResult("Voice agent present", PASS, f"'{agent}' registered"))
            if round_trip:
                results.append(await _round_trip(client, model, agent))
            return results
        else:
            results.append(
                DoctorResult(
                    "Voice agent present",
                    FAIL,
                    f"'{agent}' MISSING (server has: {', '.join(agents) or 'none'}). "
                    "This server was not started with the voice config overlay; "
                    "turns will hang. Start Mortic in managed mode so the overlay is supplied.",
                )
            )
            if round_trip:
                results.append(
                    DoctorResult("Model round-trip", WARN, "skipped: voice agent missing")
                )
            return results
    finally:
        await client.close()


async def _round_trip(client, model: "ModelRef", agent: str) -> DoctorResult:
    session_id: str | None = None
    try:
        session = await client.create_session()
        session_id = session.get("id") if isinstance(session, dict) else None
        if not session_id:
            return DoctorResult("Model round-trip", FAIL, "could not create a session")
        message = await client.prompt_sync(session_id, "Reply with exactly: pong", model, agent)
        text = _assistant_text(message)
        if text:
            return DoctorResult("Model round-trip", PASS, f"{model.opencode_name} replied ({text[:30]!r})")
        return DoctorResult("Model round-trip", WARN, "turn completed but produced no text")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        body = exc.response.text[:160]
        return DoctorResult("Model round-trip", FAIL, f"HTTP {code}: {body}")
    except Exception as exc:  # noqa: BLE001
        return DoctorResult("Model round-trip", FAIL, f"{type(exc).__name__}: {exc}")
    finally:
        if session_id:
            try:
                await client.delete_session(session_id)
            except Exception:  # noqa: BLE001 - cleanup is best-effort.
                pass


def _assistant_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
    return "".join(texts).strip()


async def run_doctor(
    config: "VoiceConfig", model: "ModelRef", agent: str, round_trip: bool = True
) -> list[DoctorResult]:
    results = check_credentials(config)
    results += await check_opencode(config, model, agent, round_trip=round_trip)
    return results


def format_report(results: list[DoctorResult]) -> str:
    glyph = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}
    width = max((len(r.name) for r in results), default=0)
    lines = ["Mortic doctor", ""]
    for r in results:
        lines.append(f"  [{glyph[r.status]}] {r.name.ljust(width)}  {r.detail}")
    failed = [r for r in results if r.status == FAIL]
    lines.append("")
    lines.append("OK — voice turns should work." if not failed else f"{len(failed)} blocking issue(s) above.")
    return "\n".join(lines)
