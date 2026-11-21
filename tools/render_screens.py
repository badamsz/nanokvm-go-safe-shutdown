#!/usr/bin/env python3
"""Render every App screen to PNG without a NanoKVM Go.

Points appbase at a plain file instead of /dev/fb0, drives the UI through
each state with a fake MCP client, and converts the RGB565 buffer to PNG
(rotated to match what you see on the device).

    python3 tools/render_screens.py --appbase /path/to/appbase.py --out docs/screens

Requires Pillow.
"""

import argparse
import importlib.util
import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "safe-shutdown"


def load_appbase(path):
    spec = importlib.util.spec_from_file_location("appbase", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["appbase"] = module
    spec.loader.exec_module(module)
    return module


class FakeTouch:
    def poll(self):
        return []

    def close(self):
        pass


def to_png(fb, path, scale=2):
    from PIL import Image
    image = Image.new("RGB", (fb.phys_w, fb.phys_h))
    pixels = image.load()
    for y in range(fb.phys_h):
        for x in range(fb.phys_w):
            (value,) = struct.unpack_from("<H", fb._buf, y * fb.stride + x * 2)
            r = (value >> 11) & 0x1F
            g = (value >> 5) & 0x3F
            b = value & 0x1F
            pixels[x, y] = (r * 255 // 31, g * 255 // 63, b * 255 // 31)
    # Physical column 0..13 is the hidden strip; crop it, then rotate so the
    # logical (portrait) canvas is upright.
    image = image.crop((14, 0, fb.phys_w, fb.phys_h))
    if fb.rotate == 90:
        image = image.rotate(90, expand=True)
    image = image.resize((image.width * scale, image.height * scale), Image.NEAREST)
    image.save(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--appbase", required=True)
    parser.add_argument("--out", default=str(ROOT / "docs" / "screens"))
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fb_file = tempfile.NamedTemporaryFile(delete=False)
    fb_file.write(b"\0" * (284 * 240 * 2))
    fb_file.close()
    os.environ["APPBASE_FB_DEVICE"] = fb_file.name
    os.environ["APPBASE_FB_ROTATE"] = "90"
    os.environ["SAFE_SHUTDOWN_LOG"] = ""

    appbase = load_appbase(args.appbase)
    sys.path.insert(0, str(APP))
    import shutdown_core as core
    import main as ui

    # Never start real network threads in the renderer.
    core.VideoProbe.start = lambda self: None

    import kvm_power

    class FakePower:
        def __init__(self, shared=False, blocking=""):
            self.shared, self.blocking, self.calls = shared, blocking, 0

        def preflight(self, target_video_on=None):
            check = kvm_power.Preflight(blocking=self.blocking, data_disk_shared=self.shared)
            if self.blocking:
                return check
            if self.shared and target_video_on is not False:
                check.warnings.append("The data disk is attached to the target. Eject it there "
                                      "first or the target's writes may be lost.")
            check.warnings.append("The KVM stops responding until you unplug and replug its power.")
            return check

        def power_off(self):
            self.calls += 1

    def make(env, state=("VIDEO", ""), power=None):
        fb = appbase.FrameBuffer()
        ctx = appbase.AppContext(fb, FakeTouch(), env)
        screen_app = ui.SafeShutdownApp(ctx, power=power or FakePower())
        screen_app.probe._state = state
        return fb, screen_app

    class FakeJob:
        def __init__(self, status):
            self.status = status

    env = {"TARGET_OS": "windows", "VERIFY_TIMEOUT": "180"}
    shots = []

    def shot(name, fb):
        shots.append((name, bytes(fb._buf)))

    fb, a = make(env); a.tick(0.1); shot("01-idle", fb)
    fb, a = make(env, ("ERROR", "MCP IS DISABLED")); a.tick(0.1); shot("02-idle-mcp-disabled", fb)
    fb, a = make(env); a.ask("target"); a.tick(0.1); shot("03-confirm-target", fb)
    fb, a = make(env, power=FakePower(shared=True)); a.ask("kvm"); a.tick(0.1); shot("04-confirm-kvm", fb)
    fb, a = make(env, power=FakePower(shared=True)); a.ask("both"); a.tick(0.1); shot("05-confirm-both", fb)
    fb, a = make(env, power=FakePower(blocking="A file transfer or virtual disk change is in progress. Let it finish first."))
    a.ask("kvm"); a.tick(0.1); shot("06-confirm-kvm-blocked", fb)
    assert "confirm" not in a.buttons, "blocked power-off must not offer CONFIRM"

    fb, a = make(env)
    a.screen = "running"
    a.job = FakeJob(core.JobStatus(phase="SENDING", step_index=3, step_count=7, step_label="KEY METALEFT+KEYR"))
    a.tick(0.1); shot("07-sending", fb)

    fb, a = make(env)
    a.screen, a.kvm_after_target = "running", True
    a.job = FakeJob(core.JobStatus(phase="WAITING", elapsed=23.4, timeout=180))
    a.tick(0.1); shot("08-waiting-both", fb)

    # BOTH flow: target reported off -> KVM power-off screen -> power_off() after a few frames
    power = FakePower()
    fb, a = make(env, power=power)
    a.screen, a.kvm_after_target = "running", True
    a.job = FakeJob(core.JobStatus(phase="OFF", elapsed=31))
    a.tick(0.1)
    assert a.screen == "kvm_off", a.screen
    shot("09-kvm-powering-off", fb)
    a.tick(0.1); a.tick(0.1)
    assert power.calls == 1, power.calls

    # TARGET-only success leaves the KVM running and offers it as a follow-up
    power = FakePower()
    fb, a = make(env, power=power)
    a.screen, a.kvm_after_target = "running", False
    a.job = FakeJob(core.JobStatus(phase="OFF", elapsed=31))
    a.tick(0.1); a.tick(0.1); a.tick(0.1); a.tick(0.1)
    assert a.screen == "result" and power.calls == 0
    assert "kvm" in a.buttons
    shot("10-target-off", fb)

    fb, a = make(env)
    a.screen, a.kvm_after_target = "result", True
    a.result = core.JobStatus(phase="TIMEOUT", timeout=180)
    a.tick(0.1); shot("11-timeout-both", fb)

    fb, a = make(env)
    a.screen, a.result = "result", core.JobStatus(phase="ERROR", error="KVM SERVICE DOWN")
    a.tick(0.1); shot("12-error", fb)

    fb, a = make(env)
    a.screen, a.kvm_error = "kvm_failed", "systemctl exited 1: Failed to power off system"
    a.tick(0.1); shot("13-kvm-failed", fb)

    fb, a = make({"TARGET_OS": "custom", "CUSTOM_SEQUENCE": "", "VERIFY_TIMEOUT": "180"})
    a.tick(0.1); shot("14-config-error", fb)
    assert "kvm" in a.buttons, "KVM power-off must stay available with a bad target profile"

    for old in out.glob("*.png"):
        old.unlink()
    for name, data in shots:
        fb._buf[:] = data
        to_png(fb, out / (name + ".png"))
        print(out / (name + ".png"))
    os.unlink(fb_file.name)


if __name__ == "__main__":
    main()
