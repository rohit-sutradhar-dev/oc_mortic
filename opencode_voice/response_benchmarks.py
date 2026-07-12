from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from opencode_voice.response_contract import (
    ReferenceExpectation,
    ResponseCase,
    SemanticAssertion,
    normalize_semantic_text,
)


@dataclass(frozen=True)
class CalibrationExample:
    example_id: str
    case: ResponseCase
    response: dict[str, str]
    should_pass: bool
    expected_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgeCalibrationFixture:
    fixture_id: str
    user_request: str
    candidate: dict[str, str]
    expected_pass: bool
    failing_dimensions: tuple[str, ...] = ()
    valid_clarification: bool = False


@dataclass(frozen=True)
class FactLedgerEntry:
    fact_id: str
    display_any: tuple[str, ...]
    spoken_any: tuple[str, ...]
    introduced_turn: int
    critical: bool = False
    kind: str = "general"
    supersedes: str | None = None
    forbidden_patterns: tuple[str, ...] = ()

    def as_assertion(self) -> SemanticAssertion:
        return SemanticAssertion(
            assertion_id=self.fact_id,
            display_any=self.display_any,
            spoken_any=self.spoken_any,
            forbidden_patterns=self.forbidden_patterns,
            critical=self.critical,
            kind=self.kind,
        )


@dataclass(frozen=True)
class ConversationExchange:
    turn: int
    user_text: str
    checkpoint_id: str | None = None


@dataclass(frozen=True)
class ConversationScript:
    script_id: str
    length: int
    exchanges: tuple[ConversationExchange, ...]
    ledger: tuple[FactLedgerEntry, ...]

    def active_facts(self, turn: int) -> tuple[FactLedgerEntry, ...]:
        introduced = [fact for fact in self.ledger if fact.introduced_turn <= turn]
        superseded = {fact.supersedes for fact in introduced if fact.supersedes}
        return tuple(fact for fact in introduced if fact.fact_id not in superseded)

    def checkpoint_case(self, exchange: ConversationExchange) -> ResponseCase:
        facts = self.active_facts(exchange.turn)
        return ResponseCase(
            case_id=f"{self.script_id}-{exchange.checkpoint_id}",
            category="long-context",
            prompt=exchange.user_text,
            assertions=tuple(fact.as_assertion() for fact in facts),
            forbidden_patterns=(r"\btoday(?:'s| is)?\b", r"\bcurrent date is\b"),
        )


@dataclass(frozen=True)
class RecallScore:
    total: int
    recalled: int
    critical_total: int
    critical_recalled: int
    contradictions: int
    display_recalled: int = 0
    spoken_recalled: int = 0
    recent_total: int = 0
    recent_recalled: int = 0
    kind_totals: dict[str, int] = field(default_factory=dict)
    kind_recalled: dict[str, int] = field(default_factory=dict)
    unsupported_ids: tuple[str, ...] = ()
    missing_ids: tuple[str, ...] = ()
    contradicted_ids: tuple[str, ...] = ()

    @property
    def recall_rate(self) -> float:
        return self.recalled / self.total if self.total else 1.0

    @property
    def critical_recall_rate(self) -> float:
        return self.critical_recalled / self.critical_total if self.critical_total else 1.0

    def as_dict(self) -> dict[str, Any]:
        value = {
            "total": self.total,
            "recalled": self.recalled,
            "critical_total": self.critical_total,
            "critical_recalled": self.critical_recalled,
            "contradictions": self.contradictions,
            "display_recalled": self.display_recalled,
            "spoken_recalled": self.spoken_recalled,
            "recent_total": self.recent_total,
            "recent_recalled": self.recent_recalled,
            "kind_totals": self.kind_totals,
            "kind_recalled": self.kind_recalled,
            "unsupported_ids": self.unsupported_ids,
            "missing_ids": self.missing_ids,
            "contradicted_ids": self.contradicted_ids,
            "recall_rate": self.recall_rate,
            "critical_recall_rate": self.critical_recall_rate,
        }
        return value


