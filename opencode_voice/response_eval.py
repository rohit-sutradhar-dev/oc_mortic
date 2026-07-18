from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

import httpx

from opencode_voice.config import ModelRef, load_local_dotenv, parse_model_ref, render_opencode_config
from opencode_voice.managed_opencode import terminate_managed_process
from opencode_voice.opencode_client import OpenCodeClient
from opencode_voice.response_contract import (
    EvaluationResult,
    GraderDecision,
    JUDGE_SCHEMA,
    RESPONSE_SCHEMA,
    ResponseCase,
    StructuredTurnTracker,
    ToolActivity,
    Violation,
    evaluate_response,
    repair_prompt,
    should_admit_repair,
    should_select_repair,
)
from opencode_voice.response_eval_corpus import load_response_cases, smoke_response_cases, web_response_cases
from opencode_voice.response_benchmarks import (
    ConversationScript,
    conversation_scripts,
    judge_calibration_fixtures,
    notation_response_cases,
    score_recall,
)
from opencode_voice.response_compaction import CompactionProfile, compaction_profiles
from opencode_voice.response_comparison import regrade_baseline
from opencode_voice.state import event_session_id
from opencode_voice.telemetry import resolve_build_sha


GENERATOR_AGENT = "response-eval"
JUDGE_AGENT = "response-judge"
SETUP_AGENT = "response-context-setup"
COMPACTION_AGENT = "response-compaction-filler"
ASSET_ROOT = Path(__file__).resolve().parent
GENERATOR_PROMPT_PATH = ASSET_ROOT / "response_eval_agent.md"
JUDGE_PROMPT_PATH = ASSET_ROOT / "response_eval_judge.md"


@dataclass
class TurnObservation:
    structured: Any | None
    assistant_error: Any | None
    tool_activity: list[ToolActivity]
    first_activity_ms: int | None
    final_ms: int
    stream_source: str
    event_healthy: bool
    idle_seen: bool
    tokens: dict[str, int] = field(default_factory=dict)
    estimated_paid_usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrialResult:
    case_id: str
    category: str
    trial: int
    response: Any | None
    first_response: Any | None
    first_pass_violations: list[Violation]
    final_violations: list[Violation]
    repaired: bool
    repair_response: Any | None
    judge: dict[str, Any] | None
    latency_ms: int
    first_activity_ms: int | None
    stream_source: str
    tool_activity: list[ToolActivity]
    source_untouched: bool
    first_decisions: list[GraderDecision] = field(default_factory=list)
    repair_decisions: list[GraderDecision] = field(default_factory=list)
    final_decisions: list[GraderDecision] = field(default_factory=list)
    repair_admitted_reason: str | None = None
    repair_selected_reason: str | None = None
    repair_error: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    estimated_paid_usage: dict[str, Any] = field(default_factory=dict)
    compaction_events: list[dict[str, Any]] = field(default_factory=list)
    fork_lineage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.final_violations and self.error is None and self.source_untouched

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["passed"] = self.passed
        return value


@dataclass
class LongContextCheckpointResult:
    script_id: str
    conversation_length: int
    checkpoint_id: str
    turn: int
    response: Any | None
    recall: dict[str, Any]
    violations: list[Violation]
    latency_ms: int
    tool_activity: list[ToolActivity]
    source_untouched: bool
    first_response: Any | None = None
    repair_response: Any | None = None
    repair_admitted_reason: str | None = None
    repair_selected_reason: str | None = None
    first_decisions: list[GraderDecision] = field(default_factory=list)
    final_decisions: list[GraderDecision] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    estimated_paid_usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.violations and not self.error and self.source_untouched

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["passed"] = self.passed
        return value


