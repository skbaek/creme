import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from creme import claude_telemetry as T


class ClaudeTelemetryReceiverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), T.make_handler(self.directory))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def post(self, path, body, content_type="application/json"):
        request = urllib.request.Request(self.url + path, data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def records(self):
        return [json.loads(line) for path in sorted(self.directory.glob("otlp-*.jsonl"))
                for line in path.read_text().splitlines()]

    def test_json_bodies_are_appended_unmodified_with_their_signal(self):
        body = {"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "claude_code.llm_request"}]}]}]}
        self.assertEqual(self.post("/v1/traces", json.dumps(body).encode()), 200)
        self.assertEqual(self.post("/v1/logs", b'{"resourceLogs": []}'), 200)
        records = self.records()
        self.assertEqual([r["signal"] for r in records], ["traces", "logs"])
        self.assertEqual(records[0]["body"], body)
        self.assertEqual(oct(next(self.directory.glob("otlp-*.jsonl")).stat().st_mode & 0o777), "0o600")

    def test_unknown_paths_protobuf_and_malformed_bodies_are_not_stored(self):
        self.assertEqual(self.post("/v1/other", b"{}"), 404)
        self.assertEqual(self.post("/v1/logs", b"\x0a\x00", "application/x-protobuf"), 415)
        self.assertEqual(self.post("/v1/logs", b"{not json"), 500)
        self.assertEqual(self.records(), [])


class ClaudeTelemetryLifecycleTest(unittest.TestCase):
    """The receiver follows the master: start is idempotent, stop signals only its own process."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.saved = (T.RUNTIME, T.PID_FILE, T.LOG_FILE)
        T.RUNTIME, T.PID_FILE, T.LOG_FILE = root, root / "receiver.pid", root / "receiver.log"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.fit = root / "fit"

    def tearDown(self):
        T.stop(self.port)
        T.RUNTIME, T.PID_FILE, T.LOG_FILE = self.saved
        self.temp.cleanup()

    def test_start_is_idempotent_and_stop_ends_the_listener(self):
        first = T.start(self.fit, self.port)
        self.assertEqual(first["status"], "started", first)
        self.assertTrue(first["listening"])
        self.assertEqual(T.start(self.fit, self.port)["status"], "running")
        self.assertEqual(T.stop(self.port)["status"], "stopped")
        self.assertFalse(T.status(self.port)["listening"])
        self.assertFalse(T.PID_FILE.exists())

    def test_stop_never_signals_a_recycled_pid(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            T.PID_FILE.write_text(f"{sleeper.pid}\n")
            self.assertEqual(T.stop(self.port)["status"], "not-running")
            self.assertIsNone(sleeper.poll())
        finally:
            sleeper.kill()
            sleeper.wait()


if __name__ == "__main__":
    unittest.main()
