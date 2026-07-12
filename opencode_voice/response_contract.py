from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from jsonschema import Draft202012Validator

from opencode_voice.state import event_properties, event_session_id


RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "displayText": {"type": "string", "minLength": 1, "maxLength": 1200},
        "spokenText": {"type": "string", "minLength": 1, "maxLength": 1200},
    },
    "required": ["displayText", "spokenText"],
}

JUDGE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "naturalness": {"type": "integer", "minimum": 1, "maximum": 5},
        "directness": {"type": "integer", "minimum": 1, "maximum": 5},
        "completeness": {"type": "integer", "minimum": 1, "maximum": 5},
        "equivalence": {"type": "integer", "minimum": 1, "maximum": 5},
        "notes": {"type": "string", "maxLength": 500},
    },
    "required": ["naturalness", "directness", "completeness", "equivalence", "notes"],
}

_RESPONSE_VALIDATOR = Draft202012Validator(RESPONSE_SCHEMA)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._-])(?:[A-Za-z]:[\\/]|/(?:[^/\s`\"']+/)+)[^\s`\"',;:!?)]*"
)
_MARKDOWN_RE = re.compile(
    r"```|~~~|^\s{0,3}#{1,6}\s|^\s*(?:[-*+]\s+|\d+[.)]\s+)|\[[^\]\n]+\]\([^\)\n]+\)",
    re.MULTILINE,
)
_RAW_URL_RE = re.compile(r"\b(?:https?|file)://\S+", re.IGNORECASE)
_SPEECH_HOSTILE_RE = re.compile(r"(?<!\w)(?:e\.g\.|i\.e\.|vs\.)(?!\w)", re.IGNORECASE)
_SPOKEN_FILE_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_-]*\.(?:py|tsx?|jsx?|json|md|toml|ya?ml|rs|go|java|swift|kt)\b",
    re.IGNORECASE,
)
_SPOKEN_PUNCTUATION_RE = re.compile(
    r"\b(?:slash|dot|equals|open|close|left|right)\s+(?:square\s+)?(?:bracket|brace|parenthes(?:is|es))\b|"
    r"\b(?:slash|dot|equals)\b",
    re.IGNORECASE,
)
_CODE_ASSIGNMENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.-]*\s*=\s*[^\s,;]+")
_PROVIDER_RE = re.compile(r"\b(?:mercury|inception|deepgram|cartesia|flux|aura)\b", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^\w%$]+", re.UNICODE)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

GateKind = Literal["safety", "semantic"]
ResponseField = Literal["displayText", "spokenText"]


@dataclass(frozen=True)
class ResponseEnvelope:
    display_text: str
    spoken_text: str

    @classmethod
    def from_value(cls, value: Any) -> ResponseEnvelope | None:
        if list(_RESPONSE_VALIDATOR.iter_errors(value)):
            return None
        assert isinstance(value, dict)
        return cls(display_text=value["displayText"].strip(), spoken_text=value["spokenText"].strip())

    def as_dict(self) -> dict[str, str]:
        return {"displayText": self.display_text, "spokenText": self.spoken_text}


@dataclass(frozen=True)
class SemanticAssertion:
    assertion_id: str
    display_any: tuple[str, ...]
    spoken_any: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    critical: bool = False
    kind: str = "fact"
    require_spoken: bool = True


@dataclass(frozen=True)
class ReferenceExpectation:
    reference_id: str
    display_any: tuple[str, ...]
    spoken_token_groups: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ResponseCase:
    case_id: str
    category: str
    prompt: str
    files: dict[str, str] = field(default_factory=dict)
    required_facts: tuple[str, ...] = ()
    expected_references: tuple[str, ...] = ()
    forbidden_references: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    secret_sentinels: tuple[str, ...] = ()
    requires_tool: bool = False
    follow_up: str | None = None
    setup_turns: tuple[str, ...] = ()
    assertions: tuple[SemanticAssertion, ...] = ()
    references: tuple[ReferenceExpectation, ...] = ()
    allow_character_reading: bool = False


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str
    repair_instruction: str
    gate: GateKind = "semantic"
    field: ResponseField | None = None
    repairable: bool = True


