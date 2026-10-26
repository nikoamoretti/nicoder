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
            .tint(Console.green)
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
                .navigationTitle(model.projectPath == nil ? "Nicoder" : model.projectName)
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
            .help("Start a new chat (⌘N). Work that runs keeps running.")
            .accessibilityIdentifier("newChatButton")
        }
        ToolbarItem(placement: .primaryAction) {
            Button {
                withAnimation { model.inspectorShown.toggle() }
            } label: {
                Label("Changes", systemImage: "sidebar.trailing")
            }
            .help(model.hasChanges ? (model.canKeep || model.canDiscard ? "Show the proposed changes" : "Show the changes")
                                    : "Changes and preview")
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
    /// The sidebar row for a chat with no project open (the agent answers about anything on the Mac).
    static let chatTag = "chat:"

    var body: some View {
        let chats = model.chatRows
        List {
            Button { model.newChat() } label: {
                ConsoleRow(text: "new chat",
                           selected: model.projectPath == nil && !chats.contains { $0.id == model.chatID })
            }
            .buttonStyle(.plain)
            .help("Ask about anything on your Mac, without a project")
            .accessibilityIdentifier("sidebarNewChat")
            Section {
                if model.recent.isEmpty {
                    Text("projects you open appear here")
                        .font(Console.mono(11.5))
                        .foregroundStyle(Console.faint)
                }
                ForEach(model.recent) { project in
                    Button { model.selectProject(project.path) } label: {
                        ConsoleRow(text: project.name, selected: project.path == model.projectPath, date: project.active,
                                   state: model.rowState(.project(project.path)))
                    }
                    .buttonStyle(.plain)
                    .help(RootView.tildePath(project.path) ?? project.path)
                    .contextMenu {
                        Button("Show in Finder") {
                            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: project.path)])
                        }
                    }
                }
            } header: {
                SidebarHeading(text: "projects")
            }
            if model.projectPath != nil && !model.history.isEmpty {
                Section {
                    ForEach(model.history) { row in
                        Button {
                            model.openHistory(row)
                        } label: {
                            HistoryRowView(row: row)
                        }
                        .buttonStyle(.plain)
                    }
                } header: {
                    SidebarHeading(text: "requests")
                }
            }
            if !chats.isEmpty {
                Section {
                    ForEach(chats) { chat in
                        Button { model.selectChat(chat.id) } label: {
                            ConsoleRow(text: chat.title, selected: model.projectPath == nil && chat.id == model.chatID,
                                       date: chat.updated, state: model.rowState(.chat(chat.id)), truncation: .tail)
                        }
                        .buttonStyle(.plain)
                        .help(chat.title)
                    }
                } header: {
                    SidebarHeading(text: "chats")
                }
            }
        }
        .listStyle(.sidebar)
        .scrollContentBackground(.hidden)
        .background(Console.sidebar)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            HStack(spacing: 2) {
                Button {
                    model.openProject()
                } label: {
                    Text("open folder…")
                        .lineLimit(1)
                        .fixedSize()
                }
                .help("Open a project folder (⌘O)")
                .accessibilityIdentifier("openProjectButton")
                Spacer()
                Button {
                    model.newProject()
                } label: {
                    Text("+ new")
                        .lineLimit(1)
                        .fixedSize()
                }
                .help("Create a new project (⇧⌘N)")
                .accessibilityIdentifier("newProjectButton")
            }
            .buttonStyle(.borderless)
            .font(Console.mono(11.5))
            .foregroundStyle(Console.mute)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Console.sidebar)
        }
    }
}

private struct SidebarHeading: View {
    let text: String

    var body: some View {
        Text(text)
            .font(Console.mono(11))
            .foregroundStyle(Console.faint)
            .textCase(nil)
    }
}

private struct HistoryRowView: View {
    let row: HistoryRow

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(row.request.isEmpty ? "Request" : row.request)
                .font(Console.mono(11.5))
                .foregroundStyle(Console.mute)
                .lineLimit(2)
                .truncationMode(.tail)
            Text(outcome.lowercased())
                .font(Console.mono(10.5))
                .foregroundStyle(Console.faint)
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
                    "Nicoder could not start",
                    systemImage: "exclamationmark.triangle",
                    description: Text(error)
                )
            } else if model.entries.isEmpty {
                StartView()
            } else {
                ConversationLog()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Console.background)
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
        .background(Console.panel, in: .rect(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Console.line))
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

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let prompt = entry.prompt {
                PromptHeadline(text: prompt)
            }
            if entry.live {
                LiveTrace()
            } else if let id = entry.waiting {
                WaitingLine(id: id)
            } else if let prompt = entry.prompt, let record = model.traces[prompt] {
                TraceSummary(record: record)
            }
            ForEach(entry.answers) { answer in
                ProseView(text: answer.text)  // its own menu has Copy and Copy Answer
            }
            if isLast && !model.busyHere && model.hasChanges && model.snapshot?["can_keep"] as? Bool == true {
                ChangesPill()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A message sent while the same kind of work runs elsewhere: it starts by itself when that ends.
private struct WaitingLine: View {
    @EnvironmentObject private var model: AppModel
    let id: UUID

    var body: some View {
        HStack(spacing: 8) {
            Text("◌")
                .foregroundStyle(Console.mute)
            Text("waits for \(model.waitsFor(id)) to finish, then starts")
                .foregroundStyle(Console.mute)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer()
            Button("cancel") { model.dropWaiting(id) }
                .controlSize(.small)
        }
        .font(Console.mono(12))
        .padding(.vertical, 4)
    }
}

private struct PromptHeadline: View {
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("›")
                .font(Console.mono(15, .bold))
                .foregroundStyle(Console.green)
            Text(text)
                .font(Console.mono(14, .semibold))
                .foregroundStyle(Console.ink)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .contextMenu {
            Button("Copy") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            }
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
            HStack(spacing: 10) {
                Text("\(model.changedFiles.count) file\(model.changedFiles.count == 1 ? "" : "s") changed")
                    .foregroundStyle(Console.ink)
                Text("+\(stats.added)").foregroundStyle(.passText)
                Text("−\(stats.removed)").foregroundStyle(.failText)
                Text("review →")
                    .fontWeight(.semibold)
                    .foregroundStyle(Console.green)
            }
            .font(Console.mono(12.5))
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Console.panel, in: .rect(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Console.line))
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Start

struct StartView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(spacing: 12) {
                    Image(nsImage: NSApp.applicationIconImage)
                        .resizable()
                        .frame(width: 44, height: 44)
                        .accessibilityHidden(true)
                    Text(model.projectPath == nil ? "nicoder" : model.projectName)
                        .font(Console.mono(22, .semibold))
                        .foregroundStyle(Console.ink)
                }
                Text(model.projectPath == nil
                     ? "Ask about any project on your Mac, or describe something to build."
                     : (model.projectSummary.isEmpty ? "Ask about this project, or describe a change." : model.projectSummary))
                    .font(.system(size: 15))
                    .foregroundStyle(Console.mute)
                    .fixedSize(horizontal: false, vertical: true)
                ModelLine()
                if !model.blockingIssues.isEmpty {
                    BlockingNote(issues: model.blockingIssues)
                }
            }
            .frame(maxWidth: 560, alignment: .leading)
            .padding(40)
            .padding(.top, 60)
            .frame(maxWidth: .infinity)
        }
    }
}