class ManagedEvalServer(AbstractContextManager["ManagedEvalServer"]):
    def __init__(
        self,
        workspace: Path,
        model: ModelRef,
        *,
        network_tools: bool = False,
        compaction_profile: CompactionProfile | None = None,
    ) -> None:
        self.workspace = workspace
        self.model = model
        self.network_tools = network_tools
        self.compaction_profile = compaction_profile
        self.process: subprocess.Popen[str] | None = None
        self.url: str | None = None

    def __enter__(self) -> ManagedEvalServer:
        port = _free_port()
        config = render_opencode_config(
            self.model,
            voice_agent_prompt=GENERATOR_PROMPT_PATH.read_text(encoding="utf-8"),
            voice_agent_name=GENERATOR_AGENT,
        )
        config["agent"][GENERATOR_AGENT]["permission"] = {
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "edit": "allow",
            "webfetch": "allow" if self.network_tools else "deny",
            "websearch": "allow" if self.network_tools else "deny",
            "bash": "deny",
            "task": "deny",
            "external_directory": "deny",
        }
        if not self.network_tools:
            config["agent"][GENERATOR_AGENT]["tools"] = {"webfetch": False, "websearch": False}
        config["agent"][JUDGE_AGENT] = {
            "description": "Eval-only rubric judge for Mortic response candidates.",
            "mode": "primary",
            "model": self.model.opencode_name,
            "prompt": JUDGE_PROMPT_PATH.read_text(encoding="utf-8"),
            "tools": {
                "read": False,
                "glob": False,
                "grep": False,
                "list": False,
                "edit": False,
                "bash": False,
                "task": False,
                "webfetch": False,
                "websearch": False,
            },
            **({"variant": self.model.variant} if self.model.variant else {}),
        }
        config["agent"][SETUP_AGENT] = {
            "description": "No-network deterministic conversation seeding agent.",
            "mode": "primary",
            "model": self.model.opencode_name,
            "prompt": (
                "This is a no-network conversation benchmark agent. For a user message beginning [SEED], return "
                "the required structured object with displayText and spokenText both exactly equal to Noted. For "
                "a message beginning [CHECKPOINT], answer the checkpoint from the conversation exactly as requested. "
                "At checkpoints, preserve every introduced fact and qualification, keep display and spoken claims "
                "equivalent, and replace filenames in spokenText with natural roles such as 'the transport module'."
            ),
            "tools": {name: False for name in ("read", "glob", "grep", "list", "edit", "bash", "task", "webfetch", "websearch")},
            **({"variant": self.model.variant} if self.model.variant else {}),
        }
        config["agent"][COMPACTION_AGENT] = {
            "description": "No-network compaction filler acknowledgement agent.",
            "mode": "primary",
            "model": self.model.opencode_name,
            "prompt": "Acknowledge every synthetic context block with exactly: Noted.",
            "tools": {
                name: False
                for name in (
                    "read", "glob", "grep", "list", "edit", "bash", "task", "webfetch", "websearch"
                )
            },
            **({"variant": self.model.variant} if self.model.variant else {}),
        }
        if self.compaction_profile is not None:
            overlay = self.compaction_profile.config_overlay()
            config["compaction"] = overlay["compaction"]
            for provider in config.get("provider", {}).values():
                for configured_model in provider.get("models", {}).values():
                    configured_model["limit"] = dict(overlay["model_limit"])
        env = os.environ.copy()
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
        if self.network_tools:
            env["OPENCODE_ENABLE_EXA"] = "1"
        env["BUN_OPTIONS"] = " ".join(
            item
            for item in (env.get("BUN_OPTIONS", "").strip(), "--dns-result-order=ipv4first")
            if item
        )
        self.process = subprocess.Popen(
            [
                "opencode",
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
                "--cors",
                "*",
            ],
            cwd=str(self.workspace),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Eval OpenCode server exited before becoming healthy")
            if _is_healthy(self.url):
                return self
            time.sleep(0.25)
        terminate_managed_process(self.process)
        raise RuntimeError("Eval OpenCode server did not become healthy within 20 seconds")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.process is not None:
            terminate_managed_process(self.process)
        self.process = None


class EventMultiplexer:
    """Share one OpenCode event stream across a long evaluation run."""

    def __init__(self, client: OpenCodeClient, directory: str) -> None:
        self.client = client
        self.directory = directory
        self.ready = asyncio.Event()
        self.error_reason: str | None = None
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="response-eval-event-multiplexer")
        await asyncio.wait_for(self.ready.wait(), timeout=3.0)

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues[session_id] = queue
        if self.error_reason is not None:
            queue.put_nowait({"type": "_stream_error", "reason": self.error_reason})
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if self._queues.get(session_id) is queue:
            self._queues.pop(session_id, None)

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._queues.clear()
        self._listeners.clear()

    async def _run(self) -> None:
        try:
            async for event in self.client.events(on_open=self.ready.set, directory=self.directory):
                for listener in tuple(self._listeners):
                    listener(event)
                queue = self._queues.get(event_session_id(event))
                if queue is not None:
                    await queue.put(event)
            await self._mark_error("EventStreamClosed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - observers hedge with polling.
            await self._mark_error(type(exc).__name__)

    async def _mark_error(self, reason: str) -> None:
        self.error_reason = reason
        self.ready.set()
        error = {"type": "_stream_error", "reason": reason}
        for queue in list(self._queues.values()):
            await queue.put(error)


async def observe_structured_turn(
    client: OpenCodeClient,
    *,
    session_id: str,
    directory: str | None,
    prompt: str,
    model: ModelRef,
    agent: str,
    schema: dict[str, Any],
    max_turn_sec: float = 120.0,
    poll_after_sec: float = 3.0,
    structured_idle_grace_sec: float = 1.0,
    event_multiplexer: EventMultiplexer | None = None,
) -> TurnObservation:
    before_messages = await client.messages(session_id)
    tracker = StructuredTurnTracker(session_id, before_messages)
    if event_multiplexer is not None:
        queue = event_multiplexer.subscribe(session_id)
        ready = event_multiplexer.ready
        reader = None
    else:
        queue = asyncio.Queue()
        ready = asyncio.Event()
        reader = asyncio.create_task(
            _read_events(client, directory, queue, ready), name=f"response-eval-events-{session_id}"
        )
    started = time.perf_counter()
    first_activity_ms: int | None = None
    event_healthy = event_multiplexer is None or event_multiplexer.error_reason is None
    result_source = "event" if event_healthy else "poll"
    last_count = 0
    structured_ms: int | None = None
    structured_grace_deadline: float | None = None
    try:
        try:
            await asyncio.wait_for(ready.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            event_healthy = False
            result_source = "poll"
        await client.prompt_async(
            session_id,
            prompt,
            model,
            agent,
            # OpenCode's StructuredOutput tool strips the annotation before
            # compiling its input schema.  Sending it over the API, however,
            # leaves it in the persisted user-message format and OpenCode
            # 1.17.18's /message decoder rejects that otherwise valid record.
            output_format={
                "type": "json_schema",
                "schema": {key: value for key, value in schema.items() if key != "$schema"},
            },
        )
        next_poll = time.perf_counter() + poll_after_sec
        deadline = started + max_turn_sec
        while time.perf_counter() < deadline:
            if tracker.state.activity_count > last_count:
                last_count = tracker.state.activity_count
                if first_activity_ms is None:
                    first_activity_ms = _elapsed_ms(started)
            if tracker.state.raw_structured is not None:
                if structured_ms is None:
                    structured_ms = _elapsed_ms(started)
                    structured_grace_deadline = time.perf_counter() + structured_idle_grace_sec
                if tracker.state.idle_seen or (
                    structured_grace_deadline is not None and time.perf_counter() >= structured_grace_deadline
                ):
                    return TurnObservation(
                        structured=tracker.state.raw_structured,
                        assistant_error=tracker.state.assistant_error,
                        tool_activity=list(tracker.state.tool_activity),
                        first_activity_ms=first_activity_ms,
                        final_ms=structured_ms,
                        stream_source=result_source,
                        event_healthy=event_healthy,
                        idle_seen=tracker.state.idle_seen,
                        **_tracker_usage_fields(tracker),
                    )
            if tracker.state.idle_seen:
                if not tracker.state.output_seen:
                    # Session creation, fork, and agent switches may emit a
                    # stale idle after this observer subscribes. It cannot
                    # complete the prompt we just submitted.
                    tracker.state.idle_seen = False
                    continue
                tracker.update_messages(await client.messages(session_id))
                if tracker.state.raw_structured is not None:
                    result_source = "hybrid" if result_source == "event" else result_source
                    continue
                return TurnObservation(
                    structured=None,
                    assistant_error=tracker.state.assistant_error or "structured_output_missing",
                    tool_activity=list(tracker.state.tool_activity),
                    first_activity_ms=first_activity_ms,
                    final_ms=_elapsed_ms(started),
                    stream_source=result_source,
                    event_healthy=event_healthy,
                    idle_seen=True,
                    **_tracker_usage_fields(tracker),
                )

            now = time.perf_counter()
            wait_until = min(deadline, next_poll, structured_grace_deadline or deadline)
            try:
                event = await asyncio.wait_for(queue.get(), timeout=max(0.01, wait_until - now))
            except asyncio.TimeoutError:
                event = None
            if event is not None:
                if event.get("type") == "_stream_error":
                    event_healthy = False
                    result_source = "poll"
                else:
                    tracker.update_event(event)
            if time.perf_counter() >= next_poll:
                previous = tracker.state.raw_structured
                tracker.update_messages(await client.messages(session_id))
                if previous is None and tracker.state.raw_structured is not None:
                    result_source = "hybrid" if event_healthy else "poll"
                next_poll = time.perf_counter() + 0.5

        return TurnObservation(
            structured=tracker.state.raw_structured,
            assistant_error=tracker.state.assistant_error or "turn_timeout",
            tool_activity=list(tracker.state.tool_activity),
            first_activity_ms=first_activity_ms,
            final_ms=_elapsed_ms(started),
            stream_source=result_source,
            event_healthy=event_healthy,
            idle_seen=tracker.state.idle_seen,
            **_tracker_usage_fields(tracker),
        )
    finally:
        if event_multiplexer is not None:
            event_multiplexer.unsubscribe(session_id, queue)
        elif reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


async def _read_events(
    client: OpenCodeClient,
    directory: str | None,
    queue: asyncio.Queue[dict[str, Any]],
    ready: asyncio.Event,
) -> None:
    try:
        async for event in client.events(on_open=ready.set, directory=directory):
            await queue.put(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the observer switches to polling.
        ready.set()
        await queue.put({"type": "_stream_error", "reason": type(exc).__name__})


def _tracker_usage_fields(tracker: StructuredTurnTracker) -> dict[str, Any]:
    tokens, provider_cost = tracker.usage()
    return {
        "tokens": tokens,
        "estimated_paid_usage": {
            "providerReportedCost": round(provider_cost, 8),
            "estimated": False,
        },
    }


class ResponseEvalRunner:
    def __init__(
        self,
        client: OpenCodeClient,
        model: ModelRef,
        workspace_root: Path,
        *,
        judge_enabled: bool = True,
        event_multiplexer: EventMultiplexer | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.workspace_root = workspace_root
        self.judge_enabled = judge_enabled
        self.event_multiplexer = event_multiplexer
        self._fixture_files: set[Path] = set()

    async def run_case(self, case: ResponseCase, trial: int) -> TrialResult:
        case_dir = self._materialize_case(case)
        source_id: str | None = None
        fork_id: str | None = None
        try:
            source = await self.client.create_session()
            source_id = str(source.get("id") or "")
            if not source_id:
                raise RuntimeError("OpenCode did not create an eval source session")
            source_before = await self.client.messages(source_id)
            fork = await self.client.fork_session(source_id)
            fork_id = str(fork.get("id") or "")
            if not fork_id:
                raise RuntimeError("OpenCode did not create an eval fork")
            session = await self.client.get_session(fork_id)
            directory = str(fork.get("directory") or session.get("directory") or self.workspace_root)
            prompt = self._case_prompt(case, case_dir)

            setup_prompts = list(case.setup_turns)
            if case.follow_up:
                setup_prompts.append(prompt)
                prompt = case.follow_up
            for setup_prompt in setup_prompts:
                setup = await observe_structured_turn(
                    self.client,
                    session_id=fork_id,
                    directory=directory,
                    prompt=setup_prompt,
                    model=self.model,
                    agent=GENERATOR_AGENT,
                    schema=RESPONSE_SCHEMA,
                    event_multiplexer=self.event_multiplexer,
                )
                if setup.structured is None:
                    raise RuntimeError(f"setup_turn_failed:{setup.assistant_error}")

            observation = await observe_structured_turn(
                self.client,
                session_id=fork_id,
                directory=directory,
                prompt=prompt,
                model=self.model,
                agent=GENERATOR_AGENT,
                schema=RESPONSE_SCHEMA,
                event_multiplexer=self.event_multiplexer,
            )
            first_value = observation.structured
            first_evaluation = evaluate_response(first_value, case, workspace_root=str(self.workspace_root))
            real_tools = [activity for activity in observation.tool_activity if activity.tool != "StructuredOutput"]
            if case.requires_tool and not real_tools:
                first_evaluation = _append_evaluation_violation(
                    first_evaluation,
                    Violation(
                        "tool_required",
                        "case required real OpenCode tool activity",
                        "Inspect or modify the supplied evaluation files with the appropriate tool before answering.",
                        gate="semantic",
                    ),
                )
            first_pass = list(first_evaluation.violations)
            selected_value = first_value
            selected_evaluation = first_evaluation
            repair_value: Any | None = None
            repair_evaluation: EvaluationResult | None = None
            repair_observation: TurnObservation | None = None
            repair_error: str | None = None
            repair_selected_reason = "first_response_no_repair"
            admitted, repair_admitted_reason = should_admit_repair(first_evaluation)
            if admitted and observation.assistant_error in {None, "structured_output_missing"}:
                repair_observation = await observe_structured_turn(
                    self.client,
                    session_id=fork_id,
                    directory=directory,
                    prompt=repair_prompt(prompt, first_value, list(first_evaluation.violations)),
                    model=self.model,
                    agent=GENERATOR_AGENT,
                    schema=RESPONSE_SCHEMA,
                    event_multiplexer=self.event_multiplexer,
                )
                repair_value = repair_observation.structured
                repair_real_tools = [
                    activity for activity in repair_observation.tool_activity if activity.tool != "StructuredOutput"
                ]
                if repair_value is None:
                    repair_error = str(repair_observation.assistant_error or "structured_output_missing")
                    repair_selected_reason = "repair_missing_preserved_first"
                else:
                    repair_evaluation = evaluate_response(
                        repair_value,
                        case,
                        workspace_root=str(self.workspace_root),
                    )
                    if case.requires_tool and not real_tools and not repair_real_tools:
                        repair_evaluation = _append_evaluation_violation(
                            repair_evaluation,
                            Violation(
                                "tool_required",
                                "case required real OpenCode tool activity",
                                "Inspect or modify the supplied evaluation files with the appropriate tool before answering.",
                                gate="semantic",
                            ),
                        )
                    select, repair_selected_reason = should_select_repair(first_evaluation, repair_evaluation)
                    if select:
                        selected_value = repair_value
                        selected_evaluation = repair_evaluation

            judge = None
            if self.judge_enabled and not selected_evaluation.violations and selected_value is not None:
                judge = await self._judge(case, selected_value, directory)
            source_after = await self.client.messages(source_id)
            all_tool_activity = [
                *observation.tool_activity,
                *(repair_observation.tool_activity if repair_observation else []),
            ]
            turn_observations = [observation, *([repair_observation] if repair_observation else [])]
            token_totals = _merge_token_usage([item.tokens for item in turn_observations])
            paid_usage = {
                "providerReportedCost": round(
                    sum(float(item.estimated_paid_usage.get("providerReportedCost", 0)) for item in turn_observations),
                    8,
                ),
                "estimated": False,
            }
            final_error = None
            if selected_value is None:
                final_error = str((repair_observation or observation).assistant_error or "structured_output_missing")
            return TrialResult(
                case_id=case.case_id,
                category=case.category,
                trial=trial,
                response=selected_value,
                first_response=first_value,
                first_pass_violations=first_pass,
                final_violations=list(selected_evaluation.violations),
                repaired=admitted,
                repair_response=repair_value,
                judge=judge,
                latency_ms=observation.final_ms + (repair_observation.final_ms if repair_observation else 0),
                first_activity_ms=observation.first_activity_ms,
                stream_source=(repair_observation or observation).stream_source,
                tool_activity=all_tool_activity,
                source_untouched=source_after == source_before,
                first_decisions=list(first_evaluation.decisions),
                repair_decisions=list(repair_evaluation.decisions) if repair_evaluation else [],
                final_decisions=list(selected_evaluation.decisions),
                repair_admitted_reason=repair_admitted_reason,
                repair_selected_reason=repair_selected_reason,
                repair_error=repair_error,
                tokens=token_totals,
                estimated_paid_usage=paid_usage,
                error=final_error,
            )
        except Exception as exc:  # noqa: BLE001 - one case failure must not discard the run.
            if isinstance(exc, httpx.HTTPStatusError):
                safe_body = exc.response.text[:500].replace("\n", " ")
                error = f"HTTP{exc.response.status_code}:{safe_body}"
            else:
                error = f"{type(exc).__name__}:{exc}"
            return TrialResult(
                case_id=case.case_id,
                category=case.category,
                trial=trial,
                response=None,
                first_response=None,
                first_pass_violations=[],
                final_violations=[],
                repaired=False,
                repair_response=None,
                judge=None,
                latency_ms=0,
                first_activity_ms=None,
                stream_source="error",
                tool_activity=[],
                source_untouched=False,
                error=error,
            )
        finally:
            for session_id in (fork_id, source_id):
                if session_id:
                    try:
                        await self.client.delete_session(session_id)
                    except Exception:
                        pass

    async def _judge(self, case: ResponseCase, response: Any, directory: str | None) -> dict[str, Any] | None:
        source_id: str | None = None
        fork_id: str | None = None
        try:
            source = await self.client.create_session()
            source_id = str(source.get("id") or "")
            fork = await self.client.fork_session(source_id)
            fork_id = str(fork.get("id") or "")
            prompt = json.dumps(
                {
                    "userRequest": case.prompt if not case.follow_up else case.follow_up,
                    "requiredFacts": list(case.required_facts),
                    "candidate": response,
                },
                ensure_ascii=False,
            )
            observation = await observe_structured_turn(
                self.client,
                session_id=fork_id,
                directory=directory,
                prompt=f"Evaluate this data object and return only the required rubric result:\n{prompt}",
                model=self.model,
                agent=JUDGE_AGENT,
                schema=JUDGE_SCHEMA,
                max_turn_sec=60.0,
                event_multiplexer=self.event_multiplexer,
            )
            return observation.structured if isinstance(observation.structured, dict) else None
        finally:
            for session_id in (fork_id, source_id):
                if session_id:
                    try:
                        await self.client.delete_session(session_id)
                    except Exception:
                        pass

    def _materialize_case(self, case: ResponseCase) -> Path:
        for previous in self._fixture_files:
            previous.unlink(missing_ok=True)
        self._fixture_files.clear()
        case_dir = self.workspace_root
        root = case_dir.resolve()
        for relative, content in case.files.items():
            target = (case_dir / relative).resolve()
            if root not in target.parents:
                raise ValueError(f"case file escapes workspace: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self._fixture_files.add(target)
        return case_dir

    def _case_prompt(self, case: ResponseCase, case_dir: Path) -> str:
        if not case.files:
            return case.prompt
        return (
            "Work only with the synthetic files in the current workspace. Use tools when the request requires inspection or a change. "
            "Do not mention the evaluation environment or any absolute path in the final response.\n\n"
            f"{case.prompt}"
        )


def _append_evaluation_violation(evaluation: EvaluationResult, violation: Violation) -> EvaluationResult:
    return EvaluationResult(
        violations=(*evaluation.violations, violation),
        decisions=(
            *evaluation.decisions,
            GraderDecision(
                grader=violation.code,
                gate=violation.gate,
                passed=False,
                detail=violation.detail,
                field=violation.field,
            ),
        ),
    )


def _merge_token_usage(items: list[dict[str, int]]) -> dict[str, int]:
    keys = {key for item in items for key in item}
    return {key: sum(int(item.get(key, 0)) for item in items) for key in sorted(keys)}


def validate_corpus(cases: list[ResponseCase]) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    for case in cases:
        if case.case_id in ids:
            errors.append(f"duplicate case id: {case.case_id}")
        ids.add(case.case_id)
        if not case.prompt.strip():
            errors.append(f"{case.case_id}: empty prompt")
        for relative in case.files:
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                errors.append(f"{case.case_id}: unsafe fixture path {relative}")
    if len(cases) != 100:
        errors.append(f"expected 100 core cases, found {len(cases)}")
    if len(smoke_response_cases()) != 20:
        errors.append(f"expected 20 smoke cases, found {len(smoke_response_cases())}")
    return errors


def summarize_results(results: list[TrialResult]) -> dict[str, Any]:
    latencies = sorted(result.latency_ms for result in results if result.latency_ms > 0)
    judges = [result.judge for result in results if isinstance(result.judge, dict)]
    judge_average = {
        key: round(sum(float(item[key]) for item in judges) / len(judges), 3)
        for key in ("naturalness", "directness", "completeness", "equivalence")
    } if judges else {}
    categories = sorted({result.category for result in results})
    return {
        "trials": len(results),
        "passed": sum(result.passed for result in results),
        "firstPassPassed": sum(not result.first_pass_violations and result.error is None for result in results),
        "repairsAdmitted": sum(result.repaired for result in results),
        "repairsSelected": sum(result.repair_selected_reason == "repair_improved_without_regression" for result in results),
        "repairErrors": sum(result.repair_error is not None for result in results),
        "errors": sum(result.error is not None for result in results),
        "sourceUntouched": sum(result.source_untouched for result in results),
        "streamSources": {
            source: sum(result.stream_source == source for result in results)
            for source in sorted({result.stream_source for result in results})
        },
        "latencyMs": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
        "judgeAverage": judge_average,
        "toolActivity": {
            "events": sum(len(result.tool_activity) for result in results),
            "trialsWithRealTools": sum(
                any(activity.tool != "StructuredOutput" for activity in result.tool_activity) for result in results
            ),
        },
        "tokens": _merge_token_usage([result.tokens for result in results]),
        "providerReportedCost": round(
            sum(float(result.estimated_paid_usage.get("providerReportedCost", 0)) for result in results),
            8,
        ),
        "firstActivityMs": {
            "p50": _percentile(
                sorted(result.first_activity_ms for result in results if result.first_activity_ms is not None), 0.50
            ),
            "p95": _percentile(
                sorted(result.first_activity_ms for result in results if result.first_activity_ms is not None), 0.95
            ),
        },
        "graderFailures": {
            "safety": sum(item.gate == "safety" for result in results for item in result.final_violations),
            "semantic": sum(item.gate == "semantic" for result in results for item in result.final_violations),
        },
        "segments": {
            "category": {
                category: {
                    "trials": sum(result.category == category for result in results),
                    "passed": sum(result.category == category and result.passed for result in results),
                }
                for category in categories
            },
            "observedTools": {
                "real": sum(any(activity.tool != "StructuredOutput" for activity in result.tool_activity) for result in results),
                "structuredOnly": sum(
                    bool(result.tool_activity)
                    and not any(activity.tool != "StructuredOutput" for activity in result.tool_activity)
                    for result in results
                ),
                "none": sum(not result.tool_activity for result in results),
            },
            "repairState": {
                "none": sum(not result.repaired for result in results),
                "selected": sum(result.repair_selected_reason == "repair_improved_without_regression" for result in results),
                "rolledBack": sum(
                    result.repaired and result.repair_selected_reason != "repair_improved_without_regression"
                    for result in results
                ),
            },
        },
    }


def live_gate_failures(
    results: list[TrialResult],
    *,
    full: bool,
    judge_authoritative: bool = False,
) -> list[str]:
    failures: list[str] = []
    total = len(results)
    if total == 0:
        return ["no trials ran"]
    if any(not result.passed for result in results):
        failures.append("one or more trials failed a hard gate")
    first_pass_rate = sum(not result.first_pass_violations and result.error is None for result in results) / total
    repair_rate = sum(result.repaired for result in results) / total
    if full and first_pass_rate < 0.99:
        failures.append(f"first-pass validity {first_pass_rate:.1%} is below 99%")
    if full and repair_rate >= 0.05:
        failures.append(f"repair rate {repair_rate:.1%} is not below 5%")
    judges = [result.judge for result in results if isinstance(result.judge, dict)]
    if full and judge_authoritative and judges:
        equivalence_rate = sum(int(item.get("equivalence", 0)) >= 4 for item in judges) / len(judges)
        quality_rate = sum(
            min(int(item.get("naturalness", 0)), int(item.get("directness", 0)), int(item.get("completeness", 0))) >= 4
            for item in judges
        ) / len(judges)
        if equivalence_rate < 0.98:
            failures.append(f"equivalence pass rate {equivalence_rate:.1%} is below 98%")
        if quality_rate < 0.95:
            failures.append(f"quality pass rate {quality_rate:.1%} is below 95%")
    no_tool_latencies = sorted(
        result.latency_ms
        for result in results
        if not any(activity.tool != "StructuredOutput" for activity in result.tool_activity)
        and not result.repaired
        and result.latency_ms > 0
    )
    if full and _percentile(no_tool_latencies, 0.50) > 3500:
        failures.append("warm no-tool p50 latency exceeds 3.5 seconds")
    if full and _percentile(no_tool_latencies, 0.95) > 5000:
        failures.append("warm no-tool p95 latency exceeds 5 seconds")
    return failures


def write_report(
    results: list[TrialResult],
    *,
    profile: str,
    model: ModelRef,
    prompt_hash: str,
    judge_authoritative: bool = False,
    root: Path = Path("runs/response-evals"),
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "mortic.response-eval.v2",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "buildSha": resolve_build_sha(cwd=Path.cwd()),
        "profile": profile,
        "model": model.opencode_name,
        "variant": model.variant or "default",
        "promptHash": prompt_hash,
        "configurationFingerprint": hashlib.sha256(
            json.dumps(
                {"profile": profile, "model": model.opencode_name, "variant": model.variant, "promptHash": prompt_hash},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "judgeAuthoritative": judge_authoritative,
        "summary": summarize_results(results),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (run_dir / "trials.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result.as_dict(), ensure_ascii=False, default=str) + "\n")
    failures = [result for result in results if not result.passed]
    lines = ["# Mercury response evaluation", "", f"Profile: {profile}", "", "```json", json.dumps(manifest["summary"], indent=2), "```"]
    if failures:
        lines.extend(["", "## Failures", ""])
        for result in failures:
            codes = [violation.code for violation in result.final_violations]
            lines.append(f"- {result.case_id} trial {result.trial}: {result.error or ', '.join(codes)}")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir


async def run_live_profile(
    cases: list[ResponseCase],
    *,
    trials: int,
    model: ModelRef,
    judge_enabled: bool,
    network_tools: bool = False,
) -> list[TrialResult]:
    with tempfile.TemporaryDirectory(prefix="mortic-response-eval-") as tmp:
        workspace = Path(tmp)
        with ManagedEvalServer(workspace, model, network_tools=network_tools) as server:
            assert server.url is not None
            client = OpenCodeClient(server.url, timeout_sec=120.0)
            event_multiplexer = EventMultiplexer(client, str(workspace))
            results: list[TrialResult] = []
            try:
                await event_multiplexer.start()
                runner = ResponseEvalRunner(
                    client,
                    model,
                    workspace,
                    judge_enabled=judge_enabled,
                    event_multiplexer=event_multiplexer,
                )
                for trial in range(1, trials + 1):
                    for index, case in enumerate(cases, 1):
                        print(f"[{trial}/{trials}] {index}/{len(cases)} {case.case_id}", file=sys.stderr)
                        results.append(await runner.run_case(case, trial))
            finally:
                await event_multiplexer.close()
                await client.close()
            return results


async def run_long_context_profile(
    scripts: list[ConversationScript],
    *,
    model: ModelRef,
) -> list[LongContextCheckpointResult]:
    """Seed deterministic conversations, then grade three structured checkpoints per script."""
    with tempfile.TemporaryDirectory(prefix="mortic-long-context-") as tmp:
        workspace = Path(tmp)
        with ManagedEvalServer(workspace, model) as server:
            assert server.url is not None
            client = OpenCodeClient(server.url, timeout_sec=120.0)
            multiplexer = EventMultiplexer(client, str(workspace))
            results: list[LongContextCheckpointResult] = []
            try:
                await multiplexer.start()
                for script in scripts:
                    source_id: str | None = None
                    fork_id: str | None = None
                    try:
                        source = await client.create_session()
                        source_id = str(source.get("id") or "")
                        source_before = await client._messages(source_id)
                        fork = await client.fork_session(source_id)
                        fork_id = str(fork.get("id") or "")
                        directory = str(fork.get("directory") or workspace)
                        for exchange in script.exchanges:
                            print(
                                f"[long-context] {script.script_id} turn {exchange.turn}/{script.length}",
                                file=sys.stderr,
                            )
                            if exchange.checkpoint_id is None:
                                seeded = await observe_structured_turn(
                                    client,
                                    session_id=fork_id,
                                    directory=directory,
                                    prompt=f"[SEED] {exchange.user_text}",
                                    model=model,
                                    agent=SETUP_AGENT,
                                    schema=RESPONSE_SCHEMA,
                                    event_multiplexer=multiplexer,
                                )
                                if seeded.structured is None:
                                    raise RuntimeError(f"seed_failed:{seeded.assistant_error}")
                                continue
                            observation = await observe_structured_turn(
                                client,
                                session_id=fork_id,
                                directory=directory,
                                prompt=f"[CHECKPOINT] {exchange.user_text}",
                                model=model,
                                agent=SETUP_AGENT,
                                schema=RESPONSE_SCHEMA,
                                event_multiplexer=multiplexer,
                            )
                            case = script.checkpoint_case(exchange)
                            first_value = observation.structured
                            first_evaluation = evaluate_response(first_value, case)
                            selected_value = first_value
                            selected_evaluation = first_evaluation
                            repair_value: Any | None = None
                            repair_observation: TurnObservation | None = None
                            admitted, admitted_reason = should_admit_repair(first_evaluation)
                            selected_reason = "first_response_no_repair"
                            if admitted:
                                repair_observation = await observe_structured_turn(
                                    client,
                                    session_id=fork_id,
                                    directory=directory,
                                    prompt="[CHECKPOINT] "
                                    + repair_prompt(
                                        exchange.user_text,
                                        first_value,
                                        list(first_evaluation.violations),
                                    ),
                                    model=model,
                                    agent=SETUP_AGENT,
                                    schema=RESPONSE_SCHEMA,
                                    event_multiplexer=multiplexer,
                                )
                                repair_value = repair_observation.structured
                                if repair_value is None:
                                    selected_reason = "repair_missing_preserved_first"
                                else:
                                    repair_evaluation = evaluate_response(repair_value, case)
                                    select, selected_reason = should_select_repair(
                                        first_evaluation,
                                        repair_evaluation,
                                    )
                                    if select:
                                        selected_value = repair_value
                                        selected_evaluation = repair_evaluation
                            active = script.active_facts(exchange.turn)
                            recall = score_recall(
                                selected_value or {},
                                active,
                                all_facts=script.ledger,
                                current_turn=exchange.turn,
                            )
                            source_after = await client._messages(source_id)
                            results.append(
                                LongContextCheckpointResult(
                                    script_id=script.script_id,
                                    conversation_length=script.length,
                                    checkpoint_id=str(exchange.checkpoint_id),
                                    turn=exchange.turn,
                                    response=selected_value,
                                    recall=recall.as_dict(),
                                    violations=list(selected_evaluation.violations),
                                    latency_ms=observation.final_ms
                                    + (repair_observation.final_ms if repair_observation else 0),
                                    tool_activity=[
                                        *observation.tool_activity,
                                        *(repair_observation.tool_activity if repair_observation else []),
                                    ],
                                    source_untouched=source_after == source_before,
                                    first_response=first_value,
                                    repair_response=repair_value,
                                    repair_admitted_reason=admitted_reason,
                                    repair_selected_reason=selected_reason,
                                    first_decisions=list(first_evaluation.decisions),
                                    final_decisions=list(selected_evaluation.decisions),
                                    tokens=_merge_token_usage(
                                        [
                                            observation.tokens,
                                            *( [repair_observation.tokens] if repair_observation else []),
                                        ]
                                    ),
                                    estimated_paid_usage={
                                        "providerReportedCost": round(
                                            float(
                                                observation.estimated_paid_usage.get(
                                                    "providerReportedCost", 0
                                                )
                                            )
                                            + (
                                                float(
                                                    repair_observation.estimated_paid_usage.get(
                                                        "providerReportedCost", 0
                                                    )
                                                )
                                                if repair_observation
                                                else 0
                                            ),
                                            8,
                                        ),
                                        "estimated": False,
                                    },
                                    error=(
                                        str(
                                            (repair_observation or observation).assistant_error
                                            or "structured_output_missing"
                                        )
                                        if selected_value is None
                                        else None
                                    ),
                                )
                            )
                    except Exception as exc:  # noqa: BLE001 - keep the remaining scripts observable.
                        results.append(
                            LongContextCheckpointResult(
                                script_id=script.script_id,
                                conversation_length=script.length,
                                checkpoint_id="setup-error",
                                turn=0,
                                response=None,
                                recall={},
                                violations=[],
                                latency_ms=0,
                                tool_activity=[],
                                source_untouched=False,
                                error=f"{type(exc).__name__}:{exc}",
                            )
                        )
                    finally:
                        for session_id in (fork_id, source_id):
                            if session_id:
                                try:
                                    await client.delete_session(session_id)
                                except Exception:
                                    pass
            finally:
                await multiplexer.close()
                await client.close()
            return results


def write_long_context_report(
    results: list[LongContextCheckpointResult],
    *,
    model: ModelRef,
    root: Path = Path("runs/response-evals"),
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    lengths = sorted({item.conversation_length for item in results})
    by_length = {
        str(length): {
            "checkpoints": sum(item.conversation_length == length for item in results),
            "passed": sum(item.conversation_length == length and item.passed for item in results),
            "generalRecall": round(
                sum(float(item.recall.get("recall_rate", 0)) for item in results if item.conversation_length == length)
                / max(1, sum(item.conversation_length == length for item in results)),
                4,
            ),
            "criticalRecall": round(
                sum(float(item.recall.get("critical_recall_rate", 0)) for item in results if item.conversation_length == length)
                / max(1, sum(item.conversation_length == length for item in results)),
                4,
            ),
            "displayRecall": _recall_ratio(results, length, "display_recalled", "total"),
            "spokenRecall": _recall_ratio(results, length, "spoken_recalled", "total"),
            "recentRecall": _recall_ratio(results, length, "recent_recalled", "recent_total"),
            "contradictions": sum(
                int(item.recall.get("contradictions", 0))
                for item in results
                if item.conversation_length == length
            ),
            "unsupportedClaims": sum(
                len(item.recall.get("unsupported_ids", ()))
                for item in results
                if item.conversation_length == length
            ),
            "realToolCheckpoints": sum(
                item.conversation_length == length
                and any(activity.tool != "StructuredOutput" for activity in item.tool_activity)
                for item in results
            ),
            "latencyMs": {
                "p50": _percentile(
                    sorted(item.latency_ms for item in results if item.conversation_length == length),
                    0.50,
                ),
                "p95": _percentile(
                    sorted(item.latency_ms for item in results if item.conversation_length == length),
                    0.95,
                ),
            },
            "repairsSelected": sum(
                item.conversation_length == length
                and item.repair_selected_reason == "repair_improved_without_regression"
                for item in results
            ),
        }
        for length in lengths
    }
    manifest = {
        "schema": "mortic.response-eval.v2",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "buildSha": resolve_build_sha(cwd=Path.cwd()),
        "profile": "long-context",
        "model": model.opencode_name,
        "segments": {"conversationLength": by_length},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (run_dir / "checkpoints.jsonl").open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(item.as_dict(), ensure_ascii=False, default=str) + "\n")
    (run_dir / "summary.md").write_text(
        "# Mercury long-context evaluation\n\n```json\n"
        + json.dumps(by_length, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    return run_dir


def _recall_ratio(
    results: list[LongContextCheckpointResult],
    length: int,
    numerator: str,
    denominator: str,
) -> float:
    selected = [item for item in results if item.conversation_length == length]
    total = sum(int(item.recall.get(denominator, 0)) for item in selected)
    recalled = sum(int(item.recall.get(numerator, 0)) for item in selected)
    return round(recalled / total, 4) if total else 1.0


async def run_judge_calibration(model: ModelRef) -> tuple[list[dict[str, Any]], bool]:
    fixtures = judge_calibration_fixtures()
    with tempfile.TemporaryDirectory(prefix="mortic-judge-calibration-") as tmp:
        workspace = Path(tmp)
        with ManagedEvalServer(workspace, model) as server:
            assert server.url is not None
            client = OpenCodeClient(server.url, timeout_sec=120.0)
            multiplexer = EventMultiplexer(client, str(workspace))
            rows: list[dict[str, Any]] = []
            try:
                await multiplexer.start()
                runner = ResponseEvalRunner(
                    client,
                    model,
                    workspace,
                    judge_enabled=True,
                    event_multiplexer=multiplexer,
                )
                for index, fixture in enumerate(fixtures, 1):
                    print(f"[calibration] {index}/{len(fixtures)} {fixture.fixture_id}", file=sys.stderr)
                    case = ResponseCase(fixture.fixture_id, "judge-calibration", fixture.user_request)
                    judgment = await runner._judge(case, fixture.candidate, str(workspace))
                    scores = judgment if isinstance(judgment, dict) else {}
                    actual_pass = bool(scores) and all(int(scores.get(key, 0)) >= 4 for key in (
                        "naturalness", "directness", "completeness", "equivalence"
                    ))
                    rows.append(
                        {
                            "fixtureId": fixture.fixture_id,
                            "expectedPass": fixture.expected_pass,
                            "actualPass": actual_pass,
                            "validClarification": fixture.valid_clarification,
                            "failingDimensions": list(fixture.failing_dimensions),
                            "judgment": judgment,
                            "agreed": actual_pass == fixture.expected_pass,
                        }
                    )
            finally:
                await multiplexer.close()
                await client.close()
    agreement = sum(bool(item["agreed"]) for item in rows) / max(1, len(rows))
    clarification_pass = all(item["actualPass"] for item in rows if item["validClarification"])
    return rows, agreement >= 0.95 and clarification_pass


def write_calibration_report(
    rows: list[dict[str, Any]],
    *,
    authoritative: bool,
    model: ModelRef,
    root: Path = Path("runs/response-evals"),
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    agreement = sum(bool(item.get("agreed")) for item in rows) / max(1, len(rows))
    clarification = [item for item in rows if item.get("validClarification")]
    manifest = {
        "schema": "mortic.response-eval.v2",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "buildSha": resolve_build_sha(cwd=Path.cwd()),
        "profile": "calibration",
        "model": model.opencode_name,
        "judge": {
            "agreement": agreement,
            "validClarificationsAccepted": sum(bool(item.get("actualPass")) for item in clarification),
            "validClarifications": len(clarification),
            "authoritative": authoritative,
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (run_dir / "judge-calibration.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    (run_dir / "summary.md").write_text(
        "# Mercury judge calibration\n\n```json\n" + json.dumps(manifest["judge"], indent=2) + "\n```\n",
        encoding="utf-8",
    )
    return run_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate Mortic's OpenCode/Mercury response presentation contract.")
    parser.add_argument("--live", action="store_true", help="Run real paid/networked Mercury trials.")
    parser.add_argument("--validate-only", action="store_true", help="Validate assets and corpus without networking.")
    parser.add_argument(
        "--profile",
        choices=["calibration", "smoke", "notation", "long-context", "compaction", "full", "web-smoke"],
        default="smoke",
    )
    parser.add_argument("--trials", type=int, help="Override profile trial count (smoke 1, full 3).")
    parser.add_argument("--model", default="inception/mercury-2")
    parser.add_argument("--model-variant", default="high", help="Use 'default' to omit the variant.")
    parser.add_argument("--no-judge", action="store_true", help="Skip the eval-only Mercury rubric judge.")
    parser.add_argument("--case-id", action="append", help="Run only the selected case id; may be repeated.")
    parser.add_argument("--native-scale", action="store_true", help="Add the real 70k and near-120k canaries.")
    parser.add_argument(
        "--allow-full",
        action="store_true",
        help="Confirm that all bounded smoke profiles passed before the paid full run.",
    )
    parser.add_argument(
        "--compare-baseline",
        type=Path,
        help="Regrade an immutable prior run into a separate report without networking.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_local_dotenv(Path("~/.mortic/.env").expanduser())
    load_local_dotenv()
    args = parse_args(argv)
    core_cases = load_response_cases()
    errors = validate_corpus(core_cases)
    notation_cases = notation_response_cases()
    scripts = conversation_scripts()
    if len(notation_cases) != 24:
        errors.append(f"expected 24 notation cases, found {len(notation_cases)}")
    if len(judge_calibration_fixtures()) != 32:
        errors.append("expected 32 judge calibration fixtures")
    if len(scripts) != 8:
        errors.append(f"expected 8 conversation scripts, found {len(scripts)}")
    if not GENERATOR_PROMPT_PATH.is_file() or not JUDGE_PROMPT_PATH.is_file():
        errors.append("response eval prompt assets are missing")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if args.compare_baseline:
        output = regrade_baseline(args.compare_baseline, core_cases)
        print(f"Comparison report: {output}")
        return 0
    if args.validate_only or not args.live:
        print(
            f"Validated {len(core_cases)} core cases, {len(smoke_response_cases())} smoke cases, "
            f"{len(notation_cases)} notation cases, {len(judge_calibration_fixtures())} judge fixtures, "
            f"and {len(scripts)} conversation scripts."
        )
        if not args.live and not args.validate_only:
            print("No network calls made. Pass --live to run Mercury trials.")
        return 0
    if not os.environ.get("INCEPTION_API_KEY"):
        print("ERROR: --live requires INCEPTION_API_KEY", file=sys.stderr)
        return 2
    variant = None if args.model_variant.lower() == "default" else args.model_variant
    model = parse_model_ref(args.model, variant=variant)
    if args.profile == "calibration":
        rows, authoritative = asyncio.run(run_judge_calibration(model))
        run_dir = write_calibration_report(rows, authoritative=authoritative, model=model)
        print(json.dumps({"judgeAuthoritative": authoritative, "report": str(run_dir)}, indent=2))
        return 0 if authoritative else 1
    if args.profile == "long-context":
        selected_scripts = [script for script in scripts if script.script_id.endswith("-a")]
        if args.case_id:
            wanted = set(args.case_id)
            selected_scripts = [script for script in scripts if script.script_id in wanted]
            missing = sorted(wanted - {script.script_id for script in selected_scripts})
            if missing:
                print(f"ERROR: unknown long-context script ids: {', '.join(missing)}", file=sys.stderr)
                return 2
        repetitions = args.trials or 1
        results: list[LongContextCheckpointResult] = []
        for _ in range(repetitions):
            results.extend(asyncio.run(run_long_context_profile(selected_scripts, model=model)))
        run_dir = write_long_context_report(results, model=model)
        print(f"Report: {run_dir}")
        return 1 if any(not item.passed for item in results) else 0
    if args.profile == "compaction":
        from opencode_voice.response_compaction_runner import run_compaction_matrix, write_compaction_report

        profile_matrix = compaction_profiles(native_scale=args.native_scale)
        if args.case_id:
            wanted = set(args.case_id)
            profile_matrix = [profile for profile in profile_matrix if profile.profile_id in wanted]
            missing = sorted(wanted - {profile.profile_id for profile in profile_matrix})
            if missing:
                print(f"ERROR: unknown compaction profile ids: {', '.join(missing)}", file=sys.stderr)
                return 2
        results = asyncio.run(run_compaction_matrix(profile_matrix, model=model))
        run_dir = write_compaction_report(results, model=model)
        print(f"Report: {run_dir}")
        return 1 if any(not item.passed for item in results) else 0
    if args.profile == "full" and not args.allow_full:
        print(
            "ERROR: full is gated until calibration, smoke, notation, long-context, and scaled compaction pass; "
            "then rerun with --allow-full.",
            file=sys.stderr,
        )
        return 2
    if args.profile == "smoke":
        cases = smoke_response_cases()
        trials = args.trials or 1
    elif args.profile == "notation":
        cases = [case for case in notation_cases if case.case_id.endswith("-a")]
        trials = args.trials or 1
    elif args.profile == "full":
        cases = [*core_cases, *notation_cases]
        trials = args.trials or 3
    else:
        cases = web_response_cases()
        trials = args.trials or 1
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case.case_id in selected]
        missing = sorted(selected - {case.case_id for case in cases})
        if missing:
            print(f"ERROR: unknown case ids for profile {args.profile}: {', '.join(missing)}", file=sys.stderr)
            return 2
    if trials <= 0:
        print("ERROR: --trials must be positive", file=sys.stderr)
        return 2
    prompt_hash = hashlib.sha256(GENERATOR_PROMPT_PATH.read_bytes()).hexdigest()
    results = asyncio.run(
        run_live_profile(
            cases,
            trials=trials,
            model=model,
            judge_enabled=not args.no_judge,
            network_tools=args.profile == "web-smoke",
        )
    )
    run_dir = write_report(
        results,
        profile=args.profile,
        model=model,
        prompt_hash=prompt_hash,
        judge_authoritative=False,
    )
    summary = summarize_results(results)
    print(json.dumps(summary, indent=2))
    print(f"Report: {run_dir}")
    failures = live_gate_failures(results, full=args.profile == "full", judge_authoritative=False)
    if args.profile == "full" and not failures:
        long_results: list[LongContextCheckpointResult] = []
        for _ in range(2):
            long_results.extend(asyncio.run(run_long_context_profile(scripts, model=model)))
        long_dir = write_long_context_report(long_results, model=model)
        print(f"Long-context report: {long_dir}")
        if any(not item.passed for item in long_results):
            failures.append("one or more full long-context checkpoints failed")
        if not failures:
            from opencode_voice.response_compaction_runner import run_compaction_matrix, write_compaction_report

            compaction_results = asyncio.run(
                run_compaction_matrix(compaction_profiles(native_scale=True), model=model)
            )
            compaction_dir = write_compaction_report(compaction_results, model=model)
            print(f"Compaction report: {compaction_dir}")
            if any(not item.passed for item in compaction_results):
                failures.append("one or more full compaction profiles failed")
    for failure in failures:
        print(f"GATE: {failure}", file=sys.stderr)
    return 1 if failures else 0


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    index = int((len(values) - 1) * percentile)
    return values[index]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_healthy(base_url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url}/global/health", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
