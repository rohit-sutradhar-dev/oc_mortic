from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum


class InterruptionPhase(str, Enum):
    QUIET = "quiet"
    USER_TURN = "user_turn"
    CANDIDATE = "candidate"
    SUPPRESSED = "suppressed"
    INTERRUPTED = "interrupted"
    CLOSED = "closed"


class InterruptionEventKind(str, Enum):
    EPISODE_STARTED = "episode_started"
    INTERIM_TRANSCRIPT = "interim_transcript"
    CANDIDATE_EVALUATION = "candidate_evaluation"
    EAGER_EOT = "eager_eot"
    FINAL_EOT = "final_eot"
    TURN_RESUMED = "turn_resumed"
    MANUAL_INTERRUPT = "manual_interrupt"
    TICK = "tick"
    CLOSE = "close"


class InterruptionActionKind(str, Enum):
    HOLD_PLAYBACK = "hold_playback"
    RESUME_PLAYBACK = "resume_playback"
    COMMIT_INTERRUPT = "commit_interrupt"
    ADMIT_TRANSCRIPT = "admit_transcript"
    SUPPRESS_EPISODE = "suppress_episode"
    CANCEL_SPECULATION = "cancel_speculation"
    EPISODE_EXPIRED = "episode_expired"


@dataclass(frozen=True)
class EpisodeIdentity:
    """Identity shared by every event belonging to one Flux/acoustic episode."""

    flux_epoch: int
    turn_index: int
    acoustic_group_id: str
    playback_generation: int


@dataclass(frozen=True)
class InterruptionPolicy:
    evaluation_ms: int = 500
    suppression_guard_ms: int = 500
    provider_silence_ms: int = 2_000
    echo_correlation: float = 0.75

    def __post_init__(self) -> None:
        if self.evaluation_ms < 0 or self.suppression_guard_ms < 0 or self.provider_silence_ms <= 0:
            raise ValueError("interruption timing values must be non-negative")
        if not 0.0 <= self.echo_correlation <= 1.0:
            raise ValueError("echo_correlation must be between 0 and 1")


@dataclass(frozen=True)
class InterruptionEvent:
    kind: InterruptionEventKind
    at_ms: int
    episode: EpisodeIdentity | None = None
    text: str = ""
    correlation: float | None = None
    playback_exposed: bool = False
    reason: str = ""

    @classmethod
    def start(
        cls, episode: EpisodeIdentity, at_ms: int, *, playback_exposed: bool
    ) -> InterruptionEvent:
        return cls(
            kind=InterruptionEventKind.EPISODE_STARTED,
            at_ms=at_ms,
            episode=episode,
            playback_exposed=playback_exposed,
        )

    @classmethod
    def interim(
        cls,
        episode: EpisodeIdentity,
        at_ms: int,
        text: str,
        *,
        correlation: float | None = None,
        playback_exposed: bool = False,
    ) -> InterruptionEvent:
        return cls(
            kind=InterruptionEventKind.INTERIM_TRANSCRIPT,
            at_ms=at_ms,
            episode=episode,
            text=text,
            correlation=correlation,
            playback_exposed=playback_exposed,
        )

    @classmethod
    def evaluate(
        cls,
        episode: EpisodeIdentity,
        at_ms: int,
        *,
        correlation: float | None,
    ) -> "InterruptionEvent":
        """Evaluate a candidate with fresh acoustic evidence.

        This is deliberately distinct from provider interim speech: a timer
        evaluation must not reopen an episode whose final EOT already arrived.
        """
        return cls(
            kind=InterruptionEventKind.CANDIDATE_EVALUATION,
            at_ms=at_ms,
            episode=episode,
            correlation=correlation,
        )

    @classmethod
    def eager_eot(
        cls,
        episode: EpisodeIdentity,
        at_ms: int,
        text: str,
        *,
        correlation: float | None = None,
    ) -> InterruptionEvent:
        return cls(
            kind=InterruptionEventKind.EAGER_EOT,
            at_ms=at_ms,
            episode=episode,
            text=text,
            correlation=correlation,
        )

    @classmethod
    def final_eot(
        cls,
        episode: EpisodeIdentity,
        at_ms: int,
        text: str,
        *,
        correlation: float | None = None,
        playback_exposed: bool = False,
    ) -> InterruptionEvent:
        return cls(
            kind=InterruptionEventKind.FINAL_EOT,
            at_ms=at_ms,
            episode=episode,
            text=text,
            correlation=correlation,
            playback_exposed=playback_exposed,
        )

    @classmethod
    def turn_resumed(cls, episode: EpisodeIdentity, at_ms: int) -> InterruptionEvent:
        return cls(kind=InterruptionEventKind.TURN_RESUMED, at_ms=at_ms, episode=episode)

    @classmethod
    def manual(
        cls, episode: EpisodeIdentity, at_ms: int, *, reason: str = "manual"
    ) -> InterruptionEvent:
        return cls(
            kind=InterruptionEventKind.MANUAL_INTERRUPT,
            at_ms=at_ms,
            episode=episode,
            reason=reason,
        )

    @classmethod
    def tick(cls, at_ms: int) -> InterruptionEvent:
        return cls(kind=InterruptionEventKind.TICK, at_ms=at_ms)

    @classmethod
    def close(cls, at_ms: int) -> InterruptionEvent:
        return cls(kind=InterruptionEventKind.CLOSE, at_ms=at_ms)