/// The selected model in one line, with a way to change it.
private struct ModelLine: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let selected = model.selectedModel
        let needsKey = selected?.provider == "orcarouter" && !model.orcaConnected
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("model")
                .foregroundStyle(Console.faint)
            Text(selected?.name ?? "none selected")
                .foregroundStyle(Console.ink)
            Text(needsKey ? "connect OrcaRouter first" : detail(selected))
                .foregroundStyle(Console.mute)
                .lineLimit(1)
            Spacer(minLength: 8)
            if needsKey {
                Button("connect…") { model.showConnect = true }
                    .buttonStyle(.borderedProminent)
            } else {
                Button("change…") { model.showBrowser = true }
                    .buttonStyle(.borderless)
                    .foregroundStyle(Console.green)
            }
        }
        .font(Console.mono(12.5))
        .padding(.top, 6)
    }

    private func detail(_ selected: CatalogModel?) -> String {
        guard let selected else { return "pick one to start" }
        switch selected.provider {
        case "orcarouter":
            return selected.free ? "free · orcarouter" : "orcarouter · \(selected.subtitle) per M tokens"
        case "nvidia": return "free · nvidia"
        default: return selected.subtitle.isEmpty ? "on this mac" : "on this mac · \(selected.subtitle)"
        }
    }
}

private struct BlockingNote: View {
    @EnvironmentObject private var model: AppModel
    let issues: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("! this mac needs attention")
                .font(Console.mono(12.5, .semibold))
                .foregroundStyle(.orange)
            ForEach(Array(issues.enumerated()), id: \.offset) { _, item in
                Text(item)
                    .font(.system(size: 13))
                    .foregroundStyle(Console.ink)
                    .textSelection(.enabled)
            }
            Button("check again") { model.refreshReadiness() }
                .font(Console.mono(12))
        }
        .padding(.top, 6)
    }
}

// MARK: - Composer

struct ComposerBar: View {
    @EnvironmentObject private var model: AppModel

    private var empty: Bool {
        model.requestText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 8) {
                Text("›")
                    .font(Console.mono(15, .bold))
                    .foregroundStyle(Console.green)
                    .padding(.top, 6)
                ZStack(alignment: .topLeading) {
                    ComposerField(
                        text: $model.requestText,
                        isEnabled: !model.busyHere,
                        onSubmit: { model.submitRequest() }
                    )
                    .frame(minHeight: 34, maxHeight: 180)
                    .fixedSize(horizontal: false, vertical: true)
                    if model.requestText.isEmpty {
                        Text(model.projectPath == nil
                             ? "describe something to build, or ask anything"
                             : "describe a change to \(model.projectName), or ask anything")
                            .font(Console.mono(13))
                            .foregroundStyle(Console.faint)
                            .padding(.leading, 6)
                            .padding(.top, 7)
                            .allowsHitTesting(false)
                    }
                }
            }
            HStack(spacing: 12) {
                ModelMenu()
                if model.selectedModel?.reasoningSwitch == true {
                    ReasoningMenu()
                }
                Spacer()
                Text(model.busyHere ? "working…" : "⏎ send · ⇧⏎ new line")
                    .font(Console.mono(11))
                    .foregroundStyle(Console.faint)
                if model.busyHere {
                    Button {
                        model.stop()
                    } label: {
                        Image(systemName: "stop.fill")
                            .font(.system(size: 10, weight: .bold))
                            .frame(width: 16, height: 16)
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
                            .font(.system(size: 12, weight: .bold))
                            .frame(width: 16, height: 16)
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
        .padding(.vertical, 10)
        .background(Console.panel, in: .rect(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Console.line))
        .frame(maxWidth: 820)
        .padding(.horizontal, 24)
        .padding(.bottom, 16)
        .padding(.top, 6)
        .frame(maxWidth: .infinity)
        .background(Console.background)
    }
}
