from __future__ import annotations

import unittest

from opencode_voice.interruption import (
    EpisodeIdentity,
    InterruptionActionKind,
    InterruptionEvent,
    InterruptionPhase,
    InterruptionSnapshot,
    is_narrow_backchannel,
    reduce_interruption,
)


def episode(
    turn_index: int = 1,
    *,
    group: str = "speech-1",
    generation: int = 7,
    epoch: int = 1,
) -> EpisodeIdentity:
    return EpisodeIdentity(
        flux_epoch=epoch,
        turn_index=turn_index,
        acoustic_group_id=group,
        playback_generation=generation,
    )


def kinds(result: object) -> list[InterruptionActionKind]:
    return [action.kind for action in result.actions]  # type: ignore[attr-defined]


class InterruptionDecisionTests(unittest.TestCase):
    def test_non_overlapping_episode_is_an_ordinary_user_turn(self) -> None:
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=False)
        )
        self.assertEqual(started.state.phase, InterruptionPhase.USER_TURN)
        self.assertEqual(started.actions, ())

        final = reduce_interruption(
            started.state, InterruptionEvent.final_eot(episode(), 300, "hello there")
        )
        self.assertEqual(final.state.phase, InterruptionPhase.QUIET)
        self.assertEqual(kinds(final), [InterruptionActionKind.ADMIT_TRANSCRIPT])
        self.assertEqual(final.actions[0].text, "hello there")

    def test_overlapping_episode_holds_playback_once(self) -> None:
        first = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
        )
        duplicate = reduce_interruption(
            first.state, InterruptionEvent.start(episode(), 20, playback_exposed=True)
        )

        self.assertEqual(first.state.phase, InterruptionPhase.CANDIDATE)
        self.assertEqual(kinds(first), [InterruptionActionKind.HOLD_PLAYBACK])
        self.assertEqual(duplicate.actions, ())
        self.assertTrue(duplicate.state.playback_held)

    def test_manual_interrupt_commits_immediately_and_is_idempotent(self) -> None:
        first = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.manual(episode(), 0, reason="user.mute")
        )
        duplicate = reduce_interruption(
            first.state, InterruptionEvent.manual(episode(), 1, reason="user.mute")
        )

        self.assertEqual(first.state.phase, InterruptionPhase.INTERRUPTED)
        self.assertEqual(kinds(first), [InterruptionActionKind.COMMIT_INTERRUPT])
        self.assertEqual(first.actions[0].reason, "manual:user.mute")
        self.assertEqual(duplicate.actions, ())

    def test_interim_stop_and_wait_commit_even_with_echo_correlation(self) -> None:
        for command in ("Stop.", "wait, use the other file"):
            with self.subTest(command=command):
                started = reduce_interruption(
                    InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
                )
                decided = reduce_interruption(
                    started.state,
                    InterruptionEvent.interim(episode(), 50, command, correlation=0.99),
                )
                self.assertEqual(decided.state.phase, InterruptionPhase.INTERRUPTED)
                self.assertEqual(kinds(decided), [InterruptionActionKind.COMMIT_INTERRUPT])
                self.assertEqual(decided.actions[0].reason, "priority_command")

    def test_two_non_backchannel_words_with_low_correlation_commit_early(self) -> None:
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
        )
        decided = reduce_interruption(
            started.state,
            InterruptionEvent.interim(episode(), 80, "yes save", correlation=0.74),
        )

        self.assertEqual(decided.state.phase, InterruptionPhase.INTERRUPTED)
        self.assertEqual(decided.actions[0].reason, "early_non_backchannel")

    def test_echo_correlation_suppresses_only_at_500ms_evaluation(self) -> None:
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
        )
        evidence = reduce_interruption(
            started.state,
            InterruptionEvent.interim(episode(), 100, "assistant words", correlation=0.75),
        )
        before = reduce_interruption(evidence.state, InterruptionEvent.tick(499))
        decided = reduce_interruption(before.state, InterruptionEvent.tick(500))

        self.assertEqual(before.state.phase, InterruptionPhase.CANDIDATE)
        self.assertEqual(decided.state.phase, InterruptionPhase.SUPPRESSED)
        self.assertEqual(
            kinds(decided),
            [InterruptionActionKind.SUPPRESS_EPISODE, InterruptionActionKind.RESUME_PLAYBACK],
        )
        self.assertEqual(decided.actions[0].reason, "echo_correlation")

    def test_exact_narrow_backchannels_suppress_but_longer_phrase_commits(self) -> None:
        for text in ("uh-huh", "mm-hmm", "mhm"):
            with self.subTest(text=text):
                self.assertTrue(is_narrow_backchannel(text))
                started = reduce_interruption(
                    InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
                )
                evidence = reduce_interruption(
                    started.state, InterruptionEvent.interim(episode(), 100, text, correlation=0.1)
                )
                decided = reduce_interruption(evidence.state, InterruptionEvent.tick(500))
                self.assertEqual(decided.state.phase, InterruptionPhase.SUPPRESSED)
                self.assertEqual(decided.actions[0].reason, "backchannel")

        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
        )
        longer = reduce_interruption(
            started.state,
            InterruptionEvent.interim(episode(), 100, "uh huh explain that", correlation=0.1),
        )
        self.assertEqual(longer.state.phase, InterruptionPhase.INTERRUPTED)

    def test_one_novel_word_without_correlation_commits_at_evaluation(self) -> None:
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
        )
        evidence = reduce_interruption(
            started.state, InterruptionEvent.interim(episode(), 100, "actually")
        )
        decided = reduce_interruption(evidence.state, InterruptionEvent.tick(500))
        self.assertEqual(decided.state.phase, InterruptionPhase.INTERRUPTED)
        self.assertEqual(decided.actions[0].reason, "evaluation_confirmed")

    def test_timer_evaluation_preserves_final_and_admits_committed_transcript(self) -> None:
        current = episode()
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(current, 0, playback_exposed=True)
        )
        interim = reduce_interruption(
            started.state, InterruptionEvent.interim(current, 50, "hello")
        )
        final = reduce_interruption(
            interim.state, InterruptionEvent.final_eot(current, 86, "hello")
        )

        decided = reduce_interruption(
            final.state, InterruptionEvent.evaluate(current, 500, correlation=0.2)
        )

        self.assertEqual(decided.state.phase, InterruptionPhase.QUIET)
        self.assertEqual(
            kinds(decided),
            [InterruptionActionKind.COMMIT_INTERRUPT, InterruptionActionKind.ADMIT_TRANSCRIPT],
        )
        self.assertEqual(decided.actions[1].text, "hello")

    def test_timer_evaluation_preserves_final_suppression_guard(self) -> None:
        current = episode()
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(current, 0, playback_exposed=True)
        )
        interim = reduce_interruption(
            started.state, InterruptionEvent.interim(current, 50, "mhm")
        )
        final = reduce_interruption(
            interim.state, InterruptionEvent.final_eot(current, 187, "mhm")
        )

        decided = reduce_interruption(
            final.state, InterruptionEvent.evaluate(current, 500, correlation=0.1)
        )

        self.assertEqual(decided.state.final_eot_at_ms, 187)
        self.assertEqual(decided.state.suppression_guard_until_ms, 687)
        fresh = episode(turn_index=2, group="speech-2")
        restarted = reduce_interruption(
            decided.state, InterruptionEvent.start(fresh, 688, playback_exposed=True)
        )
        self.assertEqual(restarted.state.phase, InterruptionPhase.CANDIDATE)
        self.assertEqual(
            kinds(restarted),
            [InterruptionActionKind.EPISODE_EXPIRED, InterruptionActionKind.HOLD_PLAYBACK],
        )


