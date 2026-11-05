// macOS Accessibility bridge. stdin/stdout are one-request/one-response JSONL.
// No screenshots, synthetic input, or permission prompts are used.
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

final class Bridge {
    let maxNodes: Int
    let maxDepth: Int
    var records: [String: NodeRecord] = [:]
    var applications: [pid_t: NSRunningApplication] = [:]
    var frontmost: NSRunningApplication?
    var nodes: [[String: Any]] = []
    var truncated = false
    var seen: [CFHashCode: [(AXUIElement, String)]] = [:]

    init(maxNodes: Int, maxDepth: Int) {
        self.maxNodes = maxNodes
        self.maxDepth = maxDepth
        // System-wide timeout is the default for subsequently created elements.
        _ = AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 0.4)
    }

    func reset() {
        records.removeAll(); applications.removeAll(); frontmost = nil
        nodes.removeAll(); seen.removeAll(); truncated = false
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
        if let app = frontmost {
            let root = AXUIElementCreateApplication(app.processIdentifier)
            if let id = try visit(root, pid: app.processIdentifier, path: "0", depth: 0) { roots.append(id) }
        }
        return ["platform": "macos", "desktop_id": "local",
                "frontmost_app": frontmost.map(applicationJSON) as Any? ?? NSNull(),
                "applications": apps.map(applicationJSON), "roots": roots, "nodes": nodes,
                "truncated": truncated,
                "capabilities": ["snapshot": true, "perform": true, "set_value": true,
                    "focus": true, "activate_app": true, "screenshots": false,
                    "scope": "frontmost_application", "max_nodes": maxNodes, "max_depth": maxDepth]]
    }

    func act(_ action: [String: Any]) throws -> [String: Any] {
        try requirePermission()
        guard let kind = action["kind"] as? String else {
            throw BridgeError(code: "invalid_action", message: "Action kind must be a string.")
        }
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.001))
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
