// Offscreen snapshots of Local Coder's screens with sample data (no engine, nothing on the user's screen).
// Run: macos/scripts/ui_snapshots/snapshots.sh <out-dir> [--light]
import AppKit
import SwiftUI

let outDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "."
let repoRoot = ProcessInfo.processInfo.environment["LOCAL_CODER_REPO"] ?? FileManager.default.currentDirectoryPath
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.appearance = NSAppearance(named: CommandLine.arguments.contains("--light") ? .aqua : .darkAqua)
if let icon = NSImage(contentsOfFile: "\(repoRoot)/macos/AppIcon.icns") {
    app.applicationIconImage = icon
}

@MainActor
func capture(_ window: NSWindow, _ name: String) {
    let stop = Date().addingTimeInterval(1.6)
    while Date() < stop {
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
    }
    let path = "\(outDir)/\(name).png"
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    task.arguments = ["-x", "-o", "-l", String(window.windowNumber), path]
    try? task.run()
    task.waitUntilExit()
    print(path)
}

@MainActor
func show<V: View>(_ view: V, size: CGSize, titled: Bool = true) -> NSWindow {
    let host = NSHostingController(rootView: view.environmentObject(AppModel.shared))
    host.sceneBridgingOptions = [.toolbars, .title]
    let style: NSWindow.StyleMask = titled ? [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        : [.titled, .fullSizeContentView]
    let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: style, backing: .buffered, defer: false)
    window.contentViewController = host
    window.setContentSize(size)
    window.toolbarStyle = .unified
    window.setFrameOrigin(NSPoint(x: -6000, y: -4000))
    window.orderFrontRegardless()
    return window
}

@MainActor
func fixture() {
    let model = AppModel.shared
    model.ready = true
    model.models = [
        CatalogModel(id: "orca:qwen/qwen3.8-27b", name: "Qwen3.8 27B", available: true, note: "", group: "OrcaRouter",
                     subtitle: "$0.33 in · $2.4 out", provider: "orcarouter", free: false, intelligence: 47, reasoningSwitch: true),
        CatalogModel(id: "orca:anthropic/claude-sonnet-5.5", name: "Claude Sonnet 5.5", available: true, note: "",
                     group: "OrcaRouter", subtitle: "$2 in · $10 out", provider: "orcarouter", free: false, intelligence: 56, reasoningSwitch: false),
        CatalogModel(id: "nemotron35-super", name: "Nemotron 3.5 Super 120B", available: true, note: "",
                     group: "NVIDIA Cloud", subtitle: "Free, hosted by NVIDIA", provider: "nvidia", free: true, intelligence: nil, reasoningSwitch: true),
        CatalogModel(id: "bonsai2-27b", name: "Bonsai 2 27B", available: false, note: "", group: "On This Mac",
                     subtitle: "Best local quality, slow", provider: "local", free: false, intelligence: nil, reasoningSwitch: false),
    ]
    model.selectedModelId = "orca:qwen/qwen3.8-27b"
    model.orcaConnected = true
    model.orcaWhere = "Keychain item key:ORCAROUTER_API_KEY"
    let catalog = NSHomeDirectory() + "/.config/local-coder/orcarouter-models.json"
    if let data = FileManager.default.contents(atPath: catalog),
       let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
       let rows = object["models"] as? [[String: Any]] {
        model.routerModels = rows.compactMap(AppModel.routerModel)
        model.routerFetchedAt = Date().addingTimeInterval(-1260)
    }
    model.recent = ["prototypes", "app-scout", "task-board", "pensees", "folium-next"].map {
        RecentProject(path: NSHomeDirectory() + "/projects/\($0)", name: $0)
    }
    model.inspection = ["path": NSHomeDirectory() + "/projects/prototypes", "name": "prototypes",
                        "summary": "Next.js host for throwaway product prototypes."]
    model.history = [
        HistoryRow(episodeID: "a", request: "Add a dark mode toggle to the Shadow Paris map", outcome: "ready",
                   updated: String(Date().addingTimeInterval(-300).timeIntervalSince1970)),
        HistoryRow(episodeID: "b", request: "Make the report page print on one sheet", outcome: "kept",
                   updated: String(Date().addingTimeInterval(-86_000).timeIntervalSince1970)),
    ]
}

