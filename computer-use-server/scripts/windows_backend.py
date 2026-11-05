"""Windows UI Automation adapter, used only inside one dedicated worker thread.

Optional Windows dependency: ``python -m pip install uiautomation==2.0.29``.
Source/API reference: https://github.com/yinkaisheng/Python-UIAutomation-for-Windows
The worker owns the COM apartment and all retained controls. Explicit native
input uses SendInput without elevation or foreground-lock bypass. A hung native
provider is terminated by the parent worker transport. Screenshots are not used.
"""
from __future__ import annotations

import ctypes
import hashlib
import importlib
import ntpath
import platform
import sys
import threading
from typing import Any

from backend_common import BackendError
from input_actions import INPUT_KINDS, finite_number
from windows_input import WindowsInput


TEXT_LIMIT = 4096
_ACTION_PATTERNS = {
    "Invoke": "InvokePattern",
    "Toggle": "TogglePattern",
    "Select": "SelectionItemPattern",
    "Expand": "ExpandCollapsePattern",
    "Collapse": "ExpandCollapsePattern",
}


def _text(value: Any) -> tuple[str | None, str | None, bool]:
    """Hash full provider text; the displayed prefix alone cannot detect edits."""
    if value is None:
        return None, None, False
    if not isinstance(value, str):
        raise BackendError("backend_unavailable", "UI Automation returned non-text content for a text property.")
    digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
    # Unpaired UTF-16 surrogates from a provider must not break JSON UTF-8 output.
    prefix = value[:TEXT_LIMIT].encode("utf-8", errors="replace").decode("utf-8")
    return prefix, digest, len(value) > TEXT_LIMIT or prefix != value[:TEXT_LIMIT]


def _native_error(exc: Exception, operation: str) -> BackendError:
    if isinstance(exc, BackendError):
        return exc
    hresult = getattr(exc, "hresult", None)
    if not isinstance(hresult, int) and exc.args and isinstance(exc.args[0], int):
        hresult = exc.args[0]
    unsigned = hresult & 0xFFFFFFFF if isinstance(hresult, int) else None
    if isinstance(exc, PermissionError) or unsigned == 0x80070005:
        code = "permission_denied"
    elif unsigned in (0x80040201, 0x80010108, 0x800706BA):
        code = "element_unavailable"
    elif unsigned in (0x80040204, 0x80004001):
        code = "unsupported"
    elif unsigned in (0x80070057, 0x80040200):
        code = "invalid_action"
    else:
        code = "backend_unavailable"
    details: dict[str, Any] = {"operation": operation, "exception_type": type(exc).__name__}
    if unsigned is not None:
        details["hresult"] = f"0x{unsigned:08x}"
    return BackendError(code, f"Windows UI Automation failed during {operation}.", details)


