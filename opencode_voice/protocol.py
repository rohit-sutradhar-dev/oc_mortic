"""Runtime validation for the Mortic sidepod <-> engine protocol v0.

Validates wire messages against ``protocol_schema.json``, which is generated
from the normative TypeScript schema in ``protocol/schema.ts`` (see
``docs/MORTIC_PROTOCOL_V0.md``). Contract semantics: unknown fields on known
message types pass; unknown message types are reported separately so callers
can log and ignore them per the compatibility rules.
"""
from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator


@functools.lru_cache(maxsize=1)
def schema_document() -> dict[str, Any]:
    text = resources.files("opencode_voice").joinpath("protocol_schema.json").read_text()
    return json.loads(text)


# The generated artifact is the single source of the version string.
PROTOCOL_VERSION = str(schema_document()["protocolVersion"])


@functools.lru_cache(maxsize=64)
def _validator(direction: str, message_type: str) -> Draft202012Validator | None:
    table = schema_document()["commands" if direction == "command" else "events"]
    schema = table.get(message_type)
    if schema is None:
        return None
    return Draft202012Validator(schema)


@dataclass(frozen=True)
class ProtocolCheck:
    ok: bool
    unknown_type: bool = False
    errors: tuple[str, ...] = field(default_factory=tuple)


def _check(direction: str, payload: Any) -> ProtocolCheck:
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        return ProtocolCheck(ok=False, errors=("$: not an object with a string `type`",))
    validator = _validator(direction, payload["type"])
    if validator is None:
        return ProtocolCheck(ok=False, unknown_type=True)
    errors = tuple(
        f"$.{'.'.join(str(part) for part in error.path) or ''}: {error.message}"
        for error in validator.iter_errors(payload)
    )
    return ProtocolCheck(ok=not errors, errors=errors)


def check_command(payload: Any) -> ProtocolCheck:
    """Validate a sidepod -> engine command."""
    return _check("command", payload)


def check_event(payload: Any) -> ProtocolCheck:
    """Validate an engine -> sidepod event."""
    return _check("event", payload)
