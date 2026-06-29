from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DRACO_URL = "https://huggingface.co/datasets/perplexity-ai/draco/resolve/main/test.jsonl"


@dataclass(frozen=True)
class DracoTask:
    task_id: str
    prompt: str
    domain: str | None
    raw: dict[str, Any]


def download_draco(cache_path: str | Path, url: str = DEFAULT_DRACO_URL, overwrite: bool = False) -> Path:
    output_path = Path(cache_path)
    if output_path.exists() and not overwrite:
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=output_path.name, suffix=".tmp", dir=str(output_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        urllib.request.urlretrieve(url, tmp_path)
        tmp_path.replace(output_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return output_path


def load_draco_tasks(cache_path: str | Path, limit: int | None = None, start: int = 0) -> list[DracoTask]:
    path = Path(cache_path)
    tasks: list[DracoTask] = []
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index < start:
                continue
            if limit is not None and len(tasks) >= limit:
                break
            if not line.strip():
                continue
            row = json.loads(line)
            tasks.append(task_from_row(row, index=index))
    return tasks


def task_from_row(row: dict[str, Any], index: int | None = None) -> DracoTask:
    task_id = str(row.get("id") or index or "")
    prompt = row.get("problem")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"DRACO row {task_id!r} is missing a non-empty 'problem' prompt")
    domain = row.get("domain")
    return DracoTask(
        task_id=task_id,
        prompt=prompt,
        domain=domain if isinstance(domain, str) else None,
        raw=row,
    )


def iter_jsonl(path: str | Path) -> Iterable[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)