@dataclass(frozen=True)
class InterruptionAction:
    kind: InterruptionActionKind
    episode: EpisodeIdentity | None
    reason: str
    text: str = ""


@dataclass(frozen=True)
class InterruptionSnapshot:
    phase: InterruptionPhase = InterruptionPhase.QUIET
    episode: EpisodeIdentity | None = None
    started_at_ms: int | None = None
    last_provider_activity_ms: int | None = None
    final_eot_at_ms: int | None = None
    suppression_guard_until_ms: int | None = None
    latest_text: str = ""
    latest_correlation: float | None = None
    playback_held: bool = False
    decision_reason: str = ""
    updated_at_ms: int = -1


@dataclass(frozen=True)
class InterruptionReduction:
    state: InterruptionSnapshot
    actions: tuple[InterruptionAction, ...] = ()


_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_BACKCHANNELS = frozenset({"uh huh", "mm hmm", "mhm"})
_PRIORITY_COMMANDS = frozenset({"stop", "wait"})


def normalized_words(text: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(text.casefold()))


def is_narrow_backchannel(text: str) -> bool:
    return " ".join(normalized_words(text)) in _BACKCHANNELS


def is_priority_interrupt(text: str) -> bool:
    words = normalized_words(text)
    return bool(words and words[0] in _PRIORITY_COMMANDS)


