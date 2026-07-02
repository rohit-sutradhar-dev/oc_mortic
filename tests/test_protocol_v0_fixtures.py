from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "protocol_v0_messages.json"
PROTOCOL_VERSION = "mortic.sidepod.v0"

COMMAND_REQUIRED_FIELDS = {
    "start": {"type", "clientEventId", "sentAt", "sourceSessionId", "keepFork"},
    "ptt.start": {"type", "clientEventId", "sentAt", "turnId", "inputMode"},
    "ptt.stop": {"type", "clientEventId", "sentAt", "turnId", "reason"},
    "live.set": {"type", "clientEventId", "sentAt", "value"},
    "refresh": {"type", "clientEventId", "sentAt", "reason"},
    "barge_in": {"type", "clientEventId", "sentAt", "reason"},
    "confirm.response": {"type", "clientEventId", "sentAt", "promptId", "actionId", "confirmed"},
}

EVENT_REQUIRED_FIELDS = {
    "ready": {"type", "sentAt", "voiceLaneId", "state"},
    "listening": {"type", "sentAt", "voiceLaneId", "mode"},
    "transcript": {"type", "sentAt", "turnId", "sequence", "text", "final"},
    "thinking": {"type", "sentAt", "turnId", "sourceMode"},
    "assistant.delta": {"type", "sentAt", "turnId", "sequence", "delta"},
    "speaking": {"type", "sentAt", "turnId"},
    "complete": {"type", "sentAt", "turnId", "latency"},
    "interrupted": {"type", "sentAt", "reason"},
    "voice_bridge_issue": {"type", "sentAt", "userMessage", "diagnosticCode", "retryable"},
}

SCREEN_ONLY_CATEGORIES = {"code", "output", "diff", "path", "json"}
FORBIDDEN_NORMAL_UI_TERMS = {"Mercury", "Inception", "Deepgram", "OPENAI_API_KEY", "sk-"}


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def messages_by_name(entries: list[dict]) -> dict[str, dict]:
    return {entry["name"]: entry["message"] for entry in entries}


def test_fixture_covers_every_frozen_command_and_event() -> None:
    fixture = load_fixture()

    assert fixture["protocolVersion"] == PROTOCOL_VERSION
    assert set(messages_by_name(fixture["commands"])) == set(COMMAND_REQUIRED_FIELDS)
    assert set(messages_by_name(fixture["events"])) == set(EVENT_REQUIRED_FIELDS)


def test_command_and_event_shapes_match_contract_required_fields() -> None:
    fixture = load_fixture()

    for message_type, message in messages_by_name(fixture["commands"]).items():
        assert message["type"] == message_type
        assert COMMAND_REQUIRED_FIELDS[message_type] <= set(message)
        parse_iso8601(message["sentAt"])

    for message_type, message in messages_by_name(fixture["events"]).items():
        assert message["type"] == message_type
        assert EVENT_REQUIRED_FIELDS[message_type] <= set(message)
        parse_iso8601(message["sentAt"])


def test_version_handshake_fixtures_are_tagged() -> None:
    fixture = load_fixture()
    commands = messages_by_name(fixture["commands"])
    events = messages_by_name(fixture["events"])

    assert commands["start"]["protocolVersion"] == PROTOCOL_VERSION
    assert events["ready"]["protocolVersion"] == PROTOCOL_VERSION


def test_turn_trace_references_known_fixtures_in_directional_order() -> None:
    fixture = load_fixture()
    known_names = set(messages_by_name(fixture["commands"])) | set(messages_by_name(fixture["events"]))
    trace = fixture["turnTrace"]

    assert trace[0] == {"direction": "sidepod_to_engine", "fixture": "start"}
    assert trace[1] == {"direction": "engine_to_sidepod", "fixture": "ready"}
    assert {entry["fixture"] for entry in trace} == known_names
    assert {entry["direction"] for entry in trace} == {"sidepod_to_engine", "engine_to_sidepod"}


def test_screen_only_examples_cover_code_output_path_diff_and_json() -> None:
    fixture = load_fixture()
    examples = fixture["screenOnlyExamples"]

    assert {example["category"] for example in examples} == SCREEN_ONLY_CATEGORIES
    for example in examples:
        message = example["message"]
        assert message["type"] == "assistant.delta"
        assert message["screenOnly"] is True
        assert EVENT_REQUIRED_FIELDS["assistant.delta"] <= set(message)
        parse_iso8601(message["sentAt"])


def test_normal_ui_fixtures_avoid_provider_runtime_and_secret_terms() -> None:
    fixture = load_fixture()
    normal_messages = fixture["commands"] + fixture["events"]
    normal_payload = json.dumps(normal_messages)

    for forbidden_term in FORBIDDEN_NORMAL_UI_TERMS:
        assert forbidden_term not in normal_payload
