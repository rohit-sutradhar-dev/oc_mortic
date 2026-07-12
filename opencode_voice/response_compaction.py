from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from opencode_voice.response_contract import normalize_semantic_text
from opencode_voice.state import event_properties, event_session_id, prompt_context_tokens


CompactionMode = Literal["disabled", "manual-legacy", "manual-v2", "auto"]


@dataclass(frozen=True)
class CompactionProfile:
    profile_id: str
    mode: CompactionMode
    context_limit: int = 128_000
    input_limit: int | None = None
    output_limit: int = 8_192
    reserved: int | None = None
    tail_turns: int = 2
    preserve_recent_tokens: int = 8_000
    manual_threshold: int | None = None
    prune: bool = True
    native_scale: bool = False

    @property
    def effective_trigger(self) -> int | None:
        if self.mode == "disabled":
            return None
        if self.manual_threshold is not None:
            return self.manual_threshold
        if self.input_limit is not None:
            return max(0, self.input_limit - (self.reserved or 0))
        return max(0, self.context_limit - self.output_limit)

    def config_overlay(self) -> dict[str, Any]:
        limit: dict[str, int] = {"context": self.context_limit, "output": self.output_limit}
        if self.input_limit is not None:
            limit["input"] = self.input_limit
        compaction: dict[str, Any] = {
            "auto": self.mode == "auto",
            "prune": self.prune,
            "tail_turns": self.tail_turns,
            "preserve_recent_tokens": self.preserve_recent_tokens,
        }
        if self.reserved is not None:
            compaction["reserved"] = self.reserved
        return {"model_limit": limit, "compaction": compaction}


def compaction_profiles(*, native_scale: bool = False) -> list[CompactionProfile]:
    profiles = [
        CompactionProfile("disabled-control", "disabled", context_limit=32_000, input_limit=32_000, prune=False),
        CompactionProfile(
            "scaled-conservative",
            "auto",
            input_limit=32_000,
            reserved=2_000,
            tail_turns=6,
            preserve_recent_tokens=12_000,
        ),
        CompactionProfile(
            "scaled-aggressive",
            "auto",
            input_limit=32_000,
            reserved=12_000,
            tail_turns=2,
            preserve_recent_tokens=4_000,
        ),
        CompactionProfile(
            "forced",
            "manual-v2",
            context_limit=32_000,
            input_limit=32_000,
            reserved=12_000,
            tail_turns=2,
            preserve_recent_tokens=8_000,
            manual_threshold=20_000,
        ),
    ]
    if native_scale:
        profiles.extend(
            [
                CompactionProfile(
                    "mortic-current",
                    "manual-legacy",
                    reserved=10_000,
                    manual_threshold=70_000,
                    native_scale=True,
                ),
                CompactionProfile("native-auto", "auto", reserved=None, native_scale=True),
            ]
        )
    return profiles


@dataclass(frozen=True)
class ContextMeasurement:
    recorded_tokens: int
    estimated_tokens: int
    context_limit: int
    effective_trigger: int | None

    @property
    def context_utilization(self) -> float:
        return self.recorded_tokens / self.context_limit if self.context_limit else 0.0

    @property
    def trigger_utilization(self) -> float | None:
        if not self.effective_trigger:
            return None
        return self.recorded_tokens / self.effective_trigger


@dataclass
class CompactionObservation:
    profile_id: str
    session_id: str
    message_id: str | None = None
    reason: str | None = None
    started_ms: int | None = None
    ended_ms: int | None = None
    summary: str = ""
    recent: str = ""
    delta_text: str = ""
    legacy_completed_seen: bool = False
    tail_start_id: str | None = None
    compaction_count: int = 0
    before: ContextMeasurement | None = None
    after: ContextMeasurement | None = None
    duplicate_text_hashes: tuple[str, ...] = ()
    duplicate_tool_hashes: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()

    @property
    def latency_ms(self) -> int | None:
        if self.started_ms is None or self.ended_ms is None:
            return None
        return max(0, self.ended_ms - self.started_ms)


