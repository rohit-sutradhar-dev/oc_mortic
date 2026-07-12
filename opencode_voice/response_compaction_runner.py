from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opencode_voice.config import ModelRef
from opencode_voice.opencode_client import OpenCodeClient
from opencode_voice.response_compaction import (
    CompactionEventTracker,
    CompactionObservation,
    CompactionProfile,
    ContextMeasurement,
    ForkSnapshot,
    ProviderTokenTracker,
    compare_fork_snapshots,
    duplicate_action_hashes,
    recorded_context_tokens,
)
from opencode_voice.response_eval import COMPACTION_AGENT, EventMultiplexer, ManagedEvalServer
from opencode_voice.state import active_context_estimate
from opencode_voice.telemetry import resolve_build_sha


@dataclass
class CompactionProfileResult:
    profile_id: str
    measurements: list[ContextMeasurement] = field(default_factory=list)
    compactions: list[CompactionObservation] = field(default_factory=list)
    fork_snapshots: list[ForkSnapshot] = field(default_factory=list)
    context_records: int = 0
    history_records: int = 0
    duplicate_text_hashes: tuple[str, ...] = ()
    duplicate_tool_hashes: tuple[str, ...] = ()
    error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.error is None
            and all(item.source_untouched and item.parent_links_valid and item.tail_links_valid for item in self.fork_snapshots)
            and not self.duplicate_tool_hashes
        )

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["passed"] = self.passed
        return value


async def run_compaction_matrix(
    profiles: list[CompactionProfile],
    *,
    model: ModelRef,
) -> list[CompactionProfileResult]:
    results: list[CompactionProfileResult] = []
    for profile in profiles:
        result = CompactionProfileResult(profile.profile_id)
        results.append(result)
        try:
            await _run_profile(profile, model, result)
        except Exception as exc:  # noqa: BLE001 - retain evidence for every profile.
            result.error = f"{type(exc).__name__}:{exc}"
    return results


async def _run_profile(
    profile: CompactionProfile,
    model: ModelRef,
    result: CompactionProfileResult,
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"mortic-compaction-{profile.profile_id}-") as tmp:
        workspace = Path(tmp)
        with ManagedEvalServer(workspace, model, compaction_profile=profile) as server:
            assert server.url is not None
            client = OpenCodeClient(server.url, timeout_sec=180.0)
            multiplexer = EventMultiplexer(client, str(workspace))
            session_id: str | None = None
            forks: list[str] = []
            try:
                await multiplexer.start()
                session = await client.create_session()
                session_id = str(session.get("id") or "")
                directory = str(session.get("directory") or workspace)
                tracker = CompactionEventTracker(session_id, profile.profile_id)
                token_tracker = ProviderTokenTracker(session_id)
                multiplexer.add_listener(tracker.update)
                multiplexer.add_listener(token_tracker.update)
                trigger = profile.effective_trigger
                basis = trigger or profile.input_limit or profile.context_limit
                targets = [int(basis * ratio) for ratio in (0.25, 0.50, 0.70, 0.80, 0.90, 0.98)]
                if profile.mode in {"manual-v2", "manual-legacy"} and trigger is not None:
                    targets.append(trigger)
                manual_invoked = False
                for target in targets:
                    await _grow_to_target(
                        client,
                        multiplexer,
                        session_id=session_id,
                        directory=directory,
                        model=model,
                        target=target,
                        token_tracker=token_tracker,
                    )
                    messages = await client.messages_for_tracking(session_id)
                    recorded = token_tracker.current_tokens or recorded_context_tokens(messages)
                    estimated = active_context_estimate(messages).tokens
                    result.measurements.append(
                        ContextMeasurement(recorded, estimated, profile.context_limit, trigger)
                    )
                    if not result.fork_snapshots and recorded >= int(basis * 0.70):
                        result.fork_snapshots.extend(await _snapshot_sibling_forks(client, session_id, messages, forks))
                    if (
                        not manual_invoked
                        and profile.mode in {"manual-v2", "manual-legacy"}
                        and trigger is not None
                        and recorded >= trigger
                    ):
                        before = result.measurements[-1]
                        if profile.mode == "manual-v2":
                            await client.compact_v2(session_id)
                        else:
                            await client.summarize(session_id, model, auto=False)
                        manual_invoked = True
                        await _send_filler_turn(
                            client,
                            multiplexer,
                            session_id=session_id,
                            directory=directory,
                            model=model,
                            filler_tokens=256,
                            sequence=10_000,
                            token_tracker=token_tracker,
                        )
                        after_messages = await client.messages_for_tracking(session_id)
                        result.fork_snapshots.extend(
                            await _snapshot_sibling_forks(client, session_id, after_messages, forks)
                        )
                        tracker.reconcile_messages(after_messages)
                        after = ContextMeasurement(
                            token_tracker.current_tokens or recorded_context_tokens(after_messages),
                            active_context_estimate(after_messages).tokens,
                            profile.context_limit,
                            trigger,
                        )
                        if tracker.observations:
                            tracker.observations[-1].before = before
                            tracker.observations[-1].after = after
                        break
                if profile.mode == "auto" and trigger is not None and not tracker.observations:
                    await _send_filler_turn(
                        client,
                        multiplexer,
                        session_id=session_id,
                        directory=directory,
                        model=model,
                        filler_tokens=max(1_024, int(trigger * 0.03)),
                        sequence=20_000,
                        token_tracker=token_tracker,
                    )
                await asyncio.sleep(0.1)
                messages = await client.messages_for_tracking(session_id)
                tracker.reconcile_messages(messages)
                if tracker.observations:
                    result.fork_snapshots.extend(
                        await _snapshot_sibling_forks(client, session_id, messages, forks)
                    )
                text_hashes, tool_hashes = duplicate_action_hashes(messages)
                result.duplicate_text_hashes = text_hashes
                result.duplicate_tool_hashes = tool_hashes
                result.compactions = tracker.observations
                try:
                    result.context_records = len(await client.session_context(session_id))
                    history = await client.session_history(session_id)
                    data = history.get("data") if isinstance(history, dict) else None
                    result.history_records = len(data) if isinstance(data, list) else 0
                except Exception as exc:  # Surface v2 instrumentation incompatibility explicitly.
                    result.error = f"instrumentation:{type(exc).__name__}:{exc}"
                multiplexer.remove_listener(tracker.update)
                multiplexer.remove_listener(token_tracker.update)
            finally:
                for fork_id in forks:
                    try:
                        await client.delete_session(fork_id)
                    except Exception:
                        pass
                if session_id:
                    try:
                        await client.delete_session(session_id)
                    except Exception:
                        pass
                await multiplexer.close()
                await client.close()


