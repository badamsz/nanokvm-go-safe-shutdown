"""Device-independent logic for the Safe Shutdown App.

Nothing in this module touches the framebuffer or touch panel, so it can be
unit-tested on any machine. It provides:

* ``MCPClient``     - a minimal JSON-RPC client for NanoKVM Go's local MCP
                      endpoint (``https://127.0.0.1/api/mcp``).
* ``parse_sequence``- a tiny step language for keyboard shutdown sequences.
* ``PROFILES``      - built-in sequences for common target operating systems.
* ``ShutdownJob``   - a background worker that sends a sequence and then
                      waits for the target's video signal to disappear.
* ``VideoProbe``    - a background poller for the idle-screen status line.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional

DEFAULT_MCP_URL = "https://127.0.0.1/api/mcp"
AI_CONFIG_PATH = "/etc/kvm/ai.json"
MCP_PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "nanokvm-safe-shutdown"
CLIENT_VERSION = "1.0.0"

# The server clamps press_keys holdMs to 1000 ms and type_text to 1024 runes.
MAX_HOLD_MS = 1000
MAX_TEXT_RUNES = 1024
MAX_WAIT_MS = 30000

# Screenshot retCode for "another capture is in progress". It says nothing
# about the target, so it never counts as a lost signal.
RET_SNAPSHOT_BUSY = -16


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MCPError(RuntimeError):
    """Any failure talking to the local MCP endpoint.

    ``short`` is an upper-case, screen-friendly summary for the 8x8 font.
    """

    def __init__(self, message: str, short: str = "MCP ERROR"):
        super().__init__(message)
        self.short = short


class SequenceError(ValueError):
    """A shutdown sequence could not be parsed."""


# ---------------------------------------------------------------------------
# API key discovery
# ---------------------------------------------------------------------------

def load_api_key(env_value: str = "", config_path: str = AI_CONFIG_PATH) -> str:
    """Return the MCP key from the App setting, else from the device config.

    The App runs as root on the device, so it can read the key that the web UI
    generated. An explicit setting always wins.
    """
    value = (env_value or "").strip()
    if value:
        return value
    try:
        with open(config_path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        raise MCPError("%s not found and NANOKVM_MCP_KEY is empty" % config_path,
                       "NO MCP KEY")
    except (OSError, ValueError) as exc:
        raise MCPError("cannot read %s: %s" % (config_path, exc), "NO MCP KEY")
    key = data.get("mcpApiKey", "") if isinstance(data, dict) else ""
    if not isinstance(key, str) or not key.strip():
        raise MCPError("no mcpApiKey in %s" % config_path, "NO MCP KEY")
    return key.strip()


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------

class MCPClient:
    """Synchronous client for the streamable-HTTP MCP endpoint.

    Thread-safe: one lock serialises requests so the UI probe and the
    shutdown worker can share a session.
    """

    def __init__(self, api_key: str, url: str = DEFAULT_MCP_URL,
                 timeout: float = 15.0):
        self.api_key = api_key
        self.url = url
        self.timeout = timeout
        self.session_id = ""
        self._next_id = 0
        self._lock = threading.Lock()
        # The device serves a self-signed certificate on loopback.
        self._ssl = ssl._create_unverified_context() if url.startswith("https") else None

    # -- transport --------------------------------------------------------
    def _post(self, payload: dict, with_session: bool, timeout: float):
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if with_session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url, json.dumps(payload).encode("utf-8"), headers=headers,
            method="POST")
        try:
            kwargs = {"timeout": timeout}
            if self._ssl is not None:
                kwargs["context"] = self._ssl
            with urllib.request.urlopen(request, **kwargs) as response:
                session = response.headers.get("Mcp-Session-Id", "")
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise self._http_error(exc.code, detail)
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError("cannot reach MCP endpoint: %s" % exc, "KVM SERVICE DOWN")
        return session, body

    @staticmethod
    def _http_error(code: int, detail: str) -> MCPError:
        text = detail.lower()
        if code == 403 and "disabled" in text:
            return MCPError("MCP service is disabled", "MCP IS DISABLED")
        if code == 401:
            return MCPError("MCP rejected the API key", "BAD MCP KEY")
        if code in (400, 404) and "session" in text:
            return _SessionExpired("MCP session expired: %s" % detail.strip())
        return MCPError("MCP HTTP %d: %s" % (code, detail.strip()[:200]),
                        "MCP HTTP %d" % code)

    @staticmethod
    def _decode(body: str):
        """Accept either a JSON body or a single SSE ``data:`` event."""
        stripped = body.strip()
        if not stripped:
            return None
        if not stripped.startswith("{"):
            for line in stripped.splitlines():
                if line.startswith("data:"):
                    stripped = line[5:].strip()
                    break
        try:
            return json.loads(stripped)
        except ValueError:
            raise MCPError("unreadable MCP response: %r" % stripped[:120])

    def _rpc(self, method: str, params: Optional[dict], notification: bool,
             with_session: bool, timeout: float):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            self._next_id += 1
            payload["id"] = self._next_id
        session, body = self._post(payload, with_session, timeout)
        message = self._decode(body)
        if notification:
            return session, None
        if not isinstance(message, dict):
            raise MCPError("empty MCP response to %s" % method)
        if message.get("error"):
            error = message["error"]
            text = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            if "session" in text.lower():
                raise _SessionExpired(text)
            raise MCPError("MCP %s: %s" % (method, text))
        return session, message.get("result")

    # -- session ----------------------------------------------------------
    def _initialize_locked(self):
        self.session_id = ""
        session, _ = self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        }, notification=False, with_session=False, timeout=self.timeout)
        if not session:
            raise MCPError("MCP returned no session ID")
        self.session_id = session
        self._rpc("notifications/initialized", {}, notification=True,
                  with_session=True, timeout=self.timeout)

    def initialize(self):
        with self._lock:
            self._initialize_locked()

    def call_tool(self, name: str, arguments: dict, timeout: Optional[float] = None):
        """Call a tool and return the raw MCP ``result`` object.

        Re-initialises once if the server forgot our session (for example
        after the KVM service restarted).
        """
        timeout = self.timeout if timeout is None else timeout
        with self._lock:
            for attempt in (1, 2):
                if not self.session_id:
                    self._initialize_locked()
                try:
                    _, result = self._rpc("tools/call",
                                          {"name": name, "arguments": arguments},
                                          notification=False, with_session=True,
                                          timeout=timeout)
                    return result if isinstance(result, dict) else {}
                except _SessionExpired:
                    self.session_id = ""
                    if attempt == 2:
                        raise MCPError("MCP session could not be re-established")
        return {}

    # -- tools used by this App ---------------------------------------------
    @staticmethod
    def _check(result: dict, name: str) -> dict:
        if result.get("isError"):
            raise MCPError("%s failed: %s" % (name, _first_text(result) or "error"),
                           "KEYBOARD ERROR")
        return result

    def press_keys(self, keys: List[str], hold_ms: int = 0):
        args = {"keys": list(keys)}
        if hold_ms > 0:
            args["holdMs"] = int(hold_ms)
        self._check(self.call_tool("press_keys", args), "press_keys")

    def type_text(self, text: str, delay_ms: int = 0) -> int:
        args = {"text": text}
        if delay_ms > 0:
            args["delayMs"] = int(delay_ms)
        result = self._check(self.call_tool("type_text", args), "type_text")
        info = _structured(result)
        skipped = int(info.get("skippedCount", 0) or 0)
        if skipped:
            raise MCPError("type_text skipped %d unsupported character(s): %r"
                           % (skipped, info.get("skipped", "")),
                           "UNTYPEABLE TEXT")
        return int(info.get("typed", len(text)) or 0)

    def video_present(self) -> Optional[bool]:
        """True if a frame can be captured, False if not, None if unknown.

        A tiny low-quality JPEG keeps the request cheap.
        """
        result = self.call_tool("screenshot", {"quality": 10, "timeoutMs": 4000},
                                timeout=10)
        info = _structured(result)
        if not result.get("isError") and info.get("ok", True):
            return True
        if int(info.get("retCode", 0) or 0) == RET_SNAPSHOT_BUSY:
            return None
        if "busy" in (_first_text(result) + str(info.get("message", ""))).lower():
            return None
        return False


class _SessionExpired(MCPError):
    pass


def _first_text(result: dict) -> str:
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text", ""))
    return ""


def _structured(result: dict) -> dict:
    """Prefer structuredContent; fall back to JSON in the first text block."""
    info = result.get("structuredContent")
    if isinstance(info, dict):
        return info
    text = _first_text(result)
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {}


# ---------------------------------------------------------------------------
# Sequence language
# ---------------------------------------------------------------------------
#
#   key:MetaLeft+KeyR        press a chord (KeyboardEvent.code names)
#   key:Power@800            ...holding it for 800 ms (max 1000)
#   text:shutdown /s /t 0    type literal text (US layout, ASCII)
#   wait:1500                pause in milliseconds (max 30000)
#
# Steps are separated by ";". Write "\;" for a literal semicolon inside text.

@dataclass(frozen=True)
class Step:
    kind: str           # "key" | "text" | "wait"
    keys: tuple = ()
    hold_ms: int = 0
    text: str = ""
    ms: int = 0

    def describe(self) -> str:
        if self.kind == "key":
            label = "+".join(self.keys)
            return ("KEY " + label + ("  HOLD %dMS" % self.hold_ms if self.hold_ms else ""))
        if self.kind == "text":
            return "TYPE TEXT"
        return "WAIT %.1fS" % (self.ms / 1000.0)


def _split_steps(source: str) -> List[str]:
    parts, current, escaped = [], [], False
    for ch in source:
        if escaped:
            current.append(ch if ch == ";" else "\\" + ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ";" or ch == "\n":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if escaped:
        current.append("\\")
    parts.append("".join(current))
    return parts


def parse_sequence(source: str) -> List[Step]:
    steps: List[Step] = []
    for index, raw in enumerate(_split_steps(source or ""), start=1):
        if not raw.strip():
            continue
        kind, sep, arg = raw.lstrip().partition(":")
        kind = kind.strip().lower()
        if not sep:
            raise SequenceError("step %d %r: expected key:, text: or wait:" % (index, raw.strip()))
        if kind == "key":
            chord, _, hold = arg.strip().partition("@")
            keys = tuple(k.strip() for k in chord.split("+") if k.strip())
            if not keys:
                raise SequenceError("step %d: key: needs at least one key" % index)
            hold_ms = 0
            if hold:
                hold_ms = _int_arg(hold, index, "hold")
                if not 0 < hold_ms <= MAX_HOLD_MS:
                    raise SequenceError("step %d: hold must be 1-%d ms" % (index, MAX_HOLD_MS))
            steps.append(Step("key", keys=keys, hold_ms=hold_ms))
        elif kind == "text":
            # Keep the text exactly as written after the colon.
            if not arg:
                raise SequenceError("step %d: text: is empty" % index)
            if len(arg) > MAX_TEXT_RUNES:
                raise SequenceError("step %d: text longer than %d characters" % (index, MAX_TEXT_RUNES))
            steps.append(Step("text", text=arg))
        elif kind == "wait":
            ms = _int_arg(arg, index, "wait")
            if not 0 <= ms <= MAX_WAIT_MS:
                raise SequenceError("step %d: wait must be 0-%d ms" % (index, MAX_WAIT_MS))
            steps.append(Step("wait", ms=ms))
        else:
            raise SequenceError("step %d: unknown step type %r" % (index, kind))
    if not any(step.kind != "wait" for step in steps):
        raise SequenceError("sequence has no key or text steps")
    return steps


def _int_arg(value: str, index: int, what: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        raise SequenceError("step %d: %s value %r is not a whole number" % (index, what, value.strip()))


@dataclass(frozen=True)
class Profile:
    key: str
    label: str          # shown on the panel (8x8 font: A-Z 0-9 space - . / :)
    sequence: str
    note: str


PROFILES = {
    "windows": Profile(
        "windows", "WINDOWS",
        # Wake the display, open Run, ask Windows for a normal shutdown.
        # Without /f, apps with unsaved work can still prompt.
        "key:ShiftLeft; wait:1000; key:MetaLeft+KeyR; wait:1500; "
        "text:shutdown /s /t 0; wait:300; key:Enter",
        "Needs an unlocked desktop session."),
    "windows-dialog": Profile(
        "windows-dialog", "WINDOWS DIALOG",
        # Show desktop, Alt+F4 opens 'Shut Down Windows', Enter accepts.
        "key:ShiftLeft; wait:1000; key:MetaLeft+KeyD; wait:1200; "
        "key:AltLeft+F4; wait:1500; key:Enter",
        "Needs an unlocked desktop session; uses the Alt+F4 dialog."),
    "linux-power-key": Profile(
        "linux-power-key", "LINUX POWER KEY",
        # HID Keyboard Power (0x66) -> KEY_POWER -> systemd-logind
        # HandlePowerKey (poweroff by default; desktops may show a dialog).
        "key:Power@200",
        "Acts like a short power-button press."),
    "linux-terminal": Profile(
        "linux-terminal", "LINUX TERMINAL",
        "key:ShiftLeft; wait:1000; key:ControlLeft+AltLeft+KeyT; wait:2500; "
        "text:systemctl poweroff; wait:300; key:Enter",
        "Needs an unlocked desktop with Ctrl+Alt+T bound to a terminal."),
    "macos": Profile(
        "macos", "MACOS",
        # Ctrl+F2 focuses the menu bar (Apple menu), Down opens it,
        # type-select 'Shut Down...', then accept the confirmation dialog.
        "key:ShiftLeft; wait:1000; key:ControlLeft+F2; wait:700; key:ArrowDown; "
        "wait:700; text:shut; wait:500; key:Enter; wait:2000; key:Enter",
        "Experimental. Needs an unlocked session and default keyboard shortcuts."),
}

DEFAULT_PROFILE = "windows"


def resolve_profile(name: str, custom_sequence: str = "") -> Profile:
    key = (name or DEFAULT_PROFILE).strip().lower().replace("_", "-").replace(" ", "-")
    if key == "custom":
        if not (custom_sequence or "").strip():
            raise SequenceError("TARGET_OS is custom but CUSTOM_SEQUENCE is empty")
        parse_sequence(custom_sequence)
        return Profile("custom", "CUSTOM", custom_sequence, "User-defined sequence.")
    if key not in PROFILES:
        raise SequenceError("unknown TARGET_OS %r (choose %s or custom)"
                            % (name, ", ".join(sorted(PROFILES))))
    return PROFILES[key]


# ---------------------------------------------------------------------------
# Background work
# ---------------------------------------------------------------------------

class VideoProbe:
    """Polls video presence for the idle screen without blocking the UI."""

    def __init__(self, client_factory: Callable[[], MCPClient], interval: float = 4.0):
        self._factory = client_factory
        self._interval = interval
        self._lock = threading.Lock()
        self._state = ("CHECKING", "")   # (status, error short text)
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="video-probe", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def pause(self, paused: bool):
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    @property
    def state(self):
        with self._lock:
            return self._state

    def _set(self, status: str, error: str = ""):
        with self._lock:
            self._state = (status, error)

    def _loop(self):
        while not self._stop.is_set():
            if not self._paused.is_set():
                try:
                    present = self._factory().video_present()
                    if present is True:
                        self._set("VIDEO")
                    elif present is False:
                        self._set("NO VIDEO")
                except MCPError as exc:
                    self._set("ERROR", exc.short)
                except Exception as exc:  # never let the probe thread die
                    self._set("ERROR", type(exc).__name__.upper())
            self._stop.wait(self._interval)


@dataclass
class JobStatus:
    phase: str = "IDLE"        # IDLE SENDING WAITING OFF TIMEOUT ERROR CANCELLED
    step_index: int = 0
    step_count: int = 0
    step_label: str = ""
    elapsed: float = 0.0       # seconds since the sequence finished
    timeout: float = 0.0
    error: str = ""
    error_detail: str = ""


class ShutdownJob:
    """Sends a sequence, then waits for the video signal to stay gone.

    The target counts as off once ``confirm_polls`` consecutive screenshots
    fail; a single dropped frame or resolution change does not end the wait.
    """

    def __init__(self, client: MCPClient, steps: List[Step], verify_timeout: float = 180.0,
                 poll_interval: float = 2.0, confirm_polls: int = 3,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.steps = steps
        self.verify_timeout = verify_timeout
        self.poll_interval = poll_interval
        self.confirm_polls = max(1, confirm_polls)
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._status = JobStatus(step_count=len(steps), timeout=verify_timeout)
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- control ------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self.run, name="shutdown-job", daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    def join(self, timeout: Optional[float] = None):
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def status(self) -> JobStatus:
        with self._lock:
            return JobStatus(**self._status.__dict__)

    def _update(self, **changes):
        with self._lock:
            for name, value in changes.items():
                setattr(self._status, name, value)

    def _pause(self, seconds: float) -> bool:
        """Sleep in small slices; return False if cancelled."""
        end = self._clock() + seconds
        while True:
            if self._cancel.is_set():
                return False
            left = end - self._clock()
            if left <= 0:
                return True
            self._sleep(min(0.1, left))

    # -- work ---------------------------------------------------------------
    def run(self):
        try:
            if not self._send():
                self._update(phase="CANCELLED")
                return
            self._wait_for_off()
        except MCPError as exc:
            self._update(phase="ERROR", error=exc.short, error_detail=str(exc))
        except Exception as exc:
            self._update(phase="ERROR", error=type(exc).__name__.upper(), error_detail=str(exc))

    def _send(self) -> bool:
        self._update(phase="SENDING")
        for index, step in enumerate(self.steps, start=1):
            if self._cancel.is_set():
                return False
            self._update(step_index=index, step_label=step.describe())
            if step.kind == "key":
                self.client.press_keys(list(step.keys), step.hold_ms)
            elif step.kind == "text":
                self.client.type_text(step.text)
            elif step.kind == "wait":
                if not self._pause(step.ms / 1000.0):
                    return False
        return True

    def _wait_for_off(self):
        self._update(phase="WAITING", elapsed=0.0)
        start = self._clock()
        misses = 0
        while True:
            elapsed = self._clock() - start
            self._update(elapsed=elapsed)
            if self._cancel.is_set():
                self._update(phase="CANCELLED")
                return
            if elapsed >= self.verify_timeout:
                self._update(phase="TIMEOUT")
                return
            try:
                present = self.client.video_present()
            except MCPError as exc:
                # The MCP service itself failing is not evidence of power-off.
                if exc.short in ("MCP IS DISABLED", "BAD MCP KEY"):
                    raise
                present = None
            if present is False:
                misses += 1
                if misses >= self.confirm_polls:
                    self._update(phase="OFF", elapsed=self._clock() - start)
                    return
            elif present is True:
                misses = 0
            if not self._pause(self.poll_interval):
                self._update(phase="CANCELLED")
                return
