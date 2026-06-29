from __future__ import annotations

import unittest

from openrouter_bench.openrouter import (
    build_checker_prompt,
    build_payload,
    build_synthesis_prompt,
    extract_delta_text,
    iter_sse_data,
)


class PayloadTests(unittest.TestCase):
    def test_builds_fusion_payload(self) -> None:
        system = {
            "name": "fusion_mercury_2_x2",
            "model": "openrouter/fusion",
            "plugins": [
                {
                    "id": "fusion",
                    "model": "inception/mercury-2",
                    "analysis_models": ["inception/mercury-2", "inception/mercury-2"],
                    "max_tool_calls": 8,
                }
            ],
        }
        payload = build_payload(system, "TASK PROMPT HERE", {"temperature": 0.2})

        self.assertEqual(payload["model"], "openrouter/fusion")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "TASK PROMPT HERE"}])
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["plugins"][0]["id"], "fusion")
        self.assertEqual(payload["plugins"][0]["analysis_models"], ["inception/mercury-2", "inception/mercury-2"])
        self.assertNotIn("name", payload)


class StreamingTests(unittest.TestCase):
    def test_iter_sse_data_ignores_comments_and_done(self) -> None:
        lines = [
            b": OPENROUTER PROCESSING\n",
            b"data: {\"choices\":[{\"delta\":{\"content\":\"Hel\"}}]}\n",
            b"\n",
            b"data: {\"choices\":[{\"delta\":{\"content\":\"lo\"}}]}\n",
            b"\n",
            b"data: [DONE]\n",
            b"\n",
        ]

        self.assertEqual(
            list(iter_sse_data(lines)),
            [
                '{"choices":[{"delta":{"content":"Hel"}}]}',
                '{"choices":[{"delta":{"content":"lo"}}]}',
                "[DONE]",
            ],
        )

    def test_extract_delta_text(self) -> None:
        chunk = {"choices": [{"delta": {"content": "hello"}}]}
        self.assertEqual(extract_delta_text(chunk), "hello")


class LocalFusionTests(unittest.TestCase):
    def test_synthesis_prompt_includes_task_and_candidates(self) -> None:
        prompt = build_synthesis_prompt(
            prompt="Task text",
            analysis_results=[
                {"model": "mercury-2", "result": type("Result", (), {"text": "Answer A"})()},
                {"model": "mercury-2", "result": type("Result", (), {"text": "Answer B"})()},
            ],
        )

        self.assertIn("Task text", prompt)
        self.assertIn("Answer A", prompt)
        self.assertIn("Answer B", prompt)
        self.assertIn("Final answer:", prompt)

    def test_checker_prompt_includes_task_and_draft(self) -> None:
        prompt = build_checker_prompt(prompt="Task text", draft="Fast draft")

        self.assertIn("Task text", prompt)
        self.assertIn("Fast draft", prompt)
        self.assertIn("Checked final answer:", prompt)


if __name__ == "__main__":
    unittest.main()
