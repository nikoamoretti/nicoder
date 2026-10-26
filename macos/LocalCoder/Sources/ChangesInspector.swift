import AppKit
import SwiftUI
import WebKit

/// The proposed changes beside the conversation: files, diff, checks, preview, Keep or Discard.
struct ChangesInspector: View {
    @EnvironmentObject private var model: AppModel
    @State private var tab: Tab = .changes
    @State private var selectedFile: String?

    enum Tab: String, CaseIterable, Identifiable {
        case changes = "Changes"
        case preview = "Preview"
        var id: String { rawValue }
    }

    private var files: [DiffFile] {
        DiffParser.parse(model.diffText, delta: model.changeDelta)
    }

    var body: some View {
        VStack(spacing: 0) {
            if model.previewURL != nil {
                Picker("View", selection: $tab) {
                    ForEach(Tab.allCases) { tab in
                        Text(tab.rawValue).tag(tab)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            }
            if tab == .preview, let url = model.previewURL.flatMap(URL.init(string:)) {
                PreviewTab(url: url)
            } else if files.isEmpty {
                ContentUnavailableView(
                    "No Changes",
                    systemImage: "doc.on.doc",
                    description: Text("When the model changes files, they wait here until you keep or discard them.")
                )
            } else {
                ChangesList(files: files, selectedFile: $selectedFile)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            // Keep and Discard only while a result still waits. Once the request is closed (kept) or held back
            // by a newer one, its files still show, so the strip says why there is no bar.
            if model.hasChanges {
                if model.canKeep || model.canDiscard {
                    ReviewBar()
                } else {
                    ReviewNote()
                }
            }
        }
        .onChange(of: model.previewURL) { _, url in
            if url == nil { tab = .changes }
        }
        .onChange(of: model.diffText) { _, _ in
            selectedFile = nil
        }
    }
}

private struct ChangesList: View {
    @EnvironmentObject private var model: AppModel
    let files: [DiffFile]
    @Binding var selectedFile: String?

    private var shown: DiffFile? {
        files.first(where: { $0.id == selectedFile }) ?? files.first
    }

    /// As tall as the rows (an inset list adds 10 pt above and below them); a long list scrolls at 200 pt.
    static func listHeight(rows: Int) -> CGFloat {
        min(CGFloat(rows) * FileRow.height + 20, 200)
    }

    var body: some View {
        let totals = DiffParser.totals(files)
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("\(files.count) file\(files.count == 1 ? "" : "s") changed")
                        .fontWeight(.semibold)
                        .foregroundStyle(Console.ink)
                    Text("+\(totals.added)")
                        .foregroundStyle(.passText)
                    Text("−\(totals.removed)")
                        .foregroundStyle(.failText)
                    Spacer()
                }
                .font(Console.mono(13))
                ChecksRow()
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 10)
            List(files, selection: Binding(get: { shown?.id }, set: { selectedFile = $0 })) { file in
                FileRow(file: file)
                    .tag(Optional(file.id))
            }
            .listStyle(.inset)
            .environment(\.defaultMinListRowHeight, FileRow.height)
            .frame(height: Self.listHeight(rows: files.count))
            Divider()
            if let shown {
                DiffView(file: shown)
            }
        }
    }
}

private struct ChecksRow: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let checks = model.verification
        let rows: [(String, String)] = [
            ("Build", checks["build"] as? String ?? "not_run"),
            ("Types", checks["typecheck"] as? String ?? "not_run"),
            ("Tests", checks["tests"] as? String ?? "not_run"),
            ("Preview", checks["preview"] as? String ?? "not_run"),
        ]
        let ran = rows.filter { $0.1 != "not_run" }
        HStack(spacing: 14) {
            if ran.isEmpty {
                Text("– no checks ran for this change")
                    .foregroundStyle(Console.faint)
            } else {
                ForEach(ran, id: \.0) { name, word in
                    let passed = CheckWord.passed(word)
                    Text("\(passed ? "✓" : "✗") \(name.lowercased())")
                        .foregroundStyle(passed ? Color.passText : Color.failText)
                }
            }
        }
        .font(Console.mono(12))
        .font(.caption.weight(.medium))
    }
}

/// The engine says "pass" or "fail" for the build and the source check and "ok" or "failed" for the preview. Only a
/// pass is drawn as one; a word this does not know reads as a failure, never as a success.
enum CheckWord {
    static func passed(_ word: String) -> Bool {
        word == "pass" || word == "ok"
    }
}

