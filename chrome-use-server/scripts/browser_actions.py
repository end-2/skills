"""Validate browser mutations against the caller's current observed state."""
import math
from urllib.parse import urlsplit

from backend_common import BackendError


def reject(message, code="invalid_action"):
    raise BackendError(code, message)


def text(value, name, maximum=256, *, empty=False):
    if not isinstance(value, str) or (not empty and not value) or len(value) > maximum:
        reject(f"{name} must be a string of at most {maximum} characters")
    return value


def validate_url(value):
    text(value, "url", 8192)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        reject("url must not contain control characters")
    if value == "about:blank":
        return
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            reject("Navigation supports http, https, and about:blank only")
        if parsed.username is not None or parsed.password is not None:
            reject("Do not put credentials in URLs")
        parsed.port
    except ValueError:
        reject("Invalid URL")


def validate_action(action, state):
    if not isinstance(action, dict):
        reject("action must be an object")
    fields = {
        "new_tab": {"url"}, "select_tab": {"tab_id"}, "close_tab": {"tab_id"},
        "navigate": {"tab_id", "url"},
        "click": {"tab_id", "node_id"}, "hover": {"tab_id", "node_id"},
        "focus": {"tab_id", "node_id"}, "fill": {"tab_id", "node_id", "value"},
        "press_key": {"tab_id", "key"},
        "scroll": {"tab_id", "direction", "pixels"},
        "handle_dialog": {"tab_id", "response"},
    }
    kind = action.get("kind")
    if not isinstance(kind, str) or kind not in fields:
        reject("Unsupported browser action")
    optional = {"text"} if kind == "handle_dialog" else set()
    required = fields[kind] | {"kind"}
    if not required <= action.keys() or set(action) - required - optional:
        reject("Unexpected or missing action fields")
    if "url" in action:
        validate_url(action["url"])
    if "tab_id" in action:
        tab_id = text(action["tab_id"], "tab_id")
        if not any(tab.get("id") == tab_id for tab in state.get("tabs", [])):
            reject("Tab is not in the observed state", "element_unavailable")
        if kind not in {"select_tab", "close_tab"} and tab_id != state.get("selected_tab_id"):
            reject("Select this tab and read status before acting", "element_unavailable")
    if state.get("pending_dialog") and kind != "handle_dialog":
        reject("Resolve the observed dialog before other mutations", "dialog_pending")
    if "node_id" in action:
        node_id = text(action["node_id"], "node_id", 512)
        node = next((item for item in state.get("nodes", []) if item.get("id") == node_id), None)
        if not node or node.get("tab_id") != action["tab_id"]:
            reject("Node is not in the observed tab", "element_unavailable")
        if node.get("enabled") is False:
            reject("Node is disabled")
        if kind in {"click", "hover"} and kind not in node.get("actions", []):
            reject("Node does not advertise this action", "unsupported")
        if kind == "focus" and "focused" not in node.get("settable", []):
            reject("Node does not support focus", "unsupported")
        if kind == "fill":
            text(action["value"], "value", 16384, empty=True)
            if "value" not in node.get("settable", []):
                reject("Node does not support filling", "unsupported")
            if node.get("value_truncated"):
                reject("Cannot replace a field whose complete current value was not observed")
    if kind == "press_key":
        text(action["key"], "key", 128)
    if kind == "scroll":
        if action["direction"] not in ("up", "down", "left", "right"):
            reject("direction must be up, down, left, or right")
        pixels = action["pixels"]
        if isinstance(pixels, bool) or not isinstance(pixels, (int, float)) or not 1 <= pixels <= 5000 or not math.isfinite(pixels):
            reject("pixels must be between 1 and 5000")
    if kind == "handle_dialog":
        dialog = state.get("pending_dialog")
        if not dialog:
            reject("No dialog was observed", "element_unavailable")
        if action["response"] not in ("accept", "dismiss"):
            reject("response must be accept or dismiss")
        if "text" in action:
            text(action["text"], "text", 16384, empty=True)
            if dialog.get("type") != "prompt" or action["response"] != "accept":
                reject("text is only supported when accepting a prompt")
    return dict(action)
