"""End-to-end transport test: a real helper process, a real WebSocket client.

Boots the packaged `mortic-helper` binary (the same one the plugin launcher
resolves in a repo checkout) against a fake OpenCode server, then drives the
documented lane lifecycle over the wire: health readiness, start -> ready with
a real fork call, unknown/invalid command handling, stop -> stopped with fork
cleanup. Audio paths are exercised in-process (tests/test_sidepod_lane.py);
this test proves the process, transport, and fork lifecycle end to end.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI

from opencode_voice.protocol import PROTOCOL_VERSION, check_event

REPO_ROOT = Path(__file__).parent.parent
HELPER_BINARY = REPO_ROOT / ".venv" / "bin" / "mortic-helper"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeOpenCode:
    """Minimal OpenCode API surface for the fork lifecycle."""

    def __init__(self) -> None:
        self.app = FastAPI()
        self.forks: list[str] = []
        self.deleted: list[str] = []
        app = self.app

        @app.get("/global/health")
        async def health() -> dict[str, Any]:
            return {"ok": True}

        @app.post("/session/{session_id}/fork")
        async def fork(session_id: str) -> dict[str, Any]:
            fork_id = f"fork_{len(self.forks) + 1}"
            self.forks.append(fork_id)
            return {"id": fork_id}

        @app.get("/session/{session_id}")
        async def get_session(session_id: str) -> dict[str, Any]:
            return {"id": session_id, "title": "Source Thread", "tokens": {}}

        @app.patch("/session/{session_id}")
        async def patch_session(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            return payload

        @app.post("/api/session/{session_id}/model")
        async def switch_model(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        @app.post("/api/session/{session_id}/agent")
        async def switch_agent(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        @app.get("/session/{session_id}/message")
        async def messages(session_id: str) -> list[dict[str, Any]]:
            return []

        @app.post("/session/{session_id}/abort")
        async def abort(session_id: str) -> dict[str, Any]:
            return {"ok": True}

        @app.delete("/session/{session_id}")
        async def delete(session_id: str) -> dict[str, Any]:
            self.deleted.append(session_id)
            return {"ok": True}


class HelperEndToEndTests(unittest.TestCase):
    @unittest.skipUnless(HELPER_BINARY.exists(), "repo .venv with mortic-helper required")
    def test_lane_lifecycle_over_a_real_process_and_socket(self) -> None:
        opencode_port = free_port()
        helper_port = free_port()
        fake = FakeOpenCode()
        server = uvicorn.Server(
            uvicorn.Config(fake.app, host="127.0.0.1", port=opencode_port, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(server.started, "fake OpenCode failed to start")

        opencode_url = f"http://127.0.0.1:{opencode_port}"
        helper_url = f"http://127.0.0.1:{helper_port}"
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "OPENCODE_VOICE_OPENCODE_URL": opencode_url,
                "DEEPGRAM_API_KEY": "e2e-audio-key",
                "INCEPTION_API_KEY": "e2e-turn-key",
            }
            helper = subprocess.Popen(
                [str(HELPER_BINARY), "--host", "127.0.0.1", "--port", str(helper_port), "--log-level", "error"],
                cwd=tmp,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            try:
                self.wait_for_ready(helper_url)
                asyncio.run(self.drive_lane(helper_port, opencode_url, fake))
            finally:
                helper.terminate()
                helper.wait(timeout=10)
                server.should_exit = True
                thread.join(timeout=10)

        self.assertEqual(fake.forks, ["fork_1"])
        self.assertEqual(fake.deleted, ["fork_1"])

    def wait_for_ready(self, helper_url: str) -> None:
        # Same contract the plugin launcher polls: ready means ready.
        deadline = time.time() + 15
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            try:
                response = httpx.get(f"{helper_url}/api/health", timeout=2)
                last = response.json()
                if last.get("ready") is True:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        self.fail(f"helper never became ready: {last}")

    async def drive_lane(self, helper_port: int, opencode_url: str, fake: FakeOpenCode) -> None:
        import websockets

        uri = f"ws://127.0.0.1:{helper_port}/ws/sidepod"
        async with websockets.connect(uri) as socket_:
            async def send(payload: dict[str, Any]) -> None:
                await socket_.send(json.dumps(payload))

            async def recv() -> dict[str, Any]:
                message = json.loads(await asyncio.wait_for(socket_.recv(), timeout=10))
                check = check_event(message)
                assert check.ok, f"engine sent off-contract message {message.get('type')}: {check.errors}"
                return message

            await send(
                {
                    "type": "start",
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientEventId": "evt_e2e_1",
                    "sentAt": "2026-07-04T00:00:00.000Z",
                    "sourceSessionId": "ses_source_e2e",
                    "keepFork": False,
                    "opencodeUrl": opencode_url,
                }
            )
            ready = await recv()
            assert ready["type"] == "ready", ready
            assert ready["protocolVersion"] == PROTOCOL_VERSION
            assert ready["sourceSessionId"] == "ses_source_e2e"
            assert ready["forkSessionId"] == "fork_1"

            # Unknown command types are logged and ignored — no reply.
            await send({"type": "future.command", "clientEventId": "evt_e2e_2", "sentAt": "2026-07-04T00:00:01.000Z"})
            # Invalid known command answers protocol_invalid_message.
            await send({"type": "live.set", "clientEventId": "evt_e2e_3", "sentAt": "2026-07-04T00:00:02.000Z"})
            issue = await recv()
            assert issue["type"] == "voice_bridge_issue", issue
            assert issue["diagnosticCode"] == "protocol_invalid_message", issue

            await send(
                {
                    "type": "stop",
                    "clientEventId": "evt_e2e_4",
                    "sentAt": "2026-07-04T00:00:03.000Z",
                    "reason": "user.end_session",
                }
            )
            stopped = await recv()
            assert stopped["type"] == "stopped", stopped
            assert stopped["reason"] == "user.end_session"
            assert stopped["forkDeleted"] is True


if __name__ == "__main__":
    unittest.main()