def reduce_interruption(
    state: InterruptionSnapshot,
    event: InterruptionEvent,
    policy: InterruptionPolicy = InterruptionPolicy(),
) -> InterruptionReduction:
    """Pure reducer for one connection's user-speech/interruption lifecycle.

    The caller executes returned actions and feeds the returned state into the
    next call. No timer, provider, playback, or logging side effect happens here.
    """

    _validate_event(state, event)
    if state.phase is InterruptionPhase.CLOSED:
        return InterruptionReduction(state)
    if event.kind is InterruptionEventKind.CLOSE:
        return InterruptionReduction(
            InterruptionSnapshot(phase=InterruptionPhase.CLOSED, updated_at_ms=event.at_ms)
        )

    advanced = _advance_expirations(state, event.at_ms, policy)
    state = advanced.state
    actions = list(advanced.actions)

    if event.kind is InterruptionEventKind.TICK:
        decision = _evaluate_candidate(state, event.at_ms, policy)
        return InterruptionReduction(decision.state, tuple(actions) + decision.actions)

    if event.kind is InterruptionEventKind.MANUAL_INTERRUPT:
        assert event.episode is not None
        if state.phase is InterruptionPhase.INTERRUPTED:
            return InterruptionReduction(replace(state, updated_at_ms=event.at_ms), tuple(actions))
        committed = _commit(
            _episode_state(
                phase=InterruptionPhase.CANDIDATE,
                episode=event.episode,
                at_ms=event.at_ms,
                playback_held=state.playback_held,
            ),
            event.at_ms,
            reason=f"manual:{event.reason or 'manual'}",
        )
        return InterruptionReduction(committed.state, tuple(actions) + committed.actions)

    assert event.episode is not None

    if event.kind is InterruptionEventKind.EPISODE_STARTED:
        started = _handle_start(state, event)
        return InterruptionReduction(started.state, tuple(actions) + started.actions)

    if event.kind is InterruptionEventKind.TURN_RESUMED:
        if _event_belongs(state, event.episode):
            state = replace(
                state,
                last_provider_activity_ms=event.at_ms,
                final_eot_at_ms=None,
                suppression_guard_until_ms=None,
                updated_at_ms=event.at_ms,
            )
        actions.append(
            InterruptionAction(
                InterruptionActionKind.CANCEL_SPECULATION,
                event.episode,
                reason="turn_resumed",
            )
        )
        return InterruptionReduction(state, tuple(actions))

    if event.kind is InterruptionEventKind.CANDIDATE_EVALUATION:
        if state.phase is InterruptionPhase.QUIET or not _event_belongs(state, event.episode):
            return InterruptionReduction(replace(state, updated_at_ms=event.at_ms), tuple(actions))
        state = replace(
            state,
            latest_correlation=(
                event.correlation if event.correlation is not None else state.latest_correlation
            ),
            updated_at_ms=event.at_ms,
        )
        decision = _evaluate_candidate(state, event.at_ms, policy)
        return InterruptionReduction(decision.state, tuple(actions) + decision.actions)

    if state.phase is InterruptionPhase.QUIET:
        # Episode ownership begins at StartOfTurn. A delayed transcript from an
        # expired provider turn must not silently resurrect it.
        return InterruptionReduction(replace(state, updated_at_ms=event.at_ms), tuple(actions))
    elif not _event_belongs(state, event.episode):
        # A late event from an expired/superseded provider turn cannot mutate
        # the episode currently owning playback.
        return InterruptionReduction(replace(state, updated_at_ms=event.at_ms), tuple(actions))

    state = _record_provider_evidence(state, event)

    if state.phase is InterruptionPhase.SUPPRESSED:
        if is_priority_interrupt(state.latest_text):
            committed = _commit(state, event.at_ms, reason="priority_command")
            return InterruptionReduction(committed.state, tuple(actions) + committed.actions)
        if event.kind is InterruptionEventKind.FINAL_EOT:
            state = replace(
                state,
                final_eot_at_ms=event.at_ms,
                suppression_guard_until_ms=event.at_ms + policy.suppression_guard_ms,
            )
        return InterruptionReduction(state, tuple(actions))

    if state.phase is InterruptionPhase.INTERRUPTED:
        if event.kind is InterruptionEventKind.FINAL_EOT:
            return InterruptionReduction(
                _quiet(event.at_ms),
                tuple(actions)
                + (
                    InterruptionAction(
                        InterruptionActionKind.ADMIT_TRANSCRIPT,
                        state.episode,
                        reason="final_eot",
                        text=state.latest_text,
                    ),
                ),
            )
        return InterruptionReduction(state, tuple(actions))

    if state.phase is InterruptionPhase.USER_TURN:
        if event.kind is InterruptionEventKind.FINAL_EOT:
            return InterruptionReduction(
                _quiet(event.at_ms),
                tuple(actions)
                + (
                    InterruptionAction(
                        InterruptionActionKind.ADMIT_TRANSCRIPT,
                        state.episode,
                        reason="final_eot",
                        text=state.latest_text,
                    ),
                ),
            )
        return InterruptionReduction(state, tuple(actions))

    decision = _evaluate_candidate(state, event.at_ms, policy)
    return InterruptionReduction(decision.state, tuple(actions) + decision.actions)


def _validate_event(state: InterruptionSnapshot, event: InterruptionEvent) -> None:
    if event.at_ms < state.updated_at_ms:
        raise ValueError("interruption events must be monotonic")
    if event.correlation is not None and not 0.0 <= event.correlation <= 1.0:
        raise ValueError("correlation must be between 0 and 1")
    if event.kind not in {InterruptionEventKind.TICK, InterruptionEventKind.CLOSE} and event.episode is None:
        raise ValueError(f"{event.kind.value} requires an episode identity")