def notation_response_cases() -> list[ResponseCase]:
    rows = [
        (
            "qualification",
            "Explain that release is ready but still pending a canary; do not read punctuation aloud.",
            ("release ready", "release is ready", "ready for release"),
            ("release ready", "release is ready", "ready for release"),
        ),
        (
            "status",
            "Report the current status label [TESTING] naturally.",
            ("testing",),
            ("testing",),
        ),
        (
            "array",
            "Explain that the selected options are [A, B, C] without narrating brackets.",
            ("A B C", "A B and C", "options A B C", "options are A B and C"),
            ("A B C", "A B and C", "options A B C", "options are A B and C"),
        ),
        (
            "map",
            "Turn {status: ready} into an ordinary sentence.",
            ("status ready", "status is ready"),
            ("status ready", "status is ready"),
        ),
        (
            "link",
            "Say that the reliability guide contains the evidence; do not print its Markdown link or URL.",
            ("reliability guide",),
            ("reliability guide",),
        ),
        (
            "ticket",
            "Report that [MOR-172] remains in testing without reading brackets.",
            ("MOR 172", "MOR-172"),
            ("ticket MOR 172", "MOR 172"),
        ),
        (
            "file-line",
            "Explain that the failure is at [server.py:1486] naturally.",
            ("server py 1486", "server.py 1486"),
            ("server module line 1486", "server file line 1486", "server module at line 1486"),
        ),
        (
            "function",
            "Explain that reconnect(socket, options) performs the retry.",
            ("reconnect socket options",),
            ("reconnect function", "reconnect takes socket and options"),
        ),
        (
            "generic",
            "Explain that Result<Error> is the returned type without reading angle brackets.",
            ("Result Error", "Result<Error>"),
            ("result error type", "result of error type", "result type containing an error"),
        ),
        (
            "nested",
            "Rewrite ((ready) [pending canary]) as a natural release update.",
            ("ready",),
            ("pending canary", "canary pending"),
        ),
        (
            "error-code",
            "Explain Request failed [ETIMEDOUT] without reading brackets.",
            ("request failed", "ETIMEDOUT"),
            (
                "request timed out",
                "E timed out",
                "request did not receive a response before the allotted time expired",
            ),
        ),
        (
            "metrics",
            "Report (p50: 2.5s, p95: 4.8s) naturally.",
            ("p50 2 5", "50th percentile 2 5", "50th percentile latency is 2 5"),
            ("fiftieth percentile 2 5", "fifty percentile latency is two point five", "p fifty 2 5"),
        ),
    ]
    cases: list[ResponseCase] = []
    for index, (family, prompt, display_any, spoken_any) in enumerate(rows, 1):
        for variant in ("a", "b"):
            qualifier = " Keep it concise." if variant == "b" else ""
            assertions = (
                SemanticAssertion(f"notation:{family}:primary", display_any, spoken_any, critical=True, kind="notation"),
            )
            if family in {"qualification", "nested"}:
                assertions += (
                    SemanticAssertion(
                        f"notation:{family}:qualification",
                        ("pending canary", "pending a canary", "canary pending"),
                        ("pending canary", "pending a canary", "canary pending"),
                        critical=True,
                        kind="qualification",
                    ),
                )
            if family == "metrics":
                assertions += (
                    SemanticAssertion(
                        "notation:metrics:p95",
                        ("p95 4 8", "95th percentile 4 8", "95th percentile latency is 4 8"),
                        (
                            "ninety fifth percentile 4 8",
                            "ninety five percentile latency is four point eight",
                            "p ninety five 4 8",
                        ),
                        critical=True,
                        kind="metric",
                    ),
                )
            references: tuple[ReferenceExpectation, ...] = ()
            if family == "file-line":
                references = (
                    ReferenceExpectation(
                        "server.py:1486",
                        ("server.py:1486", "server.py at line 1486", "server.py line 1486"),
                        (("server", "1486"),),
                    ),
                )
            cases.append(
                ResponseCase(
                    case_id=f"notation-{index:02d}-{variant}",
                    category="notation",
                    prompt=prompt + qualifier,
                    assertions=assertions,
                    references=references,
                )
            )
    return cases


def notation_calibration_examples() -> list[CalibrationExample]:
    examples: list[CalibrationExample] = []
    for case in notation_response_cases():
        display = "; ".join(assertion.display_any[0] for assertion in case.assertions)
        spoken = "; ".join(
            (assertion.spoken_any or assertion.display_any)[0] for assertion in case.assertions
        )
        if case.references:
            display = "The failure is in server.py:1486."
            spoken = "The failure is in the server module at line 1486."
        good = {"displayText": f"The result is {display}.", "spokenText": f"The result is {spoken}."}
        bad = {
            "displayText": f"The result is {display}.",
            "spokenText": f"Open bracket {spoken} close bracket.",
        }
        examples.extend(
            [
                CalibrationExample(f"{case.case_id}-good", case, good, True),
                CalibrationExample(
                    f"{case.case_id}-bad",
                    case,
                    bad,
                    False,
                    ("spoken_path_spelling",),
                ),
            ]
        )
    return examples