@MainActor
func steps(start: Date) -> [TraceStep] {
    let rows: [(String, String, String, Bool, Double)] = [
        ("find_project", "Looking for projects matching “shadow paris”", "shadow paris", true, 1.6),
        ("list", "Looking in ~/projects/prototypes", "~/projects/prototypes", true, 3.8),
        ("note", "The map lives in app/prototypes/shadow-paris; the theme comes from globals.css.", "", true, 6.2),
        ("read", "Reading app/prototypes/shadow-paris/shadow-app.tsx", "app/prototypes/shadow-paris/shadow-app.tsx", true, 9.9),
        ("grep", "Searching app for “prefers-color-scheme”", "“prefers-color-scheme” in app", true, 12.4),
        ("edit", "Editing app/prototypes/shadow-paris/shadow-app.tsx", "app/prototypes/shadow-paris/shadow-app.tsx", true, 21.0),
        ("bash", "Running npm run build", "npm run build", false, 38.5),
        ("edit", "Editing app/globals.css", "app/globals.css", true, 44.1),
        ("bash", "Running npm run build", "npm run build", true, 61.7),
    ]
    return rows.map { TraceStep(tool: $0.0, detail: $0.1, target: $0.2, ok: $0.3, started: start.addingTimeInterval($0.4)) }
}

let prompt = "Add a dark mode toggle to the Shadow Paris map"
let answer = """
Added a **dark mode toggle** to the Shadow Paris map. It sits in the top-right corner of the map and remembers your choice.

What changed:
- `shadow-app.tsx` keeps the theme in local storage and passes it to the map.
- `globals.css` gets a dark palette for the panels and the incident markers.
- The first build failed on a missing type; the second one passes.

To try it, run:

```
npm run dev
```

> The map tiles stay light in dark mode: the tile provider has no dark style without a paid key.
"""
let diff = """
--- a/app/prototypes/shadow-paris/shadow-app.tsx
+++ b/app/prototypes/shadow-paris/shadow-app.tsx
@@ -1,9 +1,12 @@
 "use client";
-import { useState } from "react";
+import { useEffect, useState } from "react";
 import { MapCanvas } from "./map-canvas";
+import { ThemeToggle } from "./theme-toggle";

 export function ShadowApp() {
   const [incidents, setIncidents] = useState([]);
+  const [dark, setDark] = useState(false);
+  useEffect(() => setDark(localStorage.getItem("theme") === "dark"), []);
   return (
-    <main className="shadow">
+    <main className={dark ? "shadow dark" : "shadow"}>
       <MapCanvas incidents={incidents} />
--- a/app/prototypes/shadow-paris/theme-toggle.tsx
+++ b/app/prototypes/shadow-paris/theme-toggle.tsx
@@ -0,0 +1,8 @@
+export function ThemeToggle({ dark, onChange }) {
+  return (
+    <button className="theme-toggle" onClick={() => onChange(!dark)}>
+      {dark ? "Light" : "Dark"}
+    </button>
+  );
+}
+
--- a/app/globals.css
+++ b/app/globals.css
@@ -12,4 +12,9 @@
 .shadow { background: #f6f5f1; }
-.panel { color: #222; }
+.panel { color: var(--ink); }
+.dark { --ink: #eee; background: #111; }
+.dark .panel { background: #1b1b1b; }
"""

