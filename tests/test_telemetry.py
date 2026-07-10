from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_voice.config import VoiceConfig
from opencode_voice.logging import RAW_AUDIO_RETENTION_SEC, RunLogger, prune_expired_audio_captures
from opencode_voice.telemetry import (
    BUILD_SHA_ENV,
    CorrelationContext,
    RunClock,
    RunMetadata,
    derive_latencies,
    resolve_build_sha,
    safe_provider_error,
    snapshot_voice_config,
    validate_phase_order,
)


class RunMetadataTests(unittest.TestCase):
    def test_snapshot_is_an_explicit_safe_allow_list_with_stable_fingerprint(self) -> None:
        secret = "cartesia-secret-value"
        config = VoiceConfig(
            opencode_url=f"https://user:{secret}@opencode.invalid",
            workspace_dir=f"/private/{secret}",
            tts_provider="cartesia",
            voice_duplex="auto",
            deepgram_sample_rate=16_000,
            cartesia_voice_id=secret,
        )
        with patch.dict(
            os.environ,
            {
                "DEEPGRAM_API_KEY": secret,
                "INCEPTION_API_KEY": secret,
                "CARTESIA_API_KEY": secret,
            },
            clear=True,
        ):
            snapshot = snapshot_voice_config(
                config,
                capture_sample_rate_hz=48_000,
                playback_sample_rate_hz=48_000,
                mic_queue_blocks=64,
                playback_queue_chunks=256,
                jitter_buffer_target_ms=120,
                network_profile="clean",
            )
            metadata = RunMetadata.create(
                snapshot,
                build_sha="A" * 40,
                version="0.1.0",
            ).as_fields()

        serialized = json.dumps(metadata, sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("opencode.invalid", serialized)
        self.assertNotIn("workspace", serialized)
        self.assertEqual(metadata["build_sha"], "a" * 40)
        self.assertEqual(metadata["voice_config"]["capture_sample_rate_hz"], 48_000)
        self.assertEqual(metadata["voice_config"]["tts_provider"], "cartesia")
        self.assertTrue(str(metadata["config_fingerprint"]).startswith("sha256:"))

        same = snapshot_voice_config(
            config,
            capture_sample_rate_hz=48_000,
            playback_sample_rate_hz=48_000,
            mic_queue_blocks=64,
            playback_queue_chunks=256,
            jitter_buffer_target_ms=120,
            network_profile="clean",
        )
        changed = dataclasses.replace(snapshot, playback_queue_chunks=128)
        self.assertEqual(snapshot.fingerprint, same.fingerprint)
        self.assertNotEqual(snapshot.fingerprint, changed.fingerprint)

    def test_invalid_build_sha_environment_is_not_logged_or_executed(self) -> None:
        secret = "not-a-sha-secret"

        def runner(*_args: object, **_kwargs: object) -> object:
            self.fail("an explicitly configured invalid SHA must not run git")

        result = resolve_build_sha(environ={BUILD_SHA_ENV: secret}, runner=runner)  # type: ignore[arg-type]

        self.assertEqual(result, "unknown")
        self.assertNotIn(secret, result)

    def test_snapshot_rejects_invalid_queue_and_rate_values(self) -> None:
        config = VoiceConfig(opencode_url="http://opencode.test")

        with self.assertRaises(ValueError):
            snapshot_voice_config(config, mic_queue_blocks=0)
        with self.assertRaises(ValueError):
            snapshot_voice_config(config, capture_sample_rate_hz=-1)
        with self.assertRaises(ValueError):
            snapshot_voice_config(config, playback_sample_rate_hz=0)

    def test_snapshot_preserves_separate_stt_tts_and_device_clocks(self) -> None:
        config = VoiceConfig(opencode_url="http://opencode.test")

        snapshot = snapshot_voice_config(config)

        self.assertEqual(snapshot.stt_sample_rate_hz, config.deepgram_sample_rate)
        self.assertEqual(snapshot.tts_sample_rate_hz, config.tts_sample_rate)
        self.assertEqual(snapshot.capture_sample_rate_hz, config.device_sample_rate)
        self.assertEqual(snapshot.playback_sample_rate_hz, config.device_sample_rate)


class MonotonicLogTests(unittest.TestCase):
    def test_logger_adds_non_decreasing_run_elapsed_ms(self) -> None:
        samples = iter((10.0, 10.125, 10.100, 10.500))
        clock = RunClock(monotonic=lambda: next(samples))

        with tempfile.TemporaryDirectory() as tmp:
            logger = RunLogger(root=tmp, clock=clock)
            logger.write("one")
            logger.write("two", run_elapsed_ms=999_999)
            logger.write("three")
            records = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([record["run_elapsed_ms"] for record in records], [125, 125, 500])


class CorrelationContractTests(unittest.TestCase):
    def test_context_serializes_complete_cross_pipeline_identity(self) -> None:
        context = CorrelationContext(
            voice_lane_id="lane_1",
            turn_id="turn_4",
            flux_epoch=2,
            flux_turn_index=0,
            stt_episode_id="stt_2_0",
            interruption_episode_id="interrupt_7",
            playback_generation=9,
            playback_burst_id="burst_11",
            provider_request_id="request_12",
            provider_context_id="context_13",
        )

        self.assertEqual(set(context.as_fields()), {
            "voice_lane_id",
            "turn_id",
            "flux_epoch",
            "flux_turn_index",
            "stt_episode_id",
            "interruption_episode_id",
            "playback_generation",
            "playback_burst_id",
            "provider_request_id",
            "provider_context_id",
        })
        self.assertEqual(context.missing_for("stt"), ())
        self.assertEqual(context.missing_for("interruption"), ())
        self.assertEqual(context.missing_for("provider_context"), ())

        next_generation = context.with_updates(playback_generation=10, playback_burst_id="burst_12")
        self.assertEqual(context.playback_generation, 9)
        self.assertEqual(next_generation.playback_generation, 10)

    def test_profiles_report_missing_ids_deterministically(self) -> None:
        context = CorrelationContext(flux_epoch=3)

        self.assertEqual(context.missing_for("stt"), ("flux_turn_index", "stt_episode_id"))
        self.assertEqual(
            context.missing_for("provider_context"),
            ("playback_generation", "provider_request_id", "provider_context_id"),
        )

    def test_content_bearing_or_negative_ids_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CorrelationContext(provider_request_id="Authorization: secret")
        with self.assertRaises(ValueError):
            CorrelationContext(turn_id=7)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            CorrelationContext(playback_generation=-1)


class LatencyContractTests(unittest.TestCase):
    def test_valid_phases_derive_metrics_from_one_monotonic_origin(self) -> None:
        phases = {
            "speech_started": 100,
            "first_transcript": 220,
            "end_of_turn": 800,
            "turn_committed": 810,
            "first_assistant_text": 1_900,
            "tts_requested": 1_910,
            "tts_first_audio": 2_080,
            "playback_started": 2_100,
            "playback_drained": 4_000,
            "turn_completed": 4_010,
            "interruption_candidate": 2_300,
            "playback_paused": 2_320,
            "interruption_committed": 2_410,
            "playback_stopped": 2_450,
        }

        self.assertEqual(validate_phase_order(phases), ())
        self.assertEqual(
            derive_latencies(phases),
            {
                "speech_to_first_transcript_ms": 120,
                "end_of_turn_to_first_assistant_text_ms": 1_100,
                "assistant_text_to_tts_first_audio_ms": 180,
                "end_of_turn_to_playback_ms": 1_300,
                "interruption_candidate_to_pause_ms": 20,
                "interruption_commit_to_stop_ms": 40,
            },
        )

    def test_impossible_phase_order_is_reported_and_not_derived(self) -> None:
        phases = {
            "speech_started": 100,
            "first_transcript": 5_000,
            "turn_completed": 2_000,
        }

        violations = validate_phase_order(phases)

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].earlier_phase, "first_transcript")
        self.assertEqual(violations[0].later_phase, "turn_completed")
        with self.assertRaisesRegex(ValueError, "invalid phase order"):
            derive_latencies(phases)


