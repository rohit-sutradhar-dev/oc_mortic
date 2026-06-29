from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def build_flux_url(
    model: str,
    sample_rate: int,
    eot_threshold: float,
    eot_timeout_ms: int,
    eager_eot_threshold: float | None = None,
) -> str:
    params: dict[str, str | int | float] = {
        "model": model,
        "encoding": "linear16",
        "sample_rate": sample_rate,
        "eot_threshold": eot_threshold,
        "eot_timeout_ms": eot_timeout_ms,
    }
    if eager_eot_threshold is not None:
        params["eager_eot_threshold"] = eager_eot_threshold
    return f"wss://api.deepgram.com/v2/listen?{urllib.parse.urlencode(params)}"


def build_tts_url(model: str, sample_rate: int) -> str:
    params = {
        "model": model,
        "encoding": "linear16",
        "sample_rate": sample_rate,
    }
    return f"wss://api.deepgram.com/v1/speak?{urllib.parse.urlencode(params)}"


def parse_flux_message(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    message_type = str(payload.get("type") or "")
    event = str(payload.get("event") or "")
    kind = event if message_type == "TurnInfo" and event else message_type or event
    transcript = str(payload.get("transcript") or "")
    channel = payload.get("channel")
    if isinstance(channel, dict):
        alternatives = channel.get("alternatives") or []
        if alternatives and isinstance(alternatives[0], dict):
            transcript = transcript or str(alternatives[0].get("transcript") or "")

    normalized = {
        "type": "deepgram.raw",
        "deepgram_type": kind,
        "transcript": transcript,
        "is_final": bool(payload.get("is_final") or payload.get("speech_final") or kind == "EndOfTurn"),
        "raw": payload,
    }
    lowered = kind.lower()
    if lowered == "startofturn":
        normalized["type"] = "speech.start"
    elif lowered in {"endofturn", "eagerendofturn"}:
        normalized["type"] = "speech.end"
        normalized["eager"] = lowered == "eagerendofturn"
    elif lowered == "turnresumed":
        normalized["type"] = "speech.resumed"
    elif transcript:
        normalized["type"] = "speech.transcript"
    return normalized


@dataclass
class FlushLimiter:
    max_flushes: int = 20
    window_sec: float = 60.0
    _timestamps: deque[float] = field(default_factory=deque)

    def allow(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        while self._timestamps and current - self._timestamps[0] >= self.window_sec:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_flushes:
            return False
        self._timestamps.append(current)
        return True


class TTSChunker:
    def __init__(self, preferred_chars: int = 220, max_chars: int = 1800) -> None:
        self.preferred_chars = preferred_chars
        self.max_chars = max_chars
        self.buffer = ""

    def push(self, text: str) -> list[str]:
        self.buffer += text
        return self._drain(force=False)

    def flush(self) -> list[str]:
        return self._drain(force=True)

    def _drain(self, force: bool) -> list[str]:
        chunks: list[str] = []
        while self.buffer:
            split_at = self._split_index(force=force)
            if split_at is None:
                break
            chunk = self.buffer[:split_at].strip()
            self.buffer = self.buffer[split_at:].lstrip()
            if chunk:
                chunks.append(chunk)
        return chunks

    def _split_index(self, force: bool) -> int | None:
        if len(self.buffer) >= self.max_chars:
            return self._last_space_before(self.max_chars) or self.max_chars
        if force:
            return len(self.buffer)
        if len(self.buffer) < self.preferred_chars:
            punctuation = self._first_sentence_end()
            return punctuation if punctuation is not None else None
        punctuation = self._first_sentence_end()
        if punctuation is not None:
            return punctuation
        return self._last_space_before(self.preferred_chars)

    def _first_sentence_end(self) -> int | None:
        for index, char in enumerate(self.buffer):
            if char in ".!?\n":
                next_index = index + 1
                if next_index == len(self.buffer) or self.buffer[next_index].isspace():
                    return next_index
        return None

    def _last_space_before(self, limit: int) -> int | None:
        index = self.buffer.rfind(" ", 0, max(1, limit))
        return index + 1 if index > 0 else None


class SpeechTextFilter:
    def __init__(self) -> None:
        self.in_fence = False
        self.pending = ""

    def push(self, text: str) -> str:
        self.pending += text
        lines = self.pending.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.pending = lines.pop()
        else:
            self.pending = ""
        return "".join(self._filter_line(line) for line in lines)

    def flush(self) -> str:
        if not self.pending:
            return ""
        line = self.pending
        self.pending = ""
        return self._filter_line(line)

    def _filter_line(self, line: str) -> str:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            self.in_fence = not self.in_fence
            return "\n"
        if self.in_fence or _drop_line_for_speech(line):
            return ""
        return _sanitize_spoken_line(line)


_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[\.)]\s+)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_RAW_URL_RE = re.compile(r"https?://\S+")


def _drop_line_for_speech(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if _looks_like_code_line(line):
        return True
    if _LIST_ITEM_RE.match(stripped):
        return True
    if lowered.startswith(("or import ", "import ", "from ")):
        return True
    if "`" in stripped and any(word in lowered for word in ("run ", "import ", "execute ", "command")):
        return True
    return False


def _sanitize_spoken_line(line: str) -> str:
    newline = "\n" if line.endswith(("\n", "\r")) else ""
    body = line.rstrip("\r\n")
    body = _MARKDOWN_LINK_RE.sub(r"\1", body)
    body = _RAW_URL_RE.sub("the link", body)
    body = _INLINE_CODE_RE.sub(lambda match: _inline_code_replacement(match.group(1)), body)
    body = body.replace("**", "").replace("__", "").replace("*", "")
    body = body.lstrip("#> ").strip()
    if body.endswith(":"):
        body = body[:-1] + "."
    body = re.sub(r"\s+", " ", body).strip()
    return f"{body}{newline}" if body else ""


def _inline_code_replacement(value: str) -> str:
    text = value.strip()
    if _looks_like_filename(text):
        return "the file"
    if _looks_like_command(text):
        return "the command"
    if _looks_like_identifier(text):
        return "the implementation detail"
    return "the detail"


def _looks_like_filename(text: str) -> bool:
    suffixes = (
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".json",
        ".md",
        ".toml",
        ".yaml",
        ".yml",
        ".txt",
        ".sh",
    )
    return text.endswith(suffixes) or "/" in text or "\\" in text


def _looks_like_command(text: str) -> bool:
    commands = ("python ", "uv ", "npm ", "pnpm ", "yarn ", "node ", "git ", "curl ", "pytest ")
    return text.startswith(commands)


def _looks_like_identifier(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?", text)) or "_" in text


def _looks_like_code_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    starts = (
        "import ",
        "from ",
        "def ",
        "class ",
        "function ",
        "const ",
        "let ",
        "var ",
        "return ",
        "#!/",
        "$ ",
        "python ",
        "uv ",
        "npm ",
        "pnpm ",
        "yarn ",
        "node ",
        "git ",
        "curl ",
        "pytest ",
    )
    if stripped.startswith(starts):
        return True
    markers = ("=>", "::", "==", "!=", "&&", "||", "();", "{", "}", "</", "/>")
    return sum(marker in stripped for marker in markers) >= 2
