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
                .padding(.horizontal, 14)
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
            // Only while a result still waits: after Keep or Discard the request is closed.
            if model.canKeep || model.canDiscard {
                ReviewBar()
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

    var body: some View {
        let totals = DiffParser.totals(files)
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("\(files.count) file\(files.count == 1 ? "" : "s") changed")
                        .font(.headline)
                    Text("+\(totals.added)")
                        .foregroundStyle(.green)
                    Text("−\(totals.removed)")
                        .foregroundStyle(.red)
                    Spacer()
                }
                .font(.callout.monospacedDigit())
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
            .frame(height: min(CGFloat(files.count) * 30 + 12, 200))
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
        HStack(spacing: 6) {
            if ran.isEmpty {
                Label("Local Coder did not run checks for this project", systemImage: "minus.circle")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(ran, id: \.0) { name, status in
                    Label(name, systemImage: status == "ok" ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundStyle(status == "ok" ? .green : .red)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(.quaternary.opacity(0.5), in: .capsule)
                }
            }
        }
        .font(.caption.weight(.medium))
    }
}

private struct FileRow: View {
    let file: DiffFile

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: file.status.symbol)
                .foregroundStyle(file.status.tint)
                .frame(width: 16)
            Text(file.name)
                .fontWeight(.medium)
                .lineLimit(1)
            Text(file.folder)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.head)
            Spacer(minLength: 6)
            if file.added > 0 {
                Text("+\(file.added)")
                    .foregroundStyle(.green)
            }
            if file.removed > 0 {
                Text("−\(file.removed)")
                    .foregroundStyle(.red)
            }
            DiffBar(added: file.added, removed: file.removed)
        }
        .font(.callout.monospacedDigit())
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

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let cost = model.requestCost {
                Text("This change cost \(TraceSummary.cost(cost)).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                Button("Discard", role: .destructive) { model.discard() }
                    .disabled(!model.canDiscard)
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
                .disabled(!model.canKeep)
                .help("Copy these changes into your project (⌘K)")
                .accessibilityIdentifier("keepButton")
            }
            .controlSize(.large)
        }
        .padding(14)
        .background(.bar)
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
                    .font(.callout.monospaced())
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
            .padding(.horizontal, 14)
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
            case .added: return .green
            case .removed: return .red
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