class CompactionEventTracker:
    def __init__(self, session_id: str, profile_id: str) -> None:
        self.session_id = session_id
        self.profile_id = profile_id
        self.observations: list[CompactionObservation] = []
        self._by_message: dict[str, CompactionObservation] = {}
        self.event_types: list[str] = []

    def update(self, event: dict[str, Any]) -> None:
        if event_session_id(event) != self.session_id:
            return
        event_type = str(event.get("type") or "")
        if "compaction" not in event_type and event_type != "session.compacted":
            return
        properties = event_properties(event)
        self.event_types.append(event_type)
        if event_type == "session.compacted":
            if self.observations:
                self.observations[-1].legacy_completed_seen = True
            return
        message_id = str(properties.get("messageID") or "")
        if not message_id:
            return
        observation = self._by_message.get(message_id)
        if observation is None:
            observation = CompactionObservation(
                profile_id=self.profile_id,
                session_id=self.session_id,
                message_id=message_id,
                compaction_count=len(self.observations) + 1,
            )
            self._by_message[message_id] = observation
            self.observations.append(observation)
        timestamp = _integer(properties.get("timestamp"))
        if event_type.endswith("started"):
            observation.started_ms = timestamp
            observation.reason = str(properties.get("reason") or "unknown")
        elif event_type.endswith("delta"):
            observation.delta_text += str(properties.get("text") or "")
        elif event_type.endswith("ended"):
            observation.ended_ms = timestamp
            observation.reason = str(properties.get("reason") or observation.reason or "unknown")
            observation.summary = str(properties.get("text") or "")
            observation.recent = str(properties.get("recent") or "")
        observation.event_types = tuple(self.event_types)

    def reconcile_messages(self, messages: list[dict[str, Any]]) -> None:
        compaction_users: dict[str, dict[str, Any]] = {}
        for message in messages:
            info = message.get("info") if isinstance(message, dict) else None
            if not isinstance(info, dict):
                continue
            message_id = str(info.get("id") or "")
            for part in message.get("parts") or []:
                if isinstance(part, dict) and part.get("type") == "compaction":
                    compaction_users[message_id] = part
        for message_id, part in compaction_users.items():
            observation = self._by_message.get(message_id)
            if observation is None:
                observation = CompactionObservation(
                    self.profile_id,
                    self.session_id,
                    message_id=message_id,
                    compaction_count=len(self.observations) + 1,
                )
                self._by_message[message_id] = observation
                self.observations.append(observation)
            observation.tail_start_id = str(part.get("tail_start_id") or "") or None
            observation.reason = observation.reason or ("auto" if part.get("auto") else "manual")
        for message in messages:
            info = message.get("info") if isinstance(message, dict) else None
            if not isinstance(info, dict) or info.get("role") != "assistant" or info.get("summary") is not True:
                continue
            parent = str(info.get("parentID") or "")
            observation = self._by_message.get(parent)
            if observation is None:
                continue
            text = "\n\n".join(
                str(part.get("text") or "").strip()
                for part in message.get("parts") or []
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
            )
            if text:
                observation.summary = text


@dataclass
class ProviderTokenTracker:
    """Capture authoritative prompt context tokens from assistant events."""

    session_id: str
    current_tokens: int = 0
    samples: list[int] = field(default_factory=list)
    message_ids: list[str] = field(default_factory=list)

    def update(self, event: dict[str, Any]) -> None:
        if event_session_id(event) != self.session_id or event.get("type") != "message.updated":
            return
        info = event_properties(event).get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant" or info.get("summary") is True:
            return
        tokens = prompt_context_tokens(info.get("tokens") or {})
        if tokens <= 0:
            return
        message_id = str(info.get("id") or "")
        if self.message_ids and self.message_ids[-1] == message_id and self.samples[-1] == tokens:
            return
        self.current_tokens = tokens
        self.samples.append(tokens)
        self.message_ids.append(message_id)


@dataclass(frozen=True)
class ForkSnapshot:
    source_hash_before: str
    source_hash_after: str
    fork_hash: str
    source_untouched: bool
    inherited_content_equal: bool
    parent_links_valid: bool
    tail_links_valid: bool
    source_message_count: int
    fork_message_count: int


