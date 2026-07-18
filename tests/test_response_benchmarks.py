from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from opencode_voice.response_benchmarks import (
    conversation_scripts,
    judge_calibration_fixtures,
    notation_calibration_examples,
    notation_response_cases,
    score_recall,
)
from opencode_voice.response_compaction import (
    CompactionEventTracker,
    ProviderTokenTracker,
    compare_fork_snapshots,
    compaction_profiles,
    compaction_thrashed,
    duplicate_action_hashes,
    recorded_context_tokens,
)
from opencode_voice.response_comparison import regrade_baseline
from opencode_voice.response_contract import (
    ReferenceExpectation,
    ResponseCase,
    SemanticAssertion,
    evaluate_response,
    normalize_semantic_text,
    should_select_repair,
)


class CalibratedContractTests(unittest.TestCase):
    def test_normalization_handles_unicode_case_hyphens_and_whitespace(self) -> None:
        self.assertEqual(normalize_semantic_text("  RéLEASE—READY\n"), "rélease ready")
        self.assertEqual(normalize_semantic_text("MOR‑172"), "mor 172")

    def test_numeric_aliases_replace_character_count_equivalence(self) -> None:
        case = ResponseCase(
            "number",
            "metrics",
            "Report it",
            assertions=(
                SemanticAssertion("year", ("2026",), ("twenty twenty-six", "two thousand twenty-six")),
            ),
        )
        result = evaluate_response(
            {"displayText": "The target is 2026.", "spokenText": "The target is twenty twenty-six."}, case
        )
        self.assertEqual(result.violations, ())

    def test_inline_json_is_found_anywhere_and_display_parentheses_remain_legal(self) -> None:
        case = ResponseCase("json", "safety", "Explain it")
        bad = evaluate_response(
            {"displayText": "Result: {\"status\": \"ready\"} is final.", "spokenText": "The result is ready."},
            case,
        )
        good = evaluate_response(
            {"displayText": "The result (still provisional) is ready.", "spokenText": "The provisional result is ready."},
            case,
        )
        self.assertIn("raw_json", {item.code for item in bad.violations})
        self.assertNotIn("raw_json", {item.code for item in good.violations})

    def test_spoken_brackets_are_rejected_without_rewriting_display_notation(self) -> None:
        case = ResponseCase("brackets", "notation", "Explain the notation")
        examples = (
            "The first item is ready (temporarily).",
            "Priority [P1] is ready.",
            "Use Map<string, T> for the result.",
            "Call refresh(options) next.",
            "Reconnect the socket, options)",
        )
        for spoken in examples:
            result = evaluate_response(
                {
                    "displayText": "Use items[0], Map<string, T>, and refresh(options) temporarily.",
                    "spokenText": spoken,
                },
                case,
            )
            self.assertIn("spoken_bracket_notation", {item.code for item in result.violations}, spoken)

        good = evaluate_response(
            {
                "displayText": "[P1] uses items[0] with Map<string, T> in refresh(options).",
                "spokenText": "Priority one uses the first item with a map from strings to T in the refresh function.",
            },
            case,
        )
        self.assertNotIn("spoken_bracket_notation", {item.code for item in good.violations})

    def test_pair_reference_requires_display_and_natural_spoken_identity(self) -> None:
        case = ResponseCase(
            "ref",
            "reference",
            "Which file?",
            references=(ReferenceExpectation("server", ("server.py",), (("server", "1486"),)),),
        )
        good = evaluate_response(
            {"displayText": "The issue is in server.py.", "spokenText": "The server module at line 1486 has the issue."},
            case,
        )
        bad = evaluate_response(
            {"displayText": "The issue is in server.py.", "spokenText": "The client module has the issue."},
            case,
        )
        self.assertEqual(good.violations, ())
        self.assertIn("spoken_reference", {item.code for item in bad.violations})

    def test_repair_must_strictly_improve_without_new_failure(self) -> None:
        case = ResponseCase("repair", "safety", "Answer", required_facts=("ready",))
        first = evaluate_response(
            {"displayText": "Ready at /tmp/app.py.", "spokenText": "It is ready."}, case
        )
        regressed = evaluate_response(
            {"displayText": "The file changed.", "spokenText": "The file changed."}, case
        )
        improved = evaluate_response(
            {"displayText": "The app file is ready.", "spokenText": "The app file is ready."}, case
        )
        self.assertFalse(should_select_repair(first, regressed)[0])
        self.assertTrue(should_select_repair(first, improved)[0])


