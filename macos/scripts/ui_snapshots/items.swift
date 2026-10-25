// Fixtures for the ten visible fixes (item01 ... item10): each state the fixes touch that the main screens do not
// show. Run through snapshots.sh, which compiles this file next to main.swift. Lines starting "MEASURE" are numbers
// read off the real views, so a before/after pair carries its own figures.
import AppKit
import SwiftUI

// MARK: - Helpers

@MainActor
func pump(_ seconds: Double) {
    let stop = Date().addingTimeInterval(seconds)
    while Date() < stop {
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
    }
}

/// The app's window (WindowRoot, with the minimum the app declares), shrunk to the smallest size it allows.
@MainActor
func showAtItsSmallest() -> NSWindow {
    let window = show(WindowRoot(), size: CGSize(width: 1500, height: 900))
    pump(0.6)
    window.setContentSize(window.contentMinSize)
    pump(0.6)
    return window
}

func views<T: NSView>(of type: T.Type, in view: NSView) -> [T] {
    var found: [T] = []
    if let match = view as? T { found.append(match) }
    for sub in view.subviews { found += views(of: type, in: sub) }
    return found
}

/// What a changed file looks like in the sample diff.
struct SampleFile {
    let path: String
    var status: DiffFile.Status = .modified
    let added: Int
    let removed: Int
}

/// A review payload shaped like the engine's: a diff for each file, the delta and the verification words.
func sampleReview(_ files: [SampleFile], checks: [String: String], canKeep: Bool = true, canDiscard: Bool = true,
                  summary: String? = nil, outcome: String = "review") -> [String: Any] {
    let diff = files.map { file -> String in
        var lines = ["--- a/\(file.path)", "+++ b/\(file.path)", "@@ -1,\(file.removed + 2) +1,\(file.added + 2) @@",
                     " import { useState } from \"react\";"]
        lines += (0..<file.removed).map { "-const before\($0) = \($0);" }
        lines += (0..<file.added).map { "+const after\($0) = \($0);" }
        lines.append(" export default App;")
        return lines.joined(separator: "\n")
    }.joined(separator: "\n")
    var snapshot: [String: Any] = [
        "episode_id": "demo", "diff": diff, "can_keep": canKeep, "can_discard": canDiscard, "outcome": outcome,
        "cost_usd": 0.041,
        "facts": ["files": files.map(\.path).sorted(),
                  "delta": ["added": files.filter { $0.status == .added }.map(\.path),
                            "modified": files.filter { $0.status == .modified }.map(\.path),
                            "deleted": files.filter { $0.status == .deleted }.map(\.path),
                            "renamed": [Any]()]],
        "verification": checks,
    ]
    if let summary { snapshot["summary"] = summary }
    return snapshot
}

/// The words the engine really sends: build and typecheck say pass or fail, the preview says ok or failed.
let engineChecksPass: [String: String] = ["build": "pass", "typecheck": "pass", "tests": "not_run", "preview": "ok"]
let engineChecksFail: [String: String] = ["build": "fail", "typecheck": "fail", "tests": "not_run", "preview": "failed"]

let threeFiles = [
    SampleFile(path: "app/globals.css", added: 3, removed: 1),
    SampleFile(path: "app/prototypes/shadow-paris/shadow-app.tsx", added: 5, removed: 2),
    SampleFile(path: "app/prototypes/shadow-paris/theme-toggle.tsx", status: .added, added: 8, removed: 0),
]

@MainActor
func resetForItems() {
    let model = AppModel.shared
    model.engine.stub = nil
    model.running = false
    model.trace = []
    model.traceStart = nil
    model.currentPrompt = nil
    model.chatTurns = []
    model.snapshot = nil
    model.previewURL = nil
    model.banner = nil
    model.inspectorShown = false
    model.showBrowser = false
    model.requestText = ""
    model.reasoningOn = true
}

/// Finished conversation behind the review, so a full-window render looks like the app.
@MainActor
func finishedConversation(prompt: String, answer: String) {
    let model = AppModel.shared
    let start = Date().addingTimeInterval(-66)
    model.chatTurns = [
        ChatTurn(id: "u1", role: "user", text: prompt),
        ChatTurn(id: "a1", role: "assistant", text: answer),
    ]
    model.traces[prompt] = TraceRecord(steps: steps(start: start), started: start, ended: start.addingTimeInterval(66),
                                       cost: 0.041)
}

// MARK: - Measurements

