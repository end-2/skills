"""Opt-in real Chrome smoke; temporary profile and loopback fixtures only.

Run CHROME_USE_SMOKE=1 python3 -m unittest discover -s tests -p test_browser_smoke.py
Set CHROME_USE_EXECUTABLE if Chrome is not in a standard location.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backend_common import BackendError
from chrome_backend import ChromeBackend
from server import ChromeServer


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/frame":
            html = '<label>Frame input<input value="frame"></label>'
        else:
            html = '''<!doctype html><title>Chrome broker fixture</title>
<label>Message<input id="message" value="initial"></label>
<label>Password<input type="password" value="never-expose-me"></label>
<button id="apply" onclick="document.querySelector('#result').textContent=String(++window.count)">Apply</button>
<button onclick="document.querySelector('#message').outerHTML='<input id=message value=replaced aria-label=Message>'">Replace</button>
<button onclick="alert('Fixture dialog')">Dialog</button>
<button onclick="alert('First dialog'); alert('Second dialog')">Two dialogs</button>
<p id="result">0</p><div id="shadow"></div><script>window.count=0;
document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML='<label>Shadow one<input></label><label>Shadow two<input></label>';
</script>
<iframe src="http://localhost:PORT/frame"></iframe>
<div style="height:2000px">Scrollable</div>'''.replace("PORT", str(self.server.server_port))
        raw = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@unittest.skipUnless(os.environ.get("CHROME_USE_SMOKE") == "1", "Set CHROME_USE_SMOKE=1 for isolated browser smoke")
class BrowserSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = os.environ.get("CHROME_USE_EXECUTABLE")
        if not executable:
            candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                          shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser")]
            executable = next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)
        if not executable:
            raise unittest.SkipTest("No Chrome executable found")
        cls.temp = tempfile.TemporaryDirectory(prefix="chrome-use-smoke-")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.http_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.chrome = subprocess.Popen([executable, "--headless=new", "--remote-debugging-port=0",
            "--user-data-dir=" + cls.temp.name, "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update", "--no-proxy-server", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        port_file = Path(cls.temp.name) / "DevToolsActivePort"
        while not port_file.exists() and cls.chrome.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if not port_file.exists():
            cls.tearDownClass()
            raise RuntimeError("Temporary Chrome did not publish its debugging endpoint")
        cls.backend = ChromeBackend(user_data_dir=cls.temp.name, request_timeout=20)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "backend"):
            cls.backend.close()
        if hasattr(cls, "chrome"):
            cls.chrome.terminate()
            try:
                cls.chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.chrome.kill()
                cls.chrome.wait(timeout=5)
        if hasattr(cls, "httpd"):
            cls.httpd.shutdown()
            cls.httpd.server_close()
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    def status(self):
        return self.backend.snapshot()

    def action(self, kind, **args):
        return self.backend.act({"kind": kind, **args})

    def node(self, state, title, role=None):
        return next(node for node in state["nodes"] if node["title"] == title and (role is None or node["role"] == role))

    def test_structured_control_identity_frames_dialog_and_disconnect(self):
        state = self.status()
        tab = state["selected_tab_id"]
        url = f"http://127.0.0.1:{self.httpd.server_port}/"
        self.action("navigate", tab_id=tab, url=url)
        # Loading the cross-origin iframe is asynchronous after DOMContentLoaded.
        deadline = time.monotonic() + 5
        while True:
            state = self.status()
            if (any(node["title"] == "Frame input" for node in state["nodes"])
                    and all(frame.get("ready_state") == "complete" for frame in state["frames"])):
                break
            self.assertLess(time.monotonic(), deadline)
        # Headless Chrome can finish its initial viewport resize after load.
        while True:
            settled = self.status()
            if settled == state:
                break
            self.assertLess(time.monotonic(), deadline, "Unchanged page must eventually have deterministic snapshots")
            state = settled
        self.assertEqual(state, self.status(), "Unchanged page must have deterministic snapshots")
        shadow_one = self.node(state, "Shadow one", "textbox")
        self.action("focus", tab_id=tab, node_id=shadow_one["id"])
        state = self.status()
        self.assertTrue(self.node(state, "Shadow one", "textbox")["focused"])
        self.action("focus", tab_id=tab, node_id=self.node(state, "Shadow two", "textbox")["id"])
        state = self.status()
        self.assertFalse(self.node(state, "Shadow one", "textbox")["focused"])
        self.assertTrue(self.node(state, "Shadow two", "textbox")["focused"])
        password = self.node(state, "Password", "textbox")
        self.assertTrue(password["secure"])
        self.assertNotIn("value", password)
        message = self.node(state, "Message", "textbox")
        self.action("fill", tab_id=tab, node_id=message["id"], value="hello")
        state = self.status()
        self.assertEqual(self.node(state, "Message", "textbox")["value"], "hello")
        # Exercise real broker preflight observation, fingerprint, and replay.
        broker = ChromeServer(self.backend)
        session = broker.create_session("browser-smoke")
        def call(operation, payload=None):
            return broker.call(session["session_id"], session["session_token"], operation, payload)
        lease = call("acquire")
        observed = call("status")
        payload = {"request_id": "apply-once", "snapshot_id": observed["snapshot_id"],
                   "lease_token": lease["lease_token"], "action": {"kind": "click", "tab_id": tab,
                   "node_id": self.node(observed["state"], "Apply", "button")["id"]}}
        first = call("act", payload)
        self.assertEqual(first, call("act", payload))
        state = call("status")["state"]
        call("close")
        self.assertIsNotNone(self.node(state, "1", "paragraph"))
        self.action("click", tab_id=tab, node_id=self.node(state, "Replace", "button")["id"])
        state = self.status()
        replacement = self.node(state, "Message", "textbox")
        self.assertNotEqual(message["id"], replacement["id"])
        with self.assertRaises(BackendError):
            self.action("fill", tab_id=tab, node_id=message["id"], value="must-not-retarget")
        state = self.status()
        self.assertEqual(self.node(state, "Message", "textbox")["value"], "replaced")
        self.action("click", tab_id=tab, node_id=self.node(state, "Dialog", "button")["id"])
        state = self.status()
        self.assertEqual(state["pending_dialog"]["message"], "Fixture dialog")
        self.action("handle_dialog", tab_id=tab, response="accept")
        state = self.status()
        self.assertNotIn("pending_dialog", state)
        self.action("click", tab_id=tab, node_id=self.node(state, "Two dialogs", "button")["id"])
        state = self.status()
        self.assertEqual(state["pending_dialog"]["message"], "First dialog")
        self.action("handle_dialog", tab_id=tab, response="accept")
        state = self.status()
        self.assertEqual(state["pending_dialog"]["message"], "Second dialog")
        self.action("handle_dialog", tab_id=tab, response="accept")
        state = self.status()
        self.assertNotIn("pending_dialog", state)
        self.action("new_tab", url="about:blank")
        state = self.status()
        new_tab = state["selected_tab_id"]
        self.assertNotEqual(tab, new_tab)
        self.action("select_tab", tab_id=tab)
        state = self.status()
        self.action("close_tab", tab_id=new_tab)
        state = self.status()
        self.assertEqual(state["selected_tab_id"], tab)
        self.assertNotIn(new_tab, [item["id"] for item in state["tabs"]])
        old_document = state["frames"][0]["document_id"]
        self.action("navigate", tab_id=tab, url=url)
        state = self.status()
        self.assertNotEqual(state["frames"][0]["document_id"], old_document)
        self.action("scroll", tab_id=tab, direction="down", pixels=250)
        state = self.status()
        self.assertGreater(state["frames"][0]["scroll"]["y"], 0)
        self.backend.close()
        self.assertIsNone(self.chrome.poll(), "Closing broker must leave Chrome running")
        self.__class__.backend = ChromeBackend(user_data_dir=self.temp.name, request_timeout=20)
        self.assertTrue(self.status()["tabs"])


if __name__ == "__main__":
    unittest.main()
