"""Deterministic demo desktop; never touches real applications."""
import copy
from input_actions import INPUT_KINDS, validate_input_action


class MockBackend:
    def __init__(self):
        self.value = ""
        self.presses = 0
        self.focused = False
        self.pointer = {"x": 0, "y": 0}
        self.scroll = {"x": 0, "y": 0}

    def snapshot(self):
        return copy.deepcopy({
            "platform": "mock", "desktop_id": "local", "truncated": False,
            "capabilities": {"scope": "mock_desktop", "screenshots": False, "accessibility": True,
                             **{kind: True for kind in INPUT_KINDS}},
            "input": {"coordinate_space": "screen", "units": "pixels", "backend": "mock",
                      "displays": [{"x": 0, "y": 0, "width": 1024, "height": 768}],
                      "pointer": self.pointer, "scroll": self.scroll},
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
        if kind in INPUT_KINDS:
            action = validate_input_action(action, self.snapshot())
            if kind == "type_text":
                self.value += action["text"]
            elif kind == "key_press":
                if action["key"] == "enter":
                    self.presses += 1
                elif action["key"] == "backspace":
                    self.value = self.value[:-1]
            elif kind == "mouse_drag":
                self.pointer = {"x": action["to_x"], "y": action["to_y"]}
            else:
                self.pointer = {"x": action["x"], "y": action["y"]}
                if kind == "scroll":
                    self.scroll["x"] += action["delta_x"]
                    self.scroll["y"] += action["delta_y"]
            return {"performed": True, "kind": kind}
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
