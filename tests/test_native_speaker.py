from __future__ import annotations

import asyncio
import json
import math
import struct
import tempfile
import unittest

from opencode_voice.config import VoiceConfig
from opencode_voice.logging import RunLogger
from opencode_voice.playback import PlaybackToken
from opencode_voice.server import NativeSpeakerSession


class NativeSpeakerTerminalTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _no_issue(payload: dict[str, object]) -> None:
        return None

    def make_speaker(
        self,
        tmp: str,
        *,
        on_drain=None,
        maxsize: int = 32,
        start_pump: bool = False,
    ) -> NativeSpeakerSession:
        speaker = NativeSpeakerSession(
            config=VoiceConfig(
                opencode_url="http://opencode.test",
                run_root=tmp,
                tts_sample_rate=48_000,
                device_sample_rate=48_000,
            ),
            logger=RunLogger(root=tmp),
            on_issue=self._no_issue,
            on_drain=on_drain,
        )
        # The fallback's worker and bounded queue are production objects; only
        # the physical PortAudio stream is absent, making each 10 ms frame a
        # deterministic no-op write.
        speaker.queue = asyncio.Queue(maxsize=maxsize)
        if start_pump:
            speaker.pump_task = asyncio.create_task(speaker.pump())
        return speaker

    async def test_nonterminal_underflow_waits_for_full_refill_and_keeps_one_burst(self) -> None:
        drains: list[None] = []

        async def on_drain(_token: PlaybackToken) -> None:
            drains.append(None)

        with tempfile.TemporaryDirectory() as tmp:
            speaker = self.make_speaker(tmp, on_drain=on_drain)
            token = PlaybackToken(0, 1)
            frame = b"\x01\x00" * 480
            self.assertTrue(speaker.begin_turn(token))
            self.assertTrue(await speaker.play(frame * 8, token))
            speaker.pump_task = asyncio.create_task(speaker.pump())

            async def wait_for_chunks(count: int) -> None:
                async with asyncio.timeout(2):
                    while speaker.played_chunks < count:
                        await asyncio.sleep(0.005)

            await wait_for_chunks(8)
            await asyncio.sleep(0.30)
            self.assertEqual(drains, [], "network starvation is not provider EOF")
            self.assertTrue(speaker.burst_active)

            # A partial refill stays silent, so provider jitter cannot create a
            # fresh acoustic edge. The full 80 ms target resumes the same burst.
            self.assertTrue(await speaker.play(frame * 3, token))
            await asyncio.sleep(0.08)
            self.assertEqual(speaker.played_chunks, 8)
            self.assertTrue(await speaker.play(frame * 5, token))
            await wait_for_chunks(16)
            self.assertEqual(drains, [])

            self.assertTrue(await speaker.finish_turn(token))
            async with asyncio.timeout(2):
                while not drains:
                    await asyncio.sleep(0.005)

            records = [json.loads(line) for line in speaker.logger.path.read_text().splitlines()]
            self.assertEqual(sum(record["event"] == "native_tts.burst.start" for record in records), 1)
            self.assertFalse(speaker.burst_active)
            await speaker.close()
            speaker.logger.close()

    async def test_generation_invalidation_discards_partial_pcm_before_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            speaker = self.make_speaker(tmp)
            stale = PlaybackToken(0, 1)
            fresh = PlaybackToken(1, 2)
            stale_half_frame = b"\x11\x00" * 240
            fresh_half_frame = b"\x22\x00" * 240

            self.assertTrue(speaker.begin_turn(stale))
            self.assertTrue(await speaker.play(stale_half_frame, stale))
            self.assertEqual(speaker.queue.qsize(), 0)
            self.assertEqual(len(speaker.frame_slicer.buffer), 480)

            speaker.invalidate_generation(1, "test_cancel")
            self.assertEqual(len(speaker.frame_slicer.buffer), 0)
            self.assertIsNone(speaker.resampler_token)

            self.assertTrue(speaker.begin_turn(fresh))
            self.assertTrue(await speaker.play(fresh_half_frame, fresh))
            self.assertEqual(speaker.queue.qsize(), 0, "stale tail must not complete a fresh frame")
            self.assertTrue(await speaker.finish_turn(fresh))

            queued_token, _sequence, frame = speaker.queue.get_nowait()
            self.assertEqual(queued_token, fresh)
            self.assertEqual(frame[:480], fresh_half_frame)
            self.assertEqual(frame[480:], bytes(480))
            self.assertTrue(speaker.turn_is_terminal(fresh))
            self.assertNotIn(stale_half_frame, frame)

            await speaker.close()
            speaker.logger.close()

    async def test_16khz_provider_pcm_is_resampled_to_exact_48khz_device_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            speaker = NativeSpeakerSession(
                config=VoiceConfig(
                    opencode_url="http://opencode.test",
                    run_root=tmp,
                    tts_sample_rate=16_000,
                    device_sample_rate=48_000,
                ),
                logger=RunLogger(root=tmp),
                on_issue=self._no_issue,
            )
            speaker.queue = asyncio.Queue(maxsize=100)
            token = PlaybackToken(0, 1)
            samples = [
                int(math.sin(index * 2 * math.pi * 440 / 16_000) * 10_000)
                for index in range(1_280)
            ]
            pcm_80ms = b"".join(struct.pack("<h", sample) for sample in samples)

            self.assertTrue(speaker.begin_turn(token))
            self.assertTrue(await speaker.play(pcm_80ms, token))
            self.assertTrue(await speaker.finish_turn(token))

            queued = [speaker.queue.get_nowait() for _ in range(speaker.queue.qsize())]
            self.assertEqual(len(queued), 8)
            self.assertTrue(all(item[0] == token for item in queued))
            self.assertTrue(all(len(item[2]) == 960 for item in queued))
            self.assertTrue(any(any(item[2]) for item in queued))

            await speaker.close()
            speaker.logger.close()


if __name__ == "__main__":
    unittest.main()
