import AppKit
import SwiftUI

/// What the app's window shows: the split view and the least size the window may be given. With Changes open the
/// columns need more than 920 pt (the sidebar counts twice): at 920 the layout ran wider than the window and cut off
/// the sidebar and the inspector. Open, the minimum is the layout's own and the window grows to it; closed, it is 920.
struct WindowRoot: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        RootView()
            .frame(minWidth: model.inspectorShown ? nil : 920, minHeight: 600)
    }
}

struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @State private var columns = NavigationSplitViewVisibility.all

    var body: some View {
        NavigationSplitView(columnVisibility: $columns) {
            SidebarView()
                .navigationSplitViewColumnWidth(min: 200, ideal: 200, max: 320)
        } detail: {
            SessionView()
                .navigationTitle(model.projectPath == nil ? "Local Coder" : model.projectName)
                .navigationSubtitle(Self.tildePath(model.projectPath) ?? "Any folder on this Mac")
                .toolbar { toolbar }
                .inspector(isPresented: $model.inspectorShown) {
                    ChangesInspector()
                        .inspectorColumnWidth(min: 340, ideal: 420, max: 760)
                }
        }
        .sheet(isPresented: $model.showConnect) {
            ConnectOrcaRouterView()
                .environmentObject(model)
        }
        .modelBrowserSheet(isPresented: $model.showBrowser)
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .primaryAction) {
            Button {
                model.newChat()
            } label: {
                Label("New Chat", systemImage: "square.and.pencil")
            }
            .disabled(model.running)
            .help(model.running ? "Stop the current request first" : "Start a new conversation (⌘N)")
            .accessibilityIdentifier("newChatButton")
        }
        ToolbarItem(placement: .primaryAction) {
            Button {
                withAnimation { model.inspectorShown.toggle() }
            } label: {
                Label("Changes", systemImage: "sidebar.trailing")
            }
            .help(model.hasChanges ? "Show the proposed changes" : "Changes and preview")
        }
    }

    static func tildePath(_ path: String?) -> String? {
        guard let path else { return nil }
        let home = NSHomeDirectory()
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }
}

// MARK: - Sidebar

struct SidebarView: View {
    @EnvironmentObject private var model: AppModel

    private var selection: Binding<String?> {
        Binding(
            get: { model.projectPath },
            set: { path in
                if let path { model.selectProject(path) }
            }
        )
    }

    var body: some View {
        List(selection: selection) {
            Section("Projects") {
                if model.recent.isEmpty {
                    Text("Projects you open appear here.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                ForEach(model.recent) { project in
                    Label(project.name, systemImage: "folder")
                        .help(RootView.tildePath(project.path) ?? project.path)
                        .tag(Optional(project.path))
                        .contextMenu {
                            Button("Show in Finder") {
                                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: project.path)])
                            }
                        }
                }
            }
            if model.projectPath != nil && !model.history.isEmpty {
                Section("Requests") {
                    ForEach(model.history) { row in
                        Button {
                            model.openHistory(row)
                        } label: {
                            HistoryRowView(row: row)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .disabled(model.running)
        .help(model.running ? "The sidebar unlocks when the current request ends (or press Stop)." : "")
        .safeAreaInset(edge: .bottom, spacing: 0) {
            HStack(spacing: 2) {
                Button {
                    model.openProject()
                } label: {
                    Label("Open Folder…", systemImage: "folder.badge.plus")
                }
                .disabled(model.running)
                .help("Open a project folder (⌘O)")
                .accessibilityIdentifier("openProjectButton")
                Spacer()
                Button {
                    model.newProject()
                } label: {
                    Image(systemName: "plus")
                }
                .disabled(model.running)
                .help("Create a new project (⇧⌘N)")
                .accessibilityIdentifier("newProjectButton")
                if model.projectPath != nil {
                    Button {
                        model.closeProject()
                    } label: {
                        Image(systemName: "xmark")
                    }
                    .disabled(model.running)
                    .help("Close this project and ask about anything on your Mac (its chat stays saved)")
                }
            }
            .buttonStyle(.borderless)
            .labelStyle(.titleAndIcon)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        }
    }
}

private struct HistoryRowView: View {
    let row: HistoryRow

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(row.request.isEmpty ? "Request" : row.request)
                .lineLimit(2)
                .truncationMode(.tail)
            Text(outcome)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
    }

    private var outcome: String {
        let label = Self.labels[row.outcome] ?? row.outcome.replacingOccurrences(of: "_", with: " ").capitalized
        guard let date = Self.parse(row.updated) else { return label }
        let when = date.formatted(.relative(presentation: .named))
        return label.isEmpty ? when : "\(label) · \(when)"
    }

    private static let labels = [
        "review": "Done", "kept": "Kept", "discarded": "Discarded", "stopped": "Stopped",
        "time_limit": "Ran out of time", "step_limit": "Ran out of steps", "error": "Failed",
        "solved": "Solved", "blocked": "Blocked",
    ]

    private static func parse(_ text: String) -> Date? {
        if let seconds = Double(text) { return Date(timeIntervalSince1970: seconds) }
        return ISO8601DateFormatter().date(from: text)
    }
}

// MARK: - Session (the conversation log)

struct SessionView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Group {
            if let error = model.startupError {
                ContentUnavailableView(
                    "Local Coder could not start",
                    systemImage: "exclamationmark.triangle",
                    description: Text(error)
                )
            } else if model.entries.isEmpty && !model.running {
                StartView()
            } else {
                ConversationLog()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ComposerBar()
        }
        .overlay(alignment: .top) {
            if let banner = model.banner {
                NoticeBar(text: banner) { model.banner = nil }
                    .padding(.top, 10)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.snappy, value: model.banner)
    }
}

private struct NoticeBar: View {
    let text: String
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: "info.circle")
                .foregroundStyle(.secondary)
            Text(text)
                .font(.callout)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Button(action: dismiss) {
                Image(systemName: "xmark")
                    .font(.caption.weight(.semibold))
            }
            .buttonStyle(.borderless)
            .help("Dismiss")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .glassEffect(.regular, in: .rect(cornerRadius: 12))
        .frame(maxWidth: 620)
        .padding(.horizontal, 20)
    }
}