class BenchmarkCorpusTests(unittest.TestCase):
    def test_notation_corpus_and_offline_examples_are_self_consistent(self) -> None:
        cases = notation_response_cases()
        examples = notation_calibration_examples()
        self.assertEqual(len(cases), 24)
        self.assertEqual(len(examples), 48)
        self.assertEqual(len({case.case_id for case in cases}), 24)
        for example in examples:
            passed = not evaluate_response(example.response, example.case).violations
            self.assertEqual(passed, example.should_pass, example.example_id)

    def test_judge_fixture_mix_matches_calibration_contract(self) -> None:
        fixtures = judge_calibration_fixtures()
        self.assertEqual(len(fixtures), 32)
        self.assertEqual(sum(item.valid_clarification for item in fixtures), 12)
        self.assertEqual(sum(item.expected_pass for item in fixtures), 20)

    def test_conversation_matrix_and_fact_supersession(self) -> None:
        scripts = conversation_scripts()
        self.assertEqual(len(scripts), 8)
        self.assertEqual(sorted(script.length for script in scripts), [8, 8, 12, 12, 20, 20, 32, 32])
        self.assertTrue(all(sum(exchange.checkpoint_id is not None for exchange in script.exchanges) == 3 for script in scripts))
        for script in scripts:
            active = script.active_facts(script.length)
            self.assertNotIn("old_date", {fact.fact_id for fact in active})
            response = {
                "displayText": " ".join(fact.display_any[0] for fact in active),
                "spokenText": " ".join(fact.spoken_any[0] for fact in active),
            }
            score = score_recall(response, active)
            self.assertEqual(score.recall_rate, 1.0, script.script_id)
            self.assertEqual(score.contradictions, 0, script.script_id)
        shortest = next(script for script in scripts if script.script_id == "conversation-08-a")
        seeded_text = " ".join(exchange.user_text for exchange in shortest.exchanges)
        self.assertIn("transport.py", seeded_text)
        self.assertIn("70,000 tokens", seeded_text)
        self.assertIn("Provider timing remains uncertain", seeded_text)


