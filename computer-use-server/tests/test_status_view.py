"""Filtered views preserve target context without weakening server observations."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from status_view import filter_status
from mock_backend import MockBackend
from server import APIError, ComputerServer


def observed_tree():
    return {"ok": True, "result": {"snapshot_id": "snapshot", "revision": 7,
        "lease": {"held": False}, "state": {"platform": "test", "desktop_id": "local",
        "frontmost_app": {"pid": 1}, "applications": [{"pid": 1}], "roots": ["window"],
        "capabilities": {"scope": "foreground_window"}, "truncated": False, "nodes": [
            {"id": "window", "role": "Window", "title": "Editor", "children": ["panel-a", "panel-b"]},
            {"id": "panel-a", "role": "Group", "title": "Draft", "children": ["apply-a", "input"]},
            {"id": "apply-a", "role": "Button", "title": "Apply", "enabled": True,
             "actions": ["Invoke"], "settable": [], "children": []},
            {"id": "input", "role": "Edit", "description": "Title", "enabled": None,
             "value": "Preview", "settable": ["value"], "children": []},
            {"id": "panel-b", "role": "Group", "title": "Published", "children": ["apply-b", "secret"]},
            {"id": "apply-b", "role": "Button", "title": "Apply", "enabled": False,
             "actions": ["Invoke"], "children": []},
            {"id": "secret", "role": "Edit", "title": "Password", "secure": True,
             "value": None, "value_hash": None, "actions": [], "settable": [], "children": []},
        ]}}}


class StatusViewTests(unittest.TestCase):
    def test_default_and_errors_are_returned_unchanged(self):
        reply = observed_tree()
        self.assertIs(filter_status(reply), reply)
        error = {"ok": False, "error": {"code": "backend_unavailable"}}
        self.assertIs(filter_status(error, match="Apply"), error)

    def test_duplicate_names_keep_distinct_ancestor_context_and_valid_tree(self):
        reply = observed_tree()
        original = copy.deepcopy(reply)
        result = filter_status(reply, match="aPpLy")["result"]
        self.assertEqual(result["view"]["matched_node_ids"], ["apply-a", "apply-b"])
        nodes = {node["id"]: node for node in result["state"]["nodes"]}
        self.assertEqual(set(nodes), {"window", "panel-a", "panel-b", "apply-a", "apply-b"})
        self.assertEqual(nodes["panel-a"]["children"], ["apply-a"])
        self.assertEqual(nodes["panel-b"]["children"], ["apply-b"])
        self.assertTrue(all(child in nodes for node in nodes.values() for child in node.get("children", [])))
        self.assertEqual(result["state"]["roots"], ["window"])
        self.assertTrue(result["state"]["truncated"])
        self.assertFalse(result["view"]["source_truncated"])
        self.assertEqual(result["view"]["context_nodes"], 3)
        self.assertEqual(reply, original)

    def test_filters_combine_and_preserve_unknown_enabled_capability(self):
        reply = observed_tree()
        result = filter_status(reply, match="Apply", role="Button", actionable=True)["result"]
        self.assertEqual(result["view"]["matched_node_ids"], ["apply-a"])
        result = filter_status(reply, actionable=True)["result"]
        self.assertEqual(result["view"]["matched_node_ids"], ["apply-a", "input"])
        self.assertEqual(filter_status(reply, role="button")["result"]["view"]["matching_nodes"], 0)

    def test_match_is_literal_label_search_not_value_search(self):
        reply = observed_tree()
        for query in (".*", "Preview"):
            result = filter_status(reply, match=query)["result"]
            self.assertEqual(result["state"]["nodes"], [])
            self.assertEqual(result["state"]["roots"], [])
            self.assertTrue(result["state"]["truncated"])
            self.assertEqual(result["state"]["capabilities"]["scope"], "foreground_window")
        self.assertEqual(filter_status(reply, match="TITLE")["result"]["view"]["matched_node_ids"], ["input"])

    def test_limit_caps_matches_but_retains_ancestors_and_source_order(self):
        result = filter_status(observed_tree(), match="Apply", limit=1)["result"]
        self.assertEqual(result["view"]["matching_nodes"], 2)
        self.assertEqual(result["view"]["returned_matches"], 1)
        self.assertEqual([node["id"] for node in result["state"]["nodes"]], ["window", "panel-a", "apply-a"])
        self.assertEqual(result["view"]["returned_nodes"], 3)

    def test_truncation_hashes_secure_markers_and_snapshot_metadata_survive(self):
        reply = observed_tree()
        reply["result"]["state"]["truncated"] = True
        reply["result"]["state"]["nodes"][2].update(text_truncated=True, title_hash="full-title-hash")
        result = filter_status(reply, limit=100)["result"]
        self.assertTrue(result["state"]["truncated"])
        self.assertTrue(result["view"]["source_truncated"])
        self.assertEqual(result["view"]["omitted_nodes"], 0)
        self.assertEqual(result["state"]["nodes"], reply["result"]["state"]["nodes"])
        self.assertEqual(result["snapshot_id"], "snapshot")
        self.assertEqual(result["revision"], 7)
        secret = result["state"]["nodes"][-1]
        self.assertTrue(secret["secure"])
        self.assertIsNone(secret["value"])
        self.assertIsNone(secret["value_hash"])

    def test_parent_cycles_and_missing_children_terminate_without_dangling_edges(self):
        reply = observed_tree()
        reply["result"]["state"]["nodes"][1]["children"] += ["window", "missing"]
        result = filter_status(reply, match="Apply", limit=1)["result"]
        self.assertEqual(len(result["state"]["nodes"]), 3)
        self.assertEqual(result["state"]["nodes"][1]["children"], ["apply-a", "window"])

    def test_change_in_an_omitted_node_still_invalidates_the_server_snapshot(self):
        backend = MockBackend()
        broker = ComputerServer(backend)
        session = broker.create_session("view-test")
        def call(operation, payload=None):
            return broker.call(session["session_id"], session["session_token"], operation, payload)
        lease = call("acquire")
        result = filter_status({"ok": True, "result": call("status")}, match="Apply", role="AXButton")["result"]
        self.assertEqual(result["view"]["matched_node_ids"], ["submit"])
        self.assertNotIn("input", [node["id"] for node in result["state"]["nodes"]])
        backend.value = "Changed outside the displayed view"
        with self.assertRaises(APIError) as caught:
            call("act", {"request_id": "stale-view", "lease_token": lease["lease_token"],
                         "snapshot_id": result["snapshot_id"],
                         "action": {"kind": "perform", "node_id": "submit", "action": "AXPress"}})
        self.assertEqual(caught.exception.code, "stale_snapshot")
        self.assertEqual(backend.presses, 0)
        fresh = filter_status({"ok": True, "result": call("status")}, match="Apply", role="AXButton")["result"]
        action = call("act", {"request_id": "fresh-view", "lease_token": lease["lease_token"],
                              "snapshot_id": fresh["snapshot_id"],
                              "action": {"kind": "perform", "node_id": "submit", "action": "AXPress"}})
        self.assertTrue(action["accepted"])
        self.assertEqual(backend.presses, 1)
        call("release", {"lease_token": lease["lease_token"]})
        call("close")


if __name__ == "__main__":
    unittest.main()
