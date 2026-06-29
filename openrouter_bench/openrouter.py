from __future__ import annotations

import json
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterable

CHAT_COMPLETIONS_PATH = "/chat/completions"
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
SYSTEM_METADATA_KEYS = {
    "name",
    "description",
    "enabled",
    "strategy",
    "api",
    "analysis_models",
    "analysis_api",
    "analysis_defaults",
    "synthesis_model",
    "synthesis_api",
    "synthesis_defaults",
    "synthesis_prompt_template",
    "draft_model",
    "draft_api",
    "draft_defaults",
    "checker_model",
    "checker_api",
    "checker_defaults",
    "checker_prompt_template",
}


@dataclass
class CompletionResult:
    text: str
    latency_ms: int
    ttft_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    response_id: str | None
    generation_id: str | None
    raw_json_response: Any
    errors: list[dict[str, Any]]
    retries: int
    status_code: int | None


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_sec: float = 300,
        headers: dict[str, str] | None = None,
        generation_id_header: str | None = "X-Generation-Id",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.headers = headers or {}
        self.generation_id_header = generation_id_header

    def complete(
        self,
        payload: dict[str, Any],
        stream: bool,
        max_retries: int,
        retry_backoff_sec: float,
    ) -> CompletionResult:
        errors: list[dict[str, Any]] = []
        overall_started = time.perf_counter()
        for attempt in range(max_retries + 1):
            attempt_started = time.perf_counter()
            try:
                if stream:
                    return self._stream_completion(payload, overall_started, errors, attempt)
                return self._nonstream_completion(payload, overall_started, errors, attempt)
            except Exception as exc:  # noqa: BLE001 - persisted per-row for benchmark diagnostics.
                error = serialize_exception(exc, attempt=attempt, latency_ms=elapsed_ms(attempt_started))
                errors.append(error)
                if not should_retry(exc) or attempt >= max_retries:
                    return CompletionResult(
                        text="",
                        latency_ms=elapsed_ms(overall_started),
                        ttft_ms=None,
                        prompt_tokens=None,
                        completion_tokens=None,
                        total_tokens=None,
                        response_id=None,
                        generation_id=getattr(exc, "generation_id", None),
                        raw_json_response=getattr(exc, "raw_json_response", None),
                        errors=errors,
                        retries=attempt,
                        status_code=getattr(exc, "code", None),
                    )
                time.sleep(float(error.get("retry_after_sec") or retry_backoff_sec * (2**attempt)))

        raise RuntimeError("unreachable retry loop exit")

    def _nonstream_completion(
        self,
        payload: dict[str, Any],
        started: float,
        errors: list[dict[str, Any]],
        attempt: int,
    ) -> CompletionResult:
        request_payload = dict(payload)
        request_payload["stream"] = False
        request = self._build_request(request_payload)
        with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
            body = response.read().decode("utf-8")
            raw = json.loads(body) if body else {}
            usage = raw.get("usage") if isinstance(raw, dict) else None
            text = extract_message_text(raw)
            return CompletionResult(
                text=text,
                latency_ms=elapsed_ms(started),
                ttft_ms=None,
                prompt_tokens=usage_value(usage, "prompt_tokens"),
                completion_tokens=usage_value(usage, "completion_tokens"),
                total_tokens=usage_value(usage, "total_tokens"),
                response_id=raw.get("id") if isinstance(raw, dict) else None,
                generation_id=self._generation_id(response),
                raw_json_response=raw,
                errors=errors.copy(),
                retries=attempt,
                status_code=response.status,
            )

    def _stream_completion(
        self,
        payload: dict[str, Any],
        started: float,
        errors: list[dict[str, Any]],
        attempt: int,
    ) -> CompletionResult:
        request_payload = dict(payload)
        request_payload["stream"] = True
        request = self._build_request(request_payload)
        chunks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        ttft_ms: int | None = None
        usage: dict[str, Any] | None = None
        response_id: str | None = None

        with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
            generation_id = self._generation_id(response)
            for event_data in iter_sse_data(response):
                if event_data == "[DONE]":
                    break
                try:
                    chunk = json.loads(event_data)
                except json.JSONDecodeError:
                    chunks.append({"_unparsed": event_data})
                    continue
                chunks.append(chunk)
                if isinstance(chunk, dict):
                    response_id = response_id or chunk.get("id")
                    usage = chunk.get("usage") or usage
                delta_text = extract_delta_text(chunk)
                if delta_text:
                    if ttft_ms is None:
                        ttft_ms = elapsed_ms(started)
                    text_parts.append(delta_text)

            return CompletionResult(
                text="".join(text_parts),
                latency_ms=elapsed_ms(started),
                ttft_ms=ttft_ms,
                prompt_tokens=usage_value(usage, "prompt_tokens"),
                completion_tokens=usage_value(usage, "completion_tokens"),
                total_tokens=usage_value(usage, "total_tokens"),
                response_id=response_id,
                generation_id=generation_id,
                raw_json_response={"chunks": chunks},
                errors=errors.copy(),
                retries=attempt,
                status_code=response.status,
            )

    def _build_request(self, payload: dict[str, Any]) -> urllib.request.Request:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            **self.headers,
        }
        return urllib.request.Request(
            f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
            data=body,
            headers=headers,
            method="POST",
        )

    def generation_metadata(self, generation_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"id": generation_id})
        request = urllib.request.Request(
            f"{self.base_url}/generation?{query}",
            headers={"Authorization": f"Bearer {self.api_key}", **self.headers},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))

    def _generation_id(self, response: Any) -> str | None:
        if not self.generation_id_header:
            return None
        return response.headers.get(self.generation_id_header)