@dataclass(frozen=True)
class GraderDecision:
    grader: str
    gate: GateKind
    passed: bool
    detail: str
    field: ResponseField | None = None
    assertion_id: str | None = None


@dataclass(frozen=True)
class EvaluationResult:
    violations: tuple[Violation, ...]
    decisions: tuple[GraderDecision, ...]

    @property
    def safety_violations(self) -> tuple[Violation, ...]:
        return tuple(item for item in self.violations if item.gate == "safety")

    @property
    def semantic_violations(self) -> tuple[Violation, ...]:
        return tuple(item for item in self.violations if item.gate == "semantic")

    @property
    def valid_schema(self) -> bool:
        return not any(item.code == "schema_invalid" for item in self.violations)

    def severity(self) -> tuple[int, int, int]:
        contradictions = sum(item.code in {"contradiction", "forbidden_pattern"} for item in self.violations)
        return (len(self.safety_violations), contradictions, len(self.violations))


@dataclass(frozen=True)
class ToolActivity:
    message_id: str
    part_id: str
    tool: str
    status: str


@dataclass
class StructuredTurnState:
    response: ResponseEnvelope | None = None
    raw_structured: Any | None = None
    assistant_error: Any | None = None
    idle_seen: bool = False
    activity_count: int = 0
    output_seen: bool = False
    tool_activity: list[ToolActivity] = field(default_factory=list)
    message_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    message_costs: dict[str, float] = field(default_factory=dict)


