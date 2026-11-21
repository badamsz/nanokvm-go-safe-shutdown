"""Clean power-off of the NanoKVM Go itself.

Pulling power from a running device can corrupt its eMMC filesystems and the
shared exFAT data image. This module powers off through systemd (services stop,
filesystems are synced and unmounted) after checking that nothing is mid-write:

* the device's own storage lock (``/run/nanokvm-storage.lock``) is held
  shared by every local file operation and exclusively by USB disk rebuilds;
  we take it exclusively and keep it until the process dies, so no new
  transfer can start during shutdown;
* if the exFAT data image is attached to the target as a USB drive, the
  target may have unflushed writes on it, so the user is warned to eject it
  on the target first (or shut the target down first).
"""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

LOCK_PATH = "/run/nanokvm-storage.lock"
GADGET = "/sys/kernel/config/usb_gadget/g0/functions"
DATA_DISK_LUN = GADGET + "/mass_storage.disk1/lun.0/file"    # /exfat.img shared with target
IMAGE_DISK_LUN = GADGET + "/mass_storage.disk0/lun.0/file"   # mounted ISO/IMG (read-only)


@dataclass
class Preflight:
    blocking: str = ""                    # non-empty: power-off is refused
    warnings: List[str] = field(default_factory=list)
    data_disk_shared: bool = False
    image_mounted: bool = False

    @property
    def ok(self) -> bool:
        return not self.blocking


def _backing(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            return stream.read().strip()
    except OSError:
        return ""


class KvmPower:
    def __init__(self, lock_path: str = LOCK_PATH, data_lun: str = DATA_DISK_LUN,
                 image_lun: str = IMAGE_DISK_LUN, runner=subprocess.run):
        self.lock_path = lock_path
        self.data_lun = data_lun
        self.image_lun = image_lun
        self._run = runner
        self._lock_fd: Optional[int] = None

    # -- checks -------------------------------------------------------------
    def lock_busy(self) -> bool:
        """True if a file transfer or disk rebuild currently holds the lock."""
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False   # no lock file possible -> nothing coordinates through it
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return False

    def preflight(self, target_video_on: Optional[bool] = None) -> Preflight:
        result = Preflight()
        if self.lock_busy():
            result.blocking = "A file transfer or virtual disk change is in progress. Let it finish first."
            return result
        result.data_disk_shared = bool(_backing(self.data_lun))
        result.image_mounted = bool(_backing(self.image_lun))
        if result.data_disk_shared and target_video_on is not False:
            result.warnings.append("The data disk is attached to the target. Eject it there first or the target's writes may be lost.")
        if result.image_mounted and target_video_on is not False:
            result.warnings.append("A disk image is mounted on the target; it will disappear.")
        result.warnings.append("The KVM stops responding until you unplug and replug its power.")
        return result

    # -- action -------------------------------------------------------------
    def hold_storage_lock(self) -> bool:
        """Take the storage lock exclusively and keep it for the process lifetime."""
        if self._lock_fd is not None:
            return True
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def power_off(self) -> None:
        """Sync and ask systemd to power off. Raises RuntimeError on failure."""
        if not self.hold_storage_lock():
            raise RuntimeError("storage became busy; power-off cancelled")
        os.sync()
        commands = []
        if shutil.which("systemctl"):
            commands.append(["systemctl", "poweroff"])
        commands.append(["poweroff"])
        errors = []
        for command in commands:
            try:
                completed = self._run(command, timeout=30, capture_output=True, text=True)
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append("%s: %s" % (command[0], exc))
                continue
            if completed.returncode == 0:
                return
            errors.append("%s exited %d: %s" % (command[0], completed.returncode,
                                                (completed.stderr or "").strip()[:120]))
        self.release()
        raise RuntimeError("; ".join(errors) or "no power-off command available")

    def release(self):
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            finally:
                self._lock_fd = None