async def _grow_to_target(
    client: OpenCodeClient,
    multiplexer: EventMultiplexer,
    *,
    session_id: str,
    directory: str,
    model: ModelRef,
    target: int,
    token_tracker: ProviderTokenTracker,
) -> None:
    attempts = 0
    while attempts < 80:
        messages = await client.messages_for_tracking(session_id)
        current = token_tracker.current_tokens or recorded_context_tokens(messages)
        if current >= target:
            return
        missing = target - current
        filler_tokens = max(256, min(1_500, int(missing * 0.75)))
        await _send_filler_turn(
            client,
            multiplexer,
            session_id=session_id,
            directory=directory,
            model=model,
            filler_tokens=filler_tokens,
            sequence=attempts,
            token_tracker=token_tracker,
        )
        attempts += 1
    raise RuntimeError(f"target_not_reached:{target}")


async def _send_filler_turn(
    client: OpenCodeClient,
    multiplexer: EventMultiplexer,
    *,
    session_id: str,
    directory: str,
    model: ModelRef,
    filler_tokens: int,
    sequence: int,
    token_tracker: ProviderTokenTracker,
) -> None:
    del directory, multiplexer  # The persistent event listener owns token telemetry.
    previous_samples = len(token_tracker.samples)
    response = await asyncio.wait_for(
        client.prompt_sync(
            session_id,
            _filler(filler_tokens, sequence),
            model,
            COMPACTION_AGENT,
        ),
        timeout=180.0,
    )
    info = response.get("info") if isinstance(response, dict) else None
    if isinstance(info, dict) and info.get("error") is not None:
        raise RuntimeError(f"filler_turn_failed:{_safe_error(info.get('error'))}")
    response_message_id = str(info.get("id") or "") if isinstance(info, dict) else ""
    if isinstance(info, dict):
        token_tracker.update(
            {
                "type": "message.updated",
                "properties": {"sessionID": session_id, "info": info},
            }
        )
    deadline = asyncio.get_running_loop().time() + 2.0
    while (
        len(token_tracker.samples) <= previous_samples
        or (response_message_id and response_message_id not in token_tracker.message_ids)
    ) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    if len(token_tracker.samples) <= previous_samples or (
        response_message_id and response_message_id not in token_tracker.message_ids
    ):
        raise RuntimeError("filler_turn_missing_provider_tokens")


