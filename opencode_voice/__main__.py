from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import uvicorn

from opencode_voice.config import (
    VoiceConfig,
    load_local_dotenv,
    load_voice_agent_prompt,
    parse_model_ref,
    render_opencode_config_content,
)
from opencode_voice.server import create_app


def main(argv: list[str] | None = None) -> int:
    load_local_dotenv()
    args = parse_args(argv)
    model = parse_model_ref(args.model, variant=args.model_variant)
    opencode_process: subprocess.Popen[str] | None = None
    opencode_url = None
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
        opencode_dir = args.opencode_dir or (detect_opencode_directory(detected_url) if detected_url else None)
        opencode_url, opencode_process = start_managed_opencode(
            model_name=args.model,
            model_variant=args.model_variant,
            opencode_dir=opencode_dir,
            voice_agent_prompt_path=args.voice_agent_prompt,
            voice_agent_name=args.agent,
        )

    config = VoiceConfig(
        opencode_url=opencode_url,
        bridge_host=args.host,
        bridge_port=args.port,
        model=model,
        context_threshold_tokens=args.context_threshold,
        deepgram_stt_model=args.stt_model,
        deepgram_tts_model=args.tts_model,
        deepgram_sample_rate=args.sample_rate,
        flux_eager_eot_threshold=args.eager_eot_threshold or None,
        voice_duplex=args.voice_duplex,
        barge_in_confirm_sec=args.barge_in_confirm_sec,
        barge_in_min_chars=args.barge_in_min_chars,
        playback_mute_sec=args.playback_mute_sec,
        event_completion_grace_sec=args.event_completion_grace_sec,
        opencode_agent=args.agent,
        voice_agent_prompt_path=args.voice_agent_prompt,
        keep_fork_default=args.keep_fork,
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
    app = create_app(config)
    try:
        uvicorn.run(app, host=config.bridge_host, port=config.bridge_port, log_level=args.log_level)
    finally:
        if opencode_process:
            opencode_process.terminate()
            try:
                opencode_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                opencode_process.kill()
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
    parser.add_argument("--stt-model", default="flux-general-en", help="Speech-to-text model id.")
    parser.add_argument("--tts-model", default="aura-2-thalia-en", help="Text-to-speech model id.")
    parser.add_argument("--sample-rate", type=int, default=16_000, help="PCM sample rate for STT and TTS.")
    parser.add_argument(
        "--eager-eot-threshold",
        type=float,
        default=0.6,
        help="Flux eager end-of-turn threshold (default 0.6; pass 0 to disable).",
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
        "--barge-in-confirm-sec",
        type=float,
        default=2.0,
        help="How long a speech.start during playback pauses audio while waiting for a transcript.",
    )
    parser.add_argument(
        "--barge-in-min-chars",
        type=int,
        default=4,
        help="Transcripts shorter than this during playback resume audio instead of interrupting.",
    )
    parser.add_argument(
        "--playback-mute-sec",
        type=float,
        default=0.6,
        help="STT hears silence this long at each playback start (echo-canceller convergence window); 0 disables.",
    )
    parser.add_argument(
        "--event-completion-grace-sec",
        type=float,
        default=0.6,
        help="Wait this long for trailing text after a completion signal before polling; 0 disables.",
    )
    parser.add_argument("--keep-fork", action="store_true", help="Keep ephemeral forks by default.")
    parser.add_argument("--print-config", action="store_true", help="Print the generated OpenCode config overlay.")
    parser.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    return parser.parse_args(argv)


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
    cwd = str(Path(opencode_dir).expanduser()) if opencode_dir else None
    process = subprocess.Popen(
        ["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(port), "--cors", "*"],
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Managed OpenCode server exited before becoming healthy.")
        if is_healthy(url):
            return url, process
        time.sleep(0.25)
    process.terminate()
    raise RuntimeError("Managed OpenCode server did not become healthy within 20 seconds.")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    raise SystemExit(main())
