from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import uvicorn

from opencode_voice.config import (
    ModelRef,
    VoiceConfig,
    load_local_dotenv,
    load_voice_agent_prompt,
    parse_model_ref,
    render_opencode_config_content,
)
from opencode_voice.managed_opencode import (
    ManagedOpenCodeLease,
    reap_stale_managed_opencode_leases,
    terminate_managed_process,
)
from opencode_voice.server import create_app


def main(argv: list[str] | None = None) -> int:
    load_local_dotenv(Path("~/.mortic/.env").expanduser())
    load_local_dotenv()
    args = parse_args(argv)
    model = parse_model_ref(args.model, variant=args.model_variant)
    if args.doctor:
        return run_doctor_cli(args, model)
    opencode_process: subprocess.Popen[str] | None = None
    managed_lease: ManagedOpenCodeLease | None = None
    opencode_url = None
    opencode_dir = args.opencode_dir
    detected_url = detect_opencode_url()
    if not args.managed_opencode:
        opencode_url = args.opencode_url or os.environ.get("OPENCODE_VOICE_OPENCODE_URL") or detected_url
    if not opencode_url and args.no_managed:
        # Plugin-spawned helpers must never start a shadow OpenCode server;
        # exiting here surfaces as VOICE OFFLINE in the sidepod instead of a
        # silently leaked `opencode serve` bound to the wrong sessions.
        print(
            "mortic-helper: no OpenCode server URL provided or detected and --no-managed is set.",
            file=sys.stderr,
        )
        return 2
    if not opencode_url:
        opencode_dir = opencode_dir or (detect_opencode_directory(detected_url) if detected_url else None)
        reap_stale_managed_opencode_leases()
        opencode_url, opencode_process = start_managed_opencode(
            model_name=args.model,
            model_variant=args.model_variant,
            opencode_dir=opencode_dir,
            voice_agent_prompt_path=args.voice_agent_prompt,
            voice_agent_name=args.agent,
        )

    # Only pass these when the CLI explicitly sets them (default=None), so an
    # unset flag lets VoiceConfig's own dataclass default win instead of a
    # stale literal baked into argparse silently overriding a config.py edit.
    model_overrides: dict[str, Any] = {}
    if args.stt_model is not None:
        model_overrides["deepgram_stt_model"] = args.stt_model
    if args.tts_model is not None:
        model_overrides["deepgram_tts_model"] = args.tts_model
    if args.sample_rate is not None:
        # Backwards-compatible alias: --sample-rate now controls Flux only.
        # TTS and device clocks have explicit flags below.
        model_overrides["deepgram_sample_rate"] = args.sample_rate
    if args.tts_sample_rate is not None:
        model_overrides["tts_sample_rate"] = args.tts_sample_rate
    if args.device_sample_rate is not None:
        model_overrides["device_sample_rate"] = args.device_sample_rate
    if args.cartesia_tts_model is not None:
        model_overrides["cartesia_tts_model"] = args.cartesia_tts_model
    if args.cartesia_voice_id is not None:
        model_overrides["cartesia_voice_id"] = args.cartesia_voice_id

    workspace_dir = str(Path(opencode_dir).expanduser()) if opencode_dir else None
    config = VoiceConfig(
        opencode_url=opencode_url,
        bridge_host=args.host,
        bridge_port=args.port,
        workspace_dir=workspace_dir,
        model=model,
        context_threshold_tokens=args.context_threshold,
        # The plugin spawns the helper with a fixed flag set (README's launch
        # chain), so a plugin-side "/mortic" session can't pass --tts-provider
        # directly; the env var (settable in .env like the API keys) is the
        # only way to configure it there.
        tts_provider=args.tts_provider or os.environ.get("OPENCODE_VOICE_TTS_PROVIDER") or "deepgram",
        flux_eager_eot_threshold=None,
        voice_duplex=args.voice_duplex,
        event_completion_grace_sec=args.event_completion_grace_sec,
        response_mode=args.response_mode
        or os.environ.get("OPENCODE_VOICE_RESPONSE_MODE")
        or "legacy",
        opencode_agent=args.agent,
        voice_agent_prompt_path=args.voice_agent_prompt,
        keep_fork_default=args.keep_fork,
        **model_overrides,
    )
    if args.print_config:
        print(
            render_opencode_config_content(
                model,
                voice_agent_prompt=load_voice_agent_prompt(args.voice_agent_prompt),
                voice_agent_name=args.agent,
            ),
            file=sys.stderr,
        )
        return 0
    if opencode_process:
        managed_lease = ManagedOpenCodeLease(process=opencode_process, url=opencode_url, workspace=workspace_dir).start()
        install_signal_exit_handlers()
    preflight_startup(config, model, args.agent)
    app = create_app(config)
    try:
        # Provider WebSockets rely on asyncio's Happy Eyeballs support. Uvicorn
        # otherwise auto-selects uvloop when installed, but uvloop 0.21 rejects
        # `happy_eyeballs_delay` / `interleave` before any network attempt.
        # Keep the helper on the standard loop so the connector used in the
        # live sidepod process matches the verified standalone transport.
        uvicorn.run(
            app,
            host=config.bridge_host,
            port=config.bridge_port,
            log_level=args.log_level,
            loop="asyncio",
        )
    finally:
        if opencode_process:
            terminate_managed_process(opencode_process)
        if managed_lease:
            managed_lease.close(remove=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Mortic local helper for OpenCode voice sessions.")
    parser.add_argument("--opencode-url", help="Existing OpenCode server URL. Auto-detected when omitted.")
    parser.add_argument("--managed-opencode", action="store_true", help="Start a clean managed OpenCode server.")
    parser.add_argument(
        "--no-managed",
        action="store_true",
        help="Exit instead of starting a managed OpenCode server when none is provided or detected.",
    )
    parser.add_argument("--opencode-dir", help="Working directory for a managed OpenCode server.")
    parser.add_argument("--host", default="127.0.0.1", help="Voice bridge host.")
    parser.add_argument("--port", type=int, default=8765, help="Voice bridge port.")
    parser.add_argument("--model", default="inception/mercury-2", help="OpenCode model in provider/model form.")
    parser.add_argument("--model-variant", default="high", help="OpenCode model variant.")
    parser.add_argument("--agent", default="voice-build", help="OpenCode agent for voice turns.")
    parser.add_argument(
        "--voice-agent-prompt",
        default="opencode_voice/voice_agent.md",
        help="Markdown prompt used for the ephemeral managed-server voice agent.",
    )
    parser.add_argument("--context-threshold", type=int, default=70_000, help="Token threshold for proactive compaction.")
    parser.add_argument(
        "--stt-model", default=None, help="Speech-to-text model id. Defaults to VoiceConfig.deepgram_stt_model."
    )
    parser.add_argument(
        "--tts-model", default=None, help="Deepgram TTS model id. Defaults to VoiceConfig.deepgram_tts_model."
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=None,
        help="Flux STT PCM sample rate. Defaults to 16000.",
    )
    parser.add_argument("--tts-sample-rate", type=int, default=None, help="Provider TTS PCM sample rate (default 16000).")
    parser.add_argument(
        "--device-sample-rate", type=int, default=None, help="Preferred native duplex device rate (default 48000)."
    )
    parser.add_argument(
        "--tts-provider",
        default=None,
        choices=["deepgram", "cartesia"],
        help=(
            "TTS engine for playback (STT always stays on Deepgram Flux). "
            "Falls back to OPENCODE_VOICE_TTS_PROVIDER, then 'deepgram'."
        ),
    )
    parser.add_argument(
        "--cartesia-tts-model", default=None, help="Cartesia TTS model id. Defaults to VoiceConfig.cartesia_tts_model."
    )
    parser.add_argument(
        "--cartesia-voice-id", default=None, help="Cartesia voice id. Defaults to VoiceConfig.cartesia_voice_id."
    )
    parser.add_argument(
        "--voice-duplex",
        default="auto",
        choices=["auto", "full", "half"],
        help=(
            "Mic behavior while the assistant speaks: auto = echo-cancel when "
            "available and gate otherwise, full = raw passthrough (headphones), "
            "half = always gate."
        ),
    )
    parser.add_argument(
        "--event-completion-grace-sec",
        type=float,
        default=0.6,
        help="Wait this long for trailing text after a completion signal before polling; 0 disables.",
    )
    parser.add_argument(
        "--response-mode",
        choices=["legacy", "structured"],
        default=None,
        help="Use legacy streaming or the canary structured display/spoken contract.",
    )
    parser.add_argument("--keep-fork", action="store_true", help="Keep ephemeral forks by default.")
    parser.add_argument("--print-config", action="store_true", help="Print the generated OpenCode config overlay.")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Diagnose the install end-to-end (server, voice agent, model round-trip, keys) and exit.",
    )
    parser.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    return parser.parse_args(argv)


