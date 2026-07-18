from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from opencode_voice.config import (
    REDACTED,
    ModelRef,
    VoiceConfig,
    load_local_dotenv,
    load_voice_credentials,
    redact_secrets,
    render_opencode_config,
    voice_bridge_issue_payload,
)

from opencode_voice.speech_filter import FlushLimiter, SpeechTextFilter
from opencode_voice.deepgram import build_flux_url, parse_flux_message
from opencode_voice.tts_chunker import TTSChunker
from opencode_voice.logging import RunLogger
from opencode_voice.opencode_client import OpenCodeClient, SSEParser
from opencode_voice.server import SIDEPOD_PROTOCOL_VERSION, create_app, helper_readiness_issues
from opencode_voice.state import (
    active_context_estimate,
    event_session_id,
    session_context_tokens,
    session_usage_tokens,
)
from tests.fakes import FakeOpenCodeClient


class MercuryConfigTests(unittest.TestCase):
    def test_latency_and_duplex_defaults(self) -> None:
        config = VoiceConfig(opencode_url="http://127.0.0.1:1")

        self.assertIsNone(config.flux_eager_eot_threshold)
        self.assertEqual(config.voice_duplex, "auto")
        self.assertEqual(config.deepgram_sample_rate, 16_000)
        self.assertEqual(config.tts_sample_rate, 16_000)
        self.assertEqual(config.device_sample_rate, 48_000)
        self.assertEqual(config.first_text_timeout_sec, 20.0)

    def test_mercury_is_used_for_all_opencode_slots(self) -> None:
        config = render_opencode_config(ModelRef(provider_id="inception", model_id="mercury-2"))

        self.assertEqual(config["model"], "inception/mercury-2")
        self.assertEqual(config["small_model"], "inception/mercury-2")
        self.assertEqual(config["agent"]["compaction"]["model"], "inception/mercury-2")
        self.assertEqual(config["agent"]["summary"]["model"], "inception/mercury-2")
        self.assertEqual(config["provider"]["inception"]["options"]["apiKey"], "{env:INCEPTION_API_KEY}")
        self.assertEqual(config["provider"]["inception"]["models"]["mercury-2"]["id"], "mercury-2")
        self.assertEqual(config["provider"]["inception"]["models"]["inception/mercury-2"]["id"], "mercury-2")
        self.assertIs(config["compaction"]["auto"], False)

    def test_ephemeral_voice_agent_prompt_is_configured_when_supplied(self) -> None:
        config = render_opencode_config(
            ModelRef(provider_id="inception", model_id="mercury-2"),
            voice_agent_prompt="Do not speak code.",
            voice_agent_name="voice-build",
        )

        self.assertEqual(config["agent"]["voice-build"]["prompt"], "Do not speak code.")
        self.assertEqual(config["agent"]["voice-build"]["mode"], "primary")
        self.assertEqual(config["agent"]["voice-build"]["model"], "inception/mercury-2")


