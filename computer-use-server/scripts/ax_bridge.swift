// macOS Accessibility bridge. stdin/stdout are one-request/one-response JSONL.
// No screenshots, clipboard access, or permission prompts are used.
import AppKit
import ApplicationServices
import CryptoKit
import Foundation

struct BridgeError: Error {
    let code: String
    let message: String
    var details: [String: Any] = [:]
    var json: [String: Any] {
        var value: [String: Any] = ["code": code, "message": message]
        if !details.isEmpty { value["details"] = details }
        return value
    }
}

func axError(_ error: AXError, _ operation: String) -> BridgeError {
    let code: String
    switch error {
    case .apiDisabled: code = "permission_denied"
    case .attributeUnsupported, .actionUnsupported, .notImplemented: code = "unsupported"
    case .invalidUIElement, .cannotComplete, .noValue: code = "element_unavailable"
    case .illegalArgument: code = "invalid_action"
    default: code = "backend_unavailable"
    }
    return BridgeError(code: code, message: "Accessibility operation failed: \(operation)",
                       details: ["ax_error": error.rawValue])
}

func requirePermission() throws {
    guard AXIsProcessTrusted() else {
        throw BridgeError(code: "permission_denied",
            message: "Accessibility access is unavailable. Grant access manually in System Settings > Privacy & Security > Accessibility, then restart the server. No permission prompt was opened.")
    }
}

func attribute(_ element: AXUIElement, _ name: String) throws -> CFTypeRef? {
    var result: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &result)
    if error == .attributeUnsupported || error == .noValue || error == .notImplemented { return nil }
    guard error == .success else { throw axError(error, name) }
    return result
}

func textAttribute(_ element: AXUIElement, _ name: String) throws -> String? {
    return try attribute(element, name) as? String
}

func textHash(_ text: String) -> String {
    return SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
}

func actionNames(_ element: AXUIElement) throws -> [String] {
    var result: CFArray?
    let error = AXUIElementCopyActionNames(element, &result)
    if error == .actionUnsupported || error == .notImplemented { return [] }
    guard error == .success else { throw axError(error, "action names") }
    return ((result as? [String]) ?? []).sorted()
}

func isSettable(_ element: AXUIElement, _ name: String) throws -> Bool {
    var result: DarwinBoolean = false
    let error = AXUIElementIsAttributeSettable(element, name as CFString, &result)
    if error == .attributeUnsupported || error == .notImplemented || error == .noValue { return false }
    guard error == .success else { throw axError(error, "settable \(name)") }
    return result.boolValue
}

func applicationJSON(_ app: NSRunningApplication) -> [String: Any] {
    return ["pid": Int(app.processIdentifier), "name": app.localizedName as Any? ?? NSNull(),
            "bundle_id": app.bundleIdentifier as Any? ?? NSNull()]
}

struct NodeRecord {
    let element: AXUIElement
    let actions: Set<String>
    let settable: Set<String>
}

func finiteCoordinate(_ value: Double) -> Bool { value.isFinite && abs(value) <= 10_000_000 }

func frameJSON(_ rect: CGRect) -> [String: Double]? {
    guard finiteCoordinate(rect.origin.x), finiteCoordinate(rect.origin.y),
          rect.size.width.isFinite, rect.size.height.isFinite, rect.size.width > 0, rect.size.height > 0 else { return nil }
    return ["x": rect.origin.x, "y": rect.origin.y, "width": rect.size.width, "height": rect.size.height]
}

func elementFrame(_ element: AXUIElement) throws -> [String: Double]? {
    do {
        guard let position = try attribute(element, kAXPositionAttribute),
              let size = try attribute(element, kAXSizeAttribute),
              CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
        let positionValue = position as! AXValue, sizeValue = size as! AXValue
        guard AXValueGetType(positionValue) == .cgPoint, AXValueGetType(sizeValue) == .cgSize else { return nil }
        var point = CGPoint.zero, dimensions = CGSize.zero
        guard AXValueGetValue(positionValue, .cgPoint, &point), AXValueGetValue(sizeValue, .cgSize, &dimensions) else { return nil }
        return frameJSON(CGRect(origin: point, size: dimensions))
    } catch let failure as BridgeError {
        if failure.code == "permission_denied" { throw failure }
        return nil  // Optional geometry must not discard an otherwise usable node.
    }
}

