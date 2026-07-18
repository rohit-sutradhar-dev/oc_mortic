from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from opencode_voice.config import VoiceConfig
from opencode_voice.logging import RunLogger
from opencode_voice.telemetry import (
    BUILD_SHA_ENV,
    RunClock,
    RunMetadata,
    resolve_build_sha,
    snapshot_voice_config,
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
        self.assertEqual(metadata["voice_config"]["response_mode"], "structured")
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


if __name__ == "__main__":
    unittest.main()
