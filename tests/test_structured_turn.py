from __future__ import annotations

import asyncio
import unittest
from typing import Any

from opencode_voice.config import ModelRef
from opencode_voice.structured_turn import run_structured_turn


class StructuredClientFake:
    def __init__(
        self,
        *,
        quiet: bool = False,
        missing: bool = False,
        stream_error: bool = False,
        assistant_error: bool = False,
    ) -> None:
        self.quiet = quiet
        self.missing = missing
        self.stream_error = stream_error
        self.assistant_error = assistant_error
        self.prompted = False
        self.session_id = "session_1"

    async def messages_for_tracking(self, session_id: str) -> list[dict[str, Any]]:
        if not self.prompted:
            return []
        info: dict[str, Any] = {
            "id": "msg_1",
            "role": "assistant",
            "sessionID": session_id,
            "time": {"created": 1, "completed": 2},
        }
        if not self.missing:
            info["structured"] = {"displayText": "Ready.", "spokenText": "Ready."}
        return [{"info": info, "parts": []}]

    async def prompt_async(self, session_id: str, text: str, model: Any, agent: str, **kwargs: Any) -> None:
        self.prompted = True

    async def events(self, on_open: Any = None, directory: str | None = None) -> Any:
        if on_open:
            on_open()
        if self.stream_error:
            raise OSError("stream failed")
        if self.quiet:
            await asyncio.Event().wait()
        if self.assistant_error:
            yield {
                "type": "session.error",
                "properties": {"sessionID": self.session_id, "error": "maximum context length"},
            }
            await asyncio.Event().wait()
        info: dict[str, Any] = {
            "id": "msg_1",
            "role": "assistant",
            "sessionID": self.session_id,
        }
        if not self.missing:
            info["structured"] = {"displayText": "Ready.", "spokenText": "Ready."}
        yield {"type": "message.updated", "properties": {"info": info}}
        yield {"type": "session.idle", "properties": {"sessionID": self.session_id}}
        await asyncio.Event().wait()


class StructuredTurnRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def run_fake(self, client: StructuredClientFake) -> Any:
        return await run_structured_turn(
            client,  # type: ignore[arg-type]
            session_id="session_1",
            directory="/project",
            prompt="Answer it.",
            model=ModelRef(),
            agent="voice-build",
            max_turn_sec=1,
            poll_after_sec=0.01,
            final_grace_sec=0.01,
        )

    async def test_event_result_admits_only_structured_final(self) -> None:
        result = await self.run_fake(StructuredClientFake())
        self.assertEqual(result.response.as_dict(), {"displayText": "Ready.", "spokenText": "Ready."})
        self.assertEqual(result.stream_source, "event")

    async def test_quiet_event_stream_recovers_from_polling(self) -> None:
        result = await self.run_fake(StructuredClientFake(quiet=True))
        self.assertIsNotNone(result.response)
        self.assertEqual(result.stream_source, "hybrid")

    async def test_event_failure_recovers_from_polling(self) -> None:
        result = await self.run_fake(StructuredClientFake(stream_error=True))
        self.assertIsNotNone(result.response)
        self.assertEqual(result.stream_source, "poll")

    async def test_idle_without_structured_output_fails_closed(self) -> None:
        result = await self.run_fake(StructuredClientFake(missing=True))
        self.assertIsNone(result.response)
        self.assertEqual(result.error, "structured_output_missing")

    async def test_assistant_error_finishes_without_waiting_for_idle(self) -> None:
        result = await self.run_fake(StructuredClientFake(missing=True, assistant_error=True))
        self.assertIsNone(result.response)
        self.assertEqual(result.error, "maximum context length")


if __name__ == "__main__":
    unittest.main()