private struct FileRow: View {
    static let height: CGFloat = 24
    let file: DiffFile

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: file.status.symbol)
                .foregroundStyle(file.status.tint)
                .frame(width: 16)
            Text(file.name)
                .font(Console.mono(12, .semibold))
                .foregroundStyle(Console.ink)
                .lineLimit(1)
                .layoutPriority(1)
            Text(file.folder)
                .font(Console.mono(11.5))
                .foregroundStyle(Console.faint)
                .lineLimit(1)
                .truncationMode(.head)
            Spacer(minLength: 6)
            if file.added > 0 {
                Text("+\(file.added)")
                    .foregroundStyle(.passText)
                    .fixedSize()
            }
            if file.removed > 0 {
                Text("−\(file.removed)")
                    .foregroundStyle(.failText)
                    .fixedSize()
            }
            DiffBar(added: file.added, removed: file.removed)
        }
        .font(Console.mono(11.5))
        .help(file.path)
    }
}

/// Five squares, shaded by the share of added and removed lines.
private struct DiffBar: View {
    let added: Int
    let removed: Int

    var body: some View {
        let total = max(1, added + removed)
        let green = added == 0 ? 0 : max(1, Int((Double(added) / Double(total) * 5).rounded()))
        let red = removed == 0 ? 0 : min(5 - green, max(1, Int((Double(removed) / Double(total) * 5).rounded())))
        HStack(spacing: 1.5) {
            ForEach(0..<5, id: \.self) { index in
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(index < green ? AnyShapeStyle(.green) : index < green + red ? AnyShapeStyle(.red)
                          : AnyShapeStyle(.quaternary))
                    .frame(width: 6, height: 6)
            }
        }
    }
}

private struct DiffView: View {
    let file: DiffFile

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(file.lines) { line in
                    DiffLineView(line: line)
                }
            }
            .padding(.vertical, 6)
        }
        .background(.background)
    }
}

private struct DiffLineView: View {
    let line: DiffLine

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            Text(line.oldNumber.map(String.init) ?? "")
                .frame(width: 34, alignment: .trailing)
                .foregroundStyle(.tertiary)
            Text(line.newNumber.map(String.init) ?? "")
                .frame(width: 34, alignment: .trailing)
                .foregroundStyle(.tertiary)
            Text(line.kind.sign)
                .frame(width: 18)
                .foregroundStyle(line.kind.tint)
            Text(line.text.isEmpty ? " " : line.text)
                .foregroundStyle(line.kind == .hunk || line.kind == .meta ? .secondary : .primary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
        .font(.system(size: 11.5, design: .monospaced))
        .padding(.vertical, 1)
        .padding(.trailing, 8)
        .background(line.kind.background)
    }
}

private struct ReviewBar: View {
    @EnvironmentObject private var model: AppModel

    /// Why Keep is off: a change running in another project, or the engine's own sentence about this result.
    private var keepReason: String? {
        if let other = model.decisionWaitsFor {
            return "Keep and Discard wait until the change in \(other) finishes."
        }
        return model.canKeep ? nil : model.snapshotSummary
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let keepReason {
                Text(keepReason)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("keepReason")
            }
            CostCaption()
            HStack(spacing: 10) {
                Button("Discard", role: .destructive) { model.discard() }
                    .disabled(!model.canDiscard || model.decisionWaitsFor != nil)
                    .accessibilityIdentifier("discardButton")
                Spacer()
                Button {
                    model.keep()
                } label: {
                    Text("Keep Changes")
                        .frame(minWidth: 120)
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut("k", modifiers: [.command])
                .disabled(!model.canKeep || model.decisionWaitsFor != nil)
                .help(keepReason ?? "Copy these changes into your project (⌘K)")
                .accessibilityIdentifier("keepButton")
            }
            .controlSize(.large)
        }
        .padding(16)
        .background(.bar)
    }
}

/// The bar's place once there is nothing to decide: the engine's sentence (kept, or held back by a newer request)
/// and the cost, in the bar's own chrome.
private struct ReviewNote: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if model.snapshotSummary != nil || model.requestCost != nil {
            VStack(alignment: .leading, spacing: 4) {
                if let summary = model.snapshotSummary {
                    Text(summary)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                CostCaption()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(.bar)
            .accessibilityIdentifier(model.blockedBy == nil ? "reviewNote" : "reviewNoteBlocked")
        }
    }
}

private struct CostCaption: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if let cost = model.requestCost {
            Text("This change cost \(TraceSummary.cost(cost)).")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

private struct PreviewTab: View {
    let url: URL

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "globe")
                    .foregroundStyle(.secondary)
                Text(url.absoluteString)
                    .font(Console.mono(11.5))
                    .lineLimit(1)
                    .textSelection(.enabled)
                Spacer()
                Button {
                    NSWorkspace.shared.open(url)
                } label: {
                    Image(systemName: "arrow.up.forward.square")
                }
                .buttonStyle(.borderless)
                .help("Open in your browser")
                .accessibilityIdentifier("openInBrowserButton")
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            Divider()
            PreviewPane(url: url)
                .accessibilityIdentifier("previewView")
        }
    }
}

