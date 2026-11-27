// Finds and prepares the Roblox Studio window for frame capture (macOS).
//
//   studio_window list                 JSON: Studio's on-screen windows, display scale, pointer position
//   studio_window activate             bring Studio to the front (it renders slowly in the background)
//   studio_window frame x y w h        move and resize the main Studio window (needs Accessibility access)
//   studio_window park                 move the pointer to the window's bottom strip with a real mouse-move event
//                                      (a cursor warp sends none, and Studio keeps drawing its hover gizmo)
import Cocoa

let bundleId = "com.Roblox.RobloxStudio"

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

guard let app = NSWorkspace.shared.runningApplications.first(where: { $0.bundleIdentifier == bundleId }) else {
    fail("Roblox Studio is not running")
}
let pid = app.processIdentifier

func windows() -> [[String: Any]] {
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    let list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
    var out: [[String: Any]] = []
    for item in list {
        guard (item[kCGWindowOwnerPID as String] as? Int32) == pid, (item[kCGWindowLayer as String] as? Int) == 0 else { continue }
        let bounds = item[kCGWindowBounds as String] as? [String: Double] ?? [:]
        out.append([
            "id": item[kCGWindowNumber as String] as? Int ?? 0,
            "name": item[kCGWindowName as String] as? String ?? "",
            "x": bounds["X"] ?? 0, "y": bounds["Y"] ?? 0, "width": bounds["Width"] ?? 0, "height": bounds["Height"] ?? 0,
        ])
    }
    return out.sorted { ($0["width"] as! Double) * ($0["height"] as! Double) > ($1["width"] as! Double) * ($1["height"] as! Double) }
}

func pointer() -> CGPoint {
    return CGEvent(source: nil)?.location ?? .zero
}

let arguments = CommandLine.arguments
let command = arguments.count > 1 ? arguments[1] : "list"

switch command {
case "list":
    let screen = NSScreen.main
    let result: [String: Any] = [
        "windows": windows(),
        "scale": Double(screen?.backingScaleFactor ?? 1),
        "screen": ["width": Double(screen?.frame.width ?? 0), "height": Double(screen?.frame.height ?? 0)],
        "visible": ["x": Double(screen?.visibleFrame.minX ?? 0), "y": Double((screen?.frame.height ?? 0) - (screen?.visibleFrame.maxY ?? 0)),
                    "width": Double(screen?.visibleFrame.width ?? 0), "height": Double(screen?.visibleFrame.height ?? 0)],
        "pointer": ["x": Double(pointer().x), "y": Double(pointer().y)],
        "frontmost": NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
    ]
    let data = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
case "activate":
    app.activate(options: [.activateAllWindows])
    print("{\"activated\": true}")
case "frame":
    guard arguments.count == 6, let x = Double(arguments[2]), let y = Double(arguments[3]), let w = Double(arguments[4]), let h = Double(arguments[5]) else {
        fail("usage: studio_window frame x y w h")
    }
    guard AXIsProcessTrusted() else { fail("no Accessibility access: allow the terminal that runs this in System Settings > Privacy & Security") }
    let element = AXUIElementCreateApplication(pid)
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXWindowsAttribute as CFString, &value) == .success, let list = value as? [AXUIElement], !list.isEmpty else {
        fail("Studio has no window to resize")
    }
    func size(_ window: AXUIElement) -> CGSize {
        var raw: CFTypeRef?
        var out = CGSize.zero
        if AXUIElementCopyAttributeValue(window, kAXSizeAttribute as CFString, &raw) == .success, let raw = raw { AXValueGetValue(raw as! AXValue, .cgSize, &out) }
        return out
    }
    let main = list.max { size($0).width * size($0).height < size($1).width * size($1).height }!
    var point = CGPoint(x: x, y: y)
    var wanted = CGSize(width: w, height: h)
    AXUIElementSetAttributeValue(main, kAXPositionAttribute as CFString, AXValueCreate(.cgPoint, &point)!)
    AXUIElementSetAttributeValue(main, kAXSizeAttribute as CFString, AXValueCreate(.cgSize, &wanted)!)
    AXUIElementSetAttributeValue(main, kAXPositionAttribute as CFString, AXValueCreate(.cgPoint, &point)!)
    let now = size(main)
    print("{\"width\": \(now.width), \"height\": \(now.height)}")
case "park":
    guard let window = windows().first else { fail("Studio has no window on screen") }
    let x = window["x"] as! Double, y = window["y"] as! Double, w = window["width"] as! Double, h = window["height"] as! Double
    let target = CGPoint(x: x + w / 2, y: y + h - 14)
    guard let event = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: target, mouseButton: .left) else {
        fail("could not create a mouse event")
    }
    event.post(tap: .cghidEventTap)
    usleep(200_000)
    print("{\"moved\": true, \"x\": \(Int(target.x)), \"y\": \(Int(target.y))}")
default:
    fail("unknown command \(command)")
}
