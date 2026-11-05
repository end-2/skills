"""Compile the real Swift event builder with a recording sink; never post UI input."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "ax_bridge.swift"
HARNESS = r'''
let line = readLine()!
let request = try JSONSerialization.jsonObject(with: line.data(using: .utf8)!) as! [String: Any]
var events: [[String: Any]] = [], pauses: [Double] = []
let input = NativeInput(post: { event in
    var length = 0
    var units = [UniChar](repeating: 0, count: 64)
    event.keyboardGetUnicodeString(maxStringLength: units.count, actualStringLength: &length, unicodeString: &units)
    events.append([
        "type": Int(event.type.rawValue), "flags": event.flags.rawValue,
        "key": event.getIntegerValueField(.keyboardEventKeycode),
        "text": String(decoding: units.prefix(length), as: UTF16.self),
        "x": event.location.x, "y": event.location.y,
        "click_count": event.getIntegerValueField(.mouseEventClickState),
        "wheel_y": event.getIntegerValueField(.scrollWheelEventDeltaAxis1),
        "wheel_x": event.getIntegerValueField(.scrollWheelEventDeltaAxis2),
        "button": event.getIntegerValueField(.mouseEventButtonNumber),
    ])
}, pause: { pauses.append($0) })
var reply: [String: Any]
do {
    if request["modifier_failure"] as? Bool == true {
        try input.withModifiers(["meta", "shift"]) { _ in
            throw BridgeError(code: "injected_failure", message: "Failure after modifier press")
        }
        reply = ["ok": true]
    } else if let text = request["chunks"] as? String {
        reply = ["ok": true, "chunks": NativeInput.unicodeChunks(text)]
    } else if request["key_names"] as? Bool == true {
        reply = ["ok": true, "keys": NativeInput.keyCodes.keys.sorted()]
    } else if let rectangles = request["frames"] as? [[Double]] {
        reply = ["ok": true, "frames": rectangles.map { values -> Any in
            frameJSON(CGRect(x: values[0], y: values[1], width: values[2], height: values[3])) as Any? ?? NSNull()
        }]
    } else {
        let displays = (request["displays"] as? [[Double]] ?? [[0, 0, 1000, 800], [-600, 0, 600, 400]])
            .map { CGRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3]) }
        let result = try input.perform(request["action"] as! [String: Any], displays: displays) {
            if request["reject_before_dispatch"] as? Bool == true ||
                (request["reject_after_events"] as? Int).map({ events.count >= $0 }) == true {
                throw BridgeError(code: "element_unavailable", message: "Foreground changed")
            }
        }
        reply = ["ok": true, "result": result]
    }
} catch let error as BridgeError {
    reply = ["ok": false, "error": error.json]
}
reply["events"] = events; reply["pauses"] = pauses
let data = try JSONSerialization.data(withJSONObject: reply, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "macOS Swift compiler required")
class MacOSNativeInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        source = Path(cls.directory.name) / "event_test.swift"
        cls.binary = Path(cls.directory.name) / "event_test"
        # Exclude only the production JSONL main loop. The tested event builder
        # is otherwise the unchanged implementation compiled by MacOSBackend.
        source.write_text(SOURCE.read_text().split("let args = CommandLine.arguments\n", 1)[0] + HARNESS)
        built = subprocess.run([shutil.which("swiftc"), str(source), "-o", str(cls.binary)],
                               capture_output=True, text=True, timeout=60)
        if built.returncode:
            raise AssertionError(built.stderr)

    def run_harness(self, **payload):
        result = subprocess.run([str(self.binary)], input=json.dumps(payload, ensure_ascii=False) + "\n",
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def perform(self, kind, **fields):
        return self.run_harness(action=dict(kind=kind, pid=1, **fields))

    def test_unicode_text_chunks_keep_surrogate_pairs_and_release_each_key(self):
        text = "a" * 19 + "😀" + "한글 e\u0301" + "🚀" * 14
        chunks = self.run_harness(chunks=text)["chunks"]
        self.assertTrue(all(0 < len(chunk) <= 20 for chunk in chunks))
        self.assertTrue(all(not 0xD800 <= chunk[-1] <= 0xDBFF for chunk in chunks))
        self.assertTrue(all(not 0xDC00 <= chunk[0] <= 0xDFFF for chunk in chunks))
        result = self.perform("type_text", text=text)
        self.assertTrue(result["ok"])
        self.assertEqual([event["type"] for event in result["events"]], [10, 11] * len(chunks))
        self.assertEqual("".join(event["text"] for event in result["events"][::2]), text)
        self.assertTrue(all(event["flags"] == 0 for event in result["events"]))

    def test_keymap_covers_contract_and_modifiers_release_in_reverse_order(self):
        expected = set("abcdefghijklmnopqrstuvwxyz0123456789-=[]\\;',./`")
        expected.update("space enter tab escape backspace delete insert home end page_up page_down left right up down".split())
        expected.update(f"f{number}" for number in range(1, 13))
        self.assertEqual(set(self.run_harness(key_names=True)["keys"]), expected)
        result = self.perform("key_press", key="a", modifiers=["meta", "shift"])
        events = result["events"]
        self.assertEqual([event["key"] for event in events], [56, 55, 0, 0, 55, 56])
        self.assertEqual(events[2]["type"], 10)
        self.assertEqual(events[3]["type"], 11)
        self.assertEqual(events[2]["flags"], 0x120000)
        self.assertEqual(events[-1]["flags"], 0)

    def test_modifiers_release_when_operation_fails_after_press(self):
        result = self.run_harness(modifier_failure=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "injected_failure")
        self.assertEqual([event["key"] for event in result["events"]], [56, 55, 55, 56])
        self.assertEqual(result["events"][-1]["flags"], 0)

    def test_foreground_recheck_prevents_any_event_dispatch(self):
        for action in (
            dict(kind="type_text", text="hello"), dict(kind="key_press", key="a", modifiers=["meta"]),
            dict(kind="mouse_move", x=10, y=20), dict(kind="mouse_click", x=10, y=20, modifiers=["shift"]),
            dict(kind="mouse_drag", from_x=10, from_y=20, to_x=100, to_y=200),
            dict(kind="scroll", x=10, y=20, delta_x=1, delta_y=1),
        ):
            with self.subTest(kind=action["kind"]):
                result = self.run_harness(action=dict(action, pid=1), reject_before_dispatch=True)
                self.assertEqual(result["error"]["code"], "element_unavailable")
                self.assertEqual(result["events"], [])

    def test_mouse_bounds_use_half_open_displays_including_negative_coordinates(self):
        self.assertTrue(self.perform("mouse_move", x=-600, y=0)["ok"])
        self.assertTrue(self.perform("mouse_move", x=999.5, y=799.5)["ok"])
        for x, y in ((1000, 1), (1, 800), (-600.1, 0), (-1, 400), (10_000_001, 0), (True, 0)):
            with self.subTest(x=x, y=y):
                result = self.perform("mouse_move", x=x, y=y)
                self.assertEqual(result["error"]["code"], "invalid_action")
                self.assertEqual(result["events"], [])

    def test_foreground_change_stops_unicode_chunks_after_releasing_current_key(self):
        result = self.run_harness(action=dict(kind="type_text", pid=1, text="a" * 45), reject_after_events=1)
        self.assertEqual(result["error"]["code"], "element_unavailable")
        self.assertEqual([event["type"] for event in result["events"]], [10, 11])
        self.assertEqual(result["events"][0]["text"], "a" * 20)

    def test_foreground_change_during_chord_releases_already_pressed_modifiers(self):
        result = self.run_harness(action=dict(kind="key_press", pid=1, key="a", modifiers=["shift", "meta"]),
                                  reject_after_events=1)
        self.assertEqual(result["error"]["code"], "element_unavailable")
        self.assertEqual([event["key"] for event in result["events"]], [56, 56])
        self.assertEqual(result["events"][-1]["flags"], 0)

    def test_foreground_change_stops_drag_motion_but_releases_button_and_modifier(self):
        result = self.run_harness(action=dict(kind="mouse_drag", pid=1, from_x=10, from_y=20,
                                              to_x=300, to_y=400, modifiers=["alt"]), reject_after_events=4)
        self.assertEqual(result["error"]["code"], "element_unavailable")
        events = result["events"]
        self.assertEqual([event["type"] for event in events[1:-1]], [5, 1, 6, 2])
        self.assertEqual((events[-2]["x"], events[-2]["y"]), (events[-3]["x"], events[-3]["y"]))
        self.assertNotEqual((events[-2]["x"], events[-2]["y"]), (300, 400))
        self.assertEqual(events[-1]["key"], 58)
        self.assertEqual(events[-1]["flags"], 0)

    def test_foreground_change_between_clicks_releases_button_without_next_click(self):
        result = self.run_harness(action=dict(kind="mouse_click", pid=1, x=50, y=60, count=3, modifiers=["ctrl"]),
                                  reject_after_events=3)
        self.assertEqual(result["error"]["code"], "element_unavailable")
        self.assertEqual([event["type"] for event in result["events"][1:-1]], [5, 1, 2])
        self.assertEqual(result["events"][-1]["flags"], 0)

    def test_foreground_change_after_pointer_move_prevents_scroll_event(self):
        result = self.run_harness(action=dict(kind="scroll", pid=1, x=50, y=60, delta_x=0, delta_y=1),
                                  reject_after_events=1)
        self.assertEqual(result["error"]["code"], "element_unavailable")
        self.assertEqual([event["type"] for event in result["events"]], [5])

    def test_click_count_button_and_modifier_pairing(self):
        result = self.perform("mouse_click", x=50, y=60, button="middle", count=3, modifiers=["ctrl"])
        events = result["events"]
        self.assertEqual([event["type"] for event in events[1:-1]], [5, 25, 26, 25, 26, 25, 26])
        clicks = events[2:-1]
        self.assertEqual([event["click_count"] for event in clicks], [1, 1, 2, 2, 3, 3])
        self.assertTrue(all(event["button"] == 2 for event in clicks))
        self.assertEqual(events[-1]["key"], 59)
        self.assertEqual(events[-1]["flags"], 0)

    def test_drag_uses_observed_points_and_always_posts_button_up(self):
        result = self.perform("mouse_drag", from_x=10, from_y=20, to_x=300, to_y=400,
                              button="right", duration_ms=400, modifiers=["alt"])
        events = result["events"]
        self.assertEqual([event["type"] for event in events[1:3]], [5, 3])
        self.assertTrue(all(event["type"] == 7 for event in events[3:-2]))
        self.assertEqual(events[-2]["type"], 4)
        self.assertEqual((events[-2]["x"], events[-2]["y"]), (300, 400))
        self.assertEqual(events[-1]["flags"], 0)
        self.assertAlmostEqual(sum(result["pauses"]), 0.4)

    def test_drag_rejects_unobserved_gaps_before_holding_any_input(self):
        result = self.run_harness(action=dict(kind="mouse_drag", pid=1, from_x=10, from_y=20, to_x=300, to_y=20),
                                  displays=[[0, 0, 100, 100], [200, 0, 200, 100]])
        self.assertEqual(result["error"]["code"], "invalid_action")
        self.assertEqual(result["events"], [])

    def test_scroll_positions_pointer_and_maps_positive_right_down_to_quartz(self):
        result = self.perform("scroll", x=50, y=60, delta_x=2, delta_y=3)
        self.assertEqual([event["type"] for event in result["events"]], [5, 22])
        self.assertEqual(result["events"][1]["wheel_x"], -2)
        self.assertEqual(result["events"][1]["wheel_y"], -3)
        self.assertEqual((result["events"][1]["x"], result["events"][1]["y"]), (50, 60))

    def test_invalid_input_never_posts_partial_events(self):
        cases = [dict(kind="type_text", text=""), dict(kind="type_text", text="a" * 4097), dict(kind="type_text", text="a\0b"),
                 dict(kind="key_press", key="A"), dict(kind="key_press", key="a", modifiers=["meta", "meta"]),
                 dict(kind="key_press", key="a", modifiers=["super"]), dict(kind="mouse_click", x=1, y=1, count=True),
                 dict(kind="mouse_click", x=1, y=1, button="unknown"), dict(kind="mouse_click", x=1, y=1, count=4),
                 dict(kind="mouse_drag", from_x=0, from_y=0, to_x=5, to_y=5, duration_ms=99),
                 dict(kind="scroll", x=1, y=1, delta_x=0, delta_y=0),
                 dict(kind="scroll", x=1, y=1, delta_x=21, delta_y=0),
                 dict(kind="scroll", x=1, y=1, delta_x=0, delta_y=0.5),
                 dict(kind="type_text", text="hello", unexpected="field")]
        for action in cases:
            with self.subTest(action=action):
                result = self.run_harness(action=dict(action, pid=1))
                self.assertEqual(result["error"]["code"], "invalid_action")
                self.assertEqual(result["events"], [])

    def test_frame_geometry_is_nullable_and_preserves_screen_coordinates(self):
        result = self.run_harness(frames=[[-500, 20, 300, 200], [0, 0, 0, 2], [0, 0, 2, -1]])
        self.assertEqual(result["frames"], [{"x": -500, "y": 20, "width": 300, "height": 200}, None, None])


if __name__ == "__main__":
    unittest.main()
