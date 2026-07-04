"""Shared OpenCode client fake for connection-level tests."""

from __future__ import annotations

from typing import Any


class FakeOpenCodeClient:
    def __init__(self, base_url: str = "http://opencode.test") -> None:
        self.base_url = base_url
        self.fork_count = 0
        self.deleted: list[str] = []
        self.aborted: list[str] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def health(self) -> dict[str, bool]:
        return {"ok": True}

    async def list_sessions(self) -> list[dict[str, object]]:
        return []

    async def fork_session(self, session_id: str) -> dict[str, str]:
        self.fork_count += 1
        return {"id": f"fork_{self.fork_count}"}

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return {"id": session_id, "title": "Source Thread", "tokens": {}}

    async def switch_model(self, session_id: str, model: Any) -> dict[str, bool]:
        return {"ok": True}

    async def switch_agent(self, session_id: str, agent: str) -> dict[str, bool]:
        return {"ok": True}

    async def update_session(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return []

    async def delete_session(self, session_id: str) -> bool:
        self.deleted.append(session_id)
        return True

    async def abort(self, session_id: str) -> bool:
        self.aborted.append(session_id)
        return True
