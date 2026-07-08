from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import httpx

from opencode_voice import doctor
from opencode_voice.config import ModelRef, VoiceConfig

BASE_ENV = {"INCEPTION_API_KEY": "k1", "DEEPGRAM_API_KEY": "k2", "CARTESIA_API_KEY": "k3"}


def config(tts_provider: str = "deepgram") -> VoiceConfig:
    return VoiceConfig(opencode_url="http://oc.test", tts_provider=tts_provider)


class FakeClient:
    def __init__(
        self,
        *,
        reachable: bool = True,
        agents: list[str] | None = None,
        pong: str = "pong",
        prompt_status: int | None = None,
    ) -> None:
        self._reachable = reachable
        self._agents = agents if agents is not None else ["voice-build", "build"]
        self._pong = pong
        self._prompt_status = prompt_status
        self.deleted: list[str] = []

    async def health(self):
        if not self._reachable:
            raise httpx.ConnectError("refused")
        return {"ok": True}

    async def agents(self):
        return list(self._agents)

    async def create_session(self):
        return {"id": "ses_test"}

    async def prompt_sync(self, session_id, text, model, agent):
        if self._prompt_status is not None:
            request = httpx.Request("POST", "http://oc.test")
            response = httpx.Response(self._prompt_status, text="quota", request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)
        return {"parts": [{"type": "text", "text": self._pong}]}

    async def delete_session(self, session_id):
        self.deleted.append(session_id)
        return True

    async def close(self):
        return None


def with_client(fake: FakeClient):
    return patch("opencode_voice.opencode_client.OpenCodeClient", lambda *a, **k: fake)


class CredentialTests(unittest.TestCase):
    def test_all_keys_present_pass(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=True):
            results = doctor.check_credentials(config("cartesia"))
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual({r.name.split()[0] for r in results}, {"LLM", "STT", "TTS"})

    def test_missing_llm_key_fails(self) -> None:
        env = dict(BASE_ENV)
        del env["INCEPTION_API_KEY"]
        with patch.dict(os.environ, env, clear=True):
            results = doctor.check_credentials(config())
        llm = next(r for r in results if r.name.startswith("LLM"))
        self.assertEqual(llm.status, doctor.FAIL)

    def test_cartesia_key_only_checked_for_cartesia_provider(self) -> None:
        with patch.dict(os.environ, {"INCEPTION_API_KEY": "k", "DEEPGRAM_API_KEY": "k"}, clear=True):
            deepgram = doctor.check_credentials(config("deepgram"))
            self.assertFalse(any("CARTESIA" in r.name for r in deepgram))


class OpenCodeCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_present_runs_round_trip_and_cleans_up(self) -> None:
        fake = FakeClient(agents=["voice-build"], pong="pong")
        with patch.dict(os.environ, BASE_ENV, clear=True), with_client(fake):
            results = await doctor.check_opencode(config(), ModelRef(), "voice-build")
        by = {r.name: r for r in results}
        self.assertEqual(by["OpenCode reachable"].status, doctor.PASS)
        self.assertEqual(by["Voice agent present"].status, doctor.PASS)
        self.assertEqual(by["Model round-trip"].status, doctor.PASS)
        self.assertEqual(fake.deleted, ["ses_test"])  # throwaway session removed

    async def test_missing_agent_fails_loud_and_skips_round_trip(self) -> None:
        fake = FakeClient(agents=["build", "plan"])
        with patch.dict(os.environ, BASE_ENV, clear=True), with_client(fake):
            results = await doctor.check_opencode(config(), ModelRef(), "voice-build")
        by = {r.name: r for r in results}
        self.assertEqual(by["Voice agent present"].status, doctor.FAIL)
        self.assertIn("MISSING", by["Voice agent present"].detail)
        self.assertIn("managed mode", by["Voice agent present"].detail)
        self.assertEqual(by["Model round-trip"].status, doctor.WARN)
        self.assertEqual(fake.deleted, [])  # never created a session

    async def test_unreachable_server_fails_before_agent_check(self) -> None:
        fake = FakeClient(reachable=False)
        with patch.dict(os.environ, BASE_ENV, clear=True), with_client(fake):
            results = await doctor.check_opencode(config(), ModelRef(), "voice-build")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "OpenCode reachable")
        self.assertEqual(results[0].status, doctor.FAIL)

    async def test_round_trip_reports_provider_http_error(self) -> None:
        fake = FakeClient(agents=["voice-build"], prompt_status=402)
        with patch.dict(os.environ, BASE_ENV, clear=True), with_client(fake):
            results = await doctor.check_opencode(config(), ModelRef(), "voice-build")
        rt = next(r for r in results if r.name == "Model round-trip")
        self.assertEqual(rt.status, doctor.FAIL)
        self.assertIn("402", rt.detail)

    async def test_round_trip_disabled_skips_the_turn(self) -> None:
        fake = FakeClient(agents=["voice-build"])
        with patch.dict(os.environ, BASE_ENV, clear=True), with_client(fake):
            results = await doctor.check_opencode(config(), ModelRef(), "voice-build", round_trip=False)
        self.assertFalse(any(r.name == "Model round-trip" for r in results))
        self.assertEqual(fake.deleted, [])


class GlobalConfigGuardTests(unittest.TestCase):
    def test_doctor_has_no_global_config_repair_path(self) -> None:
        self.assertFalse(hasattr(doctor, "apply_agent_fix"))
        self.assertFalse(hasattr(doctor, "OPENCODE_CONFIG_PATHS"))


if __name__ == "__main__":
    unittest.main()
