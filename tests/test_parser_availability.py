"""Parser availability tests; no Docker or document-processing job is started."""

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from folio import sandbox


class ParserAvailability(unittest.TestCase):
    def test_missing_docker_is_typed_before_reserving_capacity_or_spawning(self):
        slots = threading.BoundedSemaphore(2)
        with (
            patch.object(sandbox, "SLOTS", slots),
            patch("shutil.which", return_value=None),
            patch.object(
                sandbox.subprocess, "run", side_effect=FileNotFoundError()
            ) as spawn,
        ):
            try:
                with self.assertRaisesRegex(
                    ValueError, "^PARSER_UNAVAILABLE_OR_TIMEOUT$"
                ):
                    sandbox.parse_isolated(b"authored,fixture\n1,2\n", "invoice.csv")
            except FileNotFoundError:
                self.fail("MISSING_DOCKER_MUST_NOT_ESCAPE_OR_LEAK_A_PARSER_SLOT")
        spawn.assert_not_called()
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False))
        slots.release()
        slots.release()

    def test_the_resolved_cli_is_used_for_launch_inspection_and_cleanup(self):
        import hashlib
        import json
        from types import SimpleNamespace

        executable = "C:/authored-public-fixture/Docker/docker.exe"
        raw = b"key,value\nquantity,100\n"
        text = raw.decode("utf-8")
        parsed = {
            "kind": "csv",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "pages": [
                {
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "words": [],
                }
            ],
            "csv": {"encoding": "utf-8"},
            "cells": [],
        }
        state = {
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "Memory": 536870912,
                "MemorySwap": 536870912,
                "Binds": [],
                "CapDrop": ["ALL"],
                "PidsLimit": 64,
                "CpuQuota": 100000,
                "CpuPeriod": 100000,
                "Ulimits": [{"Name": "cpu", "Hard": 60, "Soft": 60}],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=256m"},
            },
            "Config": {"User": "65534:65534"},
            "Image": "authored-unit-image",
            "State": {"ExitCode": 0},
        }
        replies = [
            SimpleNamespace(returncode=0, stdout=json.dumps(parsed).encode()),
            SimpleNamespace(stdout=json.dumps([state]).encode()),
            SimpleNamespace(returncode=0),
        ]

        def run_spy(*args, **kwargs):
            reply = replies.pop(0)
            if kwargs.get("stdout") is not None and hasattr(reply, "stdout"):
                kwargs["stdout"].write(reply.stdout)
            return reply

        with (
            patch("shutil.which", return_value=executable) as resolve,
            patch.object(sandbox.subprocess, "run", side_effect=run_spy) as spawn,
        ):
            result = sandbox.parse_isolated(raw, "receipt.csv")
        resolve.assert_called_once_with("docker")
        self.assertEqual(result["sha256"], parsed["sha256"])
        self.assertEqual(spawn.call_count, 3)
        for call in spawn.call_args_list:
            self.assertEqual(call.args[0][0], executable)
            self.assertFalse(call.kwargs.get("shell", False))
        command = spawn.call_args_list[0].args[0]
        for option, value in [
            ("--network", "none"),
            ("--memory", "512m"),
            ("--memory-swap", "512m"),
            ("--pids-limit", "64"),
            ("--ulimit", "cpu=60:60"),
            ("--user", "65534:65534"),
        ]:
            self.assertEqual(command[command.index(option) + 1], value)
        self.assertIn("--read-only", command)
        self.assertIn("--network", command)
        self.assertIn("/tmp:rw,noexec,nosuid,size=256m", command)

    def test_actual_intake_route_reports_missing_parser_without_database_writes(self):
        import base64

        from fastapi.testclient import TestClient

        from folio import workbench

        body = {
            "rights_attestation": "authored-synthetic",
            "files": [
                {
                    "name": "authored-invoice.csv",
                    "document_role": "invoice",
                    "base64": base64.b64encode(b"key,value\nquantity,100\n").decode(),
                }
            ],
        }
        with (
            patch("shutil.which", return_value=None),
            # This route-level test supplies the required writable restore
            # state explicitly; restore-mode fail-closed behavior is tested
            # separately by the dedicated restore admission suite.
            patch.object(workbench, "_restore_mode_probe", return_value=False),
            patch.object(workbench.app, "middleware_stack", None),
            patch.object(
                workbench, "identity", return_value={"username": "unit-reviewer"}
            ),
            patch.object(
                workbench, "connect", side_effect=AssertionError("NO_DB_WRITE")
            ) as connect,
            patch.object(
                sandbox.subprocess, "run", side_effect=AssertionError("NO_CONTAINER")
            ) as spawn,
        ):
            client = TestClient(workbench.app, raise_server_exceptions=False)
            try:
                response = client.post(
                    f"/api/v1/workspaces/{workbench.WORKSPACE}/intakes/files",
                    json=body,
                )
            finally:
                client.close()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "NO_ACCEPTED_FILES")
        self.assertEqual(
            response.json()["detail"]["rejected"][0]["reason"],
            "PARSER_UNAVAILABLE_OR_TIMEOUT",
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        connect.assert_not_called()
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
