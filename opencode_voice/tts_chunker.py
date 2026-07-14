from __future__ import annotations


class TTSChunker:
    def __init__(self, preferred_chars: int = 120, max_chars: int = 1200) -> None:
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