def judge_calibration_fixtures() -> list[JudgeCalibrationFixture]:
    fixtures: list[JudgeCalibrationFixture] = []
    clarification_rows = [
        ("Compare it with the previous one.", "Which result should I compare, and which earlier result do you mean?"),
        ("Fix the issue.", "Which issue should I fix?"),
        ("Open that file.", "Which file do you mean?"),
        ("Use the other option.", "Which option should I use?"),
    ]
    for index in range(12):
        request, text = clarification_rows[index % len(clarification_rows)]
        fixtures.append(
            JudgeCalibrationFixture(
                f"judge-clarification-{index + 1:02d}",
                request,
                {"displayText": text, "spokenText": text},
                True,
                valid_clarification=True,
            )
        )
    direct_rows = [
        ("Is the release ready?", "No. The network canary is still pending."),
        ("What changed?", "The reconnect race is fixed and the focused tests pass."),
        ("Was code changed?", "No code changed; this was an analysis-only pass."),
        ("What remains?", "Provider timing still needs live verification."),
    ]
    for index in range(8):
        request, answer = direct_rows[index % len(direct_rows)]
        fixtures.append(
            JudgeCalibrationFixture(
                f"judge-direct-{index + 1:02d}",
                request,
                {"displayText": answer, "spokenText": answer},
                True,
            )
        )
    failure_rows = [
        ("What changed?", "I can help with that.", ("directness", "completeness")),
        (
            "The canary is still pending. Is the release ready?",
            "I don't know.",
            ("directness", "completeness"),
        ),
        ("Give me the result.", "The aforementioned operation effectuated completion successfully.", ("naturalness",)),
        ("What failed?", "The network failed.", ("equivalence",)),
    ]
    for index in range(12):
        request, answer, dimensions = failure_rows[index % len(failure_rows)]
        spoken = answer if "equivalence" not in dimensions else "Everything worked."
        fixtures.append(
            JudgeCalibrationFixture(
                f"judge-fail-{index + 1:02d}",
                request,
                {"displayText": answer, "spokenText": spoken},
                False,
                dimensions,
            )
        )
    return fixtures


def conversation_scripts() -> list[ConversationScript]:
    scripts: list[ConversationScript] = []
    for length in (8, 12, 20, 32):
        for variant in ("a", "b"):
            scripts.append(_conversation_script(length, variant))
    return scripts


def score_recall(
    response: dict[str, str],
    facts: tuple[FactLedgerEntry, ...],
    *,
    all_facts: tuple[FactLedgerEntry, ...] | None = None,
    current_turn: int | None = None,
) -> RecallScore:
    display = normalize_semantic_text(response.get("displayText", ""))
    spoken = normalize_semantic_text(response.get("spokenText", ""))
    missing: list[str] = []
    contradicted: list[str] = []
    recalled = 0
    critical_recalled = 0
    display_recalled = 0
    spoken_recalled = 0
    recent_recalled = 0
    kind_totals: dict[str, int] = {}
    kind_recalled: dict[str, int] = {}
    recent_floor = max(1, (current_turn or 0) - 5)
    for fact in facts:
        kind_totals[fact.kind] = kind_totals.get(fact.kind, 0) + 1
        display_hit = any(normalize_semantic_text(item) in display for item in fact.display_any)
        spoken_hit = any(normalize_semantic_text(item) in spoken for item in fact.spoken_any)
        display_recalled += int(display_hit)
        spoken_recalled += int(spoken_hit)
        if display_hit and spoken_hit:
            recalled += 1
            kind_recalled[fact.kind] = kind_recalled.get(fact.kind, 0) + 1
            if current_turn is not None and fact.introduced_turn >= recent_floor:
                recent_recalled += 1
            if fact.critical:
                critical_recalled += 1
        else:
            missing.append(fact.fact_id)
        combined = f"{response.get('displayText', '')}\n{response.get('spokenText', '')}"
        if any(re.search(pattern, combined, re.I | re.M) for pattern in fact.forbidden_patterns):
            contradicted.append(fact.fact_id)
    unsupported: list[str] = []
    if all_facts is not None and current_turn is not None:
        inactive = [fact for fact in all_facts if fact.introduced_turn > current_turn]
        for fact in inactive:
            if any(normalize_semantic_text(item) in display for item in fact.display_any) or any(
                normalize_semantic_text(item) in spoken for item in fact.spoken_any
            ):
                unsupported.append(fact.fact_id)
    return RecallScore(
        total=len(facts),
        recalled=recalled,
        critical_total=sum(fact.critical for fact in facts),
        critical_recalled=critical_recalled,
        contradictions=len(contradicted),
        display_recalled=display_recalled,
        spoken_recalled=spoken_recalled,
        recent_total=sum(
            current_turn is not None and fact.introduced_turn >= recent_floor for fact in facts
        ),
        recent_recalled=recent_recalled,
        kind_totals=kind_totals,
        kind_recalled=kind_recalled,
        unsupported_ids=tuple(unsupported),
        missing_ids=tuple(missing),
        contradicted_ids=tuple(contradicted),
    )