func activeDisplayBounds() -> [CGRect] {
    var count: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return [] }
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(min(count, 64)))
    let capacity = UInt32(ids.count)
    guard CGGetActiveDisplayList(capacity, &ids, &count) == .success else { return [] }
    return ids.prefix(Int(min(count, capacity))).map(CGDisplayBounds).filter { frameJSON($0) != nil }
}

func canPostInput() -> Bool {
    if #available(macOS 10.15, *) { return CGPreflightPostEventAccess() }
    return false
}

/// Event construction and dispatch are separate from permission/foreground checks.
/// Tests replace the sink and clock; they never post events to the user's desktop.
final class NativeInput {
    static let kinds: Set<String> = ["type_text", "key_press", "mouse_move", "mouse_click", "mouse_drag", "scroll"]
    // US physical positions, matching HIToolbox/Events.h kVK_* constants.
    static let keyCodes: [String: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
        "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26,
        "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
        "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43,
        "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "`": 50, "backspace": 51,
        "escape": 53, "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100, "f9": 101,
        "f11": 103, "f10": 109, "f12": 111, "insert": 114, "home": 115, "page_up": 116,
        "delete": 117, "f4": 118, "end": 119, "f2": 120, "page_down": 121, "f1": 122,
        "left": 123, "right": 124, "down": 125, "up": 126,
    ]
    static let modifierKeys: [(String, CGKeyCode, CGEventFlags)] = [
        ("ctrl", 59, .maskControl), ("alt", 58, .maskAlternate),
        ("shift", 56, .maskShift), ("meta", 55, .maskCommand),
    ]
    let post: (CGEvent) -> Void
    let pause: (Double) -> Void
    let source: CGEventSource?

    init(post: @escaping (CGEvent) -> Void = { $0.post(tap: .cghidEventTap) },
         pause: @escaping (Double) -> Void = { Thread.sleep(forTimeInterval: $0) }) {
        self.post = post; self.pause = pause
        self.source = CGEventSource(stateID: .privateState)
    }

    static func unicodeChunks(_ text: String) -> [[UniChar]] {
        var chunks: [[UniChar]] = [], chunk: [UniChar] = []
        for scalar in text.unicodeScalars {
            let units = Array(String(scalar).utf16)
            if chunk.count + units.count > 20 { chunks.append(chunk); chunk.removeAll(keepingCapacity: true) }
            chunk.append(contentsOf: units)
        }
        if !chunk.isEmpty { chunks.append(chunk) }
        return chunks
    }

    func number(_ action: [String: Any], _ field: String) throws -> Double {
        guard let value = action[field] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
              finiteCoordinate(value.doubleValue) else {
            throw BridgeError(code: "invalid_action", message: "\(field) must be a finite number within ±10000000.")
        }
        return value.doubleValue
    }

    func integer(_ action: [String: Any], _ field: String, default fallback: Int? = nil, range: ClosedRange<Int>) throws -> Int {
        if action[field] == nil, let fallback { return fallback }
        let value = try number(action, field)
        guard value.rounded() == value, range.contains(Int(value)) else {
            throw BridgeError(code: "invalid_action", message: "\(field) must be an integer in \(range).")
        }
        return Int(value)
    }

    func point(_ action: [String: Any], _ x: String, _ y: String, displays: [CGRect]) throws -> CGPoint {
        let point = try CGPoint(x: number(action, x), y: number(action, y))
        guard displays.contains(where: { point.x >= $0.minX && point.x < $0.maxX && point.y >= $0.minY && point.y < $0.maxY }) else {
            throw BridgeError(code: "invalid_action", message: "Mouse coordinates must be inside an observed display.")
        }
        return point
    }

    func keyEvent(_ key: CGKeyCode, down: Bool) throws -> CGEvent {
        guard let source, let event = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: down) else {
            throw BridgeError(code: "unsupported", message: "Cannot create a native keyboard event.")
        }
        event.flags = []
        return event
    }

    func mouseEvent(_ type: CGEventType, _ point: CGPoint, _ button: CGMouseButton = .left) throws -> CGEvent {
        guard let source, let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: button) else {
            throw BridgeError(code: "unsupported", message: "Cannot create a native mouse event.")
        }
        event.flags = []
        return event
    }

    func modifiers(_ action: [String: Any]) throws -> [String] {
        guard action["modifiers"] != nil else { return [] }
        guard let names = action["modifiers"] as? [String], Set(names).count == names.count,
              names.allSatisfy({ name in Self.modifierKeys.contains { $0.0 == name } }) else {
            throw BridgeError(code: "invalid_action", message: "modifiers must be unique ctrl, alt, shift, or meta names.")
        }
        return names
    }

    func withModifiers(_ names: [String], beforeDispatch: () throws -> Void = {}, _ body: (CGEventFlags) throws -> Void) throws {
        let entries = try Self.modifierKeys.filter { names.contains($0.0) }.map {
            (try keyEvent($0.1, down: true), try keyEvent($0.1, down: false), $0.2)
        }
        var held: [(CGEvent, CGEventFlags)] = [], flags: CGEventFlags = []
        defer {
            for (up, flag) in held.reversed() {
                flags.subtract(flag); up.flags = flags; post(up)
            }
        }
        for (down, up, flag) in entries {
            try beforeDispatch()
            flags.insert(flag); down.flags = flags
            held.append((up, flag)); post(down)
        }
        try body(flags)
    }

    func perform(_ action: [String: Any], displays: [CGRect], beforeDispatch: () throws -> Void) throws -> [String: Any] {
        guard let kind = action["kind"] as? String, Self.kinds.contains(kind) else {
            throw BridgeError(code: "unsupported", message: "Unknown native input action.")
        }
        let required: [String: Set<String>] = [
            "type_text": ["kind", "pid", "text"], "key_press": ["kind", "pid", "key"],
            "mouse_move": ["kind", "pid", "x", "y"], "mouse_click": ["kind", "pid", "x", "y"],
            "mouse_drag": ["kind", "pid", "from_x", "from_y", "to_x", "to_y"],
            "scroll": ["kind", "pid", "x", "y", "delta_x", "delta_y"],
        ]
        let optional: [String: Set<String>] = ["key_press": ["modifiers"], "mouse_click": ["button", "count", "modifiers"],
                                               "mouse_drag": ["button", "duration_ms", "modifiers"]]
        let fields = Set(action.keys), requiredFields = required[kind]!
        guard requiredFields.isSubset(of: fields), fields.isSubset(of: requiredFields.union(optional[kind] ?? [])) else {
            throw BridgeError(code: "invalid_action", message: "Unexpected or missing native input fields.")
        }
        if kind == "type_text" {
            guard let text = action["text"] as? String, !text.isEmpty, text.unicodeScalars.count <= 4096, !text.contains("\0") else {
                throw BridgeError(code: "invalid_action", message: "text must contain 1 to 4096 Unicode code points without NUL.")
            }
            let pairs = try Self.unicodeChunks(text).map { units -> (CGEvent, CGEvent) in
                let down = try keyEvent(0, down: true), up = try keyEvent(0, down: false)
                units.withUnsafeBufferPointer { buffer in
                    down.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress)
                    up.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress)
                }
                return (down, up)
            }
            try beforeDispatch()
            for (down, up) in pairs {
                try beforeDispatch()
                defer { post(up) }
                post(down)
            }
        } else if kind == "key_press" {
            guard let name = action["key"] as? String, let code = Self.keyCodes[name] else {
                throw BridgeError(code: "invalid_action", message: "key is not a supported physical key name.")
            }
            let names = try modifiers(action), down = try keyEvent(code, down: true), up = try keyEvent(code, down: false)
            try beforeDispatch()
            try withModifiers(names, beforeDispatch: beforeDispatch) { flags in
                down.flags = flags; up.flags = flags
                try beforeDispatch()
                defer { post(up) }
                post(down)
            }
        } else if kind == "mouse_drag" {
            let start = try point(action, "from_x", "from_y", displays: displays)
            let end = try point(action, "to_x", "to_y", displays: displays)
            let duration = try integer(action, "duration_ms", default: 400, range: 100...2000)
            let names = try modifiers(action), button = try mouseButton(action)
            let types = buttonTypes(button)
            let move = try mouseEvent(.mouseMoved, start), down = try mouseEvent(types.0, start, button)
            let up = try mouseEvent(types.2, start, button)
            let steps = max(2, duration / 16)
            let drags = try (1...steps).map { index -> CGEvent in
                let fraction = Double(index) / Double(steps)
                let location = CGPoint(x: start.x + (end.x - start.x) * fraction, y: start.y + (end.y - start.y) * fraction)
                guard displays.contains(where: { location.x >= $0.minX && location.x < $0.maxX && location.y >= $0.minY && location.y < $0.maxY }) else {
                    throw BridgeError(code: "invalid_action", message: "Drag path crosses a gap between observed displays.")
                }
                return try mouseEvent(types.1, location, button)
            }
            try beforeDispatch()
            try withModifiers(names, beforeDispatch: beforeDispatch) { flags in
                move.flags = flags; down.flags = flags; up.flags = flags
                try beforeDispatch()
                post(move)
                try beforeDispatch()
                defer { post(up) }
                post(down)
                for event in drags {
                    pause(Double(duration) / Double(steps) / 1000)
                    try beforeDispatch()
                    event.flags = flags; up.location = event.location; post(event)
                }
            }
        } else {
            let location = try point(action, "x", "y", displays: displays)
            let move = try mouseEvent(.mouseMoved, location)
            if kind == "mouse_move" {
                try beforeDispatch(); post(move)
            } else if kind == "scroll" {
                let dx = try integer(action, "delta_x", range: -20...20), dy = try integer(action, "delta_y", range: -20...20)
                guard dx != 0 || dy != 0 else { throw BridgeError(code: "invalid_action", message: "Scroll delta cannot be zero on both axes.") }
                guard let source, let event = CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 2,
                                                       wheel1: Int32(-dy), wheel2: Int32(-dx), wheel3: 0) else {
                    throw BridgeError(code: "unsupported", message: "Cannot create a native scroll event.")
                }
                event.location = location; event.flags = []
                try beforeDispatch(); post(move)
                try beforeDispatch(); post(event)
            } else {
                let button = try mouseButton(action), names = try modifiers(action)
                let count = try integer(action, "count", default: 1, range: 1...3), types = buttonTypes(button)
                let pairs = try (1...count).map { index -> (CGEvent, CGEvent) in
                    let down = try mouseEvent(types.0, location, button), up = try mouseEvent(types.2, location, button)
                    down.setIntegerValueField(.mouseEventClickState, value: Int64(index))
                    up.setIntegerValueField(.mouseEventClickState, value: Int64(index))
                    return (down, up)
                }
                try beforeDispatch()
                try withModifiers(names, beforeDispatch: beforeDispatch) { flags in
                    try beforeDispatch()
                    move.flags = flags; post(move)
                    for (down, up) in pairs {
                        down.flags = flags; up.flags = flags
                        try beforeDispatch()
                        defer { post(up) }
                        post(down); pause(0.01)
                    }
                }
            }
        }
        return ["accepted": true, "posted": true, "kind": kind]
    }

    func mouseButton(_ action: [String: Any]) throws -> CGMouseButton {
        guard let name = (action["button"] ?? "left") as? String,
              let button = ["left": CGMouseButton.left, "right": .right, "middle": .center][name] else {
            throw BridgeError(code: "invalid_action", message: "button must be left, right, or middle.")
        }
        return button
    }

    func buttonTypes(_ button: CGMouseButton) -> (CGEventType, CGEventType, CGEventType) {
        if button == .left { return (.leftMouseDown, .leftMouseDragged, .leftMouseUp) }
        if button == .right { return (.rightMouseDown, .rightMouseDragged, .rightMouseUp) }
        return (.otherMouseDown, .otherMouseDragged, .otherMouseUp)
    }
}

final class Bridge {
    let maxNodes: Int
    let maxDepth: Int
    var records: [String: NodeRecord] = [:]
    var applications: [pid_t: NSRunningApplication] = [:]
    var frontmost: NSRunningApplication?
    var nodes: [[String: Any]] = []
    var truncated = false
    var seen: [CFHashCode: [(AXUIElement, String)]] = [:]
    var displayBounds: [CGRect] = []
    var inputCapabilities: [String: Bool] = [:]
    let nativeInput = NativeInput()

    init(maxNodes: Int, maxDepth: Int) {
        self.maxNodes = maxNodes
        self.maxDepth = maxDepth
        // System-wide timeout is the default for subsequently created elements.
        _ = AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 0.4)
    }

    func reset() {
        records.removeAll(); applications.removeAll(); frontmost = nil
        nodes.removeAll(); seen.removeAll(); truncated = false
        displayBounds.removeAll(); inputCapabilities.removeAll()
    }

    func visit(_ element: AXUIElement, pid: pid_t, path: String, depth: Int) throws -> String? {
        let hash = CFHash(element)
        if let match = seen[hash]?.first(where: { CFEqual($0.0, element) }) { return match.1 }
        guard nodes.count < maxNodes, depth <= maxDepth else {
            truncated = true
            return nil
        }
        let role = try textAttribute(element, kAXRoleAttribute)
        let subrole = try textAttribute(element, kAXSubroleAttribute)
        let secure = subrole == kAXSecureTextFieldSubrole ||
                     (subrole?.lowercased().contains("secure") ?? false)
        let actions = try actionNames(element)
        var settable: [String] = []
        if !secure {
            if try isSettable(element, kAXValueAttribute) { settable.append("value") }
        }
        if try isSettable(element, kAXFocusedAttribute) { settable.append("focused") }
        let id = "\(pid)/\(path)"
        let title = try textAttribute(element, kAXTitleAttribute)
        let description = try textAttribute(element, kAXDescriptionAttribute)
        var textTruncated = false
        func clipped(_ text: String) -> String {
            let prefix = String(text.prefix(4096))
            if prefix != text { textTruncated = true }
            return prefix
        }
        let clippedTitle = title.map(clipped)
        let clippedDescription = description.map(clipped)
        var value: Any = NSNull()
        var valueHash: Any = NSNull()
        if !secure, let raw = try attribute(element, kAXValueAttribute) {
            if let string = raw as? String {
                value = clipped(string)
                valueHash = textHash(string)
            }
            else if let number = raw as? NSNumber, number.doubleValue.isFinite { value = number }
        }
        if textTruncated { truncated = true }
        var node: [String: Any] = [
            "id": id, "pid": Int(pid), "role": role as Any? ?? NSNull(),
            "subrole": subrole as Any? ?? NSNull(), "frame": try elementFrame(element) as Any? ?? NSNull(),
            "title": clippedTitle as Any? ?? NSNull(),
            "description": clippedDescription as Any? ?? NSNull(),
            "value": value, "enabled": try attribute(element, kAXEnabledAttribute) as? Bool as Any? ?? NSNull(),
            "focused": try attribute(element, kAXFocusedAttribute) as? Bool as Any? ?? NSNull(),
            "actions": actions, "settable": settable.sorted(), "children": [String](),
            "secure": secure,
            "text_truncated": textTruncated,
            "title_hash": title.map(textHash) as Any? ?? NSNull(),
            "description_hash": description.map(textHash) as Any? ?? NSNull(),
            "value_hash": valueHash,
        ]
        let nodeIndex = nodes.count
        nodes.append(node)
        records[id] = NodeRecord(element: element, actions: Set(actions), settable: Set(settable))
        seen[hash, default: []].append((element, id))
        // Fetch only the bounded prefix instead of materializing huge child arrays.
        var count: CFIndex = 0
        let countError = AXUIElementGetAttributeValueCount(element, kAXChildrenAttribute as CFString, &count)
        var childIDs: [String] = []
        if countError == .success && count > 0 {
            if depth == maxDepth || nodes.count >= maxNodes {
                truncated = true
            } else {
                let limit = min(count, maxNodes - nodes.count)
                if limit < count { truncated = true }
                var rawChildren: CFArray?
                let error = AXUIElementCopyAttributeValues(element, kAXChildrenAttribute as CFString, 0, limit, &rawChildren)
                if error == .success, let children = rawChildren as? [AXUIElement] {
                    for (index, child) in children.enumerated() {
                        do {
                            if let childID = try visit(child, pid: pid, path: "\(path).\(index)", depth: depth + 1) {
                                childIDs.append(childID)
                            }
                        } catch let failure as BridgeError {
                            if failure.code == "permission_denied" { throw failure }
                            truncated = true
                        }
                        if nodes.count >= maxNodes {
                            if index + 1 < count { truncated = true }
                            break
                        }
                    }
                } else if error == .apiDisabled { throw axError(error, "children") }
                else { truncated = true }
            }
        } else if countError != .success && countError != .attributeUnsupported && countError != .noValue {
            if countError == .apiDisabled { throw axError(countError, "children count") }
            truncated = true
        }
        node["children"] = childIDs
        nodes[nodeIndex] = node
        return id
    }

    func snapshot() throws -> [String: Any] {
        reset()
        try requirePermission()
        // NSWorkspace's time-varying properties need the main run loop to advance.
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.001))
        let workspace = NSWorkspace.shared
        let apps = workspace.runningApplications.filter { $0.activationPolicy == .regular && !$0.isTerminated }
            .sorted { $0.processIdentifier < $1.processIdentifier }
        applications = Dictionary(uniqueKeysWithValues: apps.map { ($0.processIdentifier, $0) })
        frontmost = workspace.frontmostApplication
        var roots: [String] = []
        var observationError: [String: Any]?
        if let app = frontmost {
            let root = AXUIElementCreateApplication(app.processIdentifier)
            do {
                if let id = try visit(root, pid: app.processIdentifier, path: "0", depth: 0) { roots.append(id) }
            } catch let failure as BridgeError {
                if failure.code == "permission_denied" { throw failure }
                // An unavailable app root must not hide the observed app list.
                // Keep activation available, but retain no unusable node targets.
                records.removeAll(); nodes.removeAll(); seen.removeAll(); roots.removeAll()
                truncated = true
                observationError = failure.json
            }
        }
        let nodeActionsAvailable = observationError == nil
        displayBounds = activeDisplayBounds()
        let postAllowed = canPostInput()
        inputCapabilities = Dictionary(uniqueKeysWithValues: NativeInput.kinds.map { kind in
            let keyboard = kind == "type_text" || kind == "key_press"
            return (kind, postAllowed && (keyboard || !displayBounds.isEmpty))
        })
        var inputState: [String: Any] = ["coordinate_space": "screen", "units": "points",
            "displays": displayBounds.compactMap(frameJSON), "pointer": NSNull(), "backend": "macOS Quartz CGEvent"]
        if let pointer = CGEvent(source: nil)?.location, finiteCoordinate(pointer.x), finiteCoordinate(pointer.y) {
            inputState["pointer"] = ["x": pointer.x, "y": pointer.y]
        }
        var unavailable: [String] = []
        if !postAllowed { unavailable.append("Native event posting access is unavailable; no permission prompt was opened.") }
        if displayBounds.isEmpty { unavailable.append("Mouse injection requires observable display bounds.") }
        if !unavailable.isEmpty { inputState["unavailable_reason"] = unavailable.joined(separator: " ") }
        var capabilities: [String: Any] = ["snapshot": true, "perform": nodeActionsAvailable, "set_value": nodeActionsAvailable,
            "focus": nodeActionsAvailable, "activate_app": true, "screenshots": false,
            "scope": "frontmost_application", "max_nodes": maxNodes, "max_depth": maxDepth]
        for (kind, available) in inputCapabilities { capabilities[kind] = available }
        var result: [String: Any] = ["platform": "macos", "desktop_id": "local",
                "frontmost_app": frontmost.map(applicationJSON) as Any? ?? NSNull(),
                "applications": apps.map(applicationJSON), "roots": roots, "nodes": nodes,
                "truncated": truncated, "input": inputState, "capabilities": capabilities]
        if let failure = observationError { result["observation_error"] = failure }
        return result
    }

    func act(_ action: [String: Any]) throws -> [String: Any] {
        try requirePermission()
        guard let kind = action["kind"] as? String else {
            throw BridgeError(code: "invalid_action", message: "Action kind must be a string.")
        }
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.001))
        if NativeInput.kinds.contains(kind) {
            guard let number = action["pid"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
                  number.doubleValue > 0, number.doubleValue <= Double(Int32.max) else {
                throw BridgeError(code: "invalid_action", message: "pid must be a positive process identifier.")
            }
            guard let app = frontmost, app.processIdentifier == pid_t(number.int32Value), !app.isTerminated else {
                throw BridgeError(code: "element_unavailable", message: "Native input requires the observed foreground application's pid.")
            }
            guard inputCapabilities[kind] == true else {
                throw BridgeError(code: "unsupported", message: "This native input is unavailable in the latest snapshot.")
            }
            return try nativeInput.perform(action, displays: displayBounds) {
                // Advance workspace notifications between chunks/motions; otherwise
                // a long input sequence can keep reading a cached foreground app.
                RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.001))
                try requirePermission()
                guard canPostInput() else {
                    throw BridgeError(code: "unsupported", message: "Native event posting access is unavailable; no permission prompt was opened.")
                }
                guard !app.isTerminated, NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier else {
                    throw BridgeError(code: "element_unavailable", message: "Foreground application changed before native input dispatch. Read a fresh snapshot.")
                }
            }
        }
        if kind == "activate_app" {
            guard let number = action["pid"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue.rounded() == number.doubleValue,
                  number.int64Value > 0, number.int64Value <= Int64(Int32.max) else {
                throw BridgeError(code: "invalid_action", message: "pid must be a positive process identifier.")
            }
            guard let app = applications[pid_t(number.int32Value)], !app.isTerminated else {
                throw BridgeError(code: "element_unavailable", message: "Application was not in the latest snapshot or has exited.")
            }
            guard app.activate(options: []) else {
                throw BridgeError(code: "unsupported", message: "macOS declined application activation.")
            }
            // Activation is asynchronous. The caller must observe the next snapshot.
            return ["accepted": true, "kind": kind]
        }
        guard ["perform", "set_value", "focus"].contains(kind) else {
            throw BridgeError(code: "unsupported", message: "Unsupported action kind.")
        }
        guard let id = action["node_id"] as? String, !id.isEmpty else {
            throw BridgeError(code: "invalid_action", message: "node_id must be a nonempty string.")
        }
        guard let record = records[id], let app = frontmost, !app.isTerminated,
              NSWorkspace.shared.frontmostApplication?.isEqual(app) == true else {
            throw BridgeError(code: "element_unavailable", message: "Element is unavailable or the foreground application changed. Read a fresh snapshot.")
        }
        // Revalidate capability immediately before using the retained AX reference.
        let element = record.element
        let error: AXError
        switch kind {
        case "perform":
            guard let name = action["action"] as? String, !name.isEmpty else {
                throw BridgeError(code: "invalid_action", message: "action must be an advertised Accessibility action name.")
            }
            guard record.actions.contains(name), try actionNames(element).contains(name) else {
                throw BridgeError(code: "unsupported", message: "Accessibility action is not advertised by this element.")
            }
            error = AXUIElementPerformAction(element, name as CFString)
        case "set_value":
            guard let value = action["value"] as? String, value.utf8.count <= 65536 else {
                throw BridgeError(code: "invalid_action", message: "value must be a string of at most 65536 UTF-8 bytes.")
            }
            let subrole = try textAttribute(element, kAXSubroleAttribute)
            guard !(subrole?.lowercased().contains("secure") ?? false), record.settable.contains("value"),
                  try isSettable(element, kAXValueAttribute) else {
                throw BridgeError(code: "unsupported", message: "Element value is not writable or is a secure field.")
            }
            error = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFString)
        default:
            guard record.settable.contains("focused"), try isSettable(element, kAXFocusedAttribute) else {
                throw BridgeError(code: "unsupported", message: "Element does not advertise writable focus.")
            }
            error = AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        }
        guard error == .success else { throw axError(error, kind) }
        return ["accepted": true, "kind": kind, "node_id": id]
    }
}

let args = CommandLine.arguments
let maxNodes = args.count > 1 ? Int(args[1]) ?? 1200 : 1200
let maxDepth = args.count > 2 ? Int(args[2]) ?? 12 : 12
let bridge = Bridge(maxNodes: min(10000, max(1, maxNodes)), maxDepth: min(40, max(1, maxDepth)))
while let line = readLine() {
    autoreleasepool {
        var reply: [String: Any]
        do {
            guard line.utf8.count <= 131072, let data = line.data(using: .utf8),
                  let request = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let op = request["op"] as? String else {
                throw BridgeError(code: "invalid_action", message: "Expected a JSON object containing op.")
            }
            let result: [String: Any]
            switch op {
            case "snapshot": result = try bridge.snapshot()
            case "act":
                guard let action = request["action"] as? [String: Any] else {
                    throw BridgeError(code: "invalid_action", message: "action must be an object.")
                }
                result = try bridge.act(action)
            default: throw BridgeError(code: "unsupported", message: "Unsupported bridge operation.")
            }
            reply = ["ok": true, "result": result]
        } catch let error as BridgeError {
            bridge.reset()
            reply = ["ok": false, "error": error.json]
        } catch {
            bridge.reset()
            reply = ["ok": false, "error": ["code": "invalid_action", "message": "Invalid JSON request."]]
        }
        if let data = try? JSONSerialization.data(withJSONObject: reply, options: [.sortedKeys]),
           let encoded = String(data: data, encoding: .utf8) {
            print(encoded)
            fflush(stdout)
        }
    }
}