class StructuredTurnTracker:
    """Track a complete OpenCode agent loop and its authoritative structured tool."""

    def __init__(self, session_id: str, before_messages: list[dict[str, Any]]) -> None:
        self.session_id = session_id
        self.existing_message_ids = {
            self._message_id(message) for message in before_messages if isinstance(message, dict)
        }
        self.state = StructuredTurnState()
        self._tool_states: set[tuple[str, str]] = set()

    def update_event(self, event: dict[str, Any]) -> StructuredTurnState:
        if event_session_id(event) != self.session_id:
            return self.state
        event_type = str(event.get("type") or "")
        properties = event_properties(event)
        if event_type == "message.updated":
            info = properties.get("info")
            if isinstance(info, dict):
                self._observe_info(info)
        elif event_type == "message.part.updated":
            part = properties.get("part")
            if isinstance(part, dict):
                self._observe_part(part)
        elif event_type in {"session.idle", "session.status"}:
            status = properties.get("status")
            if event_type == "session.idle" or (isinstance(status, dict) and status.get("type") == "idle"):
                self.state.idle_seen = True
                self.state.activity_count += 1
        elif event_type == "session.error":
            self.state.assistant_error = properties.get("error") or "session_error"
            self.state.output_seen = True
            self.state.activity_count += 1
        return self.state

    def update_messages(self, messages: list[dict[str, Any]]) -> StructuredTurnState:
        for message in messages:
            if not isinstance(message, dict):
                continue
            info = message.get("info")
            if isinstance(info, dict):
                if info.get("role") != "assistant":
                    continue
                self._observe_info(info)
                if str(info.get("id") or "") in self.existing_message_ids:
                    continue
                for part in message.get("parts") or []:
                    if isinstance(part, dict):
                        self._observe_part(part)
                continue
            self._observe_projected_message(message)
        return self.state

    def usage(self) -> tuple[dict[str, int], float]:
        totals = {"input": 0, "output": 0, "reasoning": 0, "cacheRead": 0, "cacheWrite": 0}
        for tokens in self.state.message_tokens.values():
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            totals["input"] += int(tokens.get("input") or 0)
            totals["output"] += int(tokens.get("output") or 0)
            totals["reasoning"] += int(tokens.get("reasoning") or 0)
            totals["cacheRead"] += int(cache.get("read") or 0)
            totals["cacheWrite"] += int(cache.get("write") or 0)
        return totals, sum(self.state.message_costs.values())

    @staticmethod
    def _message_id(message: dict[str, Any]) -> str:
        info = message.get("info")
        if isinstance(info, dict):
            return str(info.get("id") or "")
        return str(message.get("id") or "")

    def _observe_projected_message(self, message: dict[str, Any]) -> None:
        if message.get("type") != "assistant":
            return
        message_id = str(message.get("id") or "")
        info: dict[str, Any] = {
            "id": message_id,
            "role": "assistant",
            "time": message.get("time") or {},
            "error": message.get("error"),
        }
        for content in message.get("content") or []:
            if not isinstance(content, dict) or content.get("type") != "tool":
                continue
            state = content.get("state") or {}
            if content.get("name") == "StructuredOutput" and isinstance(state, dict):
                value = state.get("input")
                if isinstance(value, dict):
                    info["structured"] = value
            self._observe_part(
                {
                    "id": content.get("id"),
                    "messageID": message_id,
                    "type": "tool",
                    "tool": content.get("name"),
                    "state": state,
                }
            )
        self._observe_info(info)

    def _observe_info(self, info: dict[str, Any]) -> None:
        if info.get("role") != "assistant":
            return
        message_id = str(info.get("id") or "")
        if not message_id or message_id in self.existing_message_ids:
            return
        self.state.activity_count += 1
        self.state.output_seen = True
        tokens = info.get("tokens")
        if isinstance(tokens, dict):
            self.state.message_tokens[message_id] = tokens
        try:
            self.state.message_costs[message_id] = float(info.get("cost") or 0)
        except (TypeError, ValueError):
            pass
        if info.get("error") is not None:
            self.state.assistant_error = info.get("error")
        if "structured" not in info:
            return
        value = info.get("structured")
        self.state.raw_structured = value
        self.state.response = ResponseEnvelope.from_value(value)

    def _observe_part(self, part: dict[str, Any]) -> None:
        message_id = str(part.get("messageID") or "")
        if not message_id or message_id in self.existing_message_ids:
            return
        self.state.activity_count += 1
        self.state.output_seen = True
        if part.get("type") != "tool":
            return
        state = part.get("state") or {}
        status = str(state.get("status") or "unknown") if isinstance(state, dict) else "unknown"
        part_id = str(part.get("id") or "")
        key = (part_id, status)
        if key in self._tool_states:
            return
        self._tool_states.add(key)
        self.state.tool_activity.append(
            ToolActivity(
                message_id=message_id,
                part_id=part_id,
                tool=str(part.get("tool") or "unknown"),
                status=status,
            )
        )


def normalize_semantic_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = value.replace("’", "'").replace("–", "-").replace("—", "-")
    value = _CAMEL_BOUNDARY_RE.sub(" ", value)
    value = re.sub(r"(?<=\w)-(?=\w)", " ", value)
    value = _NON_WORD_RE.sub(" ", value)
    return " ".join(value.split())