def _advance_expirations(
    state: InterruptionSnapshot, at_ms: int, policy: InterruptionPolicy
) -> InterruptionReduction:
    if state.phase in {InterruptionPhase.QUIET, InterruptionPhase.CLOSED}:
        return InterruptionReduction(replace(state, updated_at_ms=at_ms))
    if (
        state.phase is InterruptionPhase.SUPPRESSED
        and state.suppression_guard_until_ms is not None
        and at_ms >= state.suppression_guard_until_ms
    ):
        episode = state.episode
        return InterruptionReduction(
            _quiet(at_ms),
            (
                InterruptionAction(
                    InterruptionActionKind.EPISODE_EXPIRED,
                    episode,
                    reason="suppression_guard_complete",
                ),
            ),
        )
    if (
        state.final_eot_at_ms is None
        and state.last_provider_activity_ms is not None
        and at_ms - state.last_provider_activity_ms >= policy.provider_silence_ms
    ):
        episode = state.episode
        return InterruptionReduction(
            _quiet(at_ms),
            (
                InterruptionAction(
                    InterruptionActionKind.EPISODE_EXPIRED,
                    episode,
                    reason="provider_silence",
                ),
            ),
        )
    return InterruptionReduction(replace(state, updated_at_ms=at_ms))


def _handle_start(state: InterruptionSnapshot, event: InterruptionEvent) -> InterruptionReduction:
    assert event.episode is not None
    if state.phase is InterruptionPhase.QUIET:
        return _start_episode(event.episode, event.at_ms, event.playback_exposed)

    if state.phase is InterruptionPhase.SUPPRESSED and _same_suppression_cluster(state.episode, event.episode):
        # Flux may emit a new turn_index after resumed playback creates another
        # acoustic edge. It remains owned by the existing suppression window.
        return InterruptionReduction(
            replace(
                state,
                episode=event.episode,
                last_provider_activity_ms=event.at_ms,
                final_eot_at_ms=None,
                suppression_guard_until_ms=None,
                updated_at_ms=event.at_ms,
            )
        )

    if _same_acoustic_episode(state.episode, event.episode):
        return InterruptionReduction(
            replace(state, last_provider_activity_ms=event.at_ms, updated_at_ms=event.at_ms)
        )

    expired = InterruptionAction(
        InterruptionActionKind.EPISODE_EXPIRED,
        state.episode,
        reason="superseded",
    )
    started = _start_episode(event.episode, event.at_ms, event.playback_exposed)
    return InterruptionReduction(started.state, (expired,) + started.actions)


def _start_episode(
    episode: EpisodeIdentity, at_ms: int, playback_exposed: bool
) -> InterruptionReduction:
    phase = InterruptionPhase.CANDIDATE if playback_exposed else InterruptionPhase.USER_TURN
    state = _episode_state(
        phase=phase,
        episode=episode,
        at_ms=at_ms,
        playback_held=playback_exposed,
    )
    if not playback_exposed:
        return InterruptionReduction(state)
    return InterruptionReduction(
        state,
        (
            InterruptionAction(
                InterruptionActionKind.HOLD_PLAYBACK,
                episode,
                reason="overlapping_speech",
            ),
        ),
    )


def _record_provider_evidence(
    state: InterruptionSnapshot, event: InterruptionEvent
) -> InterruptionSnapshot:
    text = event.text.strip() or state.latest_text
    correlation = event.correlation if event.correlation is not None else state.latest_correlation
    reopened = event.kind in {
        InterruptionEventKind.INTERIM_TRANSCRIPT,
        InterruptionEventKind.EAGER_EOT,
    }
    final_at = (
        event.at_ms
        if event.kind is InterruptionEventKind.FINAL_EOT
        else None if reopened else state.final_eot_at_ms
    )
    return replace(
        state,
        episode=event.episode if state.phase is InterruptionPhase.SUPPRESSED else state.episode,
        latest_text=text,
        latest_correlation=correlation,
        last_provider_activity_ms=event.at_ms,
        final_eot_at_ms=final_at,
        suppression_guard_until_ms=None if reopened else state.suppression_guard_until_ms,
        updated_at_ms=event.at_ms,
    )


