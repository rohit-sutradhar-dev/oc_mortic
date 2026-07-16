from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from opencode_voice.protocol import PROTOCOL_VERSION, check_command, check_event, schema_document

REPO_ROOT = Path(__file__).parent.parent
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "protocol_v0_messages.json"
SCHEMA_SOURCE = REPO_ROOT / "protocol" / "schema.ts"
CANONICAL_SCHEMA = REPO_ROOT / "protocol" / "mortic.sidepod.v0.schema.json"
HELPER_SCHEMA = REPO_ROOT / "opencode_voice" / "protocol_schema.json"
PLUGIN_SCHEMA = REPO_ROOT / "opencode_mercury_sidepod" / "src" / "protocol.gen.mjs"

SCREEN_ONLY_CATEGORIES = {"code", "output", "diff", "path", "json"}
FORBIDDEN_NORMAL_UI_TERMS = {"Mercury", "Inception", "Deepgram", "OPENAI_API_KEY", "sk-"}


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def messages_by_name(entries: list[dict]) -> dict[str, dict]:
    return {entry["name"]: entry["message"] for entry in entries}


def test_fixture_covers_every_schema_command_and_event() -> None:
    fixture = load_fixture()
    schema = schema_document()

    assert fixture["protocolVersion"] == PROTOCOL_VERSION
    assert schema["protocolVersion"] == PROTOCOL_VERSION
    assert set(messages_by_name(fixture["commands"])) == set(schema["commands"])
    assert set(messages_by_name(fixture["events"])) == set(schema["events"])


def test_every_fixture_message_validates_against_the_generated_schema() -> None:
    fixture = load_fixture()

    for message_type, message in messages_by_name(fixture["commands"]).items():
        assert message["type"] == message_type
        parse_iso8601(message["sentAt"])
        check = check_command(message)
        assert check.ok, f"{message_type}: {check.errors}"

    for message_type, message in messages_by_name(fixture["events"]).items():
        assert message["type"] == message_type
        parse_iso8601(message["sentAt"])
        check = check_event(message)
        assert check.ok, f"{message_type}: {check.errors}"


def test_validator_rejects_mutations_and_flags_unknown_types() -> None:
    fixture = load_fixture()
    commands = messages_by_name(fixture["commands"])
    events = messages_by_name(fixture["events"])

    missing_required = dict(commands["live.set"])
    del missing_required["value"]
    assert not check_command(missing_required).ok

    wrong_type = dict(events["transcript"])
    wrong_type["sequence"] = "one"
    assert not check_event(wrong_type).ok

    unknown = check_event({"type": "deepgram.telemetry", "sentAt": "2026-07-02T04:00:00.000Z"})
    assert not unknown.ok and unknown.unknown_type

    extra_field = dict(events["ready"])
    extra_field["futureField"] = {"nested": True}
    assert check_event(extra_field).ok, "unknown fields on known types must pass"

    valid_activity = dict(events["thinking"])
    valid_activity["activity"] = "inspecting"
    assert check_event(valid_activity).ok

    invalid_activity = dict(events["thinking"])
    invalid_activity["activity"] = "reading_secret_path"
    assert not check_event(invalid_activity).ok


def test_generated_artifacts_match_the_typescript_source() -> None:
    source_hash = hashlib.sha256(SCHEMA_SOURCE.read_bytes()).hexdigest()

    canonical = json.loads(CANONICAL_SCHEMA.read_text())
    assert canonical["x-source-hash"] == source_hash, "protocol/schema.ts changed: run `npm run gen` in protocol/"
    assert CANONICAL_SCHEMA.read_text() == HELPER_SCHEMA.read_text()
    assert f'export const SOURCE_HASH = "{source_hash}"' in PLUGIN_SCHEMA.read_text()


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
    assert trace[-2] == {"direction": "sidepod_to_engine", "fixture": "stop"}
    assert trace[-1] == {"direction": "engine_to_sidepod", "fixture": "stopped"}
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
        assert check_event(message).ok
        parse_iso8601(message["sentAt"])


def test_normal_ui_fixtures_avoid_provider_runtime_and_secret_terms() -> None:
    fixture = load_fixture()
    normal_messages = fixture["commands"] + fixture["events"]
    normal_payload = json.dumps(normal_messages)

    for forbidden_term in FORBIDDEN_NORMAL_UI_TERMS:
        assert forbidden_term not in normal_payload