struct PreviewPane: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        // No media until the user plays it: WebKit's media handling asked macOS for Apple Music access (2026-10-25).
        config.mediaTypesRequiringUserActionForPlayback = .all
        config.allowsAirPlayForMediaPlayback = false
        let view = WKWebView(frame: .zero, configuration: config)
        view.setAccessibilityIdentifier("previewWebView")
        view.load(URLRequest(url: url))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        if view.url != url {
            view.load(URLRequest(url: url))
        }
    }
}

// MARK: - Diff model

struct DiffFile: Identifiable, Hashable {
    enum Status: Hashable {
        case added, modified, deleted, renamed

        var symbol: String {
            switch self {
            case .added: return "plus.square.fill"
            case .modified: return "pencil.circle.fill"
            case .deleted: return "minus.square.fill"
            case .renamed: return "arrow.right.square.fill"
            }
        }

        var tint: Color {
            switch self {
            case .added: return .green
            case .modified: return .orange
            case .deleted: return .red
            case .renamed: return .blue
            }
        }
    }

    let path: String
    var status: Status
    var lines: [DiffLine]
    var added: Int
    var removed: Int
    var id: String { path }
    var name: String { (path as NSString).lastPathComponent }
    var folder: String {
        let folder = (path as NSString).deletingLastPathComponent
        return folder.isEmpty ? "" : folder + "/"
    }
}

struct DiffLine: Identifiable, Hashable {
    enum Kind: Hashable {
        case context, added, removed, hunk, meta

        var sign: String {
            switch self {
            case .added: return "+"
            case .removed: return "−"
            default: return ""
            }
        }

        var tint: Color {
            switch self {
            case .added: return .passText
            case .removed: return .failText
            default: return .secondary
            }
        }

        var background: Color {
            switch self {
            case .added: return .green.opacity(0.12)
            case .removed: return .red.opacity(0.12)
            case .hunk: return .accentColor.opacity(0.07)
            default: return .clear
            }
        }
    }

    let id: Int
    let kind: Kind
    let text: String
    let oldNumber: Int?
    let newNumber: Int?
}

/// Reads the engine's unified diff (Python difflib, "a/" and "b/" prefixes, "rename: x -> y").
enum DiffParser {
    static func parse(_ text: String, delta: [String: Any]) -> [DiffFile] {
        guard !text.isEmpty else { return [] }
        let added = Set(delta["added"] as? [String] ?? [])
        let deleted = Set(delta["deleted"] as? [String] ?? [])
        var files: [DiffFile] = []
        var current: DiffFile?
        var oldLine = 0
        var newLine = 0
        var counter = 0

        func close() {
            if let file = current { files.append(file) }
            current = nil
        }

        func push(_ kind: DiffLine.Kind, _ body: String, old: Int?, new: Int?) {
            counter += 1
            current?.lines.append(DiffLine(id: counter, kind: kind, text: body, oldNumber: old, newNumber: new))
        }

        for line in text.components(separatedBy: "\n") {
            if line.hasPrefix("--- ") {
                close()
                continue
            }
            if line.hasPrefix("+++ ") {
                var path = String(line.dropFirst(4))
                if path.hasPrefix("b/") { path.removeFirst(2) }
                let status: DiffFile.Status = added.contains(path) ? .added : deleted.contains(path) ? .deleted : .modified
                current = DiffFile(path: path, status: status, lines: [], added: 0, removed: 0)
                continue
            }
            if line.hasPrefix("rename: ") {
                close()
                let parts = line.dropFirst("rename: ".count).components(separatedBy: " -> ")
                let path = parts.last ?? ""
                current = DiffFile(path: path, status: .renamed, lines: [], added: 0, removed: 0)
                push(.meta, "Renamed from \(parts.first ?? "")", old: nil, new: nil)
                close()
                continue
            }
            guard current != nil else { continue }
            if line.hasPrefix("@@") {
                let numbers = line.matches(of: #/[-+](\d+)/#).compactMap { Int($0.1) }
                oldLine = numbers.first ?? 0
                newLine = numbers.count > 1 ? numbers[1] : 0
                push(.hunk, line, old: nil, new: nil)
            } else if line.hasPrefix("+") {
                push(.added, String(line.dropFirst()), old: nil, new: newLine)
                newLine += 1
                current?.added += 1
            } else if line.hasPrefix("-") {
                push(.removed, String(line.dropFirst()), old: oldLine, new: nil)
                oldLine += 1
                current?.removed += 1
            } else if line.hasPrefix(" ") {
                push(.context, String(line.dropFirst()), old: oldLine, new: newLine)
                oldLine += 1
                newLine += 1
            } else if line.hasPrefix("…") {
                push(.meta, line, old: nil, new: nil)
            }
        }
        close()
        return files
    }

    static func totals(_ files: [DiffFile]) -> (added: Int, removed: Int) {
        files.reduce((0, 0)) { ($0.0 + $1.added, $0.1 + $1.removed) }
    }
}
