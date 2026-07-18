from __future__ import annotations

import asyncio
import unittest
from typing import Any

from opencode_voice.config import ModelRef
from opencode_voice.structured_turn import (
    load_structured_voice_prompt,
    run_structured_turn,
    structured_repair_tool_policy,
    structured_tool_policy,
)


class StructuredClientFake:
    def __init__(
        self,
        *,
        quiet: bool = False,
        missing: bool = False,
        stream_error: bool = False,
        assistant_error: bool = False,
        tool_count: int = 0,
    ) -> None:
        self.quiet = quiet
        self.missing = missing
        self.stream_error = stream_error
        self.assistant_error = assistant_error
        self.tool_count = tool_count
        self.prompted = False
        self.prompt_kwargs: dict[str, Any] = {}
        self.aborted = False
        self.session_id = "session_1"

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
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
        self.prompt_kwargs = kwargs
        self.prompted = True

    async def abort(self, session_id: str) -> None:
        self.aborted = True

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
        if self.tool_count:
            for index in range(self.tool_count):
                yield {
                    "type": "message.part.updated",
                    "properties": {
                        "part": {
                            "id": f"prt_{index}",
                            "sessionID": self.session_id,
                            "messageID": "msg_1",
                            "type": "tool",
                            "tool": "read",
                            "state": {"status": "running"},
                        }
                    },
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
    async def run_fake(
        self,
        client: StructuredClientFake,
        *,
        tools: dict[str, bool] | None = None,
        max_real_tool_calls: int = 12,
    ) -> Any:
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
            tools=tools,
            max_real_tool_calls=max_real_tool_calls,
        )

    async def test_default_tool_policy_is_a_strict_evidence_allowlist(self) -> None:
        client = StructuredClientFake()

        await self.run_fake(client)

        self.assertEqual(
            client.prompt_kwargs["tools"],
            {
                "*": False,
                "read": True,
                "glob": True,
                "grep": True,
                "websearch": True,
                "webfetch": True,
                "StructuredOutput": True,
            },
        )
        self.assertEqual(client.prompt_kwargs["output_format"]["type"], "json_schema")

    async def test_repair_disables_every_evidence_tool(self) -> None:
        client = StructuredClientFake()

        await self.run_fake(client, tools=structured_repair_tool_policy())

        self.assertEqual(client.prompt_kwargs["tools"], {"*": False, "StructuredOutput": True})

    async def test_repeated_real_tools_abort_at_the_bounded_budget(self) -> None:
        client = StructuredClientFake(missing=True, tool_count=4)

        result = await self.run_fake(client, max_real_tool_calls=3)

        self.assertEqual(result.error, "tool_budget_exceeded")
        self.assertTrue(client.aborted)
        self.assertEqual([activity.tool for activity in result.tool_activity], ["read"] * 4)

    def test_prompt_distinguishes_evidence_tools_from_final_submission(self) -> None:
        prompt = load_structured_voice_prompt()

        for tool in ("read", "glob", "grep", "websearch", "webfetch"):
            self.assertIn(f"`{tool}`", prompt)
        self.assertIn("StructuredOutput", prompt)
        normalized_prompt = " ".join(prompt.split())
        self.assertIn("not a work step", normalized_prompt)
        self.assertIn("Never substitute an allowed evidence tool", normalized_prompt)
        self.assertEqual(structured_tool_policy()["*"], False)

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
