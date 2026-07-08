from __future__ import annotations

import argparse
import time
from pathlib import Path

from opencode_voice.managed_opencode import (
    load_leases,
    process_alive,
    remove_lease,
    terminate_process_group,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean up a Mortic-owned managed OpenCode server if its helper dies.")
    parser.add_argument("--lease-path", required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--helper-pid", required=True, type=int)
    parser.add_argument("--managed-pid", required=True, type=int)
    parser.add_argument("--pgid", required=True, type=int)
    parser.add_argument("--stale-after", type=float, default=15.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args(argv)

    lease_path = Path(args.lease_path)
    while True:
        lease = next((item for item in load_leases(lease_path)["leases"] if item.get("id") == args.lease_id), None)
        if lease is None:
            return 0
        heartbeat_at = float(lease.get("heartbeat_at") or 0)
        heartbeat_stale = heartbeat_at > 0 and time.time() - heartbeat_at > args.stale_after
        if not process_alive(args.helper_pid) or heartbeat_stale:
            terminate_process_group(args.pgid, managed_pid=args.managed_pid)
            remove_lease(args.lease_id, lease_path)
            return 0
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
