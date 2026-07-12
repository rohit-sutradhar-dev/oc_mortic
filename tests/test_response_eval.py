from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from opencode_voice.config import ModelRef
from opencode_voice.opencode_client import OpenCodeClient
from opencode_voice.response_contract import (
    RESPONSE_SCHEMA,
    ResponseCase,
    StructuredTurnTracker,
    Violation,
    grade_response,
    repair_prompt,
)
from opencode_voice.response_eval import (
    TrialResult,
    live_gate_failures,
    observe_structured_turn,
    summarize_results,
    validate_corpus,
    write_report,
)
from opencode_voice.response_eval_corpus import load_response_cases, smoke_response_cases, web_response_cases


SESSION = "ses_eval"


def message_event(info: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message.updated", "properties": {"info": {"sessionID": SESSION, **info}}}


def tool_event(part_id: str, status: str, tool: str = "read") -> dict[str, Any]:
    return {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": part_id,
                "sessionID": SESSION,
                "messageID": "msg_step",
                "type": "tool",
                "tool": tool,
                "state": {"status": status, "input": {}},
            }
        },
    }


class ResponseContractTests(unittest.TestCase):
    def test_session_error_is_preserved_for_diagnostics(self) -> None:
        tracker = StructuredTurnTracker(SESSION, [])
        error = {"name": "APIError", "data": {"statusCode": 401}}
        state = tracker.update_event(
            {"type": "session.error", "properties": {"sessionID": SESSION, "error": error}}
        )

        self.assertEqual(state.assistant_error, error)
        self.assertTrue(state.output_seen)

    def test_intermediate_assistant_completion_is_not_final(self) -> None:
        tracker = StructuredTurnTracker(SESSION, [])
        state = tracker.update_event(
            message_event(
                {
                    "id": "msg_step",
                    "role": "assistant",
                    "time": {"created": 1, "completed": 2},
                    "finish": "tool-calls",
                }
            )
        )

        self.assertIsNone(state.raw_structured)
        self.assertFalse(state.idle_seen)

        final = tracker.update_event(
            message_event(
                {
                    "id": "msg_final",
                    "role": "assistant",
                    "time": {"created": 3, "completed": 4},
                    "structured": {"displayText": "Done.", "spokenText": "Done."},
                }
            )
        )
        self.assertEqual(final.response.as_dict(), {"displayText": "Done.", "spokenText": "Done."})

    def test_tool_activity_is_deduplicated_by_part_and_state(self) -> None:
        tracker = StructuredTurnTracker(SESSION, [])
        tracker.update_event(tool_event("prt_1", "pending"))
        tracker.update_event(tool_event("prt_1", "pending"))
        tracker.update_event(tool_event("prt_1", "running"))
        tracker.update_event(tool_event("prt_1", "completed"))

        self.assertEqual([item.status for item in tracker.state.tool_activity], ["pending", "running", "completed"])

    def test_provider_usage_updates_are_deduplicated_by_message(self) -> None:
        tracker = StructuredTurnTracker(SESSION, [])
        info = {
            "id": "msg_final",
            "role": "assistant",
            "tokens": {"input": 100, "output": 20, "cache": {"read": 30, "write": 4}},
            "cost": 0.001,
        }
        tracker.update_event(message_event(info))
        tracker.update_event(message_event(info))

        tokens, cost = tracker.usage()
        self.assertEqual(tokens, {"input": 100, "output": 20, "reasoning": 0, "cacheRead": 30, "cacheWrite": 4})
        self.assertEqual(cost, 0.001)

    def test_poll_snapshot_finds_structured_final_after_tool_step(self) -> None:
        tracker = StructuredTurnTracker(SESSION, [])
        tracker.update_messages(
            [
                {
                    "info": {"id": "msg_step", "role": "assistant", "time": {"created": 1, "completed": 2}},
                    "parts": [{"id": "tool_1", "messageID": "msg_step", "type": "tool", "tool": "read", "state": {"status": "completed"}}],
                },
                {
                    "info": {
                        "id": "msg_final",
                        "role": "assistant",
                        "time": {"created": 3, "completed": 4},
                        "structured": {"displayText": "Ready.", "spokenText": "Ready."},
                    },
                    "parts": [],
                },
            ]
        )

        self.assertEqual(tracker.state.raw_structured["displayText"], "Ready.")
        self.assertEqual(tracker.state.tool_activity[0].tool, "read")

    def test_projected_snapshot_recovers_structured_tool_input(self) -> None:
        tracker = StructuredTurnTracker(SESSION, [])
        tracker.update_messages(
            [
                {
                    "id": "msg_final",
                    "type": "assistant",
                    "time": {"created": 1, "completed": 2},
                    "content": [
                        {
                            "id": "call_final",
                            "type": "tool",
                            "name": "StructuredOutput",
                            "state": {
                                "status": "completed",
                                "input": {"displayText": "Ready.", "spokenText": "Ready."},
                            },
                        }
                    ],
                }
            ]
        )

        self.assertEqual(tracker.state.raw_structured["displayText"], "Ready.")
        self.assertEqual(tracker.state.tool_activity[0].tool, "StructuredOutput")

    def test_good_plain_response_passes(self) -> None:
        case = ResponseCase(
            "reference-good",
            "reference",
            "Which file changed?",
            expected_references=("App.tsx",),
            forbidden_references=("/Users/ana/project/src/App.tsx",),
        )
        value = {
            "displayText": "I updated App.tsx and the test now passes.",
            "spokenText": "I updated the app component, and the test now passes.",
        }

        self.assertEqual(grade_response(value, case, workspace_root="/tmp/eval"), [])

    def test_absolute_path_markdown_and_hostile_speech_are_rejected(self) -> None:
        case = ResponseCase("bad", "reference", "Report it")
        value = {
            "displayText": "- Changed /Users/ana/project/src/App.tsx",
            "spokenText": "Changed it vs. the old file.",
        }

        codes = {item.code for item in grade_response(value, case)}
        self.assertTrue({"absolute_path", "markdown", "speech_hostile_abbreviation"}.issubset(codes))

    def test_spoken_path_spelling_and_raw_assignments_are_rejected(self) -> None:
        case = ResponseCase("speech-path", "reference", "Report it")
        value = {
            "displayText": "The file now says release=ready.",
            "spokenText": "The release slash status dot md file is ready.",
        }

        codes = {item.code for item in grade_response(value, case)}
        self.assertTrue({"raw_assignment", "spoken_path_spelling"}.issubset(codes))

    def test_provider_detection_uses_word_boundaries(self) -> None:
        case = ResponseCase("names", "conversation", "Reply")
        good = {"displayText": "Laura confirmed the result.", "spokenText": "Laura confirmed the result."}
        bad = {"displayText": "Mercury confirmed the result.", "spokenText": "The result is confirmed."}

        self.assertEqual(grade_response(good, case), [])
        self.assertIn("provider_disclosure", {item.code for item in grade_response(bad, case)})

    def test_repair_prompt_contains_only_actionable_corrections(self) -> None:
        violations = [
            Violation("absolute_path", "path leaked", "Use App.tsx."),
            Violation("absolute_path", "path leaked", "Use App.tsx."),
        ]
        prompt = repair_prompt("Fix it", {"displayText": "/tmp/App.tsx"}, violations)

        self.assertEqual(prompt.count("- Use App.tsx."), 1)
        self.assertIn("do not change the result", prompt)


