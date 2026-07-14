from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opencode_voice.config import parse_model_ref
from opencode_voice.opencode_client import OpenCodeClient
from opencode_voice.response_compaction import (
    compare_fork_snapshots,
    duplicate_action_hashes,
    normalized_message_graph,
    recorded_context_tokens,
)
from opencode_voice.response_eval import ManagedEvalServer
from opencode_voice.state import active_context_estimate, is_completed_assistant_summary


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _compaction_flags(messages: list[dict[str, Any]]) -> list[bool]:
    return [
        bool(part.get("auto"))
        for message in messages
        for part in message.get("parts") or []
        if isinstance(part, dict) and part.get("type") == "compaction"
    ]


async def run(source_id: str, *, repeat: bool, auto: bool, root: Path) -> Path:
    model = parse_model_ref("inception/mercury-2", variant="high")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = root / stamp
    output.mkdir(parents=True, exist_ok=False)
    fork_id: str | None = None
    descendant_id: str | None = None
    with ManagedEvalServer(Path.cwd(), model) as server:
        assert server.url is not None
        client = OpenCodeClient(server.url, timeout_sec=240)
        try:
            source_before = await client.messages(source_id)
            source_hash = _digest(normalized_message_graph(source_before))
            fork = await client.fork_session(source_id)
            fork_id = str(fork["id"])
            fork_before = await client.messages(fork_id)
            inheritance = compare_fork_snapshots(
                source_before,
                await client.messages(source_id),
                fork_before,
            )
            before_tokens = recorded_context_tokens(fork_before) or active_context_estimate(fork_before).tokens
            summaries_before = sum(is_completed_assistant_summary(message) for message in fork_before)
            before_text_dups, before_tool_dups = duplicate_action_hashes(fork_before)
            started = time.perf_counter()
            first_error: str | None = None
            try:
                await client.summarize(fork_id, model, auto=auto)
            except Exception as exc:  # noqa: BLE001 - evidence must survive expected overflow failures.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                first_error = f"{type(exc).__name__}:{status or 'unknown'}"
            first_latency = int((time.perf_counter() - started) * 1000)
            first_after = await client.messages(fork_id)
            first_after_tokens = active_context_estimate(first_after).tokens
            summaries_after_first = sum(is_completed_assistant_summary(message) for message in first_after)
            first_succeeded = summaries_after_first > summaries_before and first_after_tokens < before_tokens
            first_flags = _compaction_flags(first_after)
            first_text_dups, first_tool_dups = duplicate_action_hashes(first_after)

            post_inheritance_valid: bool | None = None
            if first_succeeded:
                descendant = await client.fork_session(fork_id)
                descendant_id = str(descendant["id"])
                post_inheritance = compare_fork_snapshots(
                    first_after,
                    await client.messages(fork_id),
                    await client.messages(descendant_id),
                )
                post_inheritance_valid = post_inheritance.inherited_content_equal \
                    and post_inheritance.parent_links_valid \
                    and post_inheritance.tail_links_valid

            second: dict[str, Any] | None = None
            second_text_dups: tuple[str, ...] = ()
            second_tool_dups: tuple[str, ...] = ()
            if repeat and first_succeeded:
                started = time.perf_counter()
                await client.summarize(fork_id, model, auto=auto)
                second_latency = int((time.perf_counter() - started) * 1000)
                second_after = await client.messages(fork_id)
                second_after_tokens = active_context_estimate(second_after).tokens
                second_text_dups, second_tool_dups = duplicate_action_hashes(second_after)
                second = {
                    "latencyMs": second_latency,
                    "beforeTokens": first_after_tokens,
                    "afterTokens": second_after_tokens,
                    "reduction": first_after_tokens - second_after_tokens,
                    "messageCount": len(second_after),
                    "occurred": len(second_after) > len(first_after),
                }

            source_after = await client.messages(source_id)
            evidence = {
                "schema": "mortic.existing-session-compaction.v1",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "sourceSessionId": source_id,
                "sourceMessageCount": len(source_before),
                "sourceTokens": before_tokens,
                "auto": auto,
                "sourceUntouched": source_hash == _digest(normalized_message_graph(source_after)),
                "initialForkInheritance": inheritance.inherited_content_equal
                and inheritance.parent_links_valid
                and inheritance.tail_links_valid,
                "firstCompaction": {
                    "succeeded": first_succeeded,
                    "persistedAutoFlag": first_flags[-1] if first_flags else None,
                    "error": first_error,
                    "latencyMs": first_latency,
                    "beforeTokens": before_tokens,
                    "afterTokens": first_after_tokens,
                    "reduction": before_tokens - first_after_tokens,
                    "messageCount": len(first_after),
                },
                "postCompactionForkInheritance": post_inheritance_valid,
                "secondCompactionWithoutGrowth": second,
                "duplicates": {
                    "beforeText": len(before_text_dups),
                    "beforeTools": len(before_tool_dups),
                    "afterFirstText": len(first_text_dups),
                    "afterFirstTools": len(first_tool_dups),
                    "introducedAfterFirstText": len(set(first_text_dups) - set(before_text_dups)),
                    "introducedAfterFirstTools": len(set(first_tool_dups) - set(before_tool_dups)),
                    "afterSecondText": len(second_text_dups),
                    "afterSecondTools": len(second_tool_dups),
                },
            }
            (output / "manifest.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            (output / "summary.md").write_text(
                "# Existing-session compaction canary\n\n```json\n"
                + json.dumps(evidence, indent=2)
                + "\n```\n",
                encoding="utf-8",
            )
            return output
        finally:
            for session_id in (descendant_id, fork_id):
                if session_id:
                    try:
                        await client.delete_session(session_id)
                    except Exception:
                        pass
            await client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_session")
    parser.add_argument("--repeat", action="store_true")
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()
    output = asyncio.run(
        run(args.source_session, repeat=args.repeat, auto=args.auto, root=Path("runs/response-evals"))
    )
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