struct ConversationLog: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                let entries = model.entries
                ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                    EntryView(entry: entry, isLast: index == entries.count - 1)
                    if index < entries.count - 1 {
                        Divider()
                            .padding(.vertical, 22)
                    }
                }
                if entries.isEmpty && model.running {
                    EntryView(entry: ConversationEntry(id: "running", prompt: model.currentPrompt, answers: []),
                              isLast: true)
                }
            }
            .padding(.horizontal, 36)
            .padding(.top, 28)
            .padding(.bottom, 24)
            .frame(maxWidth: 820, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .defaultScrollAnchor(.bottom)
    }
}

struct EntryView: View {
    @EnvironmentObject private var model: AppModel
    let entry: ConversationEntry
    let isLast: Bool

    private var live: Bool { isLast && model.running }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let prompt = entry.prompt {
                PromptHeadline(text: prompt)
            }
            if live {
                LiveTrace()
            } else if let prompt = entry.prompt, let record = model.traces[prompt] {
                TraceSummary(record: record)
            }
            ForEach(entry.answers) { answer in
                ProseView(text: answer.text)
                    .contextMenu {
                        Button("Copy Answer") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(answer.text, forType: .string)
                        }
                    }
            }
            if isLast && !model.running && model.hasChanges && model.snapshot?["can_keep"] as? Bool == true {
                ChangesPill()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct PromptHeadline: View {
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 1.5)
                .fill(.tint)
                .frame(width: 3)
            Text(text)
                .font(.system(size: 17, weight: .semibold))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

private struct ChangesPill: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let stats = DiffParser.totals(DiffParser.parse(model.diffText, delta: model.changeDelta))
        Button {
            withAnimation { model.inspectorShown = true }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "doc.on.doc")
                Text("\(model.changedFiles.count) file\(model.changedFiles.count == 1 ? "" : "s") changed")
                Text("+\(stats.added)").foregroundStyle(.passText)
                Text("−\(stats.removed)").foregroundStyle(.failText)
                Text("Review")
                    .fontWeight(.semibold)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
            }
            .font(.callout.monospacedDigit())
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
        }
        .buttonStyle(.plain)
        .glassEffect(.regular.interactive(), in: .capsule)
    }
}

// MARK: - Start

struct StartView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            VStack(spacing: 22) {
                Image(nsImage: NSApp.applicationIconImage)
                    .resizable()
                    .frame(width: 76, height: 76)
                    .accessibilityHidden(true)
                VStack(spacing: 6) {
                    Text(model.projectPath == nil ? "What should we look at?" : model.projectName)
                        .font(.system(size: 26, weight: .semibold))
                    Text(model.projectPath == nil
                         ? "Ask about any project on your Mac, or describe an app to build."
                         : (model.projectSummary.isEmpty ? "Ask about this project, or describe a change." : model.projectSummary))
                        .font(.title3)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: 520)
                }
                ModelCard()
                    .frame(maxWidth: 540)
                if !model.blockingIssues.isEmpty {
                    BlockingCard(issues: model.blockingIssues)
                        .frame(maxWidth: 540)
                }
            }
            .padding(40)
            .frame(maxWidth: .infinity)
            .padding(.top, 40)
        }
    }
}

