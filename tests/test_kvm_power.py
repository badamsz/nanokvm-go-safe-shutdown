"""Tests for kvm_power. Run: python3 -m unittest discover -s tests"""

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "safe-shutdown"))

import kvm_power  # noqa: E402


class Recorder:
    def __init__(self, codes):
        self.codes = list(codes)
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        code = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(command, code, "", "boom" if code else "")


class KvmPowerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.lock = str(d / "storage.lock")
        self.data = d / "disk1"
        self.image = d / "disk0"
        self.data.write_text("")
        self.image.write_text("")

    def tearDown(self):
        self.dir.cleanup()

    def power(self, runner=None):
        return kvm_power.KvmPower(self.lock, str(self.data), str(self.image),
                                  runner or Recorder([0]))

    def test_clean_state(self):
        check = self.power().preflight(True)
        self.assertTrue(check.ok)
        self.assertFalse(check.data_disk_shared)
        self.assertEqual(len(check.warnings), 1)   # only "stops responding"

    def test_busy_lock_blocks(self):
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_SH)   # a file transfer in progress
        try:
            check = self.power().preflight(True)
            self.assertFalse(check.ok)
            self.assertIn("transfer", check.blocking)
        finally:
            os.close(fd)

    def test_shared_data_disk_warns_while_target_on(self):
        self.data.write_text("/exfat.img\n")
        on = self.power().preflight(True)
        self.assertTrue(on.data_disk_shared)
        self.assertTrue(any("Eject" in w for w in on.warnings))
        off = self.power().preflight(False)     # target already shut down
        self.assertFalse(any("Eject" in w for w in off.warnings))

    def test_power_off_uses_systemctl_and_holds_lock(self):
        runner = Recorder([0])
        power = self.power(runner)
        power.power_off()
        self.assertEqual(runner.commands[0][-1], "poweroff")
        # lock stays held so no transfer can start during shutdown
        self.assertTrue(kvm_power.KvmPower(self.lock, str(self.data), str(self.image)).lock_busy())
        power.release()
        self.assertFalse(self.power().lock_busy())

    def test_falls_back_then_reports_failure(self):
        runner = Recorder([1, 1])
        power = self.power(runner)
        with self.assertRaises(RuntimeError):
            power.power_off()
        self.assertGreaterEqual(len(runner.commands), 1)
        self.assertFalse(self.power().lock_busy())   # released on failure

    def test_power_off_refused_if_lock_taken(self):
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_SH)
        runner = Recorder([0])
        try:
            with self.assertRaises(RuntimeError):
                self.power(runner).power_off()
            self.assertEqual(runner.commands, [])
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