def _conversation_script(length: int, variant: str) -> ConversationScript:
    project = "Atlas" if variant == "a" else "Apollo"
    other = "Apollo" if variant == "a" else "Atlas"
    owner = "Rhea" if variant == "a" else "Noah"
    old_date = "July 18, 2026" if variant == "a" else "August 4, 2026"
    new_date = "July 20, 2026" if variant == "a" else "August 6, 2026"
    threshold = "70,000 tokens" if variant == "a" else "64,000 tokens"
    middle = max(4, length // 2)
    correction_turn = max(5, length - 4)
    recent_turn = max(6, length - 2)
    ledger = (
        FactLedgerEntry("decision", ("local helper",), ("local helper",), 1, True, "decision"),
        FactLedgerEntry("decision_reason", ("privacy",), ("privacy",), 2, True, "reason"),
        FactLedgerEntry(
            "no_cloud",
            (
                "do not use cloud",
                "no cloud",
                "not use cloud",
                "avoid cloud orchestration",
                "cloud orchestration is not used",
            ),
            (
                "do not use cloud",
                "no cloud",
                "not use cloud",
                "avoid cloud orchestration",
                "cloud orchestration is not used",
            ),
            3,
            True,
            "negation",
            forbidden_patterns=(r"(?<!do not )(?<!not )\buse (?:the )?cloud(?: orchestration)?\b",),
        ),
        FactLedgerEntry("component", ("transport.py", "transport module"), ("transport module",), 4, False, "entity"),
        FactLedgerEntry(
            "project",
            (project,),
            (project,),
            middle,
            False,
            "entity",
            forbidden_patterns=(rf"\b(?:current project|project is|concerns|pertains to)\s+{other}\b",),
        ),
        FactLedgerEntry(
            "threshold",
            (threshold, threshold.replace(",", "")),
            (("seventy thousand tokens", threshold) if variant == "a" else ("sixty-four thousand tokens", threshold)),
            middle + 1,
            False,
            "number",
        ),
        FactLedgerEntry("old_date", (old_date,), (_spoken_date(old_date), old_date), middle + 1, False, "superseded"),
        FactLedgerEntry(
            "new_date",
            (new_date,),
            (_spoken_date(new_date), new_date),
            correction_turn,
            True,
            "correction",
            supersedes="old_date",
            forbidden_patterns=(rf"\b(?:current date|date is|now set to)\s+{re.escape(old_date)}\b",),
        ),
        FactLedgerEntry("uncertainty", ("provider timing",), ("provider timing",), correction_turn + 1, False, "uncertainty"),
        FactLedgerEntry("unresolved", ("native canary",), ("native canary",), recent_turn, True, "unresolved"),
        FactLedgerEntry("owner", (owner,), (owner,), recent_turn + 1, False, "attribution"),
    )
    checkpoint_turns = {max(3, length // 2), max(4, length - 2), length}
    seed_by_turn: dict[int, list[str]] = {}

    def seed(turn: int, text: str) -> None:
        seed_by_turn.setdefault(turn, []).append(text)

    seed(1, "We decided to keep the local helper.")
    seed(2, "The reason is privacy.")
    seed(3, "Do not use cloud orchestration.")
    seed(4, "The relevant component is transport.py.")
    seed(middle, f"This conversation concerns {project}, not {other}.")
    seed(middle + 1, f"The threshold is {threshold}, and the provisional date is {old_date}.")
    seed(correction_turn, f"Correction: replace {old_date} with {new_date}.")
    seed(correction_turn + 1, "Provider timing remains uncertain.")
    seed(recent_turn, "The native canary remains unresolved.")
    seed(recent_turn + 1, f"{owner} owns the next action.")
    exchanges: list[ConversationExchange] = []
    for turn in range(1, length + 1):
        seeded = seed_by_turn.get(turn) or [f"Background note {turn}: no decision changes in this exchange."]
        seed_text = " ".join(seeded)
        if turn in checkpoint_turns:
            prompt = seed_text + " At this checkpoint, recall every concrete fact and qualification introduced so far. " + (
                "Include the decision and reason, negation, component, project, threshold, corrected target date, uncertainty, "
                "unresolved action, and owner only when they have already been provided. Clearly distinguish any "
                "superseded value from its current correction. Do not add today's calendar date. Keep display and spoken content equivalent."
            )
            exchanges.append(ConversationExchange(turn, prompt, f"checkpoint-{turn:02d}"))
        else:
            exchanges.append(
                ConversationExchange(
                    turn,
                    seed_text,
                )
            )
    return ConversationScript(f"conversation-{length:02d}-{variant}", length, tuple(exchanges), ledger)


def _spoken_date(value: str) -> str:
    month, day, year = value.replace(",", "").split()
    day_words = {"18": "eighteenth", "20": "twentieth", "4": "fourth", "6": "sixth"}[day]
    year_words = "twenty twenty six" if year == "2026" else year
    return f"{month} {day_words} {year_words}"
