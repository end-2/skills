import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backend_common import BackendError
from browser_actions import validate_action


class BrowserActionsTest(unittest.TestCase):
    def setUp(self):
        self.state = {"tabs": [{"id": "a"}, {"id": "b"}], "selected_tab_id": "a",
                      "nodes": [{"id": "input", "tab_id": "a", "enabled": True,
                                 "settable": ["value", "focused"], "actions": ["click", "hover"]}]}

    def reject(self, action, state=None):
        with self.assertRaises(BackendError):
            validate_action(action, state or self.state)

    def test_observed_target_required_and_cross_tab_rejected(self):
        self.reject({"kind": "click", "tab_id": "missing", "node_id": "input"})
        self.reject({"kind": "click", "tab_id": "b", "node_id": "input"})
        self.reject({"kind": "click", "tab_id": "a", "node_id": "missing"})
        self.reject({"kind": "click", "node_id": "input"})
        self.reject({"kind": "press_key", "tab_id": "b", "key": "Enter"})
        self.assertEqual(validate_action({"kind": "select_tab", "tab_id": "b"}, self.state)["tab_id"], "b")

    def test_only_advertised_capabilities_and_complete_values(self):
        action = {"kind": "fill", "tab_id": "a", "node_id": "input", "value": ""}
        self.assertEqual(validate_action(action, self.state), action)
        for patch in ({"enabled": False}, {"settable": []}, {"value_truncated": True}):
            changed = copy.deepcopy(self.state)
            changed["nodes"][0].update(patch)
            self.reject(action, changed)

    def test_bounded_inputs_and_no_arbitrary_script(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "http://user:pass@host/", "https://example.com\n", "http://host:999999"):
            self.reject({"kind": "new_tab", "url": url})
        self.reject({"kind": "evaluate", "script": "1"})
        self.reject({"kind": "fill", "tab_id": "a", "node_id": "input", "value": "x" * 16385})
        for pixels in (True, float("nan"), float("inf"), 0, 5001, 10**1000):
            self.reject({"kind": "scroll", "tab_id": "a", "direction": "down", "pixels": pixels})
        for url in ("about:blank", "http://127.0.0.1:9000/", "https://example.com/"):
            self.assertEqual(validate_action({"kind": "new_tab", "url": url}, self.state)["url"], url)

    def test_dialog_requires_fresh_observation(self):
        self.reject({"kind": "handle_dialog", "tab_id": "a", "response": "accept"})
        self.state["pending_dialog"] = {"type": "confirm", "message": "Continue?"}
        self.reject({"kind": "click", "tab_id": "a", "node_id": "input"})
        self.reject({"kind": "select_tab", "tab_id": "b"})
        self.reject({"kind": "handle_dialog", "tab_id": "a", "response": "accept", "text": "yes"})
        self.assertEqual(validate_action({"kind": "handle_dialog", "tab_id": "a", "response": "dismiss"}, self.state)["response"], "dismiss")


if __name__ == "__main__":
    unittest.main()
