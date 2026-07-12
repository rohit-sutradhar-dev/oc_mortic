from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from opencode_voice.config import ModelRef


class SSEParser:
    def __init__(self) -> None:
        self._data_lines: list[str] = []

    def push_line(self, line: str) -> dict[str, Any] | None:
        if not line:
            return self._flush()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if not separator:
            return None
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_lines.append(value)
        return None

    def _flush(self) -> dict[str, Any] | None:
        if not self._data_lines:
            return None
        raw = "\n".join(self._data_lines)
        self._data_lines.clear()
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None


class OpenCodeClient:
    def __init__(self, base_url: str, timeout_sec: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_sec)

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        return await self._get("/global/health")

    async def list_sessions(self) -> list[dict[str, Any]]:
        data = await self._get("/session")
        return data if isinstance(data, list) else []

    async def agents(self) -> list[str]:
        """Names of the agents this server knows. A voice turn sent with an
        agent the server lacks is accepted (204) then silently never runs, so
        the doctor checks membership before a turn is ever attempted."""
        data = await self._get("/agent")
        if not isinstance(data, list):
            return []
        return [str(a.get("name")) for a in data if isinstance(a, dict) and a.get("name")]

    async def create_session(self) -> dict[str, Any]:
        return await self._post("/session", {})

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._get(f"/session/{session_id}")

    async def update_session(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._patch(f"/session/{session_id}", payload)

    async def fork_session(self, session_id: str, message_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, str] = {}
        if message_id:
            payload["messageID"] = message_id
        return await self._post(f"/session/{session_id}/fork", payload)

    async def delete_session(self, session_id: str) -> Any:
        response = await self._client.delete(f"/session/{session_id}")
        response.raise_for_status()
        return response.json() if response.content else True

    async def summarize(self, session_id: str, model: ModelRef, auto: bool = False) -> Any:
        return await self._post(
            f"/session/{session_id}/summarize",
            {"providerID": model.provider_id, "modelID": model.model_id, "auto": auto},
        )

    async def compact_v2(self, session_id: str) -> None:
        response = await self._client.post(f"/api/session/{session_id}/compact")
        response.raise_for_status()

    async def wait_for_idle(self, session_id: str) -> None:
        response = await self._client.post(f"/api/session/{session_id}/wait")
        response.raise_for_status()

    async def session_context(self, session_id: str) -> list[dict[str, Any]]:
        payload = await self._get(f"/api/session/{session_id}/context")
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, list) else []

    async def session_history(
        self,
        session_id: str,
        *,
        limit: int = 100,
        after: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, int] = {"limit": limit}
        if after is not None:
            params["after"] = after
        response = await self._client.get(f"/api/session/{session_id}/history", params=params)
        response.raise_for_status()
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {}

    async def session_events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        parser = SSEParser()
        async with self._client.stream(
            "GET",
            f"/api/session/{session_id}/event",
            headers={"accept": "text/event-stream"},
            timeout=None,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                event = parser.push_line(line)
                if event is not None:
                    yield event

    async def switch_model(self, session_id: str, model: ModelRef) -> Any:
        return await self._post(f"/api/session/{session_id}/model", {"model": model.session_payload()})

    async def switch_agent(self, session_id: str, agent: str) -> Any:
        return await self._post(f"/api/session/{session_id}/agent", {"agent": agent})

    async def prompt_text(self, session_id: str, text: str, model: ModelRef, agent: str) -> Any:
        try:
            return await self.prompt_sync(session_id, text, model, agent)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 405}:
                raise
        try:
            return await self._post(
                f"/api/session/{session_id}/prompt",
                {"prompt": {"text": text}, "delivery": "queue"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 405}:
                raise
        return await self.prompt_sync(session_id, text, model, agent)

    async def prompt_sync(self, session_id: str, text: str, model: ModelRef, agent: str) -> Any:
        payload = {
            "model": model.prompt_payload(),
            "agent": agent,
            "parts": [{"type": "text", "text": text}],
        }
        return await self._post(f"/session/{session_id}/message", payload)

    async def prompt_async(
        self,
        session_id: str,
        text: str,
        model: ModelRef,
        agent: str,
        *,
        output_format: dict[str, Any] | None = None,
        system: str | None = None,
        tools: dict[str, bool] | None = None,
    ) -> Any:
        payload = {
            "model": model.prompt_payload(),
            "agent": agent,
            "parts": [{"type": "text", "text": text}],
        }
        if output_format is not None:
            payload["format"] = output_format
        if system is not None:
            payload["system"] = system
        if tools is not None:
            payload["tools"] = tools
        return await self._post(f"/session/{session_id}/prompt_async", payload)

    async def events(
        self,
        on_open: Callable[[], None] | None = None,
        directory: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        parser = SSEParser()
        async with self._client.stream(
            "GET",
            "/event",
            params={"directory": directory} if directory else None,
            headers={"accept": "text/event-stream"},
            timeout=None,
        ) as response:
            response.raise_for_status()
            if on_open:
                on_open()
            async for line in response.aiter_lines():
                event = parser.push_line(line)
                if event is not None:
                    yield event

    async def abort(self, session_id: str) -> Any:
        return await self._post(f"/session/{session_id}/abort", {})

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/session/{session_id}/message")
        return data if isinstance(data, list) else []

    async def projected_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Read the v2 message projection used by structured-output turns.

        OpenCode 1.17.18's legacy message decoder rejects a persisted user
        message whose ``format`` contains ``retryCount``.  The v2 projection
        reads the same session without decoding it through that legacy schema.
        """
        response = await self._client.get(
            f"/api/session/{session_id}/message",
            params={"limit": 200},
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, list) else []

    async def messages_for_tracking(self, session_id: str) -> list[dict[str, Any]]:
        """Prefer the legacy shape, falling back only for its known 400."""
        try:
            return await self.messages(session_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
        return await self.projected_messages(session_id)

    async def _get(self, path: str) -> Any:
        response = await self._client.get(path)
        response.raise_for_status()
        return response.json() if response.content else {}

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        response = await self._client.post(path, json=payload)
        response.raise_for_status()
        return response.json() if response.content else {}

    async def _patch(self, path: str, payload: dict[str, Any]) -> Any:
        response = await self._client.patch(path, json=payload)
        response.raise_for_status()
        return response.json() if response.content else {}
