from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openrouter_bench.config import load_config, write_json
from openrouter_bench.draco import download_draco, load_draco_tasks
from openrouter_bench.openrouter import OpenRouterClient, build_payload, run_draft_check, run_local_fusion


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)

    dataset_config = config.get("dataset", {})
    cache_path = Path(args.dataset_cache or dataset_config.get("cache_path", "data/draco/test.jsonl"))
    dataset_url = args.dataset_url or dataset_config.get("url")
    if not dataset_url:
        raise SystemExit("Dataset URL is missing from config and --dataset-url was not provided.")

    if args.download_draco or not cache_path.exists():
        print(f"Caching DRACO at {cache_path}", file=sys.stderr)
        download_draco(cache_path, dataset_url, overwrite=args.overwrite_dataset)
        if args.download_draco:
            return 0

    limit = None if args.limit == 0 else args.limit
    tasks = load_draco_tasks(cache_path, limit=limit, start=args.start)
    if not tasks:
        raise SystemExit("No DRACO tasks loaded.")

    systems = select_systems(config.get("systems", []), args.systems)
    if not systems:
        raise SystemExit("No systems selected.")

    run_dir = Path(args.run_dir) if args.run_dir else Path("runs") / timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / "responses.jsonl"
    write_json(
        run_dir / "run_config.json",
        {
            "config": config,
            "args": vars(args),
            "task_count": len(tasks),
            "systems": [system["name"] for system in systems],
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    api_configs = get_api_configs(config)
    default_api_name = next(iter(api_configs))

    if args.dry_run:
        print(f"Dry run: {len(tasks)} tasks x {len(systems)} systems", file=sys.stderr)
        for system in systems:
            if system.get("strategy") == "local_fusion":
                payload = {
                    "strategy": "local_fusion",
                    "analysis_models": system.get("analysis_models", []),
                    "analysis_defaults": {**config.get("defaults", {}), **system.get("analysis_defaults", {})},
                    "analysis_api": system.get("analysis_api") or system.get("api") or default_api_name,
                    "synthesis_model": system.get("synthesis_model") or system.get("model"),
                    "synthesis_api": system.get("synthesis_api") or system.get("api") or default_api_name,
                    "synthesis_defaults": {**config.get("defaults", {}), **system.get("synthesis_defaults", {})},
                    "stream": args.stream,
                }
            elif system.get("strategy") == "draft_check":
                payload = {
                    "strategy": "draft_check",
                    "draft_model": system.get("draft_model") or system.get("model"),
                    "draft_api": system.get("draft_api") or system.get("api") or default_api_name,
                    "draft_defaults": {**config.get("defaults", {}), **system.get("draft_defaults", {})},
                    "checker_model": system.get("checker_model"),
                    "checker_api": system.get("checker_api") or system.get("api") or default_api_name,
                    "checker_defaults": {**config.get("defaults", {}), **system.get("checker_defaults", {})},
                    "stream": args.stream,
                }
            else:
                payload = build_payload(system, tasks[0].prompt, config.get("defaults", {}))
                payload["stream"] = args.stream
            print(
                json.dumps(
                    {"system": system["name"], "api": system_api_name(system, default_api_name), "payload": payload},
                    indent=2,
                ),
                file=sys.stderr,
            )
        return 0

    clients = build_clients(api_configs, timeout_sec=args.timeout_sec)
    default_api_config = api_configs[default_api_name]
    max_retries = int(args.max_retries if args.max_retries is not None else default_api_config.get("max_retries", 2))
    retry_backoff_sec = float(default_api_config.get("retry_backoff_sec", 1.0))

    total = len(tasks) * len(systems)
    completed = 0
    with output_path.open("a", encoding="utf-8") as output:
        for task in tasks:
            for system in systems:
                completed += 1
                print(
                    f"[{completed}/{total}] {system['name']} task={task.task_id}",
                    file=sys.stderr,
                    flush=True,
                )
                if system.get("strategy") == "local_fusion":
                    payload = {
                        "strategy": "local_fusion",
                        "analysis_models": system.get("analysis_models", []),
                        "analysis_api": system.get("analysis_api") or system.get("api") or default_api_name,
                        "synthesis_model": system.get("synthesis_model") or system.get("model"),
                        "synthesis_api": system.get("synthesis_api") or system.get("api") or default_api_name,
                        "model": system.get("synthesis_model") or system.get("model"),
                        "stream": args.stream,
                    }
                    result = run_local_fusion(
                        client=clients,
                        system=system,
                        prompt=task.prompt,
                        defaults=config.get("defaults", {}),
                        stream=args.stream,
                        max_retries=max_retries,
                        retry_backoff_sec=retry_backoff_sec,
                        default_api=default_api_name,
                    )
                elif system.get("strategy") == "draft_check":
                    payload = {
                        "strategy": "draft_check",
                        "draft_model": system.get("draft_model") or system.get("model"),
                        "draft_api": system.get("draft_api") or system.get("api") or default_api_name,
                        "checker_model": system.get("checker_model"),
                        "checker_api": system.get("checker_api") or system.get("api") or default_api_name,
                        "model": system.get("checker_model"),
                        "stream": args.stream,
                    }
                    result = run_draft_check(
                        client=clients,
                        system=system,
                        prompt=task.prompt,
                        defaults=config.get("defaults", {}),
                        stream=args.stream,
                        max_retries=max_retries,
                        retry_backoff_sec=retry_backoff_sec,
                        default_api=default_api_name,
                    )
                else:
                    payload = build_payload(system, task.prompt, config.get("defaults", {}))
                    api_name = system_api_name(system, default_api_name)
                    result = clients[api_name].complete(
                        payload,
                        stream=args.stream,
                        max_retries=max_retries,
                        retry_backoff_sec=retry_backoff_sec,
                    )
                record = make_record(
                    task=task,
                    system=system,
                    payload={**payload, "stream": args.stream},
                    stream=args.stream,
                    result=result,
                    run_dir=run_dir,
                    api_name=system_api_name(system, default_api_name),
                )
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()

    print(f"Wrote {output_path}", file=sys.stderr)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Mercury 2 DRACO benchmarks through OpenRouter or Inception.")
    parser.add_argument("--config", default="configs/fusion_mercury_2.json", help="JSON or YAML benchmark config.")
    parser.add_argument("--limit", type=int, default=10, help="Number of DRACO tasks to run. Use 0 for all tasks.")
    parser.add_argument("--start", type=int, default=0, help="Start offset in the DRACO JSONL file.")
    parser.add_argument("--systems", nargs="*", help="Optional system names to run.")
    parser.add_argument("--run-dir", help="Output directory. Defaults to runs/<timestamp>.")
    parser.add_argument("--dataset-cache", help="Local DRACO JSONL cache path.")
    parser.add_argument("--dataset-url", help="DRACO JSONL URL.")
    parser.add_argument("--download-draco", action="store_true", help="Download/cache DRACO and exit.")
    parser.add_argument("--overwrite-dataset", action="store_true", help="Overwrite the cached DRACO file.")
    parser.add_argument("--dry-run", action="store_true", help="Print the first payload per system without API calls.")
    parser.add_argument("--max-retries", type=int, help="Override API max retries.")
    parser.add_argument("--timeout-sec", type=float, help="Override API request timeout.")
    parser.add_argument("--stream", dest="stream", action="store_true", help="Use streaming mode.")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="Use non-streaming mode.")
    parser.set_defaults(stream=True)
    return parser.parse_args(argv)


def select_systems(systems: list[dict[str, Any]], requested: list[str] | None) -> list[dict[str, Any]]:
    if not isinstance(systems, list):
        raise ValueError("Config field 'systems' must be a list.")
    for system in systems:
        if "name" not in system:
            raise ValueError("Every system must have a name.")
    if not requested:
        return systems
    requested_set = set(requested)
    selected = [system for system in systems if system["name"] in requested_set]
    missing = requested_set - {system["name"] for system in selected}
    if missing:
        raise ValueError(f"Unknown system(s): {', '.join(sorted(missing))}")
    return selected


def get_api_configs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if isinstance(config.get("apis"), dict):
        apis: dict[str, dict[str, Any]] = {}
        for name, api_value in config["apis"].items():
            if not isinstance(api_value, dict):
                raise ValueError(f"API config for {name!r} must be a mapping.")
            api = dict(api_value)
            api.setdefault("name", name)
            api.setdefault("generation_id_header", None)
            apis[name] = api
        if not apis:
            raise ValueError("Config field 'apis' must not be empty.")
        return apis
    if isinstance(config.get("api"), dict):
        api = dict(config["api"])
        api.setdefault("name", "api")
        api.setdefault("env_var", "OPENROUTER_API_KEY")
        api.setdefault("base_url", "https://openrouter.ai/api/v1")
        api.setdefault("generation_id_header", None)
        return {api["name"]: api}
    if isinstance(config.get("openrouter"), dict):
        api = dict(config["openrouter"])
        api.setdefault("name", "openrouter")
        api.setdefault("env_var", "OPENROUTER_API_KEY")
        api.setdefault("base_url", "https://openrouter.ai/api/v1")
        api.setdefault("generation_id_header", "X-Generation-Id")
        return {api["name"]: api}
    api = {
        "name": "openrouter",
        "env_var": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "generation_id_header": "X-Generation-Id",
    }
    return {api["name"]: api}


def build_clients(api_configs: dict[str, dict[str, Any]], timeout_sec: float | None) -> dict[str, OpenRouterClient]:
    clients: dict[str, OpenRouterClient] = {}
    for name, api_config in api_configs.items():
        env_var = api_config.get("env_var")
        if not isinstance(env_var, str) or not env_var:
            raise SystemExit(f"API {name!r} is missing env_var.")
        api_key = os.environ.get(env_var)
        if not api_key:
            raise SystemExit(f"{env_var} is not set.")
        clients[name] = OpenRouterClient(
            api_key=api_key,
            base_url=api_config["base_url"],
            timeout_sec=float(timeout_sec or api_config.get("timeout_sec", 300)),
            headers=string_headers(api_config.get("headers", {})),
            generation_id_header=api_config.get("generation_id_header"),
        )
    return clients


def system_api_name(system: dict[str, Any], default_api_name: str) -> str:
    if system.get("strategy") in {"local_fusion", "draft_check"}:
        return "multi"
    api_name = system.get("api")
    return api_name if isinstance(api_name, str) and api_name else default_api_name


def make_record(
    task: Any,
    system: dict[str, Any],
    payload: dict[str, Any],
    stream: bool,
    result: Any,
    run_dir: Path,
    api_name: str,
) -> dict[str, Any]:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "task_id": task.task_id,
        "domain": task.domain,
        "prompt": task.prompt,
        "system": system["name"],
        "model_system_name": system["name"],
        "api": api_name,
        "request_model": payload.get("model"),
        "streaming": stream,
        "response_text": result.text,
        "latency_ms": result.latency_ms,
        "ttft_ms": result.ttft_ms,
        "completion_tokens": result.completion_tokens,
        "prompt_tokens": result.prompt_tokens,
        "total_tokens": result.total_tokens,
        "generation_id": result.generation_id,
        "response_id": result.response_id,
        "status_code": result.status_code,
        "errors": result.errors,
        "retries": result.retries,
        "request_payload": payload,
        "raw_json_response": result.raw_json_response,
    }


def string_headers(headers: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in headers.items()}


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