def run_doctor_cli(args: argparse.Namespace, model: ModelRef) -> int:
    from opencode_voice import doctor as doctor_mod

    url = args.opencode_url or os.environ.get("OPENCODE_VOICE_OPENCODE_URL") or detect_opencode_url()
    config = VoiceConfig(
        opencode_url=url or "http://127.0.0.1:0",
        model=model,
        tts_provider=args.tts_provider or os.environ.get("OPENCODE_VOICE_TTS_PROVIDER") or "deepgram",
        opencode_agent=args.agent,
    )
    results = asyncio.run(doctor_mod.run_doctor(config, model, args.agent))
    print(doctor_mod.format_report(results))
    return 0 if all(r.ok for r in results) else 1


def install_signal_exit_handlers() -> None:
    def _exit(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _exit)
        except Exception:
            pass


def preflight_startup(config: VoiceConfig, model: ModelRef, agent: str) -> None:
    """Warn-only boot check: the cheap half of the doctor (reachable + voice
    agent, no round-trip) so a misconfigured server screams at startup instead
    of hanging on the first turn. Never blocks — a slow/absent server here just
    means the helper starts and surfaces the issue on the lane as usual."""
    from opencode_voice import doctor as doctor_mod

    try:
        results = asyncio.run(doctor_mod.run_doctor(config, model, agent, round_trip=False))
    except Exception as exc:  # noqa: BLE001 - a preflight must never stop the helper booting.
        print(f"mortic-helper: startup preflight skipped ({type(exc).__name__})", file=sys.stderr)
        return
    problems = [r for r in results if not r.ok]
    if problems:
        print("mortic-helper: startup preflight found issues (helper still starting):", file=sys.stderr)
        for r in problems:
            print(f"  [{r.status.upper()}] {r.name}: {r.detail}", file=sys.stderr)


