"""Deterministic demo desktop; never touches real applications."""
import copy


class MockBackend:
    def __init__(self):
        self.value = ""
        self.presses = 0
        self.focused = False

    def snapshot(self):
        return copy.deepcopy({
            "platform": "mock", "desktop_id": "local", "truncated": False,
            "capabilities": {"scope": "mock_desktop", "screenshots": False, "accessibility": True},
            "frontmost_app": {"pid": 1, "name": "Demo", "bundle_id": "local.demo"},
            "applications": [{"pid": 1, "name": "Demo", "bundle_id": "local.demo"}],
            "roots": ["window"], "nodes": [
                {"id": "window", "pid": 1, "role": "AXWindow", "title": "Demo",
                 "children": ["input", "submit", "result"], "actions": [], "settable": []},
                {"id": "input", "pid": 1, "role": "AXTextField", "title": "Message",
                 "value": self.value, "enabled": True, "focused": self.focused,
                 "children": [], "actions": [], "settable": ["value", "focused"]},
                {"id": "submit", "pid": 1, "role": "AXButton", "title": "Apply",
                 "enabled": True, "children": [], "actions": ["AXPress"], "settable": []},
                {"id": "result", "pid": 1, "role": "AXStaticText", "title": "Apply count",
                 "value": str(self.presses), "children": [], "actions": [], "settable": []}
            ]})

    def act(self, action):
        kind = action["kind"]
        if kind == "set_value":
            self.value = action["value"]
        elif kind == "focus":
            self.focused = True
        elif kind == "perform":
            self.presses += 1
        elif kind != "activate_app":
            raise ValueError("Unsupported action")
        return {"performed": True}

    def close(self):
        pass
