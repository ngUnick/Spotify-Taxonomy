from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = ROOT / "scripts" / "catalogue-operator-exec.ps1"
STATUS = ROOT / "scripts" / "catalogue-operator-invocation-status.ps1"
POWERSHELL = "powershell.exe"


class CatalogueOperatorReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name) / "runtime"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _wrapper_command(self, child_command: str) -> list[str]:
        return [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(EXECUTOR),
            "-DataDir",
            str(self.runtime),
            "-Purpose",
            "synthetic-receipt-test",
            POWERSHELL,
            "-NoProfile",
            "-Command",
            child_command,
        ]

    def _receipts(self) -> list[Path]:
        return list((self.runtime / "operator-invocations").glob("*.json"))

    def _receipt(self) -> dict[str, object]:
        receipts = self._receipts()
        self.assertEqual(1, len(receipts))
        return json.loads(receipts[0].read_text(encoding="utf-8-sig"))

    def test_successful_child_captures_durable_output_and_finished_receipt(self) -> None:
        result = subprocess.run(
            self._wrapper_command(
                "[Console]::Out.Write('synthetic stdout'); [Console]::Error.Write('synthetic stderr'); exit 0"
            ),
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        receipt = self._receipt()
        self.assertEqual("catalogue-operator-invocation-receipt.v1", receipt["schema"])
        self.assertEqual("finished", receipt["wrapper_state"])
        self.assertEqual(0, receipt["exit_code"])
        self.assertIsNotNone(receipt["child_pid"])
        self.assertIsNotNone(receipt["finished_at"])
        stdout_path = Path(str(receipt["stdout_path"]))
        stderr_path = Path(str(receipt["stderr_path"]))
        self.assertTrue(stdout_path.is_relative_to(self.runtime))
        self.assertTrue(stderr_path.is_relative_to(self.runtime))
        self.assertEqual("synthetic stdout", stdout_path.read_text(encoding="utf-8-sig"))
        self.assertEqual("synthetic stderr", stderr_path.read_text(encoding="utf-8-sig"))
        self.assertIn("invocation_id:", result.stdout)

    def test_failing_child_preserves_its_exit_code(self) -> None:
        result = subprocess.run(
            self._wrapper_command("[Console]::Error.Write('synthetic failure'); exit 7"),
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(7, result.returncode)
        receipt = self._receipt()
        self.assertEqual("finished", receipt["wrapper_state"])
        self.assertEqual(7, receipt["exit_code"])
        self.assertEqual("synthetic failure", Path(str(receipt["stderr_path"])).read_text(encoding="utf-8-sig"))

    def test_status_reports_running_receipt_until_child_actually_exits(self) -> None:
        process = subprocess.Popen(
            self._wrapper_command("Start-Sleep -Seconds 2; [Console]::Out.Write('finished later'); exit 0"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        receipt: dict[str, object] | None = None
        while time.monotonic() < deadline:
            receipts = self._receipts()
            if receipts:
                candidate = json.loads(receipts[0].read_text(encoding="utf-8-sig"))
                if candidate["wrapper_state"] == "running":
                    receipt = candidate
                    break
            time.sleep(0.05)
        self.assertIsNotNone(receipt, "the wrapper never published a running receipt")
        assert receipt is not None
        self.assertIsNone(receipt["finished_at"])
        self.assertIsNone(receipt["exit_code"])
        self.assertIsNotNone(receipt["child_pid"])

        status = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(STATUS),
                "-DataDir",
                str(self.runtime),
                "-InvocationId",
                str(receipt["invocation_id"]),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn("wrapper_state: running", status.stdout)
        self.assertIn("exit_code: -", status.stdout)

        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(0, process.returncode, stderr)
        finished = self._receipt()
        self.assertEqual("finished", finished["wrapper_state"])
        self.assertEqual(0, finished["exit_code"])
        self.assertIn("finished later", Path(str(finished["stdout_path"])).read_text(encoding="utf-8-sig"))
        self.assertIn("wrapper_state: finished", stdout)


if __name__ == "__main__":
    unittest.main()
