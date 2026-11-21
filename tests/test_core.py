"""Tests for shutdown_core. Run: python3 -m unittest discover -s tests"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "safe-shutdown"))

import shutdown_core as core  # noqa: E402

KEY = "nag_mcp_testkey"


class FakeMCP:
    """Mimics NanoKVM Go's /api/mcp closely enough for the client."""

    def __init__(self):
        self.enabled = True
        self.sessions = set()
        self.calls = []            # (tool, arguments)
        self.video = True
        self.video_script = None   # list of bools consumed per screenshot
        self.sse = True
        self.counter = 0
        self.lock = threading.Lock()

    def handle(self, handler):
        length = int(handler.headers.get("Content-Length", 0))
        body = json.loads(handler.rfile.read(length))
        if not self.enabled:
            return handler.reply(403, {"error": "MCP service is disabled"})
        if handler.headers.get("Authorization") != "Bearer " + KEY:
            return handler.reply(401, {"error": "invalid MCP API key"})
        method = body.get("method")
        if method == "initialize":
            with self.lock:
                self.counter += 1
                sid = "sess-%d" % self.counter
                self.sessions.add(sid)
            return handler.reply(200, self.rpc(body, {"protocolVersion": "2025-06-18"}),
                                 {"Mcp-Session-Id": sid})
        sid = handler.headers.get("Mcp-Session-Id", "")
        if sid not in self.sessions:
            return handler.reply(404, "session not found", raw=True)
        if method == "notifications/initialized":
            return handler.reply(202, None)
        if method == "tools/call":
            name = body["params"]["name"]
            args = body["params"]["arguments"]
            self.calls.append((name, args))
            return handler.reply(200, self.rpc(body, self.tool(name, args)))
        return handler.reply(200, self.rpc(body, {}))

    def tool(self, name, args):
        if name == "press_keys":
            if "Bogus" in args["keys"]:
                return {"isError": True, "content": [{"type": "text", "text": "unknown key: Bogus"}]}
            return {"content": [{"type": "text", "text": '{"ok":true}'}], "structuredContent": {"ok": True}}
        if name == "type_text":
            skipped = "".join(c for c in args["text"] if ord(c) > 127)
            info = {"typed": len(args["text"]) - len(skipped), "skipped": skipped,
                    "skippedCount": len(skipped)}
            return {"content": [{"type": "text", "text": json.dumps(info)}], "structuredContent": info}
        if name == "screenshot":
            video = self.video_script.pop(0) if self.video_script else self.video
            if video == "busy":
                return {"isError": True, "content": [{"type": "text", "text": "snapshot client busy"}],
                        "structuredContent": {"ok": False, "retCode": -16}}
            if video:
                return {"content": [{"type": "image", "data": "AA==", "mimeType": "image/jpeg"}],
                        "structuredContent": {"ok": True, "retCode": 0}}
            return {"isError": True, "content": [{"type": "text", "text": "no video signal"}],
                    "structuredContent": {"ok": False, "retCode": -3}}
        return {"isError": True, "content": [{"type": "text", "text": "unknown tool"}]}

    def rpc(self, body, result):
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}


def serve(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, payload, headers=None, raw=False):
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if payload is None:
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if raw:
                data = payload.encode()
            elif fake.sse and code == 200:
                data = ("event: message\ndata: " + json.dumps(payload) + "\n\n").encode()
                self.send_header("Content-Type", "text/event-stream")
            else:
                data = json.dumps(payload).encode()
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            fake.handle(self)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/api/mcp" % server.server_address[1]


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s


class SequenceTests(unittest.TestCase):
    def test_all_profiles_parse(self):
        for name, profile in core.PROFILES.items():
            steps = core.parse_sequence(profile.sequence)
            self.assertTrue(steps, name)

    def test_step_types(self):
        steps = core.parse_sequence("key:ControlLeft+AltLeft+Delete; key:Power@800; wait:250; text:echo a\\;b")
        self.assertEqual(steps[0].keys, ("ControlLeft", "AltLeft", "Delete"))
        self.assertEqual(steps[1].hold_ms, 800)
        self.assertEqual(steps[2].ms, 250)
        self.assertEqual(steps[3].text, "echo a;b")

    def test_text_keeps_spaces(self):
        self.assertEqual(core.parse_sequence("text: sudo poweroff")[0].text, " sudo poweroff")

    def test_backslashes_preserved(self):
        self.assertEqual(core.parse_sequence(r"text:C:\Windows\x")[0].text, r"C:\Windows\x")

    def test_errors(self):
        for bad in ("", "wait:100", "key:", "key:Power@5000", "wait:abc", "jump:1", "nonsense",
                    "wait:999999; key:Enter"):
            with self.assertRaises(core.SequenceError, msg=bad):
                core.parse_sequence(bad)

    def test_resolve_profile(self):
        self.assertEqual(core.resolve_profile("Linux_Power_Key").key, "linux-power-key")
        self.assertEqual(core.resolve_profile("").key, core.DEFAULT_PROFILE)
        self.assertEqual(core.resolve_profile("custom", "key:Power").key, "custom")
        with self.assertRaises(core.SequenceError):
            core.resolve_profile("custom", "")
        with self.assertRaises(core.SequenceError):
            core.resolve_profile("amiga")

    def test_profile_labels_fit_font(self):
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -./:")
        for profile in core.PROFILES.values():
            self.assertTrue(set(profile.label) <= allowed, profile.label)
            self.assertLessEqual(len("TARGET: " + profile.label), 30)