MainActor.assumeIsolated {
    fixture()
    let model = AppModel.shared
    let start = Date().addingTimeInterval(-66)
    let user = ChatTurn(id: "u1", role: "user", text: prompt)

    // 1. A finished change with the inspector open.
    model.chatTurns = [
        ChatTurn(id: "u0", role: "user", text: "Where does Shadow Paris get its incident data?"),
        ChatTurn(id: "a0", role: "assistant", text: "From `lib/shadow/sources.ts`: the Paris open-data feed of police reports, refreshed every 15 minutes, plus a hand-kept list of transit closures."),
        user,
        ChatTurn(id: "a1", role: "assistant", text: answer),
    ]
    model.traces["Where does Shadow Paris get its incident data?"] = TraceRecord(
        steps: Array(steps(start: start).prefix(4)), started: start, ended: start.addingTimeInterval(14), cost: 0.0065)
    model.traces[prompt] = TraceRecord(steps: steps(start: start), started: start, ended: start.addingTimeInterval(66),
                                       cost: 0.041)
    model.snapshot = [
        "episode_id": "demo", "diff": diff, "can_keep": true, "can_discard": true, "cost_usd": 0.041,
        "facts": ["files": ["app/globals.css", "app/prototypes/shadow-paris/shadow-app.tsx",
                            "app/prototypes/shadow-paris/theme-toggle.tsx"],
                  "delta": ["added": ["app/prototypes/shadow-paris/theme-toggle.tsx"],
                            "modified": ["app/globals.css", "app/prototypes/shadow-paris/shadow-app.tsx"],
                            "deleted": [String](), "renamed": [Any]()]],
        "verification": ["build": "ok", "typecheck": "ok", "tests": "not_run", "preview": "not_run"],
    ]
    model.inspectorShown = true
    let main = show(RootView(), size: CGSize(width: 1320, height: 860))
    capture(main, "10-finished-with-changes")

    // 2. A run in progress.
    model.inspectorShown = false
    model.snapshot = nil
    model.chatTurns = [user]
    model.running = true
    model.currentPrompt = prompt
    model.traceStart = Date().addingTimeInterval(-47)
    model.trace = Array(steps(start: Date().addingTimeInterval(-47)).prefix(6))
    capture(main, "11-live-run")

    // 2b. A run whose messages were cleared mid-run still shows the prompt and live steps (reasoning off).
    model.chatTurns = []
    model.reasoningOn = false
    capture(main, "11b-live-run-without-messages")

    // 2c. The working indicator's walk cycle: eight poses large, then at its real size.
    let sheet = show(VStack(alignment: .leading, spacing: 8) {
        HStack(spacing: 0) {
            ForEach(0..<8, id: \.self) { frame in
                HomininFrame(phase: 2 * .pi * Double(frame) / 8).frame(width: 100, height: 80)
            }
        }
        HStack(spacing: 18) {
            ForEach(0..<8, id: \.self) { frame in
                HomininFrame(phase: 2 * .pi * Double(frame) / 8).frame(width: 20, height: 20)
            }
            Text("Writing").foregroundStyle(.secondary)
        }
    }.padding(12).background(Color(nsColor: .windowBackgroundColor)), size: CGSize(width: 840, height: 150))
    capture(sheet, "16-walk-cycle")
    sheet.orderOut(nil)

    // 3. Start screen inside a project, reasoning off.
    model.reasoningOn = false
    model.running = false
    model.trace = []
    model.chatTurns = []
    capture(main, "12-start-in-project")
    main.orderOut(nil)

    // 4. Model browser, 5. connect, 6. settings tabs.
    let browser = show(ModelBrowser(), size: CGSize(width: 860, height: 620))
    capture(browser, "13-model-browser")
    browser.orderOut(nil)
    model.orcaConnected = false
    let connect = show(ConnectOrcaRouterView(), size: CGSize(width: 500, height: 470))
    capture(connect, "14-connect")
    connect.orderOut(nil)
    model.orcaConnected = true
    for tab in ["models", "orcarouter", "advanced"] {
        model.settingsTab = tab
        let settings = show(SettingsView(), size: CGSize(width: 560, height: 440))
        capture(settings, "15-settings-\(tab)")
        settings.orderOut(nil)
    }
}
exit(0)