/// The selected model at a glance, with a way to change it.
private struct ModelCard: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let selected = model.selectedModel
        let needsKey = selected?.provider == "orcarouter" && !model.orcaConnected
        HStack(alignment: .center, spacing: 14) {
            Image(systemName: selected?.placeSymbol ?? "cpu")
                .font(.system(size: 18))
                .foregroundStyle(.tint)
                .frame(width: 34, height: 34)
                .background(Circle().fill(.tint.opacity(0.12)))
            VStack(alignment: .leading, spacing: 3) {
                Text(selected?.name ?? "No model selected")
                    .font(.headline)
                Text(needsKey ? "Connect OrcaRouter to use this model" : cardDetail(selected))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer(minLength: 8)
            if needsKey {
                Button("Connect…") { model.showConnect = true }
                    .buttonStyle(.borderedProminent)
            } else {
                Button("Change…") { model.showBrowser = true }
            }
        }
        .padding(16)
        .background(.background.secondary, in: .rect(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(.separator.opacity(0.6)))
    }

    private func cardDetail(_ selected: CatalogModel?) -> String {
        guard let selected else { return "Pick one to start." }
        switch selected.provider {
        case "orcarouter":
            return selected.free ? "Free through OrcaRouter" : "Through OrcaRouter · \(selected.subtitle) per million tokens"
        case "nvidia": return "Free · runs on NVIDIA's servers"
        default: return selected.subtitle.isEmpty ? "Runs on this Mac" : "Runs on this Mac · \(selected.subtitle)"
        }
    }
}

private struct BlockingCard: View {
    @EnvironmentObject private var model: AppModel
    let issues: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("This Mac needs attention", systemImage: "exclamationmark.triangle.fill")
                .font(.headline)
                .foregroundStyle(.orange)
            ForEach(Array(issues.enumerated()), id: \.offset) { _, item in
                Text(item)
                    .font(.callout)
                    .textSelection(.enabled)
            }
            Button("Check Again") { model.refreshReadiness() }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.orange.opacity(0.08), in: .rect(cornerRadius: 16))
    }
}

// MARK: - Composer

struct ComposerBar: View {
    @EnvironmentObject private var model: AppModel

    private var empty: Bool {
        model.requestText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ZStack(alignment: .topLeading) {
                ComposerField(
                    text: $model.requestText,
                    isEnabled: !model.running,
                    onSubmit: { model.submitRequest() }
                )
                .frame(minHeight: 40, maxHeight: 180)
                .fixedSize(horizontal: false, vertical: true)
                if model.requestText.isEmpty {
                    Text(model.projectPath == nil
                         ? "Ask about any project, or describe an app to build"
                         : "Ask about \(model.projectName), or describe a change")
                        .font(.system(size: 14))
                        .foregroundStyle(.tertiary)
                        .padding(.leading, 11)
                        .padding(.top, 8)
                        .allowsHitTesting(false)
                }
            }
            HStack(spacing: 10) {
                ModelMenu()
                if model.selectedModel?.reasoningSwitch == true {
                    ReasoningMenu()
                }
                Spacer()
                if model.running {
                    Button {
                        model.stop()
                    } label: {
                        Image(systemName: "stop.fill")
                            .font(.system(size: 11, weight: .bold))
                            .frame(width: 18, height: 18)
                    }
                    .buttonStyle(.bordered)
                    .buttonBorderShape(.circle)
                    .help("Stop")
                    .accessibilityIdentifier("stopButton")
                } else {
                    Button {
                        model.submitRequest()
                    } label: {
                        Image(systemName: "arrow.up")
                            .font(.system(size: 13, weight: .bold))
                            .frame(width: 18, height: 18)
                    }
                    .buttonStyle(.borderedProminent)
                    .buttonBorderShape(.circle)
                    .disabled(empty)
                    .keyboardShortcut(.return, modifiers: [.command])
                    .help("Send (Return)")
                    .accessibilityIdentifier("startButton")
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 10)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
        // On a light window the glass alone has no edge: a hairline and a soft lift keep it readable as a surface.
        .overlay { RoundedRectangle(cornerRadius: 22).strokeBorder(.separator) }
        .shadow(color: .black.opacity(0.08), radius: 8, y: 2)
        .frame(maxWidth: 760)
        .padding(.horizontal, 24)
        .padding(.bottom, 18)
        .padding(.top, 6)
        .frame(maxWidth: .infinity)
    }
}