class CredentialConfigTests(unittest.TestCase):
    def test_local_dotenv_loads_missing_values_without_overriding_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dotenv_path = Path(tmp) / ".env"
            dotenv_path.write_text(
                "\n".join(
                    [
                        "DEEPGRAM_API_KEY=dotenv-audio",
                        "export INCEPTION_API_KEY='dotenv-turns'",
                    ]
                ),
                encoding="utf-8",
            )
            environ = {"DEEPGRAM_API_KEY": "env-audio"}

            loaded = load_local_dotenv(dotenv_path, environ)

        self.assertEqual(loaded, ("INCEPTION_API_KEY",))
        self.assertEqual(environ["DEEPGRAM_API_KEY"], "env-audio")
        self.assertEqual(environ["INCEPTION_API_KEY"], "dotenv-turns")

    def test_missing_credentials_build_sidepod_safe_voice_bridge_issues(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            credentials = load_voice_credentials(dotenv_path="/tmp/mortic-missing-dotenv")

        events = [issue.to_voice_bridge_issue(sent_at="2026-07-03T00:00:00.000Z") for issue in credentials.issues]
        serialized = json.dumps(events)

        self.assertEqual(len(events), 3)
        self.assertEqual({event["type"] for event in events}, {"voice_bridge_issue"})
        self.assertEqual({event["userMessage"] for event in events}, {"Voice Bridge Issue"})
        self.assertIn("voice_audio", {event["capability"] for event in events})
        self.assertIn("voice_turns", {event["capability"] for event in events})
        self.assertNotIn("DEEPGRAM_API_KEY", serialized)
        self.assertNotIn("INCEPTION_API_KEY", serialized)
        self.assertNotIn("Deepgram", serialized)
        self.assertNotIn("Mercury", serialized)

    def test_cartesia_key_is_required_by_default_but_not_for_deepgram_tts(self) -> None:
        env = {"DEEPGRAM_API_KEY": "audio-key", "INCEPTION_API_KEY": "turn-key"}
        with patch.dict(os.environ, env, clear=True):
            cartesia_default = load_voice_credentials(dotenv_path="/tmp/mortic-missing-dotenv")
            deepgram_active = load_voice_credentials(
                dotenv_path="/tmp/mortic-missing-dotenv", tts_provider="deepgram"
            )

        self.assertEqual(deepgram_active.issues, ())
        self.assertEqual(len(cartesia_default.issues), 1)
        self.assertEqual(cartesia_default.issues[0].diagnostic_code, "missing_cartesia_api_key")
        self.assertEqual(cartesia_default.issues[0].capability, "voice_audio")

    def test_cartesia_is_the_default_tts_provider(self) -> None:
        self.assertEqual(VoiceConfig(opencode_url="http://opencode.test").tts_provider, "cartesia")

    def test_cartesia_key_redacted_even_when_deepgram_is_the_active_provider(self) -> None:
        env = {
            "DEEPGRAM_API_KEY": "audio-key",
            "INCEPTION_API_KEY": "turn-key",
            "CARTESIA_API_KEY": "cartesia-secret",
        }
        with patch.dict(os.environ, env, clear=True):
            payload = redact_secrets({"headers": {"X-API-Key": "cartesia-secret"}})

        self.assertNotIn("cartesia-secret", json.dumps(payload))

    def test_redacts_raw_keys_recursively(self) -> None:
        raw_key = "sk-test-secret-123"
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": raw_key}, clear=True):
            payload = redact_secrets(
                {
                    "headers": {"Authorization": f"Token {raw_key}"},
                    "apiKey": raw_key,
                    "audio": b"abc",
                    "safe": "hello",
                }
            )

        serialized = json.dumps(payload)
        self.assertNotIn(raw_key, serialized)
        self.assertEqual(payload["headers"]["Authorization"], REDACTED)
        self.assertEqual(payload["apiKey"], REDACTED)
        self.assertEqual(payload["audio"], "<3 bytes redacted>")
        self.assertEqual(payload["safe"], "hello")

    def test_run_logger_redacts_known_secret_values(self) -> None:
        raw_key = "sk-run-secret-456"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"INCEPTION_API_KEY": raw_key}, clear=True):
            logger = RunLogger(root=tmp)
            logger.write("credential.check", nested={"token": raw_key}, text=f"prefix {raw_key} suffix")
            content = logger.path.read_text(encoding="utf-8")

        self.assertNotIn(raw_key, content)
        self.assertIn(REDACTED, content)


