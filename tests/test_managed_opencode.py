from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from opencode_voice import __main__ as helper_main
from opencode_voice import managed_opencode


class ManagedOpenCodeStartupTests(unittest.TestCase):
    def test_helper_pins_uvicorn_to_standard_asyncio_for_happy_eyeballs(self) -> None:
        app = object()
        with (
            patch.object(helper_main, "load_local_dotenv"),
            patch.object(helper_main, "preflight_startup"),
            patch.object(helper_main, "create_app", return_value=app),
            patch.object(helper_main.uvicorn, "run") as run,
        ):
            result = helper_main.main(["--opencode-url", "http://127.0.0.1:4096"])

        self.assertEqual(result, 0)
        self.assertIs(run.call_args.args[0], app)
        self.assertEqual(run.call_args.kwargs["loop"], "asyncio")

    def test_start_managed_opencode_uses_owned_process_group(self) -> None:
        fake_process = SimpleNamespace(pid=4242, poll=lambda: None)
        with (
            patch.object(helper_main, "free_port", return_value=43210),
            patch.object(helper_main, "is_healthy", return_value=True),
            patch.object(helper_main.subprocess, "Popen", return_value=fake_process) as popen,
        ):
            url, process = helper_main.start_managed_opencode(model_name="inception/mercury-2")

        self.assertEqual(url, "http://127.0.0.1:43210")
        self.assertIs(process, fake_process)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.args[0][0:2], [
            "opencode",
            "serve",
        ])
        self.assertEqual(
            popen.call_args.kwargs["env"]["BUN_OPTIONS"],
            "--dns-result-order=ipv4first",
        )
        self.assertIs(popen.call_args.kwargs["stdout"], helper_main.sys.stderr)

    def test_managed_opencode_preserves_existing_bun_options(self) -> None:
        fake_process = SimpleNamespace(pid=4242, poll=lambda: None)
        with (
            patch.dict(os.environ, {"BUN_OPTIONS": "--smol"}, clear=False),
            patch.object(helper_main, "free_port", return_value=43210),
            patch.object(helper_main, "is_healthy", return_value=True),
            patch.object(helper_main.subprocess, "Popen", return_value=fake_process) as popen,
        ):
            helper_main.start_managed_opencode(model_name="inception/mercury-2")

        self.assertEqual(
            popen.call_args.kwargs["env"]["BUN_OPTIONS"],
            "--smol --dns-result-order=ipv4first",
        )


class ManagedOpenCodeLeaseTests(unittest.TestCase):
    def test_lease_records_heartbeat_metadata_and_removes_on_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leases.json"
            fake_process = SimpleNamespace(pid=12345)
            with patch.object(managed_opencode, "process_group_id", return_value=12345):
                lease = managed_opencode.ManagedOpenCodeLease(
                    process=fake_process,
                    url="http://127.0.0.1:43210",
                    workspace="/repo/worktree",
                    path=path,
                    heartbeat_interval=60,
                    watchdog=False,
                ).start()
            data = managed_opencode.load_leases(path)
            self.assertEqual(len(data["leases"]), 1)
            record = data["leases"][0]
            self.assertEqual(record["helper_pid"], os.getpid())
            self.assertEqual(record["managed_pid"], 12345)
            self.assertEqual(record["pgid"], 12345)
            self.assertEqual(record["workspace"], "/repo/worktree")
            self.assertEqual(record["url"], "http://127.0.0.1:43210")
            self.assertIsNotNone(record["heartbeat_at"])

            lease.close()

            self.assertEqual(managed_opencode.load_leases(path)["leases"], [])

    def test_reaper_terminates_stale_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leases.json"
            managed_opencode.write_leases(
                {
                    "leases": [
                        {
                            "id": "lease-owned",
                            "helper_pid": 999999,
                            "managed_pid": os.getpid(),
                            "pgid": os.getpgid(os.getpid()),
                            "heartbeat_at": 1,
                        }
                    ]
                },
                path,
            )
            with (
                patch.object(managed_opencode, "is_owned_opencode_group", return_value=True),
                patch.object(managed_opencode, "terminate_process_group", return_value=True) as terminate,
            ):
                results = managed_opencode.reap_stale_managed_opencode_leases(path=path, stale_after=1)

            self.assertEqual(results[0].action, "killed")
            terminate.assert_called_once_with(os.getpgid(os.getpid()), managed_pid=os.getpid())
            self.assertEqual(managed_opencode.load_leases(path)["leases"], [])

    def test_reaper_does_not_kill_stale_unrelated_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leases.json"
            managed_opencode.write_leases(
                {
                    "leases": [
                        {
                            "id": "lease-unrelated",
                            "helper_pid": 999999,
                            "managed_pid": os.getpid(),
                            "pgid": os.getpgid(os.getpid()),
                            "heartbeat_at": 1,
                        }
                    ]
                },
                path,
            )
            with (
                patch.object(managed_opencode, "is_owned_opencode_group", return_value=False),
                patch.object(managed_opencode, "terminate_process_group") as terminate,
            ):
                results = managed_opencode.reap_stale_managed_opencode_leases(path=path, stale_after=1)

            self.assertEqual(results[0].action, "removed")
            self.assertIn("did not match", results[0].detail)
            terminate.assert_not_called()
            self.assertEqual(managed_opencode.load_leases(path)["leases"], [])

    def test_reaper_keeps_stale_owned_lease_when_termination_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leases.json"
            managed_opencode.write_leases(
                {
                    "leases": [
                        {
                            "id": "lease-retry",
                            "helper_pid": 999999,
                            "managed_pid": os.getpid(),
                            "pgid": os.getpgid(os.getpid()),
                            "heartbeat_at": 1,
                        }
                    ]
                },
                path,
            )
            with (
                patch.object(managed_opencode, "is_owned_opencode_group", return_value=True),
                patch.object(managed_opencode, "terminate_process_group", return_value=False),
            ):
                results = managed_opencode.reap_stale_managed_opencode_leases(path=path, stale_after=1)

            self.assertEqual(results[0].action, "failed")
            self.assertEqual(managed_opencode.load_leases(path)["leases"][0]["id"], "lease-retry")

    def test_heartbeat_loop_survives_transient_lease_write_error(self) -> None:
        fake_process = SimpleNamespace(pid=os.getpid())
        second_heartbeat = threading.Event()
        calls = 0

        def flaky_update(*_args, **_kwargs) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary write failure")
            second_heartbeat.set()

        lease = managed_opencode.ManagedOpenCodeLease(
            process=fake_process,
            url="http://127.0.0.1:43210",
            workspace="/repo/worktree",
            heartbeat_interval=0.01,
            watchdog=False,
        )
        with patch.object(managed_opencode, "update_lease", side_effect=flaky_update):
            thread = threading.Thread(target=lease._heartbeat_loop, daemon=True)
            lease._thread = thread
            thread.start()
            try:
                self.assertTrue(second_heartbeat.wait(timeout=1.0))
            finally:
                lease.close(remove=False)


if __name__ == "__main__":
    unittest.main()