async def _snapshot_sibling_forks(
    client: OpenCodeClient,
    source_id: str,
    source_messages: list[dict[str, Any]],
    fork_ids: list[str],
) -> list[ForkSnapshot]:
    snapshots: list[ForkSnapshot] = []
    for _ in range(2):
        fork = await client.fork_session(source_id)
        fork_id = str(fork.get("id") or "")
        fork_ids.append(fork_id)
        fork_messages = await client.messages_for_tracking(fork_id)
        source_after = await client.messages_for_tracking(source_id)
        snapshots.append(compare_fork_snapshots(source_messages, source_after, fork_messages))
    if source_messages:
        cutoff_index = len(source_messages) // 2
        cutoff_message = source_messages[cutoff_index]
        info = cutoff_message.get("info") if isinstance(cutoff_message, dict) else None
        cutoff_id = str(
            (info.get("id") if isinstance(info, dict) else cutoff_message.get("id")) or ""
        )
        if cutoff_id:
            cutoff_fork = await client.fork_session(source_id, cutoff_id)
            cutoff_fork_id = str(cutoff_fork.get("id") or "")
            fork_ids.append(cutoff_fork_id)
            cutoff_messages = await client.messages_for_tracking(cutoff_fork_id)
            source_after = await client.messages_for_tracking(source_id)
            snapshots.append(
                compare_fork_snapshots(
                    source_messages,
                    source_after,
                    cutoff_messages,
                    expected_inherited=source_messages[:cutoff_index],
                )
            )
    return snapshots


def _filler(token_count: int, sequence: int) -> str:
    # Four short terms per repeat produce stable, non-secret, low-entropy context.
    unit = f"context block {sequence} retained fact "
    repeats = max(1, (token_count * 4) // len(unit))
    return (
        "Remember the following synthetic benchmark material without taking actions. "
        + unit * repeats
        + " Acknowledge it tersely."
    )


def _safe_error(error: Any) -> str:
    if isinstance(error, dict):
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        return (
            f"{error.get('name') or 'provider_error'}:{data.get('statusCode') or 'unknown'}:"
            f"retryable={bool(data.get('isRetryable'))}"
        )
    return str(error or "structured_output_missing")


def write_compaction_report(
    results: list[CompactionProfileResult],
    *,
    model: ModelRef,
    root: Path = Path("runs/response-evals"),
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    profile_summaries = {
        item.profile_id: {
            "passed": item.passed,
            "error": item.error,
            "measurements": [
                {
                    "recordedTokens": measurement.recorded_tokens,
                    "estimatedTokens": measurement.estimated_tokens,
                    "contextUtilization": measurement.context_utilization,
                    "triggerUtilization": measurement.trigger_utilization,
                }
                for measurement in item.measurements
            ],
            "compactionCount": len(item.compactions),
            "compactionLatenciesMs": [
                observation.latency_ms
                for observation in item.compactions
                if observation.latency_ms is not None
            ],
            "forkSnapshots": len(item.fork_snapshots),
            "forksValid": all(
                snapshot.source_untouched
                and snapshot.inherited_content_equal
                and snapshot.parent_links_valid
                and snapshot.tail_links_valid
                for snapshot in item.fork_snapshots
            ),
            "duplicateTextHashes": len(item.duplicate_text_hashes),
            "duplicateToolHashes": len(item.duplicate_tool_hashes),
        }
        for item in results
    }
    manifest = {
        "schema": "mortic.response-eval.v2",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "buildSha": resolve_build_sha(cwd=Path.cwd()),
        "profile": "compaction",
        "model": model.opencode_name,
        "profiles": profile_summaries,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (run_dir / "profiles.jsonl").open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(item.as_dict(), ensure_ascii=False, default=str) + "\n")
    lines = ["# Mercury compaction and fork evaluation", ""]
    lines.extend(f"- {item.profile_id}: {'pass' if item.passed else item.error or 'failed'}" for item in results)
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir
