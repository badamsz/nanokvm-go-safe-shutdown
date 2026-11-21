#!/usr/bin/env python3
"""Safe Shutdown - a NanoKVM Go touchscreen App.

Tap SHUT DOWN, confirm, and the App types an OS-specific shutdown sequence
into the target through the device's local MCP endpoint, then watches the
target's video signal until it disappears.
"""

import logging
import math
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from appbase import (BLACK, DKGRAY, GRAY, GREEN, ORANGE, RED, WHITE, YELLOW,  # noqa: E402
                     AppContext, Rect, app, rgb565)

import kvm_power  # noqa: E402
import shutdown_core as core  # noqa: E402

LOG_PATH = os.environ.get("SAFE_SHUTDOWN_LOG", "/tmp/safe-shutdown.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
    + ([logging.FileHandler(LOG_PATH)] if LOG_PATH else []),
)
log = logging.getLogger("safe-shutdown")

# Colours beyond the SDK set.
PANEL = rgb565(24, 26, 34)
DARK_RED = rgb565(150, 20, 20)
MID_GRAY = rgb565(80, 80, 80)

EDGE = 40            # host-reserved exit-gesture zone on each side
CONFIRM_SECONDS = 15
MCP_URL = os.environ.get("SAFE_SHUTDOWN_MCP_URL", core.DEFAULT_MCP_URL)

# ---------------------------------------------------------------------------
# Font additions. The SDK's 8x8 font only has A-Z, 0-9 and a few symbols;
# add the handful we need (glyphs from the public-domain font8x8_basic).
# ---------------------------------------------------------------------------
try:
    import appbase as _appbase
    _appbase._FONT.setdefault("+", [0x00, 0x0C, 0x0C, 0x3F, 0x0C, 0x0C, 0x00, 0x00])
    _appbase._FONT.setdefault("!", [0x18, 0x3C, 0x3C, 0x18, 0x18, 0x00, 0x18, 0x00])
    _appbase._FONT.setdefault("?", [0x1E, 0x33, 0x30, 0x18, 0x0C, 0x00, 0x0C, 0x00])
    _appbase._FONT.setdefault("(", [0x18, 0x0C, 0x06, 0x06, 0x06, 0x0C, 0x18, 0x00])
    _appbase._FONT.setdefault(")", [0x06, 0x0C, 0x18, 0x18, 0x18, 0x0C, 0x06, 0x00])
    _appbase._FONT.setdefault(",", [0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C, 0x06])
    _appbase._FONT.setdefault("_", [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF])
    _appbase._FONT.setdefault("'", [0x06, 0x06, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00])
    _GLYPHS = set(_appbase._FONT)
except Exception:  # SDK internals changed; fall back to the documented set
    _GLYPHS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 $%-./:")


def screen_text(text):
    """Upper-case and replace characters the panel font cannot draw."""
    out = []
    for ch in str(text).upper():
        out.append(ch if ch in _GLYPHS else " ")
    return " ".join("".join(out).split())


def wrap(text, width):
    words, lines, line = screen_text(text).split(" "), [], ""
    for word in words:
        while len(word) > width:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:width])
            word = word[width:]
        candidate = (line + " " + word).strip()
        if len(candidate) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def visible_rect(fb):
    if fb.rotate == 90:
        return Rect(0, 0, fb.width, fb.height - 14)
    if fb.rotate == 270:
        return Rect(0, 14, fb.width, fb.height - 14)
    if fb.rotate == 180:
        return Rect(0, 0, fb.width - 14, fb.height)
    return Rect(14, 0, fb.width - 14, fb.height)


def fill_circle(fb, cx, cy, r, color):
    for dy in range(-r, r + 1):
        half = int(math.sqrt(r * r - dy * dy))
        fb.fill_rect(cx - half, cy + dy, 2 * half + 1, 1, color)


def ring(fb, cx, cy, r, thickness, color, gap_deg=0):
    """Thick circle outline, optionally open at the top by gap_deg."""
    r_in = r - thickness
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            d2 = dx * dx + dy * dy
            if r_in * r_in <= d2 <= r * r:
                if gap_deg and dy < 0:
                    angle = math.degrees(math.atan2(abs(dx), -dy))
                    if angle < gap_deg / 2:
                        continue
                fb.put_pixel(cx + dx, cy + dy, color)


def power_icon(fb, cx, cy, r, color):
    t = max(3, r // 5)
    ring(fb, cx, cy, r, t, color, gap_deg=70)
    fb.fill_rect(cx - t // 2, cy - r - 1, t, r + 2, color)


def check_icon(fb, cx, cy, s, color):
    t = max(3, s // 6)
    for i in range(s // 2):
        fb.fill_rect(cx - s // 2 + i, cy + i - t // 2, t, t, color)
    for i in range(s):
        fb.fill_rect(cx + i - t // 2, cy + s // 2 - i - t // 2 - 1, t, t, color)


def button(fb, rect, label, bg, fg=WHITE, scale=2):
    label = screen_text(label)
    if len(label) * 8 * scale > rect.w - 8:
        scale = 1
    fb.fill_rect(rect.x, rect.y, rect.w, rect.h, bg)
    fb.text_center(rect.cx, rect.cy - 4 * scale, label, fg, scale)
    return rect


def bar(fb, x, y, w, h, fraction, fg, bg=DKGRAY):
    fraction = max(0.0, min(1.0, fraction))
    fb.fill_rect(x, y, w, h, bg)
    fb.fill_rect(x, y, int(w * fraction), h, fg)


def text_lines(fb, cx, y, lines, color, scale=1, gap=3):
    for line in lines:
        fb.text_center(cx, y, line, color, scale)
        y += 8 * scale + gap
    return y


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

SLATE = rgb565(40, 70, 120)

MODES = {
    # mode: (confirm title line 1, line 2)
    "target": ("SHUT DOWN", "THE TARGET?"),
    "kvm": ("POWER OFF", "THE NANOKVM?"),
    "both": ("SHUT DOWN", "BOTH?"),
}


class SafeShutdownApp:
    def __init__(self, ctx: AppContext, power=None):
        self.ctx = ctx
        self.fb = ctx.fb
        self.view = visible_rect(ctx.fb)
        self.cx = self.view.x + self.view.w // 2
        self.screen = "idle"
        self.mode = "target"
        self.buttons = {}
        self.confirm_left = 0.0
        self.preflight = None
        self.job = None
        self.result = None
        self.kvm_error = ""
        self.kvm_after_target = False
        self.kvm_ticks = 0
        self.config_error = ""
        self._client = None
        self.power = power or kvm_power.KvmPower()

        self.timeout = self._timeout_setting(ctx.env.get("VERIFY_TIMEOUT", "180"))
        try:
            self.profile = core.resolve_profile(ctx.env.get("TARGET_OS", ""),
                                                ctx.env.get("CUSTOM_SEQUENCE", ""))
            self.steps = core.parse_sequence(self.profile.sequence)
        except core.SequenceError as exc:
            log.error("configuration error: %s", exc)
            self.profile, self.steps = None, []
            self.config_error = str(exc)
            self.screen = "config"

        self.probe = core.VideoProbe(self.client)
        self.probe.start()
        log.info("started: profile=%s timeout=%ss",
                 self.profile.key if self.profile else "-", self.timeout)

    @staticmethod
    def _timeout_setting(value):
        try:
            return max(20, min(900, int(str(value).strip())))
        except ValueError:
            return 180

    def client(self):
        """Create the MCP client on first use (and retry until a key exists)."""
        if self._client is None:
            key = core.load_api_key(self.ctx.env.get("NANOKVM_MCP_KEY", ""))
            self._client = core.MCPClient(key, MCP_URL)
        return self._client

    def video_on(self):
        status = self.probe.state[0]
        return True if status == "VIDEO" else False if status == "NO VIDEO" else None

    # -- actions ------------------------------------------------------------
    def ask(self, mode):
        self.mode = mode
        self.preflight = None
        if mode in ("kvm", "both"):
            # For "both" the target will be off before the KVM goes down, so
            # only blocking conditions matter at this point.
            self.preflight = self.power.preflight(
                False if mode == "both" else self.video_on())
        self.screen, self.confirm_left = "confirm", CONFIRM_SECONDS

    def confirmed(self):
        if self.mode == "kvm":
            self.begin_kvm_power_off(after_target=False)
        else:
            self.kvm_after_target = self.mode == "both"
            self.start_job()

    def start_job(self):
        try:
            client = self.client()
        except core.MCPError as exc:
            self.result = core.JobStatus(phase="ERROR", error=exc.short, error_detail=str(exc))
            self.screen = "result"
            return
        self.probe.pause(True)
        self.job = core.ShutdownJob(client, self.steps, verify_timeout=self.timeout)
        log.info("target shutdown requested (%s, then kvm=%s)",
                 self.profile.key, self.kvm_after_target)
        self.job.start()
        self.screen = "running"

    def finish_job(self, status):
        log.info("job finished: phase=%s elapsed=%.0fs error=%s %s", status.phase,
                 status.elapsed, status.error, status.error_detail)
        self.result = status
        self.job = None
        self.probe.pause(False)
        if status.phase == "CANCELLED":
            self.screen = "idle"
        elif status.phase == "OFF" and self.kvm_after_target:
            self.begin_kvm_power_off(after_target=True)
        else:
            self.screen = "result"

    def begin_kvm_power_off(self, after_target):
        check = self.power.preflight(False if after_target else self.video_on())
        if not check.ok:
            self.kvm_error = check.blocking
            self.screen = "kvm_failed"
            return
        self.kvm_after_target = after_target
        self.probe.stop()
        # Draw the "powering off" screen first, then act on a later tick so
        # the panel already shows it when systemd stops the UI.
        self.kvm_ticks = 3
        self.screen = "kvm_off"

    def do_kvm_power_off(self):
        log.info("powering off the NanoKVM")
        try:
            self.power.power_off()
        except Exception as exc:
            log.error("power-off failed: %s", exc)
            self.kvm_error = str(exc)
            self.screen = "kvm_failed"
            self.probe = core.VideoProbe(self.client)
            self.probe.start()

    # -- input --------------------------------------------------------------
    def on_tap(self, x, y):
        if x < self.view.x + EDGE or x >= self.view.x + self.view.w - EDGE:
            return  # host exit-gesture zone; never act on these taps
        hit = next((name for name, rect in self.buttons.items() if rect.contains(x, y)), None)
        if hit is None:
            return
        if self.screen == "idle" and hit in MODES:
            self.ask(hit)
        elif self.screen == "config" and hit == "kvm":
            self.ask("kvm")
        elif self.screen == "confirm":
            if hit == "confirm":
                self.confirmed()
            elif hit == "cancel":
                self.screen = "config" if self.config_error else "idle"
        elif self.screen == "running" and hit == "stop" and self.job is not None:
            self.job.cancel()
        elif self.screen in ("result", "kvm_failed"):
            if hit == "retry":
                self.start_job()
            elif hit == "kvm":
                self.ask("kvm")
            elif hit == "done":
                self.screen = "config" if self.config_error else "idle"

    # -- frame ----------------------------------------------------------------
    def tick(self, dt):
        self.buttons = {}
        self.fb.clear(BLACK)
        if self.screen == "confirm":
            self.confirm_left -= dt
            if self.confirm_left <= 0:
                self.screen = "config" if self.config_error else "idle"
        if self.screen == "running" and self.job is not None:
            status = self.job.status
            if status.phase in ("OFF", "TIMEOUT", "ERROR", "CANCELLED"):
                self.finish_job(status)
        if self.screen == "kvm_off":
            self.kvm_ticks -= 1
            if self.kvm_ticks == 0:
                self.do_kvm_power_off()
        getattr(self, "draw_" + self.screen)()

    def top(self, offset):
        return self.view.y + offset

    def draw_header(self, title, color=WHITE):
        self.fb.text_center(self.cx, self.top(10), title, color, 2)

    def wide(self, y, h):
        return Rect(self.cx - 76, self.top(y), 152, h)

    def draw_idle(self):
        fb, cx = self.fb, self.cx
        self.draw_header("SAFE SHUTDOWN")
        fb.text_center(cx, self.top(32), "TARGET OS: " + self.profile.label, GRAY)

        status, error = self.probe.state
        label, color = {
            "VIDEO": ("TARGET ON", GREEN),
            "NO VIDEO": ("NO VIDEO", YELLOW),
            "ERROR": (error or "MCP ERROR", RED),
        }.get(status, ("CHECKING...", GRAY))
        fb.fill_rect(self.view.x + 16, self.top(46), self.view.w - 32, 34, PANEL)
        fill_circle(fb, self.view.x + 32, self.top(63), 5, color)
        label = screen_text(label)
        if len(label) <= 11:
            fb.draw_text(self.view.x + 46, self.top(56), label, color, 2)
        else:
            fb.draw_text(self.view.x + 46, self.top(60), label[:22], color, 1)

        if status == "ERROR":
            text_lines(fb, cx, self.top(86), wrap(self.error_hint(error), 28)[:2], ORANGE, gap=2)
        else:
            fb.text_center(cx, self.top(94), "SHUT DOWN:", GRAY)
        self.buttons["target"] = button(fb, self.wide(110, 44), "TARGET", RED)
        self.buttons["kvm"] = button(fb, self.wide(160, 44), "NANOKVM", SLATE)
        self.buttons["both"] = button(fb, self.wide(210, 44), "BOTH", DARK_RED)
        fb.text_center(cx, self.top(261), "EDGE SWIPE TO EXIT", MID_GRAY)

    def draw_confirm(self):
        fb, cx = self.fb, self.cx
        line1, line2 = MODES[self.mode]
        self.draw_header(line1, RED)
        fb.text_center(cx, self.top(32), line2, WHITE, 2)
        y = self.top(58)
        notes, color = [], GRAY
        if self.mode in ("target", "both"):
            fb.text_center(cx, y, "PROFILE: " + self.profile.label, GRAY)
            y += 14
        if self.mode == "target":
            notes = [self.profile.note]
            if self.probe.state[0] == "NO VIDEO":
                notes.append("No video right now - the target may be asleep or off.")
        elif self.mode == "both":
            notes = ["Target first. The NanoKVM powers off only after the target's video is gone."]
        if self.preflight is not None and not self.preflight.ok:
            notes, color = [self.preflight.blocking], ORANGE
        elif self.preflight is not None and self.mode == "kvm":
            notes = self.preflight.warnings
            color = YELLOW if self.preflight.data_disk_shared else GRAY
        lines = []
        for note in notes:
            lines += wrap(note, 28)
        text_lines(fb, cx, y, lines[:7], color, gap=2)
        if self.preflight is None or self.preflight.ok:
            self.buttons["confirm"] = button(fb, self.wide(158, 46), "CONFIRM", RED)
        self.buttons["cancel"] = button(fb, self.wide(212, 38), "CANCEL", DKGRAY)
        fb.text_center(cx, self.top(258), "AUTO CANCEL IN %dS" % math.ceil(self.confirm_left), MID_GRAY)

    def draw_running(self):
        fb, cx = self.fb, self.cx
        status = self.job.status if self.job else core.JobStatus()
        if status.phase in ("IDLE", "SENDING"):
            self.draw_header("SENDING KEYS", ORANGE)
            fb.text_center(cx, self.top(44), "STEP %d OF %d" % (status.step_index, status.step_count), WHITE, 2)
            bar(fb, self.view.x + 24, self.top(70), self.view.w - 48, 8,
                status.step_index / float(max(1, status.step_count)), ORANGE)
            text_lines(fb, cx, self.top(92), wrap(status.step_label, 28)[:2], GRAY)
            text_lines(fb, cx, self.top(130), ["DON'T USE THE TARGET'S", "KEYBOARD OR MOUSE NOW"], YELLOW)
        else:
            self.draw_header("WAITING FOR", ORANGE)
            fb.text_center(cx, self.top(32), "POWER OFF", ORANGE, 2)
            fb.text_center(cx, self.top(66), "%dS" % int(status.elapsed), WHITE, 4)
            bar(fb, self.view.x + 24, self.top(110), self.view.w - 48, 8,
                status.elapsed / float(max(1, status.timeout)), ORANGE)
            lines = ["WATCHING FOR THE VIDEO", "SIGNAL TO DROP. LIMIT %dS" % status.timeout]
            if self.kvm_after_target:
                lines.append("THEN THE NANOKVM POWERS OFF")
            text_lines(fb, cx, self.top(130), lines, GRAY)
        self.buttons["stop"] = button(fb, self.wide(208, 40), "STOP", DKGRAY)
        fb.text_center(cx, self.top(254), "STOP CAN'T UNDO SENT KEYS", MID_GRAY)

    def draw_result(self):
        fb, cx = self.fb, self.cx
        status = self.result or core.JobStatus(phase="ERROR", error="UNKNOWN")
        if status.phase == "OFF":
            fill_circle(fb, cx, self.top(56), 36, GREEN)
            check_icon(fb, cx - 2, self.top(52), 30, BLACK)
            fb.text_center(cx, self.top(102), "TARGET IS OFF", GREEN, 2)
            fb.text_center(cx, self.top(124), "VIDEO LOST AFTER %dS" % int(status.elapsed), GRAY)
            self.buttons["kvm"] = button(fb, self.wide(154, 46), "POWER OFF NANOKVM TOO", SLATE)
            self.buttons["done"] = button(fb, self.wide(210, 40), "DONE", DKGRAY)
            return
        if status.phase == "TIMEOUT":
            self.draw_header("STILL ON?", YELLOW)
            text = ("Video did not drop within %ds. The target may be locked, waiting on an app, "
                    "or showing a dialog. Check its screen." % int(status.timeout))
            if self.kvm_after_target:
                text += " The NanoKVM was left on."
            text_lines(fb, cx, self.top(40), wrap(text, 28)[:7], WHITE, gap=2)
        else:
            self.draw_header("FAILED", RED)
            y = text_lines(fb, cx, self.top(40), wrap(status.error or "ERROR", 15)[:2], WHITE, 2)
            text_lines(fb, cx, y + 6, wrap(self.error_hint(status.error, status.error_detail), 28)[:4], GRAY)
        self.buttons["retry"] = button(fb, self.wide(158, 46), "RETRY", ORANGE, BLACK)
        self.buttons["done"] = button(fb, self.wide(212, 38), "DONE", DKGRAY)

    def draw_kvm_off(self):
        fb, cx = self.fb, self.cx
        y = self.top(24)
        if self.kvm_after_target:
            fb.text_center(cx, self.top(12), "TARGET IS OFF", GREEN)
            y = self.top(34)
        power_icon(fb, cx, y + 34, 26, SLATE)
        fb.text_center(cx, self.top(104), "POWERING OFF", WHITE, 2)
        fb.text_center(cx, self.top(124), "THE NANOKVM", WHITE, 2)
        text_lines(fb, cx, self.top(158), ["WAIT 30S,", "THEN UNPLUG"], YELLOW, 2, gap=6)
        text_lines(fb, cx, self.top(220), ["THIS SCREEN MAY STAY", "LIT AFTER SHUTDOWN.",
                                           "REPLUG POWER TO START."], GRAY, gap=2)

    def draw_kvm_failed(self):
        fb, cx = self.fb, self.cx
        self.draw_header("KVM STILL ON", ORANGE)
        text_lines(fb, cx, self.top(40), wrap(self.kvm_error, 28)[:8], WHITE, gap=2)
        self.buttons["kvm"] = button(fb, self.wide(158, 46), "TRY AGAIN", SLATE)
        self.buttons["done"] = button(fb, self.wide(212, 38), "DONE", DKGRAY)

    def draw_config(self):
        fb, cx = self.fb, self.cx
        self.draw_header("SETTINGS ERROR", RED)
        y = text_lines(fb, cx, self.top(40), wrap(self.config_error, 28)[:7], WHITE)
        text_lines(fb, cx, y + 8, ["FIX IT IN THE WEB UI:", "SETTINGS / APPS / INSTALLED"], GRAY)
        self.buttons["kvm"] = button(fb, self.wide(212, 40), "POWER OFF NANOKVM", SLATE)

    @staticmethod
    def error_hint(short, detail=""):
        return {
            "MCP IS DISABLED": "Turn on MCP in the NanoKVM web UI, then try again.",
            "BAD MCP KEY": "Clear the App's MCP key setting or paste the current key.",
            "NO MCP KEY": "Turn on MCP in the NanoKVM web UI so a key is created.",
            "KVM SERVICE DOWN": "The NanoKVM service is not answering. Wait and retry.",
            "UNTYPEABLE TEXT": "Sequence text must be plain ASCII.",
            "KEYBOARD ERROR": detail or "The target rejected a key step.",
        }.get(short, detail or "See /tmp/safe-shutdown.log")


@app()
def main(ctx: AppContext) -> None:
    shutdown_app = SafeShutdownApp(ctx)
    try:
        ctx.run(shutdown_app.tick, fps=10, on_tap=shutdown_app.on_tap)
    finally:
        shutdown_app.probe.stop()
        if shutdown_app.job is not None:
            shutdown_app.job.cancel()


if __name__ == "__main__":
    main()