class KeyTests(unittest.TestCase):
    def test_env_wins(self):
        self.assertEqual(core.load_api_key("  abc "), "abc")

    def test_reads_device_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"mcpEnabled": True, "mcpApiKey": KEY}, f)
        try:
            self.assertEqual(core.load_api_key("", f.name), KEY)
        finally:
            os.unlink(f.name)

    def test_missing(self):
        with self.assertRaises(core.MCPError) as cm:
            core.load_api_key("", "/nonexistent/ai.json")
        self.assertEqual(cm.exception.short, "NO MCP KEY")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMCP()
        self.server, self.url = serve(self.fake)
        self.client = core.MCPClient(KEY, self.url, timeout=5)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_press_and_type(self):
        self.client.press_keys(["MetaLeft", "KeyR"], 100)
        self.assertEqual(self.client.type_text("shutdown /s /t 0"), 16)
        self.assertEqual(self.fake.calls[0], ("press_keys", {"keys": ["MetaLeft", "KeyR"], "holdMs": 100}))

    def test_plain_json_responses(self):
        self.fake.sse = False
        self.client.press_keys(["Enter"])
        self.assertEqual(self.fake.calls[0][0], "press_keys")

    def test_session_recovery(self):
        self.client.press_keys(["Enter"])
        self.fake.sessions.clear()   # KVM service restarted
        self.client.press_keys(["Enter"])
        self.assertEqual(self.fake.counter, 2)

    def test_tool_error(self):
        with self.assertRaises(core.MCPError) as cm:
            self.client.press_keys(["Bogus"])
        self.assertEqual(cm.exception.short, "KEYBOARD ERROR")

    def test_untypeable(self):
        with self.assertRaises(core.MCPError) as cm:
            self.client.type_text("caf\u00e9")
        self.assertEqual(cm.exception.short, "UNTYPEABLE TEXT")

    def test_disabled_and_bad_key(self):
        self.fake.enabled = False
        with self.assertRaises(core.MCPError) as cm:
            self.client.press_keys(["Enter"])
        self.assertEqual(cm.exception.short, "MCP IS DISABLED")
        self.fake.enabled = True
        with self.assertRaises(core.MCPError) as cm:
            core.MCPClient("wrong", self.url).press_keys(["Enter"])
        self.assertEqual(cm.exception.short, "BAD MCP KEY")

    def test_unreachable(self):
        with self.assertRaises(core.MCPError) as cm:
            core.MCPClient(KEY, "http://127.0.0.1:9/api/mcp", timeout=1).press_keys(["Enter"])
        self.assertEqual(cm.exception.short, "KVM SERVICE DOWN")

    def test_video_present(self):
        self.fake.video_script = [True, False, "busy"]
        self.assertIs(self.client.video_present(), True)
        self.assertIs(self.client.video_present(), False)
        self.assertIsNone(self.client.video_present())


class JobTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMCP()
        self.server, self.url = serve(self.fake)
        self.client = core.MCPClient(KEY, self.url, timeout=5)
        self.t = FakeTime()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def job(self, seq, timeout=60):
        return core.ShutdownJob(self.client, core.parse_sequence(seq), verify_timeout=timeout,
                                poll_interval=2, confirm_polls=3,
                                sleep=self.t.sleep, clock=self.t.clock)

    def test_off_after_consecutive_misses(self):
        # one blip (resolution change) must not end the wait
        self.fake.video_script = [True, False, True, "busy", False, False, False]
        job = self.job(core.PROFILES["windows"].sequence)
        job.run()
        st = job.status
        self.assertEqual(st.phase, "OFF")
        sent = [c for c in self.fake.calls if c[0] != "screenshot"]
        self.assertEqual(sent[1], ("press_keys", {"keys": ["MetaLeft", "KeyR"]}))
        self.assertEqual(sent[2], ("type_text", {"text": "shutdown /s /t 0"}))
        self.assertEqual(sent[-1], ("press_keys", {"keys": ["Enter"]}))
        self.assertEqual(st.step_index, st.step_count)

    def test_timeout(self):
        self.fake.video = True
        job = self.job("key:Power@200", timeout=30)
        job.run()
        self.assertEqual(job.status.phase, "TIMEOUT")
        self.assertGreaterEqual(job.status.elapsed, 30)

    def test_error_stops_sequence(self):
        job = self.job("key:Bogus; text:never typed")
        job.run()
        self.assertEqual(job.status.phase, "ERROR")
        self.assertFalse(any(c[0] == "type_text" for c in self.fake.calls))

    def test_cancel(self):
        job = self.job("key:Enter")
        job.cancel()
        job.run()
        self.assertEqual(job.status.phase, "CANCELLED")
        self.assertEqual(self.fake.calls, [])

    def test_mcp_disabled_mid_wait_is_error_not_off(self):
        job = self.job("key:Enter")
        original = self.client.video_present

        def flaky():
            self.fake.enabled = False
            return original()
        self.client.video_present = flaky
        job.run()
        self.assertEqual(job.status.phase, "ERROR")
        self.assertEqual(job.status.error, "MCP IS DISABLED")

    def test_threaded_run(self):
        self.fake.video = False
        job = core.ShutdownJob(self.client, core.parse_sequence("key:Enter"),
                               verify_timeout=10, poll_interval=0.01)
        job.start()
        job.join(5)
        self.assertEqual(job.status.phase, "OFF")


if __name__ == "__main__":
    unittest.main()