def evaluate_response(value: Any, case: ResponseCase, workspace_root: str | None = None) -> EvaluationResult:
    violations: list[Violation] = []
    decisions: list[GraderDecision] = []
    schema_errors = sorted(_RESPONSE_VALIDATOR.iter_errors(value), key=lambda item: list(item.path))
    decisions.append(
        GraderDecision(
            grader="schema",
            gate="safety",
            passed=not schema_errors,
            detail="valid response envelope" if not schema_errors else "invalid response envelope",
        )
    )
    if schema_errors:
        for error in schema_errors:
            path = ".".join(str(item) for item in error.path) or "response"
            violations.append(
                Violation(
                    "schema_invalid",
                    f"{path}: {error.message}",
                    "Return exactly displayText and spokenText as non-empty strings under 1,200 characters.",
                    gate="safety",
                )
            )
        return EvaluationResult(tuple(dedupe_violations(violations)), tuple(decisions))

    envelope = ResponseEnvelope.from_value(value)
    assert envelope is not None
    for field_name, text in (("displayText", envelope.display_text), ("spokenText", envelope.spoken_text)):
        field = field_name  # narrow for type checkers at the construction sites below.
        checks: list[tuple[str, bool, str, str]] = [
            ("single_paragraph", "\n" not in text and "\r" not in text, "Use one plain-text paragraph.", "multiline"),
            ("markdown", not _MARKDOWN_RE.search(text), "Remove Markdown and write conversational prose.", "markdown"),
            (
                "absolute_path",
                not (_ABSOLUTE_PATH_RE.search(text) or (workspace_root and workspace_root in text)),
                "Use a basename or the shortest distinguishing workspace-relative path.",
                "absolute_path",
            ),
            ("raw_url", not _RAW_URL_RE.search(text), "Describe the destination without a raw URL.", "raw_url"),
            (
                "provider_disclosure",
                not _PROVIDER_RE.search(text),
                "Remove model, provider, speech-service, and runtime names.",
                "provider_disclosure",
            ),
            (
                "inline_json",
                not contains_inline_json(text),
                "Explain structured data in ordinary prose instead of reproducing JSON.",
                "raw_json",
            ),
            (
                "assignment_syntax",
                not _CODE_ASSIGNMENT_RE.search(text),
                "Paraphrase code or log assignments in ordinary prose.",
                "raw_assignment",
            ),
        ]
        for grader, passed, repair, code in checks:
            decisions.append(
                GraderDecision(grader=grader, gate="safety", passed=bool(passed), detail=grader, field=field)  # type: ignore[arg-type]
            )
            if not passed:
                violations.append(
                    Violation(
                        code,
                        f"{field_name} failed {grader}",
                        repair,
                        gate="safety",
                        field=field,  # type: ignore[arg-type]
                    )
                )
        for sentinel in case.secret_sentinels:
            passed = not sentinel or sentinel not in text
            decisions.append(
                GraderDecision("secret", "safety", passed, "secret sentinel absent" if passed else "secret exposed", field=field)  # type: ignore[arg-type]
            )
            if not passed:
                violations.append(
                    Violation(
                        "secret",
                        f"{field_name} exposes a secret sentinel",
                        "Remove credentials and secret values.",
                        gate="safety",
                        field=field,  # type: ignore[arg-type]
                        repairable=False,
                    )
                )

    spoken_checks = [
        (
            "speech_hostile_abbreviation",
            not _SPEECH_HOSTILE_RE.search(envelope.spoken_text),
            "Use complete spoken words such as versus, for example, or that is.",
        ),
        (
            "spoken_path_spelling",
            case.allow_character_reading
            or not (
                _SPOKEN_PUNCTUATION_RE.search(envelope.spoken_text)
                or _SPOKEN_FILE_RE.search(envelope.spoken_text)
            ),
            "Replace the filename or notation with a natural role derived from its basename and distinguishing folder; "
            "for example, release/status.md becomes 'the release status file.' Never substitute an unrelated generic "
            "role, and never say slash, dot, equals, open bracket, or close bracket.",
        ),
    ]
    for code, passed, repair in spoken_checks:
        decisions.append(GraderDecision(code, "safety", bool(passed), code, field="spokenText"))
        if not passed:
            violations.append(Violation(code, code, repair, gate="safety", field="spokenText"))

    assertions = [*_legacy_assertions(case), *case.assertions]
    for assertion in assertions:
        display_pass = _matches_any(envelope.display_text, assertion.display_any)
        spoken_options = assertion.spoken_any or assertion.display_any
        spoken_pass = not assertion.require_spoken or _matches_any(envelope.spoken_text, spoken_options)
        fields = [("displayText", display_pass)]
        if assertion.require_spoken:
            fields.append(("spokenText", spoken_pass))
        for field, passed in fields:
            decisions.append(
                GraderDecision(
                    "semantic_assertion",
                    "semantic",
                    passed,
                    f"{assertion.assertion_id} {'present' if passed else 'missing'}",
                    field=field,
                    assertion_id=assertion.assertion_id,
                )
            )
            if not passed:
                allowed = assertion.display_any if field == "displayText" else spoken_options
                examples = " or ".join(allowed[:3])
                violations.append(
                    Violation(
                        "missing_fact",
                        f"{field} is missing semantic assertion {assertion.assertion_id}",
                        f"Preserve the intended claim in {field} using one of these equivalent forms: {examples}.",
                        gate="semantic",
                        field=field,
                    )
                )
        for pattern in assertion.forbidden_patterns:
            contradiction = bool(re.search(pattern, f"{envelope.display_text}\n{envelope.spoken_text}", re.I | re.M))
            decisions.append(
                GraderDecision(
                    "contradiction",
                    "semantic",
                    not contradiction,
                    f"{assertion.assertion_id} contradiction {'found' if contradiction else 'absent'}",
                    assertion_id=assertion.assertion_id,
                )
            )
            if contradiction:
                violations.append(
                    Violation(
                        "contradiction",
                        f"response contradicts {assertion.assertion_id}",
                        f"Remove the contradiction and preserve the current value for {assertion.assertion_id}.",
                        gate="semantic",
                    )
                )

    references = [*_legacy_references(case), *case.references]
    for reference in references:
        display_pass = _matches_any(envelope.display_text, reference.display_any)
        spoken_tokens = set(normalize_semantic_text(envelope.spoken_text).split())
        spoken_pass = any(
            set(normalize_semantic_text(" ".join(group)).split()).issubset(spoken_tokens)
            for group in reference.spoken_token_groups
        )
        decisions.extend(
            [
                GraderDecision("reference", "semantic", display_pass, reference.reference_id, "displayText", reference.reference_id),
                GraderDecision("reference", "semantic", spoken_pass, reference.reference_id, "spokenText", reference.reference_id),
            ]
        )
        if not display_pass:
            violations.append(
                Violation(
                    "missing_reference",
                    f"displayText does not identify {reference.reference_id}",
                    f"Keep one of these exact display references: {', '.join(reference.display_any)}.",
                    gate="semantic",
                    field="displayText",
                )
            )
        if not spoken_pass:
            natural = " or ".join(" ".join(group) for group in reference.spoken_token_groups)
            violations.append(
                Violation(
                    "spoken_reference",
                    f"spokenText does not naturally identify {reference.reference_id}",
                    f"Identify the same item naturally in spokenText using {natural}; retain the displayed path unchanged.",
                    gate="semantic",
                    field="spokenText",
                )
            )

    display_normalized = normalize_semantic_text(envelope.display_text)
    spoken_normalized = normalize_semantic_text(envelope.spoken_text)
    for reference in case.forbidden_references:
        forbidden = normalize_semantic_text(reference)
        passed = forbidden not in display_normalized and forbidden not in spoken_normalized
        decisions.append(GraderDecision("forbidden_reference", "safety", passed, reference))
        if not passed:
            violations.append(
                Violation(
                    "forbidden_reference",
                    f"response contains forbidden reference: {reference}",
                    "Remove the private or over-specific reference.",
                    gate="safety",
                )
            )
    combined = f"{envelope.display_text}\n{envelope.spoken_text}"
    for pattern in case.forbidden_patterns:
        passed = not re.search(pattern, combined, re.IGNORECASE | re.MULTILINE)
        decisions.append(GraderDecision("forbidden_pattern", "semantic", passed, pattern))
        if not passed:
            violations.append(
                Violation(
                    "forbidden_pattern",
                    f"response matched forbidden pattern: {pattern}",
                    "Rewrite without that prohibited claim or form.",
                    gate="semantic",
                )
            )
    return EvaluationResult(tuple(dedupe_violations(violations)), tuple(decisions))