class ProviderErrorTests(unittest.TestCase):
    def test_provider_error_discards_messages_and_unsafe_categories(self) -> None:
        secret = "Bearer super-secret-token"
        context = CorrelationContext(
            playback_generation=5,
            provider_request_id="request_5",
            provider_context_id="context_5",
        )
        error = safe_provider_error(
            provider=f"cartesia {secret}",
            stage=f"send {secret}",
            code=f"socket_{secret}",
            retryable=True,
            correlation=context,
            exception=RuntimeError(secret),
            http_status=503,
        )

        serialized = json.dumps(error, sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertEqual(error["provider"], "unknown")
        self.assertEqual(error["stage"], "unknown")
        self.assertEqual(error["code"], "provider_error")
        self.assertEqual(error["exception_type"], "RuntimeError")
        self.assertEqual(error["http_status"], 503)
        self.assertEqual(error["playback_generation"], 5)

    def test_safe_provider_error_can_be_written_without_content(self) -> None:
        record = safe_provider_error(
            provider="cartesia_tts",
            stage="receive",
            code="socket_closed",
            retryable=True,
            correlation=CorrelationContext(
                playback_generation=2,
                provider_request_id="req_2",
                provider_context_id="ctx_2",
            ),
        )
        event = record.pop("event")

        with tempfile.TemporaryDirectory() as tmp:
            logger = RunLogger(root=tmp)
            logger.write(str(event), **record)
            logged = json.loads(logger.path.read_text(encoding="utf-8").splitlines()[-1])

        self.assertEqual(logged["event"], "provider.error")
        self.assertEqual(logged["provider_context_id"], "ctx_2")
        self.assertIn("run_elapsed_ms", logged)

    def test_consented_pcm_capture_is_deleted_after_seven_days_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture_dir = Path(tmp) / "old-run" / "barge_pcm"
            capture_dir.mkdir(parents=True)
            expired = capture_dir / "episode.mic.pcm"
            fresh = capture_dir / "episode.render.pcm"
            event_log = Path(tmp) / "old-run" / "events.jsonl"
            expired.write_bytes(b"private")
            fresh.write_bytes(b"fresh")
            event_log.write_text("{}\n", encoding="utf-8")
            now = time.time()
            os.utime(expired, (now - RAW_AUDIO_RETENTION_SEC - 1, now - RAW_AUDIO_RETENTION_SEC - 1))

            removed = prune_expired_audio_captures(tmp, now=now)

            self.assertEqual(removed, 1)
            self.assertFalse(expired.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(event_log.exists())


if __name__ == "__main__":
    unittest.main()
