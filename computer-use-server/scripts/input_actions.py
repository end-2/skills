"""Portable input validation, shared by the broker and Python native adapters."""
import math

from backend_common import BackendError


INPUT_KINDS = frozenset({"type_text", "key_press", "mouse_move", "mouse_click", "mouse_drag", "scroll"})
MODIFIERS = frozenset({"ctrl", "alt", "shift", "meta"})
KEYS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-=[]\\;',./`") | {
    "space", "enter", "tab", "escape", "backspace", "delete", "insert", "home", "end",
    "page_up", "page_down", "left", "right", "up", "down", *[f"f{i}" for i in range(1, 13)],
}


def invalid(message):
    raise BackendError("invalid_action", message)


def finite_number(value):
    # Bound integers before float conversion; JSON integers can be arbitrarily large.
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and -10_000_000 <= value <= 10_000_000 and math.isfinite(value))


def validate_input_action(action, state):
    """Validate and supply explicit defaults without changing the caller's payload."""
    if not isinstance(action, dict) or not isinstance(action.get("kind"), str) or action["kind"] not in INPUT_KINDS:
        invalid("Unsupported native input action")
    kind = action["kind"]
    required, optional = {
        "type_text": ({"text"}, set()),
        "key_press": ({"key"}, {"modifiers"}),
        "mouse_move": ({"x", "y"}, set()),
        "mouse_click": ({"x", "y"}, {"button", "count", "modifiers"}),
        "mouse_drag": ({"from_x", "from_y", "to_x", "to_y"}, {"button", "duration_ms", "modifiers"}),
        "scroll": ({"x", "y", "delta_x", "delta_y"}, set()),
    }[kind]
    required = required | {"kind", "pid"}
    if not required <= action.keys() or not action.keys() <= required | optional:
        invalid(f"Unexpected or missing fields for {kind}")
    pid = action["pid"]
    if type(pid) is not int or not 0 < pid <= 2_147_483_647:
        invalid("pid must be a positive process identifier")
    if pid != (state.get("frontmost_app") or {}).get("pid"):
        raise BackendError("element_unavailable", "Input target is not the observed foreground app; observe and activate it first")
    if state.get("capabilities", {}).get(kind) is not True:
        raise BackendError("unsupported", f"The current desktop does not advertise {kind}")
    result = dict(action)
    if "modifiers" in optional:
        modifiers = action.get("modifiers", [])
        if (not isinstance(modifiers, list) or any(not isinstance(item, str) or item not in MODIFIERS for item in modifiers)
                or len(modifiers) != len(set(modifiers))):
            invalid("modifiers must contain unique ctrl, alt, shift, or meta names")
        result["modifiers"] = list(modifiers)
    if kind == "type_text":
        text = action["text"]
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            invalid("text must contain 1 to 4096 Unicode characters")
        try:
            text.encode("utf-8")
        except UnicodeError:
            invalid("text must contain valid Unicode scalar values")
        if "\0" in text:
            invalid("text cannot contain NUL")
    if kind == "key_press" and (not isinstance(action["key"], str) or action["key"] not in KEYS):
        invalid("key must be a supported lowercase key name; use type_text for Unicode text")
    if "button" in optional:
        result["button"] = action.get("button", "left")
        if result["button"] not in ("left", "right", "middle"):
            invalid("button must be left, right, or middle")
    if kind == "mouse_click":
        result["count"] = action.get("count", 1)
        if type(result["count"]) is not int or not 1 <= result["count"] <= 3:
            invalid("count must be an integer from 1 to 3")
    if kind == "mouse_drag":
        result["duration_ms"] = action.get("duration_ms", 400)
        if type(result["duration_ms"]) is not int or not 100 <= result["duration_ms"] <= 2000:
            invalid("duration_ms must be an integer from 100 to 2000")
    if kind == "scroll":
        if any(type(action[key]) is not int or not -20 <= action[key] <= 20 for key in ("delta_x", "delta_y")):
            invalid("scroll deltas must be integer wheel steps from -20 to 20")
        if action["delta_x"] == action["delta_y"] == 0:
            invalid("At least one scroll delta must be nonzero")
    points = [("x", "y")] if kind in {"mouse_move", "mouse_click", "scroll"} else []
    if kind == "mouse_drag":
        points = [("from_x", "from_y"), ("to_x", "to_y")]
    displays = state.get("input", {}).get("displays", [])
    for x_key, y_key in points:
        x, y = action[x_key], action[y_key]
        if not finite_number(x) or not finite_number(y):
            invalid("Pointer coordinates must be finite screen coordinates")
        def contains(display):
            if not isinstance(display, dict) or any(not finite_number(display.get(key)) for key in ("x", "y", "width", "height")):
                return False
            return (display["width"] > 0 and display["height"] > 0
                    and display["x"] <= x < display["x"] + display["width"]
                    and display["y"] <= y < display["y"] + display["height"])
        if not any(contains(display) for display in displays):
            invalid("Pointer coordinates must lie inside an observed display")
    return result