def _evaluate_candidate(
    state: InterruptionSnapshot, at_ms: int, policy: InterruptionPolicy
) -> InterruptionReduction:
    if state.phase is not InterruptionPhase.CANDIDATE or state.started_at_ms is None:
        return InterruptionReduction(state)
    if is_priority_interrupt(state.latest_text):
        return _commit(state, at_ms, reason="priority_command")

    words = normalized_words(state.latest_text)
    correlation = state.latest_correlation
    if (
        len(words) >= 2
        and not is_narrow_backchannel(state.latest_text)
        and correlation is not None
        and correlation < policy.echo_correlation
    ):
        return _commit(state, at_ms, reason="early_non_backchannel")

    if at_ms - state.started_at_ms < policy.evaluation_ms:
        return InterruptionReduction(state)
    if is_narrow_backchannel(state.latest_text):
        return _suppress(state, at_ms, policy, reason="backchannel")
    if correlation is not None and correlation >= policy.echo_correlation:
        return _suppress(state, at_ms, policy, reason="echo_correlation")
    return _commit(state, at_ms, reason="evaluation_confirmed")


def _commit(state: InterruptionSnapshot, at_ms: int, *, reason: str) -> InterruptionReduction:
    episode = state.episode
    commit = InterruptionAction(
        InterruptionActionKind.COMMIT_INTERRUPT,
        episode,
        reason=reason,
        text=state.latest_text,
    )
    if state.final_eot_at_ms is not None:
        return InterruptionReduction(
            _quiet(at_ms),
            (
                commit,
                InterruptionAction(
                    InterruptionActionKind.ADMIT_TRANSCRIPT,
                    episode,
                    reason="final_eot",
                    text=state.latest_text,
                ),
            ),
        )
    return InterruptionReduction(
        replace(
            state,
            phase=InterruptionPhase.INTERRUPTED,
            playback_held=False,
            decision_reason=reason,
            updated_at_ms=at_ms,
        ),
        (commit,),
    )


def _suppress(
    state: InterruptionSnapshot,
    at_ms: int,
    policy: InterruptionPolicy,
    *,
    reason: str,
) -> InterruptionReduction:
    guard_until = (
        state.final_eot_at_ms + policy.suppression_guard_ms
        if state.final_eot_at_ms is not None
        else None
    )
    suppressed = replace(
        state,
        phase=InterruptionPhase.SUPPRESSED,
        suppression_guard_until_ms=guard_until,
        playback_held=False,
        decision_reason=reason,
        updated_at_ms=at_ms,
    )
    actions = (
        InterruptionAction(
            InterruptionActionKind.SUPPRESS_EPISODE,
            state.episode,
            reason=reason,
            text=state.latest_text,
        ),
        InterruptionAction(
            InterruptionActionKind.RESUME_PLAYBACK,
            state.episode,
            reason=reason,
        ),
    )
    if guard_until is not None and at_ms >= guard_until:
        return InterruptionReduction(
            _quiet(at_ms),
            actions
            + (
                InterruptionAction(
                    InterruptionActionKind.EPISODE_EXPIRED,
                    state.episode,
                    reason="suppression_guard_complete",
                ),
            ),
        )
    return InterruptionReduction(suppressed, actions)


def _event_belongs(state: InterruptionSnapshot, episode: EpisodeIdentity) -> bool:
    if state.episode is None:
        return False
    if state.phase is InterruptionPhase.SUPPRESSED:
        return _same_suppression_cluster(state.episode, episode)
    return _same_acoustic_episode(state.episode, episode)


def _same_acoustic_episode(left: EpisodeIdentity | None, right: EpisodeIdentity) -> bool:
    return bool(
        left
        and left.flux_epoch == right.flux_epoch
        and left.acoustic_group_id == right.acoustic_group_id
        and left.playback_generation == right.playback_generation
    )


def _same_suppression_cluster(left: EpisodeIdentity | None, right: EpisodeIdentity) -> bool:
    # During the post-resume guard even a fresh acoustic edge can be playback
    # leakage. A new Flux connection or playback generation is genuinely new.
    return bool(
        left
        and left.flux_epoch == right.flux_epoch
        and left.playback_generation == right.playback_generation
    )


def _episode_state(
    *,
    phase: InterruptionPhase,
    episode: EpisodeIdentity,
    at_ms: int,
    playback_held: bool,
) -> InterruptionSnapshot:
    return InterruptionSnapshot(
        phase=phase,
        episode=episode,
        started_at_ms=at_ms,
        last_provider_activity_ms=at_ms,
        playback_held=playback_held,
        updated_at_ms=at_ms,
    )


def _quiet(at_ms: int) -> InterruptionSnapshot:
    return InterruptionSnapshot(updated_at_ms=at_ms)