class SuppressionOwnershipTests(unittest.TestCase):
    def suppress(self) -> InterruptionSnapshot:
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
        )
        evidence = reduce_interruption(
            started.state,
            InterruptionEvent.interim(episode(), 100, "echoed assistant", correlation=0.9),
        )
        return reduce_interruption(evidence.state, InterruptionEvent.tick(500)).state

    def test_suppression_waits_for_final_eot_plus_500ms_guard(self) -> None:
        suppressed = self.suppress()
        final = reduce_interruption(
            suppressed, InterruptionEvent.final_eot(episode(), 550, "echoed assistant", correlation=0.9)
        )
        before = reduce_interruption(final.state, InterruptionEvent.tick(1_049))
        expired = reduce_interruption(before.state, InterruptionEvent.tick(1_050))

        self.assertEqual(before.state.phase, InterruptionPhase.SUPPRESSED)
        self.assertEqual(expired.state.phase, InterruptionPhase.QUIET)
        self.assertEqual(kinds(expired), [InterruptionActionKind.EPISODE_EXPIRED])
        self.assertEqual(expired.actions[0].reason, "suppression_guard_complete")

    def test_same_playback_generation_restart_is_owned_without_reduck(self) -> None:
        suppressed = self.suppress()
        final = reduce_interruption(
            suppressed, InterruptionEvent.final_eot(episode(), 550, "echoed assistant")
        )
        restarted_episode = episode(turn_index=2, group="speech-2")
        restarted = reduce_interruption(
            final.state,
            InterruptionEvent.start(restarted_episode, 954, playback_exposed=True),
        )
        resumed = reduce_interruption(
            restarted.state, InterruptionEvent.turn_resumed(restarted_episode, 967)
        )

        self.assertEqual(restarted.state.phase, InterruptionPhase.SUPPRESSED)
        self.assertEqual(restarted.actions, ())
        self.assertIsNone(restarted.state.final_eot_at_ms)
        self.assertEqual(kinds(resumed), [InterruptionActionKind.CANCEL_SPECULATION])
        self.assertNotIn(InterruptionActionKind.COMMIT_INTERRUPT, kinds(resumed))
        self.assertNotIn(InterruptionActionKind.HOLD_PLAYBACK, kinds(resumed))

    def test_new_playback_generation_is_a_fresh_candidate(self) -> None:
        suppressed = self.suppress()
        fresh = episode(turn_index=2, group="speech-2", generation=8)
        restarted = reduce_interruption(
            suppressed, InterruptionEvent.start(fresh, 600, playback_exposed=True)
        )

        self.assertEqual(restarted.state.phase, InterruptionPhase.CANDIDATE)
        self.assertEqual(
            kinds(restarted),
            [InterruptionActionKind.EPISODE_EXPIRED, InterruptionActionKind.HOLD_PLAYBACK],
        )

    def test_priority_command_can_override_suppression(self) -> None:
        suppressed = self.suppress()
        override = reduce_interruption(
            suppressed,
            InterruptionEvent.interim(
                episode(turn_index=2, group="speech-2"),
                700,
                "wait please",
                correlation=0.99,
            ),
        )
        self.assertEqual(override.state.phase, InterruptionPhase.INTERRUPTED)
        self.assertEqual(kinds(override), [InterruptionActionKind.COMMIT_INTERRUPT])

    def test_activity_inside_final_guard_reopens_before_priority_override(self) -> None:
        suppressed = self.suppress()
        final = reduce_interruption(
            suppressed, InterruptionEvent.final_eot(episode(), 550, "echoed assistant")
        )
        fresh = episode(turn_index=2, group="speech-2")
        override = reduce_interruption(
            final.state,
            InterruptionEvent.interim(fresh, 700, "wait please", correlation=0.2),
        )

        self.assertEqual(override.state.phase, InterruptionPhase.INTERRUPTED)
        self.assertIsNone(override.state.final_eot_at_ms)
        self.assertEqual(kinds(override), [InterruptionActionKind.COMMIT_INTERRUPT])
        completed = reduce_interruption(
            override.state, InterruptionEvent.final_eot(fresh, 900, "wait please", correlation=0.2)
        )
        self.assertEqual(kinds(completed), [InterruptionActionKind.ADMIT_TRANSCRIPT])

    def test_missing_final_expires_after_two_seconds_provider_silence(self) -> None:
        suppressed = self.suppress()
        before = reduce_interruption(suppressed, InterruptionEvent.tick(2_099))
        expired = reduce_interruption(before.state, InterruptionEvent.tick(2_100))

        self.assertEqual(before.state.phase, InterruptionPhase.SUPPRESSED)
        self.assertEqual(expired.state.phase, InterruptionPhase.QUIET)
        self.assertEqual(expired.actions[0].reason, "provider_silence")

        stale = reduce_interruption(
            expired.state,
            InterruptionEvent.interim(episode(), 2_200, "late stale words", correlation=0.1),
        )
        self.assertEqual(stale.state.phase, InterruptionPhase.QUIET)
        self.assertEqual(stale.actions, ())

    def test_late_evaluation_does_not_extend_an_already_elapsed_final_guard(self) -> None:
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
        )
        final = reduce_interruption(
            started.state,
            InterruptionEvent.final_eot(episode(), 100, "assistant echo", correlation=0.9),
        )
        evaluated_late = reduce_interruption(final.state, InterruptionEvent.tick(1_000))

        self.assertEqual(evaluated_late.state.phase, InterruptionPhase.QUIET)
        self.assertEqual(
            kinds(evaluated_late),
            [
                InterruptionActionKind.SUPPRESS_EPISODE,
                InterruptionActionKind.RESUME_PLAYBACK,
                InterruptionActionKind.EPISODE_EXPIRED,
            ],
        )

    def test_repeated_identical_text_in_fresh_episode_is_not_time_deduped(self) -> None:
        first = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=False)
        )
        first_final = reduce_interruption(
            first.state, InterruptionEvent.final_eot(episode(), 100, "yes")
        )
        second_episode = episode(turn_index=2, group="speech-2")
        second = reduce_interruption(
            first_final.state,
            InterruptionEvent.start(second_episode, 200, playback_exposed=False),
        )
        second_final = reduce_interruption(
            second.state, InterruptionEvent.final_eot(second_episode, 300, "yes")
        )

        self.assertEqual(kinds(first_final), [InterruptionActionKind.ADMIT_TRANSCRIPT])
        self.assertEqual(kinds(second_final), [InterruptionActionKind.ADMIT_TRANSCRIPT])