def grade_response(value: Any, case: ResponseCase, workspace_root: str | None = None) -> list[Violation]:
    return list(evaluate_response(value, case, workspace_root).violations)


def should_admit_repair(evaluation: EvaluationResult) -> tuple[bool, str | None]:
    repairable = [item for item in evaluation.violations if item.repairable]
    if not repairable:
        return False, None
    return True, ",".join(sorted({item.code for item in repairable}))


def should_select_repair(first: EvaluationResult, repaired: EvaluationResult) -> tuple[bool, str]:
    if not repaired.valid_schema:
        return False, "repair_schema_invalid"
    first_codes = {(item.code, item.field) for item in first.violations}
    new_violations = [item for item in repaired.violations if (item.code, item.field) not in first_codes]
    if new_violations:
        return False, "repair_introduced_new_violation"
    if repaired.severity() >= first.severity():
        return False, "repair_did_not_improve"
    return True, "repair_improved_without_regression"


def repair_prompt(original_prompt: str, value: Any, violations: list[Violation]) -> str:
    unique = dedupe_violations(violations)
    by_field: dict[str, list[str]] = {"displayText": [], "spokenText": [], "both": []}
    for item in unique:
        by_field[item.field or "both"].append(item.repair_instruction)
    sections = []
    for field_name in ("displayText", "spokenText", "both"):
        if by_field[field_name]:
            sections.append(f"{field_name}:\n" + "\n".join(f"- {text}" for text in by_field[field_name]))
    return (
        "Rewrite only the required final object. Do not repeat completed tool work and do not change the result. "
        "Apply each correction only to its named field and preserve a field that already passes. Never copy an exact "
        "display path or filename into spokenText. Preserve every fact, outcome, "
        "entity, certainty, negation, and qualification. A correction for spokenText must not remove an exact path "
        "required in displayText.\n\n"
        f"Original user request:\n{original_prompt}\n\n"
        f"First response:\n{json.dumps(value, ensure_ascii=False, default=str)}\n\n"
        f"Field-specific corrections:\n{'\n\n'.join(sections)}"
    )


