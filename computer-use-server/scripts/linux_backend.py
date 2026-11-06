"""Semantic Linux AT-SPI adapter, run by the broker's isolated worker process.

Production requires the desktop user's session bus and distribution pyatspi2.
No native library is imported at module import time. ``api`` is a test seam.
Only the uniquely identifiable ACTIVE top-level window is exposed for editing.
"""
from __future__ import annotations

import hashlib
import importlib
import math
import os
from pathlib import Path
import platform

from backend_common import BackendError
from input_actions import INPUT_KINDS
from linux_input import LinuxInput, rectangle


class LinuxBackend:
    def __init__(self, *, max_nodes=1200, max_depth=12, api=None, input_driver=None):
        if type(max_nodes) is not int or not 1 <= max_nodes <= 10000:
            raise ValueError("max_nodes must be an integer from 1 to 10000")
        if type(max_depth) is not int or not 1 <= max_depth <= 40:
            raise ValueError("max_depth must be an integer from 1 to 40")
        if api is None:
            if platform.system() != "Linux":
                raise BackendError("backend_unavailable", "The Linux backend requires Linux.")
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                raise BackendError("backend_unavailable", "Run inside the logged-in Linux graphical desktop session; DISPLAY/WAYLAND_DISPLAY is missing.")
            runtime = os.environ.get("XDG_RUNTIME_DIR")
            if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not (runtime and (Path(runtime) / "bus").exists()):
                raise BackendError("backend_unavailable", "The desktop user's D-Bus session is unavailable; run from that user's graphical session.")
            try:
                api = importlib.import_module("pyatspi")
            except (ImportError, ValueError) as exc:
                raise BackendError("backend_unavailable", "pyatspi2 is unavailable. On Debian/Ubuntu install python3-venv and python3-pyatspi, create a venv with /usr/bin/python3 -m venv --system-site-packages, and start the server with its Python; see references/operations.md#linux.",
                                   {"dependency": "python3-pyatspi"}) from exc
        self.api = api
        self.max_nodes, self.max_depth = max_nodes, max_depth
        self._closed = False
        self._records = {}
        self._active = None
        self._activation = {}
        self._truncated = False
        self._input = input_driver if input_driver is not None else LinuxInput(api)
        self._last_state = None

    def _check_open(self):
        if self._closed:
            raise BackendError("backend_unavailable", "Backend has been closed.")

    @staticmethod
    def _refresh(element):
        # pyatspi's clearCache recursively invalidates libatspi's cached fields.
        # A JSONL worker does not run Registry.start()'s perpetual GLib loop.
        element.clearCache()

    def _state(self, element):
        state = element.getState()
        if state.contains(self.api.STATE_DEFUNCT):
            raise BackendError("element_unavailable", "AT-SPI object is defunct; read a fresh snapshot.")
        return state

    @staticmethod
    def _interface(element, method):
        try:
            return getattr(element, method)()
        except NotImplementedError:
            return None

    def _focusable(self, element, state):
        return (state.contains(self.api.STATE_FOCUSABLE)
                and self._interface(element, "queryComponent") is not None)

    def _scan(self):
        """Return apps, unique active frame and unambiguous activation targets."""
        try:
            desktop = self.api.Registry.getDesktop(0)
            if desktop is None:
                raise RuntimeError("No AT-SPI desktop")
            self._refresh(desktop)
            count = desktop.childCount
        except Exception as exc:
            raise BackendError("backend_unavailable", "Cannot read the AT-SPI desktop. Check the user D-Bus session, at-spi2-core, and application accessibility support.") from exc
        if count == 0:
            raise BackendError("backend_unavailable", "AT-SPI exposes no applications. Check accessibility support in this graphical session.")
        apps, active, targets = {}, [], {}
        incomplete = count > 256
        frame_budget = 1024
        frame_roles = {getattr(self.api, name) for name in ("ROLE_FRAME", "ROLE_DIALOG", "ROLE_WINDOW") if hasattr(self.api, name)}
        for index in range(min(count, 256)):
            try:
                app = desktop.getChildAtIndex(index)
                self._state(app)
                pid = app.get_process_id()
                if type(pid) is not int or pid <= 0 or pid in apps:
                    incomplete = True
                    continue
                name = app.name
                title, title_hash, clipped = self._text(name)
                apps[pid] = {"pid": pid, "name": title, "bundle_id": None,
                             "name_hash": title_hash, "text_truncated": clipped, "can_activate": False}
                self._truncated |= clipped
                candidates = []
                children = app.childCount
                take = min(children, frame_budget)
                frame_budget -= take
                if take < children:
                    incomplete = True
                for child_index in range(take):
                    try:
                        frame = app.getChildAtIndex(child_index)
                        if frame.getRole() not in frame_roles:
                            continue
                        state = self._state(frame)
                        if state.contains(self.api.STATE_ACTIVE):
                            active.append((pid, frame, child_index))
                        if self._focusable(frame, state) and state.contains(self.api.STATE_ENABLED):
                            candidates.append(frame)
                    except Exception:
                        incomplete = True
                # A pid cannot select between several windows; do not choose one.
                if len(candidates) == 1:
                    targets[pid] = candidates[0]
                    apps[pid]["can_activate"] = True
            except Exception:
                incomplete = True
        foreground = active[0] if len(active) == 1 and not incomplete else None
        reason = "incomplete" if incomplete else "identified" if foreground else "ambiguous" if active else "unavailable"
        return apps, foreground, targets, reason, incomplete

    @staticmethod
    def _text(value):
        if not isinstance(value, str):
            return None, None, False
        return value[:4096], hashlib.sha256(value.encode("utf-8")).hexdigest(), len(value) > 4096

    def _action_names(self, element):
        interface = self._interface(element, "queryAction")
        if interface is None:
            return None, {}
        count = interface.nActions
        if count > 128:
            self._truncated = True
        names = {}
        duplicates = set()
        for index in range(min(count, 128)):
            name = interface.getName(index)
            if not isinstance(name, str) or not name or len(name) > 256:
                self._truncated = True
                continue
            if name in names:
                duplicates.add(name)
            names[name] = index
        # Name-addressed actions must not silently select an ambiguous index.
        for name in duplicates:
            del names[name]
        return interface, names

    def _frame(self, element, state):
        # AT-SPI only promises meaningful screen extents for visible/showing
        # components; missing geometry is not a reason to guess a rectangle.
        try:
            if getattr(self._input, "is_wayland", False):
                return None
            if not all(state.contains(getattr(self.api, "STATE_" + name)) for name in ("VISIBLE", "SHOWING")):
                return None
            component = self._interface(element, "queryComponent")
            return rectangle(component.getExtents(self.api.DESKTOP_COORDS)) if component is not None else None
        except Exception:
            return None

    def _visit(self, element, pid, path, depth, nodes, seen):
        if len(nodes) >= self.max_nodes or depth > self.max_depth:
            self._truncated = True
            return None
        if element in seen:
            return seen[element]
        state = self._state(element)
        role = element.getRole()
        secure = role == self.api.ROLE_PASSWORD_TEXT
        _, actions = self._action_names(element)
        settable = []
        if not secure and state.contains(self.api.STATE_EDITABLE) and self._interface(element, "queryEditableText") is not None:
            settable.append("value")
        if self._focusable(element, state):
            settable.append("focused")
        text = None
        if not secure:
            interface = self._interface(element, "queryText")
            if interface is not None:
                text = interface.getText(0, -1)
        title, title_hash, a = self._text(element.name)
        description, description_hash, b = self._text(element.description)
        value, value_hash, c = self._text(text)
        if not secure and text is None:
            interface = self._interface(element, "queryValue")
            if interface is not None:
                numeric = interface.currentValue
                if isinstance(numeric, (int, float)) and math.isfinite(numeric):
                    value = numeric
        node_id = f"{pid}/{path}"
        node = {"id": node_id, "pid": pid, "role": element.getRoleName(),
                "title": title, "description": description, "value": value,
                "title_hash": title_hash, "description_hash": description_hash, "value_hash": value_hash,
                "text_truncated": a or b or c, "secure": secure,
                "frame": self._frame(element, state),
                "enabled": state.contains(self.api.STATE_ENABLED), "focused": state.contains(self.api.STATE_FOCUSED),
                "actions": sorted(actions), "settable": sorted(settable), "children": []}
        # These flags let agents verify toggles/selections and let preflight
        # detect state changes even when the element's text stays unchanged.
        for name in ("CHECKED", "SELECTED", "EXPANDED", "PRESSED", "INDETERMINATE", "BUSY", "VISIBLE", "SHOWING"):
            constant = getattr(self.api, "STATE_" + name, None)
            node[name.lower()] = state.contains(constant) if constant is not None else None
        self._truncated |= node["text_truncated"]
        nodes.append(node)
        seen[element] = node_id
        self._records[node_id] = (element, set(actions), set(settable))
        try:
            count = element.childCount
            limit = min(count, self.max_nodes - len(nodes))
            if limit < count:
                self._truncated = True
            for index in range(limit):
                if len(nodes) >= self.max_nodes or depth == self.max_depth:
                    self._truncated = True
                    break
                try:
                    child = element.getChildAtIndex(index)
                    child_id = self._visit(child, pid, f"{path}.{index}", depth + 1, nodes, seen)
                    if child_id is not None:
                        node["children"].append(child_id)
                except Exception:
                    self._truncated = True
        except Exception:
            self._truncated = True
        return node_id

    def snapshot(self):
        self._check_open()
        self._records, self._activation, self._active = {}, {}, None
        self._truncated = False
        apps, active, targets, reason, incomplete = self._scan()
        self._truncated |= incomplete
        self._active, self._activation = active, targets
        nodes, roots = [], []
        if active:
            pid, frame, index = active
            try:
                root = self._visit(frame, pid, str(index), 0, nodes, {})
                if root is not None:
                    roots.append(root)
            except Exception:
                self._truncated = True
                self._records.clear()
                self._active = None
                reason = "unavailable"
                nodes = []
        identified = self._active is not None
        input_capabilities, input_state = self._input.status(identified)
        state = {"platform": "linux", "desktop_id": "local",
                "frontmost_app": apps[self._active[0]] if identified else None,
                "applications": [apps[pid] for pid in sorted(apps)], "roots": roots, "nodes": nodes,
                "truncated": self._truncated, "input": input_state,
                "capabilities": {"snapshot": True, "accessibility": True, "screenshots": False,
                    **input_capabilities,
                    "perform": identified, "set_value": identified, "focus": identified,
                    "activate_app": identified and bool(targets), "scope": "active_window",
                    "foreground_status": reason, "max_nodes": self.max_nodes, "max_depth": self.max_depth,
                    "activation_method": "AT-SPI Component.grabFocus on an unambiguous focusable window",
                    "activation_limitations": "Application/toolkit/compositor may decline focus, including on Wayland; verify status."}}
        self._last_state = state
        return state

    def _verify_foreground(self):
        if self._active is None:
            raise BackendError("unsupported", "No unique active AT-SPI window was identified; UI mutations are unavailable.")
        _, current, targets, _, _ = self._scan()
        if current is None or current[:2] != self._active[:2]:
            raise BackendError("element_unavailable", "The foreground window changed or cannot be verified; read a fresh snapshot.")
        return targets

    def act(self, action):
        self._check_open()
        if not isinstance(action, dict):
            raise BackendError("invalid_action", "Action must be an object.")
        kind = action.get("kind")
        if isinstance(kind, str) and kind in INPUT_KINDS:
            if self._last_state is None:
                raise BackendError("unsupported", "Read a fresh snapshot before native input.")
            return self._input.act(action, self._last_state, self._verify_foreground)
        fields = {"perform": {"kind", "node_id", "action"}, "set_value": {"kind", "node_id", "value"},
                  "focus": {"kind", "node_id"}, "activate_app": {"kind", "pid"}}
        if not isinstance(kind, str) or kind not in fields or set(action) != fields[kind]:
            raise BackendError("invalid_action", "Unsupported action or fields.")
        try:
            targets = self._verify_foreground()
            if kind == "activate_app":
                pid = action["pid"]
                if type(pid) is not int or pid <= 0:
                    raise BackendError("invalid_action", "pid must be a positive integer.")
                target = self._activation.get(pid)
                if target is None or targets.get(pid) != target:
                    raise BackendError("unsupported", "Application has no unique observed focusable AT-SPI window.")
                accepted = target.queryComponent().grabFocus()
            else:
                node_id = action["node_id"]
                if not isinstance(node_id, str) or node_id not in self._records:
                    raise BackendError("element_unavailable", "Node was not in the latest snapshot.")
                element, actions, settable = self._records[node_id]
                self._refresh(element)
                state = self._state(element)
                if not state.contains(self.api.STATE_ENABLED):
                    raise BackendError("invalid_action", "Node is disabled.")
                if kind == "perform":
                    name = action["action"]
                    interface, current = self._action_names(element)
                    if not isinstance(name, str) or name not in actions or name not in current:
                        raise BackendError("unsupported", "Action is not advertised by this node.")
                    accepted = interface.doAction(current[name])
                elif kind == "set_value":
                    value = action["value"]
                    if not isinstance(value, str) or len(value) > 16384 or "\0" in value:
                        raise BackendError("invalid_action", "value must be at most 16384 characters without NUL.")
                    if element.getRole() == self.api.ROLE_PASSWORD_TEXT or "value" not in settable or not state.contains(self.api.STATE_EDITABLE):
                        raise BackendError("unsupported", "Node is not editable or is a password field.")
                    interface = self._interface(element, "queryEditableText")
                    if interface is None:
                        raise BackendError("unsupported", "Node no longer supports editable text.")
                    accepted = interface.setTextContents(value)
                else:
                    if "focused" not in settable or not self._focusable(element, state):
                        raise BackendError("unsupported", "Node does not support focus.")
                    accepted = element.queryComponent().grabFocus()
            if not accepted:
                raise BackendError("unsupported", "AT-SPI application or compositor declined the operation; inspect status.")
            return {"accepted": True, "kind": kind}
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("element_unavailable", "AT-SPI object became unavailable; inspect status before retrying.") from exc

    def close(self):
        self._closed = True
        self._records.clear()
        self._activation.clear()
        self._active = None
        self._last_state = None
        self._input.close()