def build_payload(system: dict[str, Any], prompt: str, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    if "model" not in system:
        raise ValueError(f"System {system.get('name', '<unnamed>')} is missing 'model'")

    payload: dict[str, Any] = {
        "model": system["model"],
        "messages": [{"role": "user", "content": prompt}],
    }

    if defaults:
        payload.update(defaults)

    for key, value in system.items():
        if key in SYSTEM_METADATA_KEYS or key == "model":
            continue
        payload[key] = value

    return payload


def run_local_fusion(
    client: OpenRouterClient | dict[str, OpenRouterClient],
    system: dict[str, Any],
    prompt: str,
    defaults: dict[str, Any] | None,
    stream: bool,
    max_retries: int,
    retry_backoff_sec: float,
    default_api: str | None = None,
) -> CompletionResult:
    analysis_models = system.get("analysis_models")
    if not isinstance(analysis_models, list) or not analysis_models:
        raise ValueError(f"Local fusion system {system.get('name', '<unnamed>')} needs analysis_models.")

    started = time.perf_counter()
    analysis_defaults = merge_dicts(defaults or {}, system.get("analysis_defaults", {}))
    synthesis_defaults = merge_dicts(defaults or {}, system.get("synthesis_defaults", {}))
    analysis_api = as_optional_str(system.get("analysis_api")) or as_optional_str(system.get("api")) or default_api

    analysis_results: list[dict[str, Any] | None] = [None] * len(analysis_models)
    with ThreadPoolExecutor(max_workers=len(analysis_models)) as executor:
        model_specs = [
            normalize_model_spec(model, default_api=analysis_api, defaults=analysis_defaults)
            for model in analysis_models
        ]
        futures = {
            executor.submit(
                get_client(client, spec["api"]).complete,
                {"model": spec["model"], "messages": [{"role": "user", "content": prompt}], **spec["defaults"]},
                stream,
                max_retries,
                retry_backoff_sec,
            ): index
            for index, spec in enumerate(model_specs)
        }
        for future in as_completed(futures):
            index = futures[future]
            spec = model_specs[index]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - convert worker failures into benchmark data.
                result = CompletionResult(
                    text="",
                    latency_ms=elapsed_ms(started),
                    ttft_ms=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    total_tokens=None,
                    response_id=None,
                    generation_id=None,
                    raw_json_response=None,
                    errors=[serialize_exception(exc, attempt=0, latency_ms=elapsed_ms(started))],
                    retries=0,
                    status_code=None,
                )
            analysis_results[index] = {"api": spec["api"], "model": spec["model"], "result": result}

    synthesis_model = system.get("synthesis_model") or system.get("model")
    if not isinstance(synthesis_model, str):
        raise ValueError(f"Local fusion system {system.get('name', '<unnamed>')} needs model or synthesis_model.")
    synthesis_api = as_optional_str(system.get("synthesis_api")) or as_optional_str(system.get("api")) or default_api

    synthesis_prompt = build_synthesis_prompt(
        prompt=prompt,
        analysis_results=[item for item in analysis_results if item is not None],
        template=system.get("synthesis_prompt_template"),
    )
    synthesis_payload = {
        "model": synthesis_model,
        "messages": [{"role": "user", "content": synthesis_prompt}],
        **synthesis_defaults,
    }
    synthesis_result = get_client(client, synthesis_api).complete(
        synthesis_payload,
        stream,
        max_retries,
        retry_backoff_sec,
    )

    child_results = [item["result"] for item in analysis_results if item is not None] + [synthesis_result]
    errors: list[dict[str, Any]] = []
    for index, item in enumerate(analysis_results):
        if item is None:
            errors.append({"stage": "analysis", "index": index, "message": "analysis call did not return"})
            continue
        for error in item["result"].errors:
            errors.append({"stage": "analysis", "index": index, "api": item["api"], "model": item["model"], **error})
    for error in synthesis_result.errors:
        errors.append({"stage": "synthesis", "api": synthesis_api, "model": synthesis_model, **error})

    return CompletionResult(
        text=synthesis_result.text,
        latency_ms=elapsed_ms(started),
        ttft_ms=elapsed_ms(started) - synthesis_result.latency_ms + synthesis_result.ttft_ms
        if synthesis_result.ttft_ms is not None
        else None,
        prompt_tokens=sum_optional(result.prompt_tokens for result in child_results),
        completion_tokens=sum_optional(result.completion_tokens for result in child_results),
        total_tokens=sum_optional(result.total_tokens for result in child_results),
        response_id=synthesis_result.response_id,
        generation_id=synthesis_result.generation_id,
        raw_json_response={
            "strategy": "local_fusion",
            "analysis": [
                {
                    "api": item["api"],
                    "model": item["model"],
                    "text": item["result"].text,
                    "latency_ms": item["result"].latency_ms,
                    "ttft_ms": item["result"].ttft_ms,
                    "prompt_tokens": item["result"].prompt_tokens,
                    "completion_tokens": item["result"].completion_tokens,
                    "total_tokens": item["result"].total_tokens,
                    "response_id": item["result"].response_id,
                    "generation_id": item["result"].generation_id,
                    "status_code": item["result"].status_code,
                    "errors": item["result"].errors,
                    "raw_json_response": item["result"].raw_json_response,
                }
                for item in analysis_results
                if item is not None
            ],
            "synthesis_api": synthesis_api,
            "synthesis_payload": synthesis_payload,
            "synthesis": synthesis_result.raw_json_response,
        },
        errors=errors,
        retries=sum(result.retries for result in child_results),
        status_code=synthesis_result.status_code,
    )


def run_draft_check(
    client: OpenRouterClient | dict[str, OpenRouterClient],
    system: dict[str, Any],
    prompt: str,
    defaults: dict[str, Any] | None,
    stream: bool,
    max_retries: int,
    retry_backoff_sec: float,
    default_api: str | None = None,
) -> CompletionResult:
    started = time.perf_counter()
    draft_model = system.get("draft_model") or system.get("model")
    checker_model = system.get("checker_model")
    if not isinstance(draft_model, str):
        raise ValueError(f"Draft-check system {system.get('name', '<unnamed>')} needs draft_model or model.")
    if not isinstance(checker_model, str):
        raise ValueError(f"Draft-check system {system.get('name', '<unnamed>')} needs checker_model.")

    draft_api = as_optional_str(system.get("draft_api")) or as_optional_str(system.get("api")) or default_api
    checker_api = as_optional_str(system.get("checker_api")) or as_optional_str(system.get("api")) or default_api
    draft_defaults = merge_dicts(defaults or {}, system.get("draft_defaults", {}))
    checker_defaults = merge_dicts(defaults or {}, system.get("checker_defaults", {}))

    draft_payload = {
        "model": draft_model,
        "messages": [{"role": "user", "content": prompt}],
        **draft_defaults,
    }
    draft_result = get_client(client, draft_api).complete(
        draft_payload,
        stream,
        max_retries,
        retry_backoff_sec,
    )

    checker_prompt = build_checker_prompt(
        prompt=prompt,
        draft=draft_result.text,
        template=system.get("checker_prompt_template"),
    )
    checker_payload = {
        "model": checker_model,
        "messages": [{"role": "user", "content": checker_prompt}],
        **checker_defaults,
    }
    checker_result = get_client(client, checker_api).complete(
        checker_payload,
        stream,
        max_retries,
        retry_backoff_sec,
    )

    child_results = [draft_result, checker_result]
    errors = [
        {"stage": "draft", "api": draft_api, "model": draft_model, **error}
        for error in draft_result.errors
    ]
    errors.extend(
        {"stage": "checker", "api": checker_api, "model": checker_model, **error}
        for error in checker_result.errors
    )

    return CompletionResult(
        text=checker_result.text,
        latency_ms=elapsed_ms(started),
        ttft_ms=elapsed_ms(started) - checker_result.latency_ms + checker_result.ttft_ms
        if checker_result.ttft_ms is not None
        else None,
        prompt_tokens=sum_optional(result.prompt_tokens for result in child_results),
        completion_tokens=sum_optional(result.completion_tokens for result in child_results),
        total_tokens=sum_optional(result.total_tokens for result in child_results),
        response_id=checker_result.response_id,
        generation_id=checker_result.generation_id,
        raw_json_response={
            "strategy": "draft_check",
            "draft_api": draft_api,
            "draft_payload": draft_payload,
            "draft": {
                "text": draft_result.text,
                "latency_ms": draft_result.latency_ms,
                "ttft_ms": draft_result.ttft_ms,
                "prompt_tokens": draft_result.prompt_tokens,
                "completion_tokens": draft_result.completion_tokens,
                "total_tokens": draft_result.total_tokens,
                "response_id": draft_result.response_id,
                "generation_id": draft_result.generation_id,
                "status_code": draft_result.status_code,
                "errors": draft_result.errors,
                "raw_json_response": draft_result.raw_json_response,
            },
            "checker_api": checker_api,
            "checker_payload": checker_payload,
            "checker": checker_result.raw_json_response,
        },
        errors=errors,
        retries=sum(result.retries for result in child_results),
        status_code=checker_result.status_code,
    )


def build_synthesis_prompt(prompt: str, analysis_results: list[dict[str, Any]], template: Any = None) -> str:
    answers = []
    for index, item in enumerate(analysis_results, start=1):
        result = item["result"]
        answers.append(
            f"Candidate answer {index} from {item['model']}:\n"
            f"{result.text if result.text else '[empty response]'}"
        )
    joined_answers = "\n\n---\n\n".join(answers)
    if isinstance(template, str) and template.strip():
        return template.format(prompt=prompt, answers=joined_answers)
    return (
        "You are synthesizing multiple candidate answers to the same benchmark task.\n"
        "Produce one final answer that is more accurate, complete, and concise than any single candidate.\n"
        "Do not mention candidates, panels, fusion, or the synthesis process.\n\n"
        f"Task:\n{prompt}\n\n"
        f"Candidate answers:\n{joined_answers}\n\n"
        "Final answer:"
    )


def build_checker_prompt(prompt: str, draft: str, template: Any = None) -> str:
    if isinstance(template, str) and template.strip():
        return template.format(prompt=prompt, draft=draft)
    return (
        "You are checking a fast draft answer against a benchmark task.\n"
        "Correct factual errors, fill important omissions, remove unsupported claims, and return one final answer.\n"
        "Do not mention the draft, the checker, or this review process.\n\n"
        f"Task:\n{prompt}\n\n"
        f"Fast draft answer:\n{draft if draft else '[empty response]'}\n\n"
        "Checked final answer:"
    )


def iter_sse_data(lines: Iterable[bytes]) -> Iterable[str]:
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def extract_message_text(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return normalize_content(content)


def extract_delta_text(chunk: Any) -> str:
    if not isinstance(chunk, dict):
        return ""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if isinstance(delta, dict):
        return normalize_content(delta.get("content"))
    return ""


def normalize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def usage_value(usage: Any, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    return value if isinstance(value, int) else None


def sum_optional(values: Iterable[int | None]) -> int | None:
    total = 0
    seen = False
    for value in values:
        if value is None:
            continue
        total += value
        seen = True
    return total if seen else None


def merge_dicts(base: dict[str, Any], override: Any) -> dict[str, Any]:
    merged = dict(base)
    if isinstance(override, dict):
        merged.update(override)
    return merged


def get_client(client: OpenRouterClient | dict[str, OpenRouterClient], api_name: str | None) -> OpenRouterClient:
    if isinstance(client, OpenRouterClient):
        return client
    if api_name and api_name in client:
        return client[api_name]
    if len(client) == 1:
        return next(iter(client.values()))
    raise KeyError(f"Unknown or missing API name: {api_name}")


def normalize_model_spec(model: Any, default_api: str | None, defaults: dict[str, Any]) -> dict[str, Any]:
    if isinstance(model, str):
        return {"api": default_api, "model": model, "defaults": dict(defaults)}
    if isinstance(model, dict):
        model_id = model.get("model") or model.get("id")
        if not isinstance(model_id, str):
            raise ValueError(f"Analysis model spec is missing model/id: {model}")
        spec_defaults = merge_dicts(defaults, model.get("defaults", {}))
        return {
            "api": as_optional_str(model.get("api")) or default_api,
            "model": model_id,
            "defaults": spec_defaults,
        }
    raise ValueError(f"Unsupported analysis model spec: {model!r}")


def as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def should_retry(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_STATUS_CODES
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    return False


def serialize_exception(exc: Exception, attempt: int, latency_ms: int) -> dict[str, Any]:
    error: dict[str, Any] = {
        "attempt": attempt,
        "type": type(exc).__name__,
        "message": str(exc),
        "latency_ms": latency_ms,
    }
    if isinstance(exc, urllib.error.HTTPError):
        error["status_code"] = exc.code
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                error["retry_after_sec"] = float(retry_after)
            except ValueError:
                pass
        try:
            body = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001 - best-effort error capture.
            body = ""
        if body:
            error["body"] = body
            match = re.search(r"try again in ([0-9.]+)s", body, flags=re.IGNORECASE)
            if match:
                error["retry_after_sec"] = float(match.group(1))
    else:
        error["traceback"] = traceback.format_exc(limit=5)
    return error