class CompactionInstrumentationTests(unittest.TestCase):
    def test_provider_token_tracker_uses_assistant_input_and_cache_tokens(self) -> None:
        tracker = ProviderTokenTracker("ses_1")
        tracker.update({
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_1",
                "info": {
                    "id": "m1",
                    "role": "assistant",
                    "tokens": {"input": 900, "cache": {"read": 100}},
                },
            },
        })
        tracker.update({
            "type": "message.updated",
            "properties": {
                "sessionID": "other",
                "info": {"id": "m2", "role": "assistant", "tokens": {"input": 9999}},
            },
        })

        self.assertEqual(tracker.current_tokens, 1000)
        self.assertEqual(tracker.samples, [1000])

    def test_profiles_expose_expected_effective_triggers(self) -> None:
        profiles = {item.profile_id: item for item in compaction_profiles(native_scale=True)}
        self.assertEqual(profiles["scaled-conservative"].effective_trigger, 30_000)
        self.assertEqual(profiles["scaled-aggressive"].effective_trigger, 20_000)
        self.assertEqual(profiles["mortic-current"].effective_trigger, 70_000)
        self.assertEqual(profiles["native-auto"].effective_trigger, 119_808)

    def test_compaction_events_reconcile_with_persisted_messages(self) -> None:
        tracker = CompactionEventTracker("ses_1", "forced")
        tracker.update({"type": "session.next.compaction.started", "properties": {"sessionID": "ses_1", "messageID": "m1", "timestamp": 100, "reason": "manual"}})
        tracker.update({"type": "session.next.compaction.delta", "properties": {"sessionID": "ses_1", "messageID": "m1", "text": "sum"}})
        tracker.update({"type": "session.next.compaction.ended", "properties": {"sessionID": "ses_1", "messageID": "m1", "timestamp": 180, "text": "summary", "recent": "tail"}})
        tracker.reconcile_messages([
            {"info": {"id": "m1", "role": "user"}, "parts": [{"type": "compaction", "tail_start_id": "m0", "auto": False}]},
            {"info": {"id": "m2", "role": "assistant", "parentID": "m1", "summary": True}, "parts": [{"type": "text", "text": "canonical summary"}]},
        ])
        observation = tracker.observations[0]
        self.assertEqual(observation.latency_ms, 80)
        self.assertEqual(observation.summary, "canonical summary")
        self.assertEqual(observation.tail_start_id, "m0")

    def test_fork_graph_ignores_ids_and_detects_mutation_or_broken_links(self) -> None:
        source = [
            {"info": {"id": "a", "role": "user"}, "parts": [{"type": "text", "text": "hello"}]},
            {"info": {"id": "b", "role": "assistant", "parentID": "a"}, "parts": [{"type": "text", "text": "noted"}]},
        ]
        fork = [
            {"info": {"id": "x", "role": "user"}, "parts": [{"type": "text", "text": "hello"}]},
            {"info": {"id": "y", "role": "assistant", "parentID": "x"}, "parts": [{"type": "text", "text": "noted"}]},
        ]
        snapshot = compare_fork_snapshots(source, list(source), fork)
        self.assertTrue(snapshot.source_untouched)
        self.assertTrue(snapshot.inherited_content_equal)
        self.assertTrue(snapshot.parent_links_valid)
        cutoff_snapshot = compare_fork_snapshots(
            source,
            list(source),
            fork[:1],
            expected_inherited=source[:1],
        )
        self.assertTrue(cutoff_snapshot.inherited_content_equal)
        self.assertTrue(cutoff_snapshot.source_untouched)

    def test_token_duplicate_and_thrash_helpers(self) -> None:
        messages = [
            {"info": {"id": "a", "role": "assistant", "tokens": {"input": 100, "cache": {"read": 40}}}, "parts": [{"type": "text", "text": "Done"}]},
            {"info": {"id": "b", "role": "assistant", "tokens": {"input": 500, "cache": {"read": 200}}}, "parts": [{"type": "text", "text": "Done"}]},
        ]
        self.assertEqual(recorded_context_tokens(messages), 700)
        self.assertEqual(len(duplicate_action_hashes(messages)[0]), 1)
        self.assertTrue(compaction_thrashed(10_000, 13_000, 20_000))
        self.assertFalse(compaction_thrashed(10_000, 24_100, 20_000))


class ImmutableComparisonTests(unittest.TestCase):
    def test_regrade_writes_separate_report_without_modifying_source(self) -> None:
        case = ResponseCase("case-1", "answer", "Answer", required_facts=("ready",))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.mkdir()
            source = baseline / "trials.jsonl"
            source.write_text(json.dumps({
                "case_id": "case-1", "trial": 1, "passed": False,
                "first_response": {"displayText": "Ready.", "spokenText": "Ready."},
                "first_pass_violations": [{"code": "equivalence"}],
            }) + "\n", encoding="utf-8")
            before = source.read_bytes()
            output = regrade_baseline(baseline, [case], output_root=root / "comparisons")
            self.assertEqual(source.read_bytes(), before)
            result = json.loads((output / "regraded.jsonl").read_text().splitlines()[0])
            self.assertEqual(result["classification"], "evaluator_false_positive")


if __name__ == "__main__":
    unittest.main()