class HelperReadinessTests(unittest.TestCase):
    def test_readiness_reports_issues_until_audio_and_keys_are_available(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            issues = helper_readiness_issues(
                transport_ready=True,
                audio_ready=False,
                dotenv_path="/tmp/mortic-missing-dotenv",
            )

        codes = {issue["diagnosticCode"] for issue in issues}
        serialized = json.dumps(issues)
        self.assertEqual(
            codes,
            {
                "audio_dependency_unavailable",
                "missing_voice_audio_key",
                "missing_voice_turn_key",
                "missing_cartesia_api_key",
            },
        )
        self.assertNotIn("DEEPGRAM_API_KEY", serialized)
        self.assertNotIn("INCEPTION_API_KEY", serialized)
        self.assertNotIn("Deepgram", serialized)
        self.assertNotIn("Mercury", serialized)

    def test_readiness_has_no_issues_when_runtime_checks_pass(self) -> None:
        with patch.dict(
            os.environ,
            {"DEEPGRAM_API_KEY": "audio-key", "INCEPTION_API_KEY": "turn-key"},
            clear=True,
        ):
            issues = helper_readiness_issues(transport_ready=True, audio_ready=True)

        self.assertEqual(issues, ())

    def test_run_logger_summarizes_prompt_content_but_keeps_turn_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = RunLogger(root=tmp)
            logger.write("turn.debug", turn_id=7, text="do not persist this prompt", raw={"payload": "large"})
            record = json.loads(logger.path.read_text(encoding="utf-8").splitlines()[-1])

        self.assertEqual(record["turn_id"], 7)
        self.assertEqual(record["text"]["kind"], "text")
        self.assertEqual(record["text"]["chars"], len("do not persist this prompt"))
        self.assertEqual(record["raw"]["kind"], "dict")


class HealthEndpointTests(unittest.TestCase):
    def test_helper_exposes_no_browser_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "opencode_voice.server.helper_readiness_issues",
            return_value=(),
        ):
            app = create_app(
                VoiceConfig(opencode_url="http://opencode.test", run_root=tmp),
                client_factory=lambda _url, _timeout: FakeOpenCodeClient(),
            )
            with TestClient(app) as client:
                for path in (
                    "/",
                    "/app.js",
                    "/styles.css",
                    "/api/sessions",
                    "/docs",
                    "/redoc",
                    "/openapi.json",
                ):
                    with self.subTest(path=path):
                        self.assertEqual(client.get(path).status_code, 404)

    def test_health_never_500s_when_opencode_is_unreachable(self) -> None:
        class UnreachableClient:
            async def health(self) -> dict[str, Any]:
                raise httpx.ConnectError("All connection attempts failed")

            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp, patch(
            "opencode_voice.server.helper_readiness_issues",
            return_value=(),
        ):
            app = create_app(
                VoiceConfig(opencode_url="http://127.0.0.1:4096", run_root=tmp, workspace_dir="/tmp/worktree"),
                client_factory=lambda _url, _timeout: UnreachableClient(),
            )
            with TestClient(app) as client:
                response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["workspace_dir"], "/tmp/worktree")
        self.assertEqual(payload["opencode"]["reachable"], False)
        self.assertEqual(payload["deepgram"]["sample_rate"], 16_000)
        self.assertEqual(payload["deepgram"]["tts_sample_rate"], 16_000)
        self.assertEqual(payload["cartesia"]["sample_rate"], 16_000)
        self.assertEqual(payload["issues"][0]["diagnosticCode"], "opencode_unreachable")
        self.assertEqual(payload["issues"][0]["safeDetail"], "Mortic could not reach its OpenCode voice server.")

    def test_health_reports_missing_voice_agent_before_mic_start(self) -> None:
        class MissingAgentClient(FakeOpenCodeClient):
            async def agents(self) -> list[str]:
                return ["build"]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "opencode_voice.server.helper_readiness_issues",
            return_value=(),
        ):
            app = create_app(
                VoiceConfig(opencode_url="http://127.0.0.1:4096", run_root=tmp, opencode_agent="voice-build"),
                client_factory=lambda _url, _timeout: MissingAgentClient(),
            )
            with TestClient(app) as client:
                response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["opencode"]["agent_present"])
        self.assertEqual(payload["issues"][0]["diagnosticCode"], "opencode_agent_missing")
        self.assertEqual(payload["issues"][0]["safeDetail"], "Mortic voice agent is missing from the OpenCode voice server.")

    def test_lane_start_reaps_stale_voice_tmp_forks_after_source_validation(self) -> None:
        class StaleForkClient(FakeOpenCodeClient):
            async def list_sessions(self) -> list[dict[str, Any]]:
                return [
                    {"id": "fork_stale", "title": "[voice tmp] Source Thread"},
                    {"id": "source_keep", "title": "Source Thread"},
                ]

        fake = StaleForkClient()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "opencode_voice.server.helper_readiness_issues",
            return_value=(),
        ), patch(
            "opencode_voice.server.SidepodConnection.schedule_audio_prewarm",
        ):
            app = create_app(
                VoiceConfig(opencode_url="http://opencode.test", run_root=tmp),
                client_factory=lambda _url, _timeout: fake,
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws/sidepod") as websocket:
                    websocket.send_json(
                        {
                            "type": "start",
                            "protocolVersion": SIDEPOD_PROTOCOL_VERSION,
                            "clientEventId": "evt_reap_1",
                            "sentAt": "2026-07-03T00:00:00.000Z",
                            "sourceSessionId": "source_keep",
                            "keepFork": False,
                        }
                    )
                    ready = websocket.receive_json()
                    self.assertEqual(ready["type"], "ready")
                    self.assertEqual(fake.deleted, ["fork_stale"])

        self.assertEqual(fake.deleted, ["fork_stale", "fork_1"])

    def test_lane_start_preserves_voice_tmp_forks_when_keep_fork_is_default(self) -> None:
        class StaleForkClient(FakeOpenCodeClient):
            async def list_sessions(self) -> list[dict[str, Any]]:
                return [{"id": "fork_debug", "title": "[voice tmp] Debug Thread"}]

        fake = StaleForkClient()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "opencode_voice.server.helper_readiness_issues",
            return_value=(),
        ), patch(
            "opencode_voice.server.SidepodConnection.schedule_audio_prewarm",
        ):
            app = create_app(
                VoiceConfig(opencode_url="http://opencode.test", run_root=tmp, keep_fork_default=True),
                client_factory=lambda _url, _timeout: fake,
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws/sidepod") as websocket:
                    websocket.send_json(
                        {
                            "type": "start",
                            "protocolVersion": SIDEPOD_PROTOCOL_VERSION,
                            "clientEventId": "evt_reap_2",
                            "sentAt": "2026-07-03T00:00:00.000Z",
                            "sourceSessionId": "source_keep",
                            "keepFork": True,
                        }
                    )
                    ready = websocket.receive_json()
                    self.assertEqual(ready["type"], "ready")

        self.assertEqual(fake.deleted, [])


class SidepodTransportTests(unittest.TestCase):
    def test_sidepod_start_ready_and_refresh_clean_up_forks(self) -> None:
        fake = FakeOpenCodeClient()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "opencode_voice.server.helper_readiness_issues",
            return_value=(),
        ), patch(
            "opencode_voice.server.SidepodConnection.schedule_audio_prewarm",
        ):
            app = create_app(
                VoiceConfig(opencode_url="http://opencode.test", run_root=tmp),
                client_factory=lambda _url, _timeout: fake,
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws/sidepod") as websocket:
                    websocket.send_json(
                        {
                            "type": "start",
                            "protocolVersion": SIDEPOD_PROTOCOL_VERSION,
                            "clientEventId": "evt_test_1",
                            "sentAt": "2026-07-03T00:00:00.000Z",
                            "sourceSessionId": "source_1",
                            "keepFork": False,
                        }
                    )
                    ready = websocket.receive_json()
                    self.assertEqual(ready["type"], "ready")
                    self.assertEqual(ready["protocolVersion"], SIDEPOD_PROTOCOL_VERSION)
                    self.assertEqual(ready["sourceSessionId"], "source_1")
                    self.assertEqual(ready["forkSessionId"], "fork_1")

                    websocket.send_json(
                        {
                            "type": "refresh",
                            "clientEventId": "evt_test_2",
                            "sentAt": "2026-07-03T00:00:01.000Z",
                            "reason": "user.confirmed_refresh",
                            "sourceSessionId": "source_1",
                        }
                    )
                    refreshed = websocket.receive_json()
                    self.assertEqual(refreshed["type"], "ready")
                    self.assertEqual(refreshed["forkSessionId"], "fork_2")
                    self.assertEqual(fake.deleted, ["fork_1"])

                self.assertEqual(fake.deleted, ["fork_1", "fork_2"])

    def test_sidepod_readiness_issues_are_protocol_events(self) -> None:
        fake = FakeOpenCodeClient()
        issue = voice_bridge_issue_payload(
            capability="voice_audio",
            diagnostic_code="audio_dependency_unavailable",
            safe_detail="Audio capture unavailable",
            sent_at="2026-07-03T00:00:00.000Z",
        )
        with tempfile.TemporaryDirectory() as tmp, patch(
            "opencode_voice.server.helper_readiness_issues",
            return_value=(issue,),
        ):
            app = create_app(
                VoiceConfig(opencode_url="http://opencode.test", run_root=tmp),
                client_factory=lambda _url, _timeout: fake,
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws/sidepod") as websocket:
                    event = websocket.receive_json()
                    websocket.send_json(
                        {
                            "type": "start",
                            "protocolVersion": SIDEPOD_PROTOCOL_VERSION,
                            "clientEventId": "evt_blocked",
                            "sentAt": "2026-07-03T00:00:00.000Z",
                            "sourceSessionId": "source_1",
                            "keepFork": False,
                        }
                    )
                    blocked = websocket.receive_json()

        self.assertEqual(event["type"], "voice_bridge_issue")
        self.assertEqual(event["userMessage"], "Voice Bridge Issue")
        self.assertEqual(event["diagnosticCode"], "audio_dependency_unavailable")
        self.assertEqual(blocked["diagnosticCode"], "audio_dependency_unavailable")
        self.assertEqual(fake.fork_count, 0)

    def test_sidepod_rejects_unsupported_protocol_without_forking(self) -> None:
        fake = FakeOpenCodeClient()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "opencode_voice.server.helper_readiness_issues",
            return_value=(),
        ):
            app = create_app(
                VoiceConfig(opencode_url="http://opencode.test", run_root=tmp),
                client_factory=lambda _url, _timeout: fake,
            )
            with TestClient(app) as client:
                with client.websocket_connect("/ws/sidepod") as websocket:
                    websocket.send_json(
                        {
                            "type": "start",
                            "protocolVersion": "mortic.sidepod.v9",
                            "clientEventId": "evt_test_bad",
                            "sentAt": "2026-07-03T00:00:00.000Z",
                            "sourceSessionId": "source_1",
                            "keepFork": False,
                        }
                    )
                    event = websocket.receive_json()

        self.assertEqual(event["type"], "voice_bridge_issue")
        self.assertEqual(event["diagnosticCode"], "protocol_version_unsupported")
        self.assertFalse(event["retryable"])
        self.assertEqual(fake.fork_count, 0)


class TokenTests(unittest.TestCase):
    def test_session_tokens_are_usage_accounting(self) -> None:
        self.assertEqual(
            session_usage_tokens({"tokens": {"input": 70000, "output": 12, "reasoning": 3, "cache": {"read": 9}}}),
            70015,
        )
        self.assertEqual(
            session_context_tokens({"tokens": {"input": 70000, "output": 12, "reasoning": 3, "cache": {"read": 9}}}),
            70015,
        )

    def test_active_context_uses_latest_assistant_input_tokens(self) -> None:
        estimate = active_context_estimate(
            [
                {
                    "info": {"id": "msg_user", "role": "user", "time": {"created": 1}},
                    "parts": [{"type": "text", "text": "hello"}],
                },
                {
                    "info": {
                        "id": "msg_assistant",
                        "role": "assistant",
                        "time": {"created": 2, "completed": 3},
                        "tokens": {"input": 521, "output": 100, "cache": {"read": 70000}},
                    },
                    "parts": [{"type": "text", "text": "hi"}],
                },
            ]
        )

        self.assertEqual(estimate.tokens, 70521)
        self.assertEqual(estimate.source, "assistant_input")
        self.assertEqual(estimate.measured_message_id, "msg_assistant")

    def test_active_context_resets_after_completed_summary(self) -> None:
        messages = [
            {
                "info": {"id": "msg_user", "role": "user", "time": {"created": 1}},
                "parts": [{"type": "text", "text": "A" * 300_000}],
            },
            {
                "info": {
                    "id": "msg_assistant",
                    "role": "assistant",
                    "time": {"created": 2, "completed": 3},
                    "tokens": {"input": 75000, "output": 20},
                },
                "parts": [{"type": "text", "text": "older answer"}],
            },
            {
                "info": {"id": "msg_compaction", "role": "user", "time": {"created": 4}},
                "parts": [{"type": "compaction", "auto": False}],
            },
            {
                "info": {
                    "id": "msg_summary",
                    "role": "assistant",
                    "summary": True,
                    "finish": "stop",
                    "time": {"created": 5, "completed": 6},
                    "tokens": {"input": 76000, "output": 128},
                },
                "parts": [{"type": "text", "text": "Short summary."}],
            },
        ]

        estimate = active_context_estimate(messages)

        self.assertLess(estimate.tokens, 70_000)
        self.assertEqual(estimate.source, "content_estimate")
        self.assertEqual(estimate.summary_message_id, "msg_summary")

    def test_errored_summary_does_not_reset_active_context(self) -> None:
        messages = [
            {
                "info": {
                    "id": "msg_assistant",
                    "role": "assistant",
                    "time": {"created": 1, "completed": 2},
                    "tokens": {"input": 80_000, "output": 20},
                },
                "parts": [{"type": "text", "text": "active answer"}],
            },
            {
                "info": {
                    "id": "msg_failed_summary",
                    "role": "assistant",
                    "summary": True,
                    "finish": "error",
                    "error": {"name": "UnknownError"},
                    "time": {"created": 3, "completed": 4},
                },
                "parts": [],
            },
        ]

        estimate = active_context_estimate(messages)

        self.assertEqual(estimate.tokens, 80_000)
        self.assertIsNone(estimate.summary_message_id)


class SSEParserTests(unittest.TestCase):
    def test_parses_multiline_data_frame(self) -> None:
        parser = SSEParser()

        self.assertIsNone(parser.push_line("event: message"))
        self.assertIsNone(parser.push_line('data: {"type":'))
        self.assertIsNone(parser.push_line('data: "session.idle"}'))
        event = parser.push_line("")

        self.assertEqual(event, {"type": "session.idle"})

    def test_skips_malformed_data_frame(self) -> None:
        parser = SSEParser()

        parser.push_line("data: {not json")

        self.assertIsNone(parser.push_line(""))


class OpenCodeStructuredMessageCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_legacy_tail_keeps_the_v2_projection(self) -> None:
        projected = [
            {"info": {"id": "msg_user", "role": "user"}, "parts": []},
            {"info": {"id": "msg_assistant", "role": "assistant"}, "parts": []},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/api/session/"):
                return httpx.Response(200, json={"data": projected, "cursor": None})
            return httpx.Response(
                400,
                json={"name": "BadRequest", "data": {"message": "retryCount decode failed"}},
            )

        client = OpenCodeClient("http://opencode.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            base_url="http://opencode.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            messages = await client.messages("ses_structured")
        finally:
            await client.close()

        self.assertEqual(messages, projected)

    async def test_empty_v2_projection_falls_back_to_decodable_legacy_tail(self) -> None:
        recent = [
            {"info": {"id": "msg_user", "role": "user"}, "parts": []},
            {"info": {"id": "msg_assistant", "role": "assistant"}, "parts": []},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/api/session/"):
                return httpx.Response(200, json={"data": [], "cursor": None})
            if request.url.params.get("limit") == "2":
                return httpx.Response(200, json=recent)
            return httpx.Response(400, json={"name": "BadRequest"})

        client = OpenCodeClient("http://opencode.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            base_url="http://opencode.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            messages = await client.messages("ses_structured")
        finally:
            await client.close()

        self.assertEqual(messages, recent)

    async def test_nonempty_stale_projection_is_merged_with_recent_structured_pair(self) -> None:
        summary = {"info": {"id": "msg_summary", "role": "assistant", "summary": True}, "parts": []}
        recent = [
            {"info": {"id": "msg_user", "role": "user"}, "parts": []},
            {"info": {"id": "msg_assistant", "role": "assistant"}, "parts": []},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/api/session/"):
                return httpx.Response(200, json={"data": [summary], "cursor": None})
            if request.url.params.get("limit") == "2":
                return httpx.Response(200, json=recent)
            return httpx.Response(400, json={"name": "BadRequest"})

        client = OpenCodeClient("http://opencode.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            base_url="http://opencode.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            messages = await client.messages("ses_structured")
        finally:
            await client.close()

        self.assertEqual(messages, [summary, *recent])


class EventSessionIdTests(unittest.TestCase):
    def test_resolves_top_level_session_id(self) -> None:
        event = {"type": "session.idle", "properties": {"sessionID": "ses_1"}}

        self.assertEqual(event_session_id(event), "ses_1")

    def test_resolves_message_updated_info_session_id(self) -> None:
        event = {
            "type": "message.updated",
            "properties": {"info": {"id": "msg_1", "role": "assistant", "sessionID": "ses_1"}},
        }

        self.assertEqual(event_session_id(event), "ses_1")

    def test_resolves_part_updated_nested_session_id(self) -> None:
        event = {
            "type": "message.part.updated",
            "properties": {"part": {"id": "prt_1", "sessionID": "ses_1", "type": "text"}},
        }

        self.assertEqual(event_session_id(event), "ses_1")

    def test_returns_empty_when_absent(self) -> None:
        self.assertEqual(event_session_id({"type": "server.connected", "properties": {}}), "")


class DeepgramProtocolTests(unittest.TestCase):
    def test_flux_url_uses_v2_listen_and_eighty_ms_compatible_audio_params(self) -> None:
        url = build_flux_url("flux-general-en", 16000, 0.7, 5000, eager_eot_threshold=0.5)

        self.assertTrue(url.startswith("wss://api.deepgram.com/v2/listen?"))
        self.assertIn("model=flux-general-en", url)
        self.assertIn("encoding=linear16", url)
        self.assertIn("sample_rate=16000", url)
        self.assertIn("eager_eot_threshold=0.5", url)

    def test_parse_flux_turn_events(self) -> None:
        start = parse_flux_message(json.dumps({"type": "StartOfTurn"}))
        self.assertEqual(start["type"], "speech.start")

        transcript = parse_flux_message(
            json.dumps(
                {
                    "type": "Results",
                    "is_final": True,
                    "channel": {"alternatives": [{"transcript": "hello mercury"}]},
                }
            )
        )
        self.assertEqual(transcript["type"], "speech.transcript")
        self.assertEqual(transcript["transcript"], "hello mercury")
        self.assertTrue(transcript["is_final"])

        end = parse_flux_message(json.dumps({"type": "EndOfTurn"}))
        self.assertEqual(end["type"], "speech.end")

    def test_parse_flux_mean_word_confidence(self) -> None:
        end = parse_flux_message(
            json.dumps(
                {
                    "type": "TurnInfo",
                    "event": "EndOfTurn",
                    "transcript": "stop that",
                    "words": [
                        {"word": "stop", "confidence": 0.9},
                        {"word": "that", "confidence": 0.5},
                    ],
                }
            )
        )
        self.assertEqual(end["type"], "speech.end")
        self.assertAlmostEqual(end["confidence"], 0.7)

        no_words = parse_flux_message(json.dumps({"type": "TurnInfo", "event": "EndOfTurn"}))
        self.assertNotIn("confidence", no_words)

    def test_parse_flux_turninfo_shape(self) -> None:
        start = parse_flux_message(
            json.dumps({"type": "TurnInfo", "event": "StartOfTurn", "transcript": "Hello."})
        )
        self.assertEqual(start["type"], "speech.start")
        self.assertEqual(start["transcript"], "Hello.")

        update = parse_flux_message(
            json.dumps({"type": "TurnInfo", "event": "Update", "transcript": "Hello from Mercury"})
        )
        self.assertEqual(update["type"], "speech.transcript")
        self.assertEqual(update["transcript"], "Hello from Mercury")

        end = parse_flux_message(
            json.dumps({"type": "TurnInfo", "event": "EndOfTurn", "transcript": "Hello from Mercury"})
        )
        self.assertEqual(end["type"], "speech.end")
        self.assertTrue(end["is_final"])


class EchoProbeTests(unittest.TestCase):
    SAMPLE_RATE = 16_000

    @staticmethod
    def speechlike_pcm(seconds: float, sample_rate: int, seed: int) -> bytes:
        """Noise with syllable-like amplitude modulation AND per-utterance
        spectral coloration (random resonances standing in for formants).
        Both matter: all human speech shares roughly the same syllable rate,
        so single-envelope periodicity alone makes unrelated segments look
        alike — which is exactly the false positive the multi-band probe is
        built to avoid, and what this fixture must be able to expose."""
        import numpy as np

        rng = np.random.default_rng(seed)
        n = int(seconds * sample_rate)
        t = np.arange(n) / sample_rate
        envelope = 0.4 + 0.6 * np.abs(
            np.sin(2 * np.pi * 4 * t + rng.uniform(0, 6)) * np.sin(2 * np.pi * 0.7 * t)
        )
        signal = rng.standard_normal(n)
        for _ in range(3):
            frequency = rng.uniform(200, 3500)
            signal += np.sin(2 * np.pi * frequency * t + rng.uniform(0, 6)) * rng.uniform(0.3, 1.0)
        return (signal * envelope * 6000).astype(np.int16).tobytes()

    def test_shifted_attenuated_copy_correlates_high(self) -> None:
        from opencode_voice.echo_probe import echo_correlation

        import numpy as np

        render = self.speechlike_pcm(2.0, self.SAMPLE_RATE, seed=1)
        samples = np.frombuffer(render, dtype=np.int16).astype(np.float32)
        # Echo: 200ms acoustic delay, 70% attenuation, a little room noise.
        delay = int(0.2 * self.SAMPLE_RATE)
        echoed = np.concatenate([np.zeros(delay, dtype=np.float32), samples * 0.3])
        echoed += np.random.default_rng(2).standard_normal(len(echoed)) * 200
        mic = echoed.astype(np.int16).tobytes()

        self.assertGreaterEqual(echo_correlation(mic, render, self.SAMPLE_RATE), 0.75)

    def test_independent_speech_correlates_low(self) -> None:
        from opencode_voice.echo_probe import echo_correlation

        render = self.speechlike_pcm(2.0, self.SAMPLE_RATE, seed=3)
        for seed in (42, 7, 11, 99, 123):
            mic = self.speechlike_pcm(1.2, self.SAMPLE_RATE, seed=seed)
            self.assertLess(echo_correlation(mic, render, self.SAMPLE_RATE), 0.55, f"seed={seed}")

    def test_user_talking_over_playback_stays_below_the_floor(self) -> None:
        # The mixed-voice case: a real barge-in while the reply's echo tail is
        # still in the mic. The probe must NOT read this as echo (0.6 floor).
        import numpy as np

        from opencode_voice.echo_probe import echo_correlation

        render = self.speechlike_pcm(2.0, self.SAMPLE_RATE, seed=3)
        render_samples = np.frombuffer(render, dtype=np.int16).astype(np.float32)
        delay = int(0.2 * self.SAMPLE_RATE)
        echo_tail = np.concatenate([np.zeros(delay, dtype=np.float32), render_samples * 0.3])
        user = np.frombuffer(self.speechlike_pcm(1.2, self.SAMPLE_RATE, seed=42), dtype=np.int16).astype(np.float32)
        mixed = (user * 0.8 + echo_tail[: len(user)] * 0.5).astype(np.int16).tobytes()

        self.assertLess(echo_correlation(mixed, render, self.SAMPLE_RATE), 0.6)

    def test_too_short_segments_score_zero(self) -> None:
        from opencode_voice.echo_probe import echo_correlation

        blip = self.speechlike_pcm(0.1, self.SAMPLE_RATE, seed=5)
        render = self.speechlike_pcm(2.0, self.SAMPLE_RATE, seed=6)

        self.assertEqual(echo_correlation(blip, render, self.SAMPLE_RATE), 0.0)
        self.assertEqual(echo_correlation(b"", render, self.SAMPLE_RATE), 0.0)

    def test_ring_buffer_extracts_by_wall_clock_and_trims(self) -> None:
        from opencode_voice.echo_probe import PcmRingBuffer

        ring = PcmRingBuffer(self.SAMPLE_RATE, max_sec=2.0, direction="ending")
        base = 1000.0
        frame = b"\x01\x00" * int(0.08 * self.SAMPLE_RATE)  # 80ms
        for i in range(50):  # 4s of frames; only the last 2s survive
            ring.append(frame, at=base + i * 0.08)
        self.assertLessEqual(len(ring.frames), 26)
        segment = ring.extract(base + 3.8, base + 4.0)
        self.assertGreaterEqual(len(segment), len(frame) * 2)
        self.assertEqual(ring.extract(base + 100, base + 101), b"")


class TTSTests(unittest.TestCase):
    def test_chunker_flushes_sentences_and_caps_long_chunks(self) -> None:
        chunker = TTSChunker(preferred_chars=20, max_chars=40)
        self.assertEqual(chunker.push("Hello there. More text"), ["Hello there."])
        self.assertEqual(chunker.flush(), ["More text"])

        chunker = TTSChunker(preferred_chars=20, max_chars=10)
        chunks = chunker.push("alpha beta gamma")
        self.assertEqual(chunks, ["alpha", "beta"])
        self.assertLessEqual(max(len(chunk) for chunk in chunks), 10)

    def test_flush_limiter_respects_window(self) -> None:
        limiter = FlushLimiter(max_flushes=2, window_sec=10)
        self.assertTrue(limiter.allow(now=0))
        self.assertTrue(limiter.allow(now=1))
        self.assertFalse(limiter.allow(now=2))
        self.assertTrue(limiter.allow(now=11))

    # SpeechTextFilter is currently unwired: the structured-turn contract asks
    # the model for speech-ready `spokenText` instead of post-filtering deltas.
    # Kept covered because it is the starting point for a spoken-text
    # normalizer targeting the contract's own safety codes.
    def test_speech_filter_removes_fenced_code(self) -> None:
        filter_ = SpeechTextFilter()

        spoken = filter_.push("Here is the file.\n```python\nprint('nope')\n```\nDone.\n")

        self.assertIn("Here is the file.", spoken)
        self.assertNotIn("print", spoken)
        self.assertIn("Done.", spoken)

    def test_speech_filter_removes_markdown_code_details(self) -> None:
        filter_ = SpeechTextFilter()

        spoken = filter_.push(
            "I created **`paninian_tokenizer.py`** in the project root.\n"
            "It provides a simple pipeline:\n"
            "1. **`basic_tokenize`** - splits text into words and punctuation.\n"
            "2. **`sandhi_split`** - naive Sandhi splitter.\n"
            "You can run the script directly:\n"
            "```bash\n"
            "python paninian_tokenizer.py\n"
            "```\n"
            "or import `parse_sentence` in your own code.\n"
        )

        self.assertIn("I created the file in the project root.", spoken)
        self.assertIn("It provides a simple pipeline.", spoken)
        self.assertNotIn("paninian_tokenizer.py", spoken)
        self.assertNotIn("basic_tokenize", spoken)
        self.assertNotIn("sandhi_split", spoken)
        self.assertNotIn("python paninian_tokenizer.py", spoken)
        self.assertNotIn("parse_sentence", spoken)

    def test_speech_filter_releases_safe_partial_sentences(self) -> None:
        filter_ = SpeechTextFilter()

        spoken = filter_.push("Done. I am still forming the next sentence")

        self.assertEqual(spoken, "Done.")
        self.assertEqual(filter_.flush(), "I am still forming the next sentence")


class NativeSpeakerPlaybackTests(unittest.IsolatedAsyncioTestCase):
    """Playback buffering: TTS delivers faster than realtime, so play() must
    apply backpressure instead of dropping audio (one live turn lost 3.8s of
    speech to queue overflow), and pause/resume must hold chunks intact."""

    def make_speaker(self, tmp: str, maxsize: int) -> Any:
        from opencode_voice.server import NativeSpeakerSession

        speaker = NativeSpeakerSession(
            # These queue mechanics use one provider frame per device frame;
            # 16 -> 48 kHz conversion is covered by test_native_speaker.py.
            config=VoiceConfig(
                opencode_url="http://opencode.test",
                run_root=tmp,
                tts_sample_rate=48_000,
                device_sample_rate=48_000,
            ),
            logger=RunLogger(root=tmp),
            on_issue=self._no_issue,
        )
        # Wired by hand: no sounddevice stream (write_output becomes a no-op),
        # which exercises the queue/pump machinery exactly as production does.
        speaker.queue = asyncio.Queue(maxsize=maxsize)
        speaker.pump_task = asyncio.create_task(speaker.pump())
        return speaker

    @staticmethod
    async def _no_issue(payload: dict[str, object]) -> None:
        return None

    async def test_fast_producer_loses_no_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            speaker = self.make_speaker(tmp, maxsize=2)
            for index in range(20):
                self.assertTrue(await speaker.play(bytes([index]) * 960, turn_id=1))
            while speaker.played_chunks < 20:
                await asyncio.sleep(0.001)
            await speaker.close()

            self.assertEqual(speaker.played_chunks, 20)
            self.assertEqual(speaker.dropped_chunks, 0)

    async def test_close_unblocks_a_pending_put_and_counts_teardown_discard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # maxsize=1: the paused pump holds the first chunk, the second
            # fills the queue, so the third put genuinely blocks.
            speaker = self.make_speaker(tmp, maxsize=1)
            speaker.pause()
            await speaker.play(b"\x01" * 960, turn_id=1)
            await speaker.play(b"\x02" * 960, turn_id=1)
            blocked = asyncio.create_task(speaker.play(b"\x03" * 960, turn_id=1))
            await asyncio.sleep(0.01)
            self.assertFalse(blocked.done())

            await speaker.close()

            self.assertFalse(await blocked)  # woke up and reported closed
            self.assertGreaterEqual(speaker.dropped_chunks, 1)  # teardown discard only

    async def test_pause_holds_playback_and_resume_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            speaker = self.make_speaker(tmp, maxsize=8)
            speaker.pause()
            await speaker.play(b"\x01" * 960, turn_id=1)
            await asyncio.sleep(0.02)
            self.assertEqual(speaker.played_chunks, 0)

            speaker.resume()
            while speaker.played_chunks < 1:
                await asyncio.sleep(0.001)
            self.assertEqual(speaker.played_chunks, 1)
            await speaker.close()


if __name__ == "__main__":
    unittest.main()