/// The window, the three columns and how much of the layout the window shows. The detail column is what is left of the
/// layout between the sidebar and the inspector (the sidebar floats over it).
@MainActor
func reportColumns(_ label: String, _ window: NSWindow) {
    guard let root = window.contentView else { return }
    let splits = views(of: NSSplitView.self, in: root)
    guard let outer = splits.first, outer.arrangedSubviews.count == 2 else { return }
    let sidebar = outer.arrangedSubviews[0].frame.width
    let layout = outer.arrangedSubviews[1].frame.width
    let inspectorItem = splits.count > 1 && splits[1].arrangedSubviews.count == 2 ? splits[1].arrangedSubviews[1] : nil
    let inspector = inspectorItem.map { $0.isHidden ? 0 : $0.frame.width } ?? 0
    let shown = window.frame.width
    print("MEASURE \(label): window \(Int(shown)) wide, layout \(Int(layout)) wide (\(Int(max(0, layout - shown))) pt cut off), "
          + "sidebar \(Int(sidebar)), inspector \(Int(inspector)), conversation column \(Int(layout - sidebar - inspector)), "
          + "window minimum \(Int(window.contentMinSize.width))x\(Int(window.contentMinSize.height))")
}

@MainActor
func reportList(_ label: String, _ window: NSWindow) {
    guard let root = window.contentView else { return }
    for table in views(of: NSTableView.self, in: root) where table.numberOfRows > 0 {
        let last = table.numberOfRows - 1
        let end = table.rect(ofRow: last).maxY
        let scroll = table.enclosingScrollView
        let height = scroll?.frame.height ?? table.frame.height
        let insets = scroll?.contentInsets
        print("MEASURE \(label) list rows=\(table.numberOfRows) rowHeight=\(table.rect(ofRow: 0).height) "
              + "rowsEnd=\(end) listHeight=\(height) insets=\(insets?.top ?? 0)/\(insets?.bottom ?? 0) "
              + "emptyBelow=\(height - end - (insets?.top ?? 0))")
    }
}

@MainActor
func fitting<V: View>(_ view: V, width: CGFloat) -> CGFloat {
    NSHostingView(rootView: view.frame(width: width).environmentObject(AppModel.shared)).fittingSize.height
}

// MARK: - Fixtures