def detect_opencode_url() -> str | None:
    candidates = []
    for port in ports_from_processes():
        candidates.append(f"http://127.0.0.1:{port}")
    candidates.extend(["http://127.0.0.1:4096", "http://127.0.0.1:17242"])
    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        if is_healthy(url):
            return url
    return None


def ports_from_processes() -> list[int]:
    try:
        output = subprocess.check_output(["pgrep", "-fl", "opencode"], text=True)
    except Exception:
        return []
    ports: list[int] = []
    words = output.replace("=", " ").split()
    for index, word in enumerate(words):
        if word == "--port" and index + 1 < len(words):
            try:
                ports.append(int(words[index + 1]))
            except ValueError:
                pass
    return ports


def is_healthy(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/global/health", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def detect_opencode_directory(base_url: str | None) -> str | None:
    if not base_url:
        return None
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/path", timeout=2) as response:
            data = __import__("json").loads(response.read().decode("utf-8"))
    except Exception:
        return None
    directory = data.get("directory") or data.get("worktree")
    return str(directory) if directory else None


def start_managed_opencode(
    model_name: str,
    model_variant: str | None = "high",
    opencode_dir: str | None = None,
    voice_agent_prompt_path: str = "opencode_voice/voice_agent.md",
    voice_agent_name: str = "voice-build",
) -> tuple[str, subprocess.Popen[str]]:
    port = free_port()
    env = os.environ.copy()
    env["OPENCODE_CONFIG_CONTENT"] = render_opencode_config_content(
        parse_model_ref(model_name, variant=model_variant),
        voice_agent_prompt=load_voice_agent_prompt(voice_agent_prompt_path),
        voice_agent_name=voice_agent_name,
    )
    # OpenCode is distributed as a standalone Bun executable. Runtime flags
    # passed in argv are parsed by OpenCode's own CLI, where this flag is not a
    # valid `serve` option. BUN_OPTIONS is Bun's supported runtime-flag channel
    # for standalone executables. Append so existing user runtime options are
    # preserved; the managed process remains the only process affected.
    env["BUN_OPTIONS"] = " ".join(
        option
        for option in (
            env.get("BUN_OPTIONS", "").strip(),
            "--dns-result-order=ipv4first",
        )
        if option
    )
    cwd = str(Path(opencode_dir).expanduser()) if opencode_dir else None
    process = subprocess.Popen(
        [
            "opencode",
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
            "--cors",
            "*",
        ],
        env=env,
        cwd=cwd,
        # The plugin already redirects helper stderr to MORTIC_HELPER_LOG.
        # Forward child output there too: an unread PIPE both swallowed useful
        # startup errors and could eventually block a long-running server.
        stdout=sys.stderr,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Managed OpenCode server exited before becoming healthy.")
        if is_healthy(url):
            return url, process
        time.sleep(0.25)
    terminate_managed_process(process)
    raise RuntimeError("Managed OpenCode server did not become healthy within 20 seconds.")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    raise SystemExit(main())
