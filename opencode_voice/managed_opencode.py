from __future__ import annotations

"""Lifecycle hygiene for Mortic-owned managed OpenCode servers."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LEASE_ENV = "MORTIC_MANAGED_OPENCODE_LEASE_PATH"
DEFAULT_LEASE_PATH = Path("~/.mortic/managed-opencode-leases.json")
LEASE_VERSION = 1


@dataclass(frozen=True)
class ReapResult:
    lease_id: str
    action: str
    detail: str


def default_lease_path() -> Path:
    return Path(os.environ.get(LEASE_ENV) or DEFAULT_LEASE_PATH).expanduser()


def process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_command(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
    except Exception:
        return ""


def process_group_id(pid: int) -> int:
    try:
        return int(os.getpgid(pid))
    except Exception:
        return int(pid)


def is_managed_opencode_process(pid: int) -> bool:
    command = process_command(pid).lower()
    return "opencode" in command and "serve" in command


def is_owned_opencode_group(managed_pid: int, pgid: int) -> bool:
    if not process_alive(managed_pid):
        return False
    try:
        if os.getpgid(managed_pid) != pgid:
            return False
    except Exception:
        return False
    return is_managed_opencode_process(managed_pid)


def terminate_process_group(pgid: int, *, managed_pid: int | None = None, timeout: float = 5.0) -> bool:
    if pgid <= 1:
        return False
    if managed_pid is not None and not is_owned_opencode_group(managed_pid, pgid):
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        return False

    deadline = time.time() + timeout
    while managed_pid and process_alive(managed_pid) and time.time() < deadline:
        time.sleep(0.1)
    if managed_pid and process_alive(managed_pid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            return False
    return True


def load_leases(path: Path | None = None) -> dict[str, Any]:
    lease_path = path or default_lease_path()
    try:
        raw = json.loads(lease_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": LEASE_VERSION, "leases": []}
    except Exception:
        return {"version": LEASE_VERSION, "leases": []}
    if not isinstance(raw, dict):
        return {"version": LEASE_VERSION, "leases": []}
    leases = raw.get("leases")
    if not isinstance(leases, list):
        leases = []
    return {"version": LEASE_VERSION, "leases": [lease for lease in leases if isinstance(lease, dict)]}


def write_leases(data: dict[str, Any], path: Path | None = None) -> None:
    lease_path = path or default_lease_path()
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{lease_path.name}.", suffix=".tmp", dir=str(lease_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": LEASE_VERSION, "leases": data.get("leases", [])}, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, lease_path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def update_lease(lease_id: str, patch: dict[str, Any], path: Path | None = None) -> None:
    data = load_leases(path)
    changed = False
    for lease in data["leases"]:
        if lease.get("id") == lease_id:
            lease.update(patch)
            changed = True
            break
    if changed:
        write_leases(data, path)


def remove_lease(lease_id: str, path: Path | None = None) -> None:
    data = load_leases(path)
    kept = [lease for lease in data["leases"] if lease.get("id") != lease_id]
    if len(kept) != len(data["leases"]):
        data["leases"] = kept
        write_leases(data, path)


def _lease_is_stale(lease: dict[str, Any], *, now: float, stale_after: float) -> bool:
    helper_pid = _as_int(lease.get("helper_pid"))
    heartbeat_at = _as_float(lease.get("heartbeat_at"))
    return not process_alive(helper_pid) or (heartbeat_at is not None and now - heartbeat_at > stale_after)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def reap_stale_managed_opencode_leases(
    *,
    path: Path | None = None,
    stale_after: float = 30.0,
) -> list[ReapResult]:
    lease_path = path or default_lease_path()
    data = load_leases(lease_path)
    now = time.time()
    kept: list[dict[str, Any]] = []
    results: list[ReapResult] = []
    changed = False

    for lease in data["leases"]:
        lease_id = str(lease.get("id") or "")
        if not _lease_is_stale(lease, now=now, stale_after=stale_after):
            kept.append(lease)
            continue

        changed = True
        managed_pid = _as_int(lease.get("managed_pid"))
        pgid = _as_int(lease.get("pgid"))
        if not managed_pid or not pgid or not process_alive(managed_pid):
            results.append(ReapResult(lease_id, "removed", "managed process is already gone"))
            continue
        if not is_owned_opencode_group(managed_pid, pgid):
            results.append(ReapResult(lease_id, "removed", "stale lease did not match an owned opencode serve"))
            continue
        killed = terminate_process_group(pgid, managed_pid=managed_pid)
        results.append(
            ReapResult(
                lease_id,
                "killed" if killed else "failed",
                "terminated stale managed opencode process group" if killed else "failed to terminate process group",
            )
        )
        if not killed:
            kept.append(lease)

    if changed:
        data["leases"] = kept
        write_leases(data, lease_path)
    return results


class ManagedOpenCodeLease:
    def __init__(
        self,
        *,
        process: subprocess.Popen[Any],
        url: str,
        workspace: str | None,
        path: Path | None = None,
        heartbeat_interval: float = 2.0,
        watchdog: bool = True,
        watchdog_stale_after: float = 15.0,
    ) -> None:
        self.process = process
        self.url = url
        self.workspace = workspace
        self.path = path or default_lease_path()
        self.heartbeat_interval = heartbeat_interval
        self.watchdog = watchdog
        self.watchdog_stale_after = watchdog_stale_after
        self.lease_id = f"{os.getpid()}:{process.pid}:{uuid.uuid4().hex}"
        self.pgid = process_group_id(process.pid)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "ManagedOpenCodeLease":
        now = time.time()
        data = load_leases(self.path)
        data["leases"].append(
            {
                "id": self.lease_id,
                "helper_pid": os.getpid(),
                "managed_pid": self.process.pid,
                "pgid": self.pgid,
                "workspace": self.workspace,
                "url": self.url,
                "started_at": now,
                "heartbeat_at": now,
                "watchdog_pid": None,
            }
        )
        write_leases(data, self.path)
        watchdog_pid = self._start_watchdog()
        if watchdog_pid:
            update_lease(self.lease_id, {"watchdog_pid": watchdog_pid}, self.path)
        self._thread = threading.Thread(target=self._heartbeat_loop, name="mortic-opencode-heartbeat", daemon=True)
        self._thread.start()
        return self

    def _start_watchdog(self) -> int | None:
        if not self.watchdog:
            return None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "opencode_voice.managed_watchdog",
                    "--lease-path",
                    str(self.path),
                    "--lease-id",
                    self.lease_id,
                    "--helper-pid",
                    str(os.getpid()),
                    "--managed-pid",
                    str(self.process.pid),
                    "--pgid",
                    str(self.pgid),
                    "--stale-after",
                    str(self.watchdog_stale_after),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return int(process.pid)
        except Exception:
            return None

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            try:
                update_lease(self.lease_id, {"heartbeat_at": time.time()}, self.path)
            except Exception as exc:  # noqa: BLE001 - a transient write failure must not stop heartbeats forever.
                print(f"mortic managed opencode heartbeat failed: {type(exc).__name__}", file=sys.stderr)

    def close(self, *, remove: bool = True) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(0.1, self.heartbeat_interval + 0.5))
        if remove:
            remove_lease(self.lease_id, self.path)


def terminate_managed_process(process: subprocess.Popen[Any], *, timeout: float = 5.0) -> bool:
    pgid = process_group_id(process.pid)
    if terminate_process_group(pgid, managed_pid=process.pid, timeout=timeout):
        try:
            process.wait(timeout=timeout)
        except Exception:
            pass
        return True
    try:
        process.terminate()
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        return True
    except Exception:
        return False
