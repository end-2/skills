"""Deterministic mock Chrome; never touches the real browser or network."""
import copy

from browser_actions import validate_action


class MockBackend:
    backend_name = "mock"

    def __init__(self):
        self.tabs = [{"id": "tab-1", "url": "https://example.test/demo", "title": "Demo"}]
        self.selected_tab_id = "tab-1"
        self.next_tab = 2
        self.value = ""
        self.presses = 0
        self.focused = False
        self.scroll = {"x": 0, "y": 0}

    def snapshot(self):
        tab_id = self.selected_tab_id
        nodes = [] if tab_id is None else [
            {"id": "document", "tab_id": tab_id, "role": "RootWebArea", "title": "Demo",
             "children": ["input", "submit", "result"], "actions": [], "settable": []},
            {"id": "input", "tab_id": tab_id, "role": "textbox", "title": "Message",
             "value": self.value, "enabled": True, "focused": self.focused,
             "children": [], "actions": ["hover"], "settable": ["value", "focused"]},
            {"id": "submit", "tab_id": tab_id, "role": "button", "title": "Apply",
             "enabled": True, "children": [], "actions": ["click", "hover"], "settable": ["focused"]},
            {"id": "result", "tab_id": tab_id, "role": "StaticText", "title": "Apply count",
             "value": str(self.presses), "children": [], "actions": [], "settable": []},
        ]
        return copy.deepcopy({
            "platform": "mock", "browser_id": "local", "truncated": False,
            "capabilities": {"scope": "selected_tab", "screenshots": False, "accessibility": True,
                             **{kind: True for kind in ("select_tab", "new_tab", "navigate", "close_tab",
                                                        "click", "fill", "focus", "hover", "press_key", "scroll")}},
            "tabs": self.tabs, "selected_tab_id": tab_id,
            "roots": ["document"] if tab_id else [], "nodes": nodes,
            "scroll": self.scroll,
        })

    def act(self, action):
        action = validate_action(action, self.snapshot())
        kind = action["kind"]
        if kind == "new_tab":
            tab_id = f"tab-{self.next_tab}"
            self.next_tab += 1
            self.tabs.append({"id": tab_id, "url": action["url"], "title": "New tab"})
            self.selected_tab_id = tab_id
        elif kind == "select_tab":
            self.selected_tab_id = action["tab_id"]
        elif kind == "close_tab":
            self.tabs = [tab for tab in self.tabs if tab["id"] != action["tab_id"]]
            if action["tab_id"] == self.selected_tab_id:
                self.selected_tab_id = self.tabs[0]["id"] if self.tabs else None
        elif kind == "navigate":
            next(tab for tab in self.tabs if tab["id"] == action["tab_id"])["url"] = action["url"]
            self.value, self.focused = "", False
        elif kind == "fill":
            self.value = action["value"]
        elif kind == "focus":
            self.focused = action["node_id"] == "input"
        elif kind == "click":
            self.presses += 1
        elif kind == "press_key":
            if action["key"] == "Enter":
                self.presses += 1
            elif action["key"] == "Backspace":
                self.value = self.value[:-1]
        elif kind == "scroll":
            direction, pixels = action["direction"], action["pixels"]
            axis = "x" if direction in {"left", "right"} else "y"
            self.scroll[axis] += pixels if direction in {"down", "right"} else -pixels
        return {"performed": True, "kind": kind, "selected_tab_id": self.selected_tab_id}

    def close(self):
        pass
