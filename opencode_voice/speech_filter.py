from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

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


class SpeechTextFilter:
    def __init__(self) -> None:
        self.in_fence = False
        self.pending = ""
        self.early_chars = 120

    def push(self, text: str) -> str:
        self.pending += text
        lines = self.pending.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.pending = lines.pop()
        else:
            self.pending = ""
        return "".join(self._filter_line(line) for line in lines) + self._drain_pending_prose()

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

    def _drain_pending_prose(self) -> str:
        if not self.pending or self.in_fence:
            return ""
        stripped = self.pending.lstrip()
        if stripped.startswith(("```", "~~~")) or _drop_line_for_speech(self.pending):
            return ""
        split_at = _first_sentence_end(self.pending)
        if split_at is None and len(self.pending) >= self.early_chars:
            split_at = self.pending.rfind(" ", 0, self.early_chars)
            if split_at <= 0:
                split_at = self.early_chars
        if split_at is None:
            return ""
        chunk = self.pending[:split_at]
        self.pending = self.pending[split_at:].lstrip()
        return _sanitize_spoken_line(chunk)


_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[\.)]\s+)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_RAW_URL_RE = re.compile(r"https?://\S+")


def _first_sentence_end(text: str) -> int | None:
    for index, char in enumerate(text):
        if char in ".!?\n":
            next_index = index + 1
            if next_index == len(text) or text[next_index].isspace():
                return next_index
    return None


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