@MainActor
func itemFixtures() {
    let model = AppModel.shared
    let prompt = "Add a dark mode toggle to the Shadow Paris map"
    let answer = "Added a **dark mode toggle** to the Shadow Paris map. It sits in the top-right corner and remembers your choice."

    // Item 1: the check chips with the words the engine sends. Every state maps to green or red, none is dropped.
    resetForItems()
    model.snapshot = sampleReview(threeFiles, checks: engineChecksPass)
    var inspector = show(ChangesInspector(), size: CGSize(width: 460, height: 300))
    capture(inspector, "item01-check-chips-pass")
    model.snapshot = sampleReview(threeFiles, checks: engineChecksFail)
    capture(inspector, "item01-check-chips-fail")
    model.snapshot = sampleReview(threeFiles, checks: ["build": "pass", "typecheck": "pass", "tests": "not_run",
                                                       "preview": "not_run"])
    capture(inspector, "item01-check-chips-build-and-types")
    inspector.orderOut(nil)

    // Item 3: the composer with a draft, over a finished conversation.
    resetForItems()
    finishedConversation(prompt: prompt, answer: answer)
    model.requestText = "Make the toggle remember my choice"
    var window = show(WindowRoot(), size: CGSize(width: 1100, height: 700))
    capture(window, "item03-composer-glass")
    window.orderOut(nil)

    // Item 4: the smallest window the app allows, with Changes open and closed.
    resetForItems()
    finishedConversation(prompt: prompt, answer: answer)
    model.snapshot = sampleReview(threeFiles, checks: engineChecksPass)
    model.inspectorShown = true
    window = showAtItsSmallest()
    capture(window, "item04-smallest-window-changes-open")
    reportColumns("item04 smallest window, Changes open", window)
    window.orderOut(nil)
    // The window the app opens at (1220 pt wide) with Changes open.
    window = show(WindowRoot(), size: CGSize(width: 1220, height: 800))
    pump(0.8)
    capture(window, "item04-default-window-changes-open")
    reportColumns("item04 default window (1220), Changes open", window)
    window.orderOut(nil)
    // A new window, not a toggle: an offscreen window never finishes the inspector's open and close animation.
    model.inspectorShown = false
    window = showAtItsSmallest()
    capture(window, "item04-smallest-window-changes-closed")
    reportColumns("item04 smallest window, Changes closed", window)
    window.orderOut(nil)

    // Item 5: the model browser opened over the smallest window.
    resetForItems()
    window = showAtItsSmallest()
    model.showBrowser = true
    pump(1.5)
    if let sheet = window.attachedSheet {
        let overhang = window.frame.minY - sheet.frame.minY
        print("MEASURE item05 window height \(Int(window.frame.height)), sheet \(Int(sheet.frame.width))x"
              + "\(Int(sheet.frame.height)), sheet top \(Int(window.frame.maxY - sheet.frame.maxY)) below the window top, "
              + "sheet bottom \(Int(overhang)) pt below the window bottom (negative = inside)")
        capture(window, "item05-model-browser-over-smallest-window")
        capture(sheet, "item05-model-browser-sheet")
    } else {
        print("MEASURE item05 no sheet was attached")
    }
    model.showBrowser = false
    pump(0.6)
    window.orderOut(nil)

    // Item 6: Keep is off and the engine says why (a result with no files left).
    resetForItems()
    finishedConversation(prompt: prompt, answer: answer)
    model.snapshot = sampleReview(
        threeFiles, checks: engineChecksPass, canKeep: false, canDiscard: true,
        summary: "This result has no files left, so Keep would empty your project. Your project was not changed.")
    model.inspectorShown = true
    window = show(WindowRoot(), size: CGSize(width: 1320, height: 860))
    capture(window, "item06-keep-disabled-with-reason")
    window.orderOut(nil)

    // Item 7: the walker's row against a step's row, and the walker as the first row.
    resetForItems()
    let start = Date().addingTimeInterval(-47)
    let sample = Array(steps(start: start).prefix(2))
    let one = fitting(TraceTimeline(steps: Array(sample.prefix(1)), start: start, live: false), width: 600)
    let two = fitting(TraceTimeline(steps: sample, start: start, live: false), width: 600)
    let live = fitting(TraceTimeline(steps: sample, start: start, live: true), width: 600)
    let alone = fitting(TraceTimeline(steps: [], start: start, live: true), width: 600)
    print("MEASURE item07 step row \(two - one) pt, walker row \(live - two) pt, walker alone \(alone) pt")
    model.currentPrompt = prompt
    model.chatTurns = [ChatTurn(id: "u1", role: "user", text: prompt)]
    model.running = true
    model.traceStart = start
    model.trace = Array(steps(start: start).prefix(2))
    window = show(WindowRoot(), size: CGSize(width: 1100, height: 700))
    capture(window, "item07-walker-after-two-steps")
    model.trace = []
    capture(window, "item07-walker-starting")
    window.orderOut(nil)

    // Item 8: a short list, a one-file list and a long one (which scrolls).
    resetForItems()
    model.snapshot = sampleReview(threeFiles, checks: engineChecksPass)
    inspector = show(ChangesInspector(), size: CGSize(width: 460, height: 520))
    capture(inspector, "item08-three-files")
    reportList("item08 three files", inspector)
    model.snapshot = sampleReview([threeFiles[0]], checks: engineChecksPass)
    capture(inspector, "item08-one-file")
    reportList("item08 one file", inspector)
    let many = (1...12).map { SampleFile(path: "src/components/panel\($0).tsx", added: $0, removed: $0 % 3) }
    model.snapshot = sampleReview(many, checks: engineChecksPass)
    capture(inspector, "item08-twelve-files")
    reportList("item08 twelve files", inspector)
    inspector.orderOut(nil)

    // Item 9: Keep and Discard with a preview open. The real keep() and discard() run against a stub engine.
    let recent: [[String: Any]] = model.recent.map { ["path": $0.path, "name": $0.name] }
    let requests: [[String: Any]] = model.history.map {
        ["episode_id": $0.episodeID, "request": $0.request, "outcome": $0.outcome, "updated_ts": $0.updated]
    }
    for (name, closing) in [("keep", "kept"), ("discard", "discarded")] {
        resetForItems()
        finishedConversation(prompt: prompt, answer: answer)
        let files = threeFiles
        model.snapshot = sampleReview(files, checks: engineChecksPass)
        model.previewURL = "http://127.0.0.1:5173/"
        model.inspectorShown = true
        window = show(WindowRoot(), size: CGSize(width: 1320, height: 860))
        capture(window, "item09-\(name)-before-the-click")
        let closed = sampleReview(files, checks: engineChecksPass, canKeep: false, canDiscard: false,
                                  summary: closing == "kept" ? "These changes are in your project."
                                  : "Discarded. These changes were not added to your project.", outcome: closing)
        model.engine.stub = { method, _ in
            switch method {
            case "snapshot": return closed
            case "prefs": return ["recent_projects": recent]
            case "history": return ["episodes": requests]
            default: return [:]
            }
        }
        if name == "keep" { model.keep() } else { model.discard() }
        pump(0.8)
        model.banner = nil
        model.inspectorShown = true
        capture(window, "item09-\(name)-then-changes-reopened")
        print("MEASURE item09 after \(name): previewURL = \(model.previewURL ?? "nil")")
        window.orderOut(nil)
    }
    model.engine.stub = nil

    // Item 10: the file list at the inspector's narrowest, with long names and long folders.
    resetForItems()
    model.snapshot = sampleReview([
        SampleFile(path: "src/components/settings/AccountPreferencesPanel.tsx", added: 12, removed: 4),
        SampleFile(path: "app/prototypes/shadow-paris/theme-toggle-button.tsx", status: .added, added: 8, removed: 0),
        SampleFile(path: "app/globals.css", added: 3, removed: 1),
    ], checks: engineChecksPass)
    inspector = show(ChangesInspector(), size: CGSize(width: 340, height: 360))
    capture(inspector, "item10-file-rows-at-340")
    inspector.orderOut(nil)
    resetForItems()
}