class ResponseCorpusTests(unittest.TestCase):
    def test_committed_corpus_has_expected_profiles(self) -> None:
        cases = load_response_cases()

        self.assertEqual(len(cases), 100)
        self.assertEqual(len(smoke_response_cases()), 20)
        self.assertEqual(len(web_response_cases()), 5)
        self.assertEqual(validate_corpus(cases), [])
        self.assertEqual(
            {case.category for case in cases},
            {"conversation", "implementation", "reference", "pronunciation", "tool", "adversarial"},
        )


class PayloadClient(OpenCodeClient):
    def __init__(self) -> None:
        super().__init__("http://opencode.test")
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        self.calls.append((path, payload))
        return {}


class OpenCodeStructuredPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_async_adds_optional_structured_fields_without_changing_default_callers(self) -> None:
        client = PayloadClient()
        try:
            await client.prompt_async(
                SESSION,
                "Respond",
                ModelRef(),
                "response-eval",
                output_format={"type": "json_schema", "schema": RESPONSE_SCHEMA},
                system="candidate",
                tools={"bash": False},
            )
        finally:
            await client.close()

        path, payload = client.calls[0]
        self.assertEqual(path, f"/session/{SESSION}/prompt_async")
        self.assertEqual(payload["format"]["type"], "json_schema")
        self.assertEqual(payload["system"], "candidate")
        self.assertEqual(payload["tools"], {"bash": False})


class EventClient:
    def __init__(self, *, events: list[dict[str, Any]], messages: list[dict[str, Any]]) -> None:
        self.staged_events = events
        self.staged_messages = messages
        self.prompted = False

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.staged_messages if self.prompted else []

    async def messages_for_tracking(self, session_id: str) -> list[dict[str, Any]]:
        return await self.messages(session_id)

    async def prompt_async(self, *args: Any, **kwargs: Any) -> dict[str, bool]:
        self.prompted = True
        return {"ok": True}

    def events(self, on_open: Any = None, directory: str | None = None) -> Any:
        return self._events(on_open)

    async def _events(self, on_open: Any) -> Any:
        if on_open:
            on_open()
        while not self.prompted:
            await asyncio.sleep(0)
        for event in self.staged_events:
            yield event
        await asyncio.Event().wait()


class StructuredObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_idle_before_assistant_activity_does_not_finish_turn(self) -> None:
        events = [
            {"type": "session.idle", "properties": {"sessionID": SESSION}},
            message_event(
                {
                    "id": "msg_final",
                    "role": "assistant",
                    "structured": {"displayText": "Ready.", "spokenText": "Ready."},
                }
            ),
            {"type": "session.idle", "properties": {"sessionID": SESSION}},
        ]
        client = EventClient(events=events, messages=[])

        result = await observe_structured_turn(
            client,  # type: ignore[arg-type]
            session_id=SESSION,
            directory="/project",
            prompt="Do it",
            model=ModelRef(),
            agent="response-eval",
            schema=RESPONSE_SCHEMA,
            poll_after_sec=0.01,
        )

        self.assertEqual(result.structured["displayText"], "Ready.")

    async def test_event_observer_waits_through_tool_step_for_structured_final(self) -> None:
        events = [
            message_event({"id": "msg_step", "role": "assistant", "time": {"completed": 1}}),
            tool_event("prt_tool", "running"),
            tool_event("prt_tool", "completed"),
            message_event(
                {
                    "id": "msg_final",
                    "role": "assistant",
                    "structured": {"displayText": "Done.", "spokenText": "Done."},
                }
            ),
            {"type": "session.idle", "properties": {"sessionID": SESSION}},
        ]
        client = EventClient(events=events, messages=[])

        result = await observe_structured_turn(
            client,  # type: ignore[arg-type]
            session_id=SESSION,
            directory="/project",
            prompt="Do it",
            model=ModelRef(),
            agent="response-eval",
            schema=RESPONSE_SCHEMA,
            poll_after_sec=0.01,
        )

        self.assertEqual(result.structured["displayText"], "Done.")
        self.assertEqual(result.stream_source, "event")
        self.assertIn("read", [activity.tool for activity in result.tool_activity])

    async def test_quiet_event_stream_recovers_structured_result_from_polling(self) -> None:
        messages = [
            {
                "info": {
                    "id": "msg_final",
                    "role": "assistant",
                    "sessionID": SESSION,
                    "structured": {"displayText": "Polled.", "spokenText": "Polled."},
                },
                "parts": [],
            }
        ]
        client = EventClient(events=[], messages=messages)

        result = await asyncio.wait_for(
            observe_structured_turn(
                client,  # type: ignore[arg-type]
                session_id=SESSION,
                directory="/project",
                prompt="Do it",
                model=ModelRef(),
                agent="response-eval",
                schema=RESPONSE_SCHEMA,
                poll_after_sec=0.01,
                structured_idle_grace_sec=0.01,
            ),
            timeout=1,
        )

        self.assertEqual(result.structured["displayText"], "Polled.")
        self.assertEqual(result.stream_source, "hybrid")


class ResponseReportTests(unittest.TestCase):
    def result(self, **overrides: Any) -> TrialResult:
        fields = {
            "case_id": "conversation-01",
            "category": "conversation",
            "trial": 1,
            "response": {"displayText": "Done.", "spokenText": "Done."},
            "first_response": {"displayText": "Done.", "spokenText": "Done."},
            "first_pass_violations": [],
            "final_violations": [],
            "repaired": False,
            "repair_response": None,
            "judge": {"naturalness": 5, "directness": 5, "completeness": 5, "equivalence": 5, "notes": "Pass"},
            "latency_ms": 2000,
            "first_activity_ms": 500,
            "stream_source": "event",
            "tool_activity": [],
            "source_untouched": True,
            "error": None,
        }
        fields.update(overrides)
        return TrialResult(**fields)

    def test_summary_and_report_are_stable(self) -> None:
        results = [self.result(), self.result(case_id="conversation-02", repaired=True)]
        summary = summarize_results(results)

        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["latencyMs"], {"p50": 2000, "p95": 2000})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = write_report(
                results,
                profile="smoke",
                model=ModelRef(),
                prompt_hash="abc",
                root=Path(tmp),
            )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            trials = (run_dir / "trials.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(manifest["summary"]["trials"], 2)
        self.assertEqual(len(trials), 2)
        self.assertEqual(live_gate_failures(results, full=False), [])


if __name__ == "__main__":
    unittest.main()