def compare_fork_snapshots(
    source_before: list[dict[str, Any]],
    source_after: list[dict[str, Any]],
    fork_messages: list[dict[str, Any]],
    *,
    expected_inherited: list[dict[str, Any]] | None = None,
) -> ForkSnapshot:
    source_graph = normalized_message_graph(source_before)
    source_after_graph = normalized_message_graph(source_after)
    expected_messages = source_before if expected_inherited is None else expected_inherited
    expected_graph = normalized_message_graph(expected_messages)
    fork_graph = normalized_message_graph(fork_messages)
    return ForkSnapshot(
        source_hash_before=_hash(source_graph),
        source_hash_after=_hash(source_after_graph),
        fork_hash=_hash(fork_graph),
        source_untouched=source_graph == source_after_graph,
        inherited_content_equal=_content_graph(expected_graph) == _content_graph(fork_graph),
        parent_links_valid=_links_valid(fork_graph, "parentID"),
        tail_links_valid=_links_valid(fork_graph, "tail_start_id"),
        source_message_count=len(expected_messages),
        fork_message_count=len(fork_messages),
    )


def normalized_message_graph(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    message_ids: dict[str, str] = {}
    for index, message in enumerate(messages):
        info = message.get("info") if isinstance(message, dict) else None
        raw = str(info.get("id") or "") if isinstance(info, dict) else str(message.get("id") or "")
        if raw:
            message_ids[raw] = f"m{index}"
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if isinstance(info, dict):
            role = str(info.get("role") or "")
            item: dict[str, Any] = {
                "id": f"m{index}",
                "role": role,
                "summary": bool(info.get("summary")),
                "finish": info.get("finish"),
            }
            if info.get("parentID"):
                item["parentID"] = message_ids.get(str(info["parentID"]), "missing")
            parts = message.get("parts") or []
        else:
            role = str(message.get("type") or "")
            item = {"id": f"m{index}", "role": role}
            parts = message.get("content") or []
        normalized_parts: list[dict[str, Any]] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            normalized: dict[str, Any] = {"type": part_type}
            if part_type in {"text", "reasoning"}:
                normalized["text"] = str(part.get("text") or "")
            if part_type == "tool":
                normalized["tool"] = str(part.get("tool") or part.get("name") or "")
                state = part.get("state") or {}
                normalized["status"] = state.get("status") if isinstance(state, dict) else None
                normalized["input"] = state.get("input") if isinstance(state, dict) else None
            if part_type == "compaction":
                normalized["auto"] = bool(part.get("auto"))
                normalized["overflow"] = bool(part.get("overflow"))
                if part.get("tail_start_id"):
                    normalized["tail_start_id"] = message_ids.get(str(part["tail_start_id"]), "missing")
            normalized_parts.append(normalized)
        item["parts"] = normalized_parts
        result.append(item)
    return result


def recorded_context_tokens(messages: list[dict[str, Any]]) -> int:
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if isinstance(info, dict):
            role = info.get("role")
            summary = info.get("summary")
            tokens_payload = info.get("tokens") or {}
        else:
            role = message.get("type") if isinstance(message, dict) else None
            summary = message.get("summary") if isinstance(message, dict) else None
            tokens_payload = message.get("tokens") or {} if isinstance(message, dict) else {}
        if role != "assistant" or summary is True:
            continue
        tokens = prompt_context_tokens(tokens_payload)
        if tokens > 0:
            return tokens
    return 0


def duplicate_action_hashes(messages: list[dict[str, Any]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    texts: list[str] = []
    tools: list[str] = []
    for item in normalized_message_graph(messages):
        if item.get("role") != "assistant" or item.get("summary"):
            continue
        for part in item.get("parts") or []:
            if part.get("type") == "text" and part.get("text"):
                texts.append(_hash(normalize_semantic_text(str(part["text"]))))
            if part.get("type") == "tool":
                tools.append(_hash({"tool": part.get("tool"), "input": part.get("input")}))
    return _duplicates(texts), _duplicates(tools)


def compaction_thrashed(
    previous_after_tokens: int,
    next_before_tokens: int,
    effective_trigger: int,
) -> bool:
    minimum_growth = max(4_096, int(effective_trigger * 0.10))
    return next_before_tokens < effective_trigger or next_before_tokens - previous_after_tokens < minimum_growth


def _content_graph(graph: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if key not in {"id", "parentID"}} for item in graph]


def _links_valid(graph: list[dict[str, Any]], key: str) -> bool:
    ids = {str(item.get("id")) for item in graph}
    for item in graph:
        if key in item and item[key] not in ids:
            return False
        for part in item.get("parts") or []:
            if key in part and part[key] not in ids:
                return False
    return True


def _duplicates(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def observation_dict(observation: CompactionObservation) -> dict[str, Any]:
    return asdict(observation)
