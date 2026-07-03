from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_voice.config import (
    REDACTED,
    ModelRef,
    load_local_dotenv,
    load_voice_credentials,
    redact_secrets,
    render_opencode_config,
)
from opencode_voice.deepgram import FlushLimiter, SpeechTextFilter, TTSChunker, build_flux_url, parse_flux_message
from opencode_voice.logging import RunLogger
from opencode_voice.opencode_client import SSEParser
from opencode_voice.server import helper_readiness_issues
from opencode_voice.state import (
    AssistantTextTracker,
    OpenCodeEventTurnTracker,
    active_context_estimate,
    session_context_tokens,
    session_usage_tokens,
)


class MercuryConfigTests(unittest.TestCase):
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

        self.assertEqual(len(events), 2)
        self.assertEqual({event["type"] for event in events}, {"voice_bridge_issue"})
        self.assertEqual({event["userMessage"] for event in events}, {"Voice Bridge Issue"})
        self.assertIn("voice_audio", {event["capability"] for event in events})
        self.assertIn("voice_turns", {event["capability"] for event in events})
        self.assertNotIn("DEEPGRAM_API_KEY", serialized)
        self.assertNotIn("INCEPTION_API_KEY", serialized)
        self.assertNotIn("Deepgram", serialized)
        self.assertNotIn("Mercury", serialized)

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


class AssistantTrackerTests(unittest.TestCase):
    def test_tracks_only_new_assistant_delta(self) -> None:
        before = [
            {
                "info": {"id": "msg_old", "role": "assistant", "time": {"completed": 1}},
                "parts": [{"type": "text", "text": "old text"}],
            }
        ]
        tracker = AssistantTextTracker(before)
        update = tracker.update(
            before
            + [
                {
                    "info": {"id": "msg_new", "role": "assistant", "time": {"created": 2}},
                    "parts": [{"type": "text", "text": "hel"}],
                }
            ]
        )
        self.assertEqual(update.deltas, ["hel"])
        update = tracker.update(
            before
            + [
                {
                    "info": {"id": "msg_new", "role": "assistant", "time": {"created": 2, "completed": 3}},
                    "parts": [{"type": "text", "text": "hello"}],
                }
            ]
        )
        self.assertEqual(update.deltas, ["lo"])
        self.assertTrue(update.completed)
        self.assertEqual(update.full_text, "hello")

    def test_tracks_new_zero_text_assistant_error(self) -> None:
        tracker = AssistantTextTracker([])
        update = tracker.update(
            [
                {
                    "info": {
                        "id": "msg_err",
                        "role": "assistant",
                        "time": {"created": 1, "completed": 2},
                        "error": {"name": "APIError"},
                    },
                    "parts": [],
                }
            ]
        )

        self.assertEqual(update.message_id, "msg_err")
        self.assertTrue(update.completed)
        self.assertEqual(update.error, {"name": "APIError"})


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


class OpenCodeEventTurnTrackerTests(unittest.TestCase):
    def test_ignores_user_text_part_updates(self) -> None:
        tracker = OpenCodeEventTurnTracker("ses_1", existing_message_ids=set())
        tracker.update(
            {
                "type": "message.updated",
                "properties": {"sessionID": "ses_1", "info": {"id": "msg_user", "role": "user"}},
            }
        )

        update = tracker.update(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_1",
                    "part": {"id": "prt_1", "messageID": "msg_user", "type": "text", "text": "hello"},
                },
            }
        )

        self.assertEqual(update.deltas, [])
        self.assertEqual(update.full_text, "")

    def test_accepts_assistant_text_delta(self) -> None:
        tracker = OpenCodeEventTurnTracker("ses_1", existing_message_ids={"msg_old"})
        tracker.update(
            {
                "type": "message.updated",
                "properties": {"sessionID": "ses_1", "info": {"id": "msg_new", "role": "assistant"}},
            }
        )

        update = tracker.update(
            {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": "ses_1",
                    "messageID": "msg_new",
                    "partID": "prt_1",
                    "field": "text",
                    "delta": "hel",
                },
            }
        )

        self.assertEqual(update.deltas, ["hel"])
        self.assertEqual(update.full_text, "hel")

    def test_deduplicates_full_text_updates(self) -> None:
        tracker = OpenCodeEventTurnTracker("ses_1", existing_message_ids=set())
        tracker.update(
            {
                "type": "message.updated",
                "properties": {"sessionID": "ses_1", "info": {"id": "msg_new", "role": "assistant"}},
            }
        )

        first = tracker.update(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_1",
                    "part": {"id": "prt_1", "messageID": "msg_new", "type": "text", "text": "hel"},
                },
            }
        )
        second = tracker.update(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_1",
                    "part": {"id": "prt_1", "messageID": "msg_new", "type": "text", "text": "hello"},
                },
            }
        )
        duplicate = tracker.update(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_1",
                    "part": {"id": "prt_1", "messageID": "msg_new", "type": "text", "text": "hello"},
                },
            }
        )

        self.assertEqual(first.deltas, ["hel"])
        self.assertEqual(second.deltas, ["lo"])
        self.assertEqual(duplicate.deltas, [])
        self.assertEqual(duplicate.full_text, "hello")

    def test_completes_on_session_idle(self) -> None:
        tracker = OpenCodeEventTurnTracker("ses_1", existing_message_ids=set())

        update = tracker.update({"type": "session.idle", "properties": {"sessionID": "ses_1"}})

        self.assertTrue(update.completed)


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


if __name__ == "__main__":
    unittest.main()