class IncidentTimelineTests(unittest.TestCase):
    def test_171336_echo_restart_then_turn_resumed_never_commits_or_reducks(self) -> None:
        # Relative replay of events.jsonl:503-515. The new Start is 404ms after
        # resume; TurnResumed follows the second false alarm by about 12ms.
        first = episode(turn_index=40, group="echo-1")
        state = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(first, 0, playback_exposed=True)
        ).state
        state = reduce_interruption(
            state, InterruptionEvent.eager_eot(first, 100, "assistant echo", correlation=0.91)
        ).state
        decision = reduce_interruption(state, InterruptionEvent.tick(500))
        self.assertEqual(kinds(decision).count(InterruptionActionKind.RESUME_PLAYBACK), 1)
        state = reduce_interruption(
            decision.state, InterruptionEvent.final_eot(first, 533, "assistant echo", correlation=0.91)
        ).state

        restart = episode(turn_index=41, group="echo-2")
        new_start = reduce_interruption(
            state, InterruptionEvent.start(restart, 937, playback_exposed=True)
        )
        eager = reduce_interruption(
            new_start.state, InterruptionEvent.eager_eot(restart, 971, "echo fragment", correlation=0.8)
        )
        resumed = reduce_interruption(eager.state, InterruptionEvent.turn_resumed(restart, 984))

        all_actions = decision.actions + new_start.actions + eager.actions + resumed.actions
        all_kinds = [action.kind for action in all_actions]
        self.assertEqual(all_kinds.count(InterruptionActionKind.HOLD_PLAYBACK), 0)
        self.assertEqual(all_kinds.count(InterruptionActionKind.COMMIT_INTERRUPT), 0)
        self.assertEqual(all_kinds.count(InterruptionActionKind.RESUME_PLAYBACK), 1)
        self.assertEqual(resumed.state.phase, InterruptionPhase.SUPPRESSED)

    def test_real_interrupt_commits_once_then_admits_final_once(self) -> None:
        current = episode()
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(current, 0, playback_exposed=True)
        )
        committed = reduce_interruption(
            started.state,
            InterruptionEvent.interim(current, 100, "look at tests", correlation=0.2),
        )
        duplicate = reduce_interruption(
            committed.state,
            InterruptionEvent.interim(current, 150, "look at tests", correlation=0.2),
        )
        final = reduce_interruption(
            duplicate.state,
            InterruptionEvent.final_eot(current, 700, "look at tests", correlation=0.2),
        )

        self.assertEqual(kinds(committed), [InterruptionActionKind.COMMIT_INTERRUPT])
        self.assertEqual(duplicate.actions, ())
        self.assertEqual(kinds(final), [InterruptionActionKind.ADMIT_TRANSCRIPT])
        self.assertEqual(final.state.phase, InterruptionPhase.QUIET)


class ControllerSafetyTests(unittest.TestCase):
    def test_close_is_terminal(self) -> None:
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 0, playback_exposed=True)
        )
        closed = reduce_interruption(started.state, InterruptionEvent.close(10))
        ignored = reduce_interruption(
            closed.state, InterruptionEvent.interim(episode(), 20, "stop", correlation=0.0)
        )
        self.assertEqual(closed.state.phase, InterruptionPhase.CLOSED)
        self.assertEqual(ignored.state, closed.state)
        self.assertEqual(ignored.actions, ())

    def test_rejects_non_monotonic_time_and_invalid_correlation(self) -> None:
        started = reduce_interruption(
            InterruptionSnapshot(), InterruptionEvent.start(episode(), 100, playback_exposed=True)
        )
        with self.assertRaises(ValueError):
            reduce_interruption(started.state, InterruptionEvent.tick(99))
        with self.assertRaises(ValueError):
            reduce_interruption(
                started.state, InterruptionEvent.interim(episode(), 101, "hello", correlation=1.1)
            )


if __name__ == "__main__":
    unittest.main()