def contains_inline_json(text: str) -> bool:
    for start, opening in enumerate(text):
        if opening not in "[{":
            continue
        stack = [opening]
        in_string = False
        escaped = False
        for index in range(start + 1, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "[{":
                stack.append(char)
            elif char in "]}":
                if not stack or (stack[-1], char) not in {("[", "]"), ("{", "}")}:
                    break
                stack.pop()
                if not stack:
                    candidate = text[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, (dict, list)):
                        return True
                    break
    return False


def natural_reference(reference: str) -> ReferenceExpectation:
    path = reference.replace("\\", "/")
    parts = [part for part in path.split("/") if part]
    basename = parts[-1] if parts else reference
    stem = basename.rsplit(".", 1)[0]
    stem_words = tuple(normalize_semantic_text(stem).split()) or (normalize_semantic_text(basename),)
    if len(parts) > 1:
        folder = tuple(normalize_semantic_text(parts[-2]).split())
        groups = (folder + stem_words,)
    else:
        groups = (stem_words,)
    return ReferenceExpectation(reference, (reference,), groups)


def _legacy_assertions(case: ResponseCase) -> list[SemanticAssertion]:
    aliases = {
        "timeout": ("timeout", "timed out"),
        "real race": ("real race", "genuine race", "true race", "real race condition", "genuine race condition"),
        "no code changed": ("no code changed", "no code was changed", "no code changes", "no code modifications"),
        "three seconds": ("three seconds", "three second", "3 seconds", "3 second"),
    }
    return [
        SemanticAssertion(
            f"legacy:{fact}",
            aliases.get(normalize_semantic_text(fact), (fact,)),
            (),
            require_spoken=False,
        )
        for fact in case.required_facts
    ]


def _legacy_references(case: ResponseCase) -> list[ReferenceExpectation]:
    return [natural_reference(reference) for reference in case.expected_references]


def _matches_any(text: str, alternatives: tuple[str, ...]) -> bool:
    normalized = normalize_semantic_text(text)
    return any(normalize_semantic_text(item) in normalized for item in alternatives if item)


def dedupe_violations(violations: list[Violation]) -> list[Violation]:
    result: list[Violation] = []
    seen: set[tuple[str, str, str | None]] = set()
    for violation in violations:
        key = (violation.code, violation.detail, violation.field)
        if key in seen:
            continue
        seen.add(key)
        result.append(violation)
    return result
