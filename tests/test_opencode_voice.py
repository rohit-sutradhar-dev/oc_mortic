from __future__ import annotations

import json
import unittest

from opencode_voice.config import ModelRef, render_opencode_config
from opencode_voice.deepgram import FlushLimiter, SpeechTextFilter, TTSChunker, build_flux_url, parse_flux_message
from opencode_voice.state import AssistantTextTracker, session_context_tokens


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

    def test_ephemeral_voice_agent_prompt_is_configured_when_supplied(self) -> None:
        config = render_opencode_config(
            ModelRef(provider_id="inception", model_id="mercury-2"),
            voice_agent_prompt="Do not speak code.",
            voice_agent_name="voice-build",
        )

        self.assertEqual(config["agent"]["voice-build"]["prompt"], "Do not speak code.")
        self.assertEqual(config["agent"]["voice-build"]["mode"], "primary")
        self.assertEqual(config["agent"]["voice-build"]["model"], "inception/mercury-2")


class TokenTests(unittest.TestCase):
    def test_context_tokens_sum_input_output_reasoning(self) -> None:
        self.assertEqual(
            session_context_tokens({"tokens": {"input": 70000, "output": 12, "reasoning": 3, "cache": {"read": 9}}}),
            70015,
        )


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


if __name__ == "__main__":
    unittest.main()
