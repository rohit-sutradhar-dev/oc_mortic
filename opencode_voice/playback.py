from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlaybackToken:
    """Identity of one assistant playback generation.

    ``generation`` is the cancellation fence. ``turn_id`` keeps provider and
    device telemetry attributable to the originating assistant turn.
    """

    generation: int
    turn_id: int
