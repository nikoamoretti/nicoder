// Offscreen snapshots of Nicoder's screens with sample data (no engine, nothing on the user's screen).
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
    print(FileManager.default.fileExists(atPath: path) ? path : "NOT CAPTURED (is the Mac locked?): \(path)")
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
        ("bash", "Build the site to check the toggle compiles", "Build the site to check the toggle compiles", false, 38.5),
        ("edit", "Editing app/globals.css", "app/globals.css", true, 44.1),
        ("bash", "Build again after the CSS fix", "Build again after the CSS fix", true, 61.7),
    ]
    return rows.map {
        TraceStep(tool: $0.0, detail: $0.1, target: $0.2, ok: $0.3, started: start.addingTimeInterval($0.4),
                  command: $0.0 == "bash" ? "cd ~/projects/prototypes && npm run build 2>&1 | tail -20" : "")
    }
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
    model.showDemoTrace(TraceRecord(
        steps: Array(steps(start: start).prefix(4)), started: start, ended: start.addingTimeInterval(14), cost: 0.0065),
        for: "Where does Shadow Paris get its incident data?")
    model.showDemoTrace(TraceRecord(steps: steps(start: start), started: start, ended: start.addingTimeInterval(66),
                                    cost: 0.041), for: prompt)
    let finished: [String: Any] = [
        "episode_id": "demo", "diff": diff, "can_keep": true, "can_discard": true, "cost_usd": 0.041,
        "facts": ["files": ["app/globals.css", "app/prototypes/shadow-paris/shadow-app.tsx",
                            "app/prototypes/shadow-paris/theme-toggle.tsx"],
                  "delta": ["added": ["app/prototypes/shadow-paris/theme-toggle.tsx"],
                            "modified": ["app/globals.css", "app/prototypes/shadow-paris/shadow-app.tsx"],
                            "deleted": [String](), "renamed": [Any]()]],
        "verification": ["build": "ok", "typecheck": "ok", "tests": "not_run", "preview": "not_run"],
    ]
    model.snapshot = finished
    model.inspectorShown = true
    let main = show(RootView(), size: CGSize(width: 1320, height: 860))
    capture(main, "10-finished-with-changes")

    // 1b. After Discard: the request is closed. No "files changed" pill under the answer; the inspector
    // says "No Changes" and has no Keep or Discard bar.
    let discarded: [String: Any] = [
        "episode_id": "demo", "diff": "", "can_keep": false, "can_discard": false, "outcome": "discarded",
        "summary": "Discarded. These changes were not added to your project.", "cost_usd": 0.041,
        "facts": ["files": [String](),
                  "delta": ["added": [String](), "modified": [String](), "deleted": [String](), "renamed": [Any]()]],
        "verification": ["build": "not_run", "typecheck": "not_run", "tests": "not_run", "preview": "not_run"],
    ]
    model.snapshot = discarded
    model.inspectorShown = true
    capture(main, "10a-after-discard")

    // 1c. After Keep: the change is in the project. The diff stays, but there is no pill and no Keep or Discard bar;
    // a strip in the bar's place carries the engine's sentence and the cost.
    var kept = finished
    kept["can_keep"] = false
    kept["can_discard"] = false
    kept["outcome"] = "kept"
    kept["summary"] = "These changes are in your project."
    model.snapshot = kept
    model.inspectorShown = true
    capture(main, "10b-after-keep")

    // 1d. An older request held back by a newer one built on it (opened from History): its files and diff show, but
    // neither Keep nor Discard is offered, so the strip says what to do instead.
    var blocked = finished
    blocked["can_keep"] = false
    blocked["can_discard"] = false
    blocked["outcome"] = "review"
    blocked["blocked_by"] = "req-2"
    blocked["summary"] = "A newer request in this project builds on this one, so Keep or Discard the newer one first."
    model.snapshot = blocked
    model.inspectorShown = true
    capture(main, "10c-blocked-older-request")

    // 1e. The same while the newer request is still running (the longer sentence).
    blocked["summary"] = "A newer request in this project builds on this one and is still running, so let it finish or stop it first."
    model.snapshot = blocked
    capture(main, "10c-blocked-older-request-running")

    // 1f. A History row opened for a discarded request: no files, so a notice says so and the inspector stays closed.
    model.snapshot = discarded
    model.inspectorShown = false
    model.banner = "This request was discarded. Your project was not changed."
    capture(main, "10d-history-notice")
    model.banner = nil

    // 2. A run in progress.
    model.inspectorShown = false
    model.snapshot = nil
    model.chatTurns = [user]
    model.showDemoRun(prompt: prompt, started: Date().addingTimeInterval(-47),
                      steps: Array(steps(start: Date().addingTimeInterval(-47)).prefix(6)))
    capture(main, "11-live-run")
    capture(main, "11-live-run-later")

    // 2a. A command while it runs reads as the model's words for it, not the command (Nico, 2026-10-27), and the
    // finished steps list them the same way.
    let commandRun = Date().addingTimeInterval(-200)
    model.showDemoRun(prompt: prompt, started: commandRun, steps: Array(steps(start: commandRun).prefix(6)) + [
        TraceStep(tool: "bash", detail: "Run the scraper on the live page", target: "Run the scraper on the live page",
                  ok: true, started: Date().addingTimeInterval(-152),
                  command: "cd ~/projects/ingestion-scrape && python3 scrape.py --live canary", running: true, call: "c1"),
    ])
    capture(main, "11c-live-run-command")
    let timeline = show(TraceTimeline(steps: steps(start: commandRun), start: commandRun, live: false).padding(16),
                        size: CGSize(width: 640, height: 280))
    capture(timeline, "11d-steps-with-commands")
    timeline.orderOut(nil)

    // 2a'. A command on the Mac itself, outside the sandbox, says so (Nico, 2026-10-27: "i want nicoder to be able to
    // manipulated my mac if needed").
    let macRun = Date().addingTimeInterval(-95)
    let macSteps = [
        TraceStep(tool: "read", detail: "Reading sql/init.sql", target: "sql/init.sql", ok: true,
                  started: macRun.addingTimeInterval(2)),
        TraceStep(tool: "bash", detail: "Create the tables", target: "Create the tables", ok: false,
                  started: macRun.addingTimeInterval(9), command: "psql -d crime_analysis_db -f sql/init.sql"),
        TraceStep(tool: "bash", detail: "Install PostGIS with Homebrew", target: "Install PostGIS with Homebrew",
                  ok: true, started: macRun.addingTimeInterval(14), command: "brew install postgis", mac: true),
        TraceStep(tool: "bash", detail: "Create the tables", target: "Create the tables", ok: true,
                  started: macRun.addingTimeInterval(71), command: "psql -d crime_analysis_db -f sql/init.sql"),
    ]
    model.showDemoRun(prompt: "Set up the crime analysis database", started: macRun, steps: Array(macSteps.prefix(2)) + [
        TraceStep(tool: "bash", detail: "Install PostGIS with Homebrew", target: "Install PostGIS with Homebrew",
                  ok: true, started: Date().addingTimeInterval(-81), command: "brew install postgis", running: true,
                  call: "m1", mac: true),
    ])
    capture(main, "11e-live-mac-command")
    let macTimeline = show(TraceTimeline(steps: macSteps, start: macRun, live: false).padding(16),
                           size: CGSize(width: 640, height: 170))
    capture(macTimeline, "11f-steps-with-a-mac-command")
    macTimeline.orderOut(nil)

    // 2b. A run whose messages were cleared mid-run still shows the prompt and live steps (reasoning off).
    model.chatTurns = []
    model.reasoningOn = false
    capture(main, "11b-live-run-without-messages")

    // 2c. The working indicator's walk cycle: eight poses large, then at its real size.
    let sheet = show(VStack(alignment: .leading, spacing: 10) {
        HStack(spacing: 0) {
            ForEach(0..<8, id: \.self) { frame in
                HomininFrame(phase: 2 * .pi * Double(frame) / 8).frame(width: 130, height: 130)
            }
        }
        HStack(spacing: 14) {
            ForEach(0..<8, id: \.self) { frame in
                HomininFrame(phase: 2 * .pi * Double(frame) / 8).frame(width: 50, height: 50)
            }
            Text("Writing").foregroundStyle(.secondary)
        }
    }.padding(12).background(Color(nsColor: .windowBackgroundColor)), size: CGSize(width: 1080, height: 230))
    capture(sheet, "16-walk-cycle")
    sheet.orderOut(nil)

    // 3. Start screen inside a project, reasoning off.
    model.reasoningOn = false
    model.endDemoRun()
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

    // 7. The states the ui-fixes items touch (items.swift).
    itemFixtures()
    // 8. Saved chats, and work that keeps running while the window shows another chat (items.swift).
    chatsFixtures()
    // 9. Files and images attached to a message (items.swift).
    attachmentsFixtures()
}
exit(0)