def _process_name(pid: int) -> str | None:
    """Query executable basename without reading process memory or command lines."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                  wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return ntpath.basename(buffer.value)
        return None
    finally:
        kernel32.CloseHandle(handle)


def _frame(control):
    """UIA bounding rectangles use physical screen coordinates; absent is null."""
    try:
        rect = control.BoundingRectangle
        values = (rect.left, rect.top, rect.right, rect.bottom)
        if not all(finite_number(value) for value in values):
            return None
        left, top, right, bottom = values
        if right <= left or bottom <= top:
            return None
        return {"x": left, "y": top, "width": right-left, "height": bottom-top}
    except Exception:
        # Some providers omit geometry while still supporting semantic actions.
        return None


class WindowsBackend:
    """UIA controls and COM initialization never leave the creating thread.

    ``_automation`` and ``_name_lookup`` are private fake-native test seams.
    Actual construction still requires Windows; imports remain optional on
    other operating systems. The worker's main thread must own no UI windows.
    """

    def __init__(self, *, max_nodes: int = 1200, max_depth: int = 12,
                 _automation=None, _name_lookup=None, _input=None):
        if platform.system() != "Windows":
            raise BackendError("backend_unavailable", "The Windows backend requires Windows.")
        if type(max_nodes) is not int or not 1 <= max_nodes <= 10000:
            raise ValueError("max_nodes must be an integer from 1 to 10000")
        if type(max_depth) is not int or not 1 <= max_depth <= 40:
            raise ValueError("max_depth must be an integer from 1 to 40")
        self.max_nodes, self.max_depth = max_nodes, max_depth
        self._thread = threading.get_ident()
        self._closed = False
        self._initialized = False
        self._auto = None
        self._name_lookup = _name_lookup or _process_name
        self._input = _input if _input is not None else WindowsInput()
        self._reset()
        missing = object()
        previous = getattr(sys, "coinit_flags", missing)
        try:
            # comtypes initializes COM at import; ensure it chooses MTA first.
            sys.coinit_flags = 0  # COINIT_MULTITHREADED, in dedicated worker only
            try:
                self._auto = _automation or importlib.import_module("uiautomation")
            except ImportError as exc:
                raise BackendError("backend_unavailable", "Windows UI Automation dependency is missing.",
                                   {"install": "python -m pip install uiautomation==2.0.29",
                                    "dependency": "uiautomation"}) from exc
            # Balance this adapter-owned COM reference in close. comtypes owns
            # its separate import-time reference and releases it at process exit.
            self._auto.InitializeUIAutomationInCurrentThread()
            self._initialized = True
        except Exception as exc:
            raise _native_error(exc, "initialization") from exc
        finally:
            if previous is missing:
                del sys.coinit_flags
            else:
                sys.coinit_flags = previous

    def _check(self) -> None:
        if threading.get_ident() != self._thread:
            raise BackendError("backend_unavailable", "Windows UI Automation must run on its owning worker thread.")
        if self._closed:
            raise BackendError("backend_unavailable", "Backend has been closed.")

    def _reset(self) -> None:
        self._last_state = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._apps: dict[int, dict[str, Any]] = {}
        self._frontmost = None
        self._front_identity = None
        self._nodes: list[dict[str, Any]] = []
        self._seen: dict[tuple[int, ...], str] = {}
        self._truncated = False

    @staticmethod
    def _runtime_id(control) -> tuple[int, ...]:
        raw = control.GetRuntimeId()
        if raw is None:
            return ()
        if not isinstance(raw, (tuple, list)) or any(type(part) is not int for part in raw):
            raise BackendError("backend_unavailable", "UI Automation returned an invalid runtime identifier.")
        return tuple(raw)

    def _identity(self, control) -> tuple[int, int, tuple[int, ...]]:
        return int(control.ProcessId), int(control.NativeWindowHandle), self._runtime_id(control)

    def _pattern(self, control, name: str):
        return control.GetPattern(getattr(self._auto.PatternId, name))

    def _inspect_patterns(self, control, secure: bool):
        patterns = {name: self._pattern(control, name) for name in sorted(set(_ACTION_PATTERNS.values()))}
        actions = [action for action, name in _ACTION_PATTERNS.items() if patterns[name] is not None]
        states: dict[str, Any] = {}
        toggle = patterns["TogglePattern"]
        if toggle is not None:
            states["toggle"] = int(toggle.ToggleState)
        selected = patterns["SelectionItemPattern"]
        if selected is not None:
            states["selected"] = bool(selected.IsSelected)
        expanded = patterns["ExpandCollapsePattern"]
        if expanded is not None:
            state = int(expanded.ExpandCollapseState)
            states["expand_collapse"] = state
            if state == 3:  # ExpandCollapseState.LeafNode
                actions = [action for action in actions if action not in ("Expand", "Collapse")]
        # Do not even request ValuePattern on a password control.
        value_pattern = None if secure else self._pattern(control, "ValuePattern")
        return sorted(actions), states, value_pattern

    def _visit(self, control, path: str, depth: int) -> str | None:
        runtime_id = self._runtime_id(control)
        if runtime_id and runtime_id in self._seen:
            return self._seen[runtime_id]
        if len(self._nodes) >= self.max_nodes or depth > self.max_depth:
            self._truncated = True
            return None
        pid = int(control.ProcessId)
        node_id = f"{pid}/{path}"
        secure = bool(control.IsPassword)
        enabled = bool(control.IsEnabled)
        actions, states, value_pattern = self._inspect_patterns(control, secure)
        title, title_hash, title_clipped = _text(control.Name)
        description, description_hash, description_clipped = _text(control.HelpText)
        raw_value = value_pattern.Value if value_pattern is not None else None
        value, value_hash, value_clipped = _text(raw_value)
        settable = []
        if enabled and value_pattern is not None and not value_pattern.IsReadOnly:
            settable.append("value")
        if enabled and control.IsKeyboardFocusable:
            settable.append("focused")
        text_truncated = title_clipped or description_clipped or value_clipped
        self._truncated = self._truncated or text_truncated
        node = {"id": node_id, "pid": pid, "role": str(control.ControlTypeName),
                "title": title, "description": description, "value": value,
                "enabled": enabled, "focused": bool(control.HasKeyboardFocus),
                "actions": actions, "settable": sorted(settable), "children": [],
                "secure": secure, "text_truncated": text_truncated,
                "title_hash": title_hash, "description_hash": description_hash, "value_hash": value_hash,
                "runtime_id": list(runtime_id), "states": states, "frame": _frame(control)}
        self._nodes.append(node)
        self._records[node_id] = {"control": control, "runtime_id": runtime_id, "pid": pid,
                                  "actions": set(actions), "settable": set(settable)}
        if runtime_id:
            self._seen[runtime_id] = node_id
        child = control.GetFirstChildControl()
        if child is not None and (depth == self.max_depth or len(self._nodes) >= self.max_nodes):
            self._truncated = True
            return node_id
        index = 0
        siblings: set[tuple[int, ...]] = set()
        while child is not None:
            child_runtime = self._runtime_id(child)
            if child_runtime and child_runtime in siblings:
                self._truncated = True
                break
            if child_runtime:
                siblings.add(child_runtime)
            child_id = self._visit(child, f"{path}.{index}", depth + 1)
            if child_id is not None:
                node["children"].append(child_id)
            following = child.GetNextSiblingControl()
            if len(self._nodes) >= self.max_nodes or index + 1 >= self.max_nodes:
                if following is not None:
                    self._truncated = True
                break
            child, index = following, index + 1
        return node_id

    def _app_json(self, pid: int) -> dict[str, Any]:
        return {"pid": pid, "name": self._apps[pid]["name"], "bundle_id": None}

    def _check_foreground(self) -> None:
        current = self._auto.GetForegroundControl()
        if current is None or self._frontmost is None or self._identity(current) != self._front_identity or not self._auto.ControlsAreSame(current, self._frontmost):
            raise BackendError("element_unavailable", "The foreground window changed. Read a fresh snapshot.")

    def snapshot(self) -> dict[str, Any]:
        self._check()
        self._reset()
        try:
            self._frontmost = self._auto.GetForegroundControl()
            if self._frontmost is not None:
                self._front_identity = self._identity(self._frontmost)
            root = self._auto.GetRootControl()
            current = root.GetFirstChildControl()
            seen_windows = set()
            visited = 0
            while current is not None and visited < self.max_nodes:
                identity = self._identity(current)
                if identity in seen_windows:
                    self._truncated = True
                    break
                seen_windows.add(identity)
                pid, handle, runtime_id = identity
                if pid > 0 and handle and self._auto.IsWindowVisible(handle):
                    existing = self._apps.get(pid)
                    if existing is None or handle < existing["handle"]:
                        self._apps[pid] = {"name": self._name_lookup(pid) or f"Process {pid}",
                                           "control": current, "handle": handle, "runtime_id": runtime_id}
                current = current.GetNextSiblingControl()
                visited += 1
            if current is not None:
                self._truncated = True
            roots = []
            frontmost = None
            if self._frontmost is not None:
                pid, handle, runtime_id = self._front_identity
                self._apps[pid] = {"name": self._name_lookup(pid) or f"Process {pid}",
                                   "control": self._frontmost, "handle": handle, "runtime_id": runtime_id}
                frontmost = self._app_json(pid)
                frontmost["window_id"] = handle
                node_id = self._visit(self._frontmost, "0", 0)
                if node_id is not None:
                    roots.append(node_id)
                self._check_foreground()
            input_state, input_caps = self._input.snapshot(frontmost["pid"] if frontmost else None)
            state = {"platform": "windows", "desktop_id": "local", "frontmost_app": frontmost,
                    "applications": [self._app_json(pid) for pid in sorted(self._apps)],
                    "roots": roots, "nodes": self._nodes, "truncated": self._truncated, "input": input_state,
                    "capabilities": {"snapshot": True, "perform": True, "set_value": True,
                                     "focus": True, "activate_app": True, "screenshots": False,
                                     "scope": "foreground_window", "application_scope": "visible_top_level_windows",
                                     "max_nodes": self.max_nodes, "max_depth": self.max_depth, **input_caps}}
            self._last_state = state
            return state
        except Exception as exc:
            self._reset()
            raise _native_error(exc, "snapshot") from exc

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        self._check()
        try:
            if not isinstance(action, dict) or not isinstance(action.get("kind"), str):
                raise BackendError("invalid_action", "action must be an object with a string kind.")
            kind = action["kind"]
            if kind in INPUT_KINDS:
                self._check_foreground()
                return self._input.act(action, self._last_state)
            fields = {"activate_app": {"kind", "pid"}, "perform": {"kind", "node_id", "action"},
                      "set_value": {"kind", "node_id", "value"}, "focus": {"kind", "node_id"}}
            if kind not in fields:
                raise BackendError("unsupported", "Unsupported action kind.")
            if set(action) != fields[kind]:
                raise BackendError("invalid_action", "Unexpected or missing action fields.")
            if kind == "activate_app":
                pid = action["pid"]
                if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
                    raise BackendError("invalid_action", "pid must be a positive process identifier.")
                app = self._apps.get(pid)
                if app is None:
                    raise BackendError("element_unavailable", "Application is not in the latest snapshot.")
                current = self._auto.ControlFromHandle(app["handle"])
                if current is None or self._identity(current) != (pid, app["handle"], app["runtime_id"]) or not self._auto.ControlsAreSame(current, app["control"]):
                    raise BackendError("element_unavailable", "The observed application window is no longer available.")
                if not current.IsEnabled:
                    raise BackendError("invalid_action", "The observed application window is disabled.")
                if not self._auto.SetForegroundWindow(app["handle"]):
                    raise BackendError("unsupported", "Windows declined foreground activation; no synthetic-input workaround was attempted.",
                                       {"reason": "foreground_activation_denied"})
                return {"accepted": True, "kind": kind, "pid": pid}
            node_id = action["node_id"]
            if not isinstance(node_id, str) or not node_id:
                raise BackendError("invalid_action", "node_id must be a nonempty string.")
            record = self._records.get(node_id)
            if record is None:
                raise BackendError("element_unavailable", "Element is not in the latest snapshot.")
            self._check_foreground()
            control = record["control"]
            if int(control.ProcessId) != record["pid"] or self._runtime_id(control) != record["runtime_id"]:
                raise BackendError("element_unavailable", "Element identity changed. Read a fresh snapshot.")
            if not control.IsEnabled:
                raise BackendError("invalid_action", "Element is disabled.")
            if kind == "perform":
                name = action["action"]
                if not isinstance(name, str) or name not in record["actions"] or name not in _ACTION_PATTERNS:
                    raise BackendError("unsupported", "Action is not advertised by this element.")
                pattern = self._pattern(control, _ACTION_PATTERNS[name])
                if pattern is None or (name in ("Expand", "Collapse") and int(pattern.ExpandCollapseState) == 3):
                    raise BackendError("unsupported", "Element no longer supports this action.")
                accepted = getattr(pattern, name)(waitTime=0)
            elif kind == "set_value":
                value = action["value"]
                if not isinstance(value, str) or len(value) > 16384:
                    raise BackendError("invalid_action", "value must be a string of at most 16384 characters.")
                if control.IsPassword or "value" not in record["settable"]:
                    raise BackendError("unsupported", "Element value is not writable or is a password field.")
                pattern = self._pattern(control, "ValuePattern")
                if pattern is None or pattern.IsReadOnly:
                    raise BackendError("unsupported", "Element no longer exposes a writable ValuePattern.")
                accepted = pattern.SetValue(value, waitTime=0)
            else:
                if "focused" not in record["settable"] or not control.IsKeyboardFocusable:
                    raise BackendError("unsupported", "Element does not advertise keyboard focus.")
                accepted = control.SetFocus()
            if accepted is not True:
                raise BackendError("element_unavailable", "The UI Automation provider did not accept the action.",
                                   {"action_outcome_unknown": True})
            return {"accepted": True, "kind": kind, "node_id": node_id}
        except Exception as exc:
            self._reset()
            raise _native_error(exc, "action") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._check()
        self._reset()
        self._closed = True
        if self._initialized:
            self._initialized = False
            self._auto.UninitializeUIAutomationInCurrentThread()
        self._auto = None
