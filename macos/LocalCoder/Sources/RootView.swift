import AppKit
import SwiftUI
import WebKit

private enum LCColor {
    static let brand = Color(red: 0.18, green: 0.36, blue: 0.28)
    static let paper = Color(red: 0.96, green: 0.96, blue: 0.94)
    static let sidebar = Color(red: 0.93, green: 0.93, blue: 0.90)
    static let bubbleUser = Color(red: 0.18, green: 0.36, blue: 0.28).opacity(0.12)
    static let bubbleAssistant = Color.white
    static let ink = Color(red: 0.12, green: 0.14, blue: 0.13)
}

struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            sidebar
                .frame(width: 248)
                .padding(.horizontal, 14)
                .padding(.vertical, 18)
                .background(LCColor.sidebar)
            detail
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .frame(minWidth: 960, minHeight: 680)
        .background(LCColor.paper)
        .preferredColorScheme(.light)
        .sheet(isPresented: $model.showSettings) {
            SettingsView()
                .environmentObject(model)
                .frame(width: 480, height: 420)
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Local Coder")
                .font(.title2.weight(.semibold))
                .foregroundStyle(LCColor.brand)

            Button {
                model.newChat()
            } label: {
                Label("New chat", systemImage: "square.and.pencil")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.borderedProminent)
            .tint(LCColor.brand)
            .accessibilityIdentifier("newChatButton")

            VStack(alignment: .leading, spacing: 6) {
                Button {
                    model.openProject()
                } label: {
                    Label("Open Project…", systemImage: "folder")
                }
                .accessibilityIdentifier("openProjectButton")

                Button {
                    model.newProject()
                } label: {
                    Label("New Project…", systemImage: "plus.square")
                }
                .accessibilityIdentifier("newProjectButton")

                Button("Advanced settings") {
                    model.showSettings = true
                }
                .font(.caption)
            }
            .buttonStyle(.plain)
            .foregroundStyle(LCColor.ink.opacity(0.8))

            if !model.recent.isEmpty {
                sidebarSection("Recent projects") {
                    ForEach(model.recent) { project in
                        Button(project.name) { model.selectProject(project.path) }
                            .buttonStyle(.plain)
                            .foregroundStyle(model.projectPath == project.path ? LCColor.brand : LCColor.ink)
                            .fontWeight(model.projectPath == project.path ? .semibold : .regular)
                            .help(project.path)
                    }
                }
            }

            if !model.history.isEmpty {
                sidebarSection("Recent requests") {
                    ForEach(model.history) { row in
                        Button {
                            model.openHistory(row)
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(row.request.isEmpty ? row.episodeID : row.request)
                                    .lineLimit(2)
                                    .multilineTextAlignment(.leading)
                                Text(row.outcome)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func sidebarSection<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.top, 8)
            content()
        }
    }

    private var detail: some View {
        Group {
            if let error = model.startupError {
                ContentUnavailableView(
                    "Local Coder could not start",
                    systemImage: "exclamationmark.triangle",
                    description: Text(error)
                )
                .padding(28)
            } else if model.projectPath == nil {
                welcomeShell
            } else {
                projectShell
            }
        }
    }

    private var welcomeShell: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Chat")
                        .font(.system(size: 34, weight: .semibold, design: .rounded))
                        .foregroundStyle(LCColor.ink)
                    Text("Ask a question or describe what to build. A project folder is created when you send.")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let name = model.selectedModelName {
                        modelChip(name)
                    }
                    if let banner = model.banner {
                        bannerCard(banner)
                    }
                    if model.running {
                        livingProgress
                    }
                    prerequisiteBox
                }
                .padding(28)
                .frame(maxWidth: 720, alignment: .leading)
            }
            composerBar
        }
    }

    private var projectShell: some View {
        VStack(spacing: 0) {
            slimHeader
            if let banner = model.banner {
                bannerCard(banner)
                    .padding(.horizontal, 24)
                    .padding(.top, 8)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        if model.chatTurns.isEmpty && model.explainSummary == nil && model.snapshot == nil && !model.running {
                            emptyChatHint
                        }
                        chatTranscript
                        if let summary = model.explainSummary, model.chatTurns.isEmpty {
                            explainBox(summary)
                        }
                        if model.running {
                            livingProgress
                                .id("progress")
                        }
                        if shouldShowReview {
                            reviewBox
                                .id("review")
                        }
                        Color.clear.frame(height: 8).id("chat-bottom")
                    }
                    .padding(.horizontal, 24)
                    .padding(.vertical, 16)
                    .frame(maxWidth: 820, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: model.chatTurns.count) { _, _ in
                    withAnimation(.easeOut(duration: 0.25)) {
                        proxy.scrollTo("chat-bottom", anchor: .bottom)
                    }
                }
                .onChange(of: model.running) { _, running in
                    if running {
                        withAnimation(.easeOut(duration: 0.25)) {
                            proxy.scrollTo("progress", anchor: .bottom)
                        }
                    }
                }
            }
            composerBar
        }
    }

    private var shouldShowReview: Bool {
        guard model.snapshot != nil else { return false }
        if !model.changedFiles.isEmpty { return true }
        if !model.diffText.isEmpty { return true }
        if model.previewURL != nil { return true }
        // Empty coding result: show only when commentary exists and chat didn't already absorb it.
        if let commentary = model.modelCommentary, !commentary.isEmpty, model.chatTurns.isEmpty {
            return true
        }
        return false
    }

    private var slimHeader: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.projectName)
                    .font(.headline)
                    .foregroundStyle(LCColor.ink)
                Text(model.projectPath ?? "")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            if let name = model.selectedModelName {
                modelChip(name)
            }
            if !model.supported {
                Text("Limited")
                    .font(.caption2.weight(.semibold))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(Color.orange.opacity(0.15), in: Capsule())
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .background(LCColor.paper.opacity(0.95))
        .overlay(alignment: .bottom) {
            Divider().opacity(0.5)
        }
    }

    private func modelChip(_ name: String) -> some View {
        Text(name)
            .font(.caption.weight(.medium))
            .foregroundStyle(LCColor.brand)
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(LCColor.brand.opacity(0.1), in: Capsule())
            .help(model.selectedModelNote ?? name)
    }

    private func bannerCard(_ text: String) -> some View {
        Text(text)
            .font(.callout)
            .textSelection(.enabled)
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.yellow.opacity(0.18), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var emptyChatHint: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Start here")
                .font(.title3.weight(.semibold))
                .foregroundStyle(LCColor.ink)
            Text("Ask where files live, what the project is, or describe a change to build.")
                .foregroundStyle(.secondary)
            Text(model.projectSummary)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            prerequisiteBox
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.7), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private var chatTranscript: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(model.chatTurns) { turn in
                chatBubble(turn)
                    .id(turn.id)
                    .transition(.asymmetric(
                        insertion: .opacity.combined(with: .move(edge: .bottom)),
                        removal: .opacity
                    ))
            }
        }
        .animation(.spring(response: 0.35, dampingFraction: 0.86), value: model.chatTurns.count)
    }

    private func chatBubble(_ turn: ChatTurn) -> some View {
        let isUser = turn.role == "user"
        return HStack {
            if isUser { Spacer(minLength: 48) }
            VStack(alignment: .leading, spacing: 4) {
                Text(isUser ? "You" : "Local Coder")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(turn.text)
                    .textSelection(.enabled)
                    .foregroundStyle(LCColor.ink)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(12)
            .background(
                isUser ? LCColor.bubbleUser : LCColor.bubbleAssistant,
                in: RoundedRectangle(cornerRadius: 14, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .stroke(isUser ? Color.clear : Color.black.opacity(0.06), lineWidth: 1)
            )
            .shadow(color: .black.opacity(isUser ? 0 : 0.04), radius: 6, y: 2)
            .contextMenu {
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(turn.text, forType: .string)
                }
            }
            if !isUser { Spacer(minLength: 48) }
        }
    }

    private var composerBar: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.running && model.projectPath == nil {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                    Text(model.progress)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer()
                    Text(model.elapsedLabel)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            HStack(alignment: .bottom, spacing: 10) {
                ComposerField(
                    text: $model.requestText,
                    isEnabled: !model.running,
                    onSubmit: { model.submitRequest() }
                )
                .frame(minHeight: 52, maxHeight: 140)
                .background(Color.white, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .stroke(Color.black.opacity(0.08), lineWidth: 1)
                )
                .overlay(alignment: .topLeading) {
                    if model.requestText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Text(model.projectPath == nil
                             ? "Message Local Coder… e.g. Build a task board"
                             : "Ask a question or describe a change…")
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 14)
                            .allowsHitTesting(false)
                    }
                }

                Button(model.running ? "…" : "Send") { model.submitRequest() }
                    .buttonStyle(.borderedProminent)
                    .tint(LCColor.brand)
                    .disabled(model.running || model.requestText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .keyboardShortcut(.return, modifiers: [.command])
                    .accessibilityIdentifier("startButton")
            }
            Text("Enter to send · Shift+Enter for a new line")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
        .background(.ultraThinMaterial)
        .overlay(alignment: .top) {
            Divider().opacity(0.45)
        }
    }

    private var livingProgress: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 10) {
                ProgressView()
                    .controlSize(.small)
                Text("Working")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(LCColor.brand)
                Spacer()
                Text(model.elapsedLabel)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Button("Stop") { model.stop() }
                    .font(.caption)
                    .accessibilityIdentifier("stopButton")
            }
            // Headline only when it adds info beyond the latest Actions row.
            if model.progressLog.last != model.progress {
                Text(model.progress)
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(LCColor.ink)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !model.progressLog.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Actions")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ScrollViewReader { proxy in
                        ScrollView {
                            VStack(alignment: .leading, spacing: 3) {
                                ForEach(Array(model.progressLog.enumerated()), id: \.offset) { index, step in
                                    HStack(alignment: .top, spacing: 6) {
                                        Text("•")
                                            .foregroundStyle(index == model.progressLog.count - 1
                                                             ? LCColor.brand : .secondary)
                                        Text(step)
                                            .font(.caption)
                                            .foregroundStyle(index == model.progressLog.count - 1
                                                             ? .primary : .secondary)
                                            .textSelection(.enabled)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                    .id("action-\(index)")
                                }
                            }
                        }
                        .frame(maxHeight: 180)
                        .onChange(of: model.progressLog.count) { _, _ in
                            if let last = model.progressLog.indices.last {
                                withAnimation(.easeOut(duration: 0.2)) {
                                    proxy.scrollTo("action-\(last)", anchor: .bottom)
                                }
                            }
                        }
                    }
                }
            } else {
                Text(model.progress)
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(LCColor.ink)
                    .textSelection(.enabled)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LCColor.brand.opacity(0.08), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .accessibilityIdentifier("livingProgress")
    }

    private func explainBox(_ summary: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.explainHits.isEmpty ? "Local Coder" : "Read-only look at the selected code")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(summary)
                .textSelection(.enabled)
            ForEach(Array(model.explainHits.enumerated()), id: \.offset) { _, hit in
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(hit["path"] as? String ?? "")")
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                    Text(hit["excerpt"] as? String ?? "")
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                }
                .padding(8)
                .background(Color.gray.opacity(0.08), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            }
        }
        .padding(12)
        .background(LCColor.bubbleAssistant, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Color.black.opacity(0.06), lineWidth: 1)
        )
        .contextMenu {
            Button("Copy") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(summary, forType: .string)
            }
        }
    }

    private var reviewBox: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(model.complete ? "Ready to review" : "Result")
                .font(.headline)
                .foregroundStyle(LCColor.ink)
            Text(model.reviewExplanation)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            if !model.changedFiles.isEmpty {
                Text("Files touched: \(model.changedFiles.joined(separator: ", "))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            if model.hasRealChecks {
                checks
            }
            if !model.diffText.isEmpty {
                DisclosureGroup("Show file difference") {
                    ScrollView {
                        DiffView(text: model.diffText)
                    }
                    .frame(minHeight: 120, maxHeight: 280)
                    .background(Color.white, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(.separator))
                }
                .font(.subheadline)
                .foregroundStyle(.secondary)
            }
            if let url = model.previewURL, let parsed = URL(string: url) {
                Text("Running preview")
                    .font(.headline)
                PreviewPane(url: parsed)
                    .frame(minHeight: 280)
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .accessibilityIdentifier("previewView")
                Button("Open in Browser") {
                    NSWorkspace.shared.open(parsed)
                }
                .accessibilityIdentifier("openInBrowserButton")
            }
            HStack {
                Button("Keep changes") { model.keep() }
                    .buttonStyle(.borderedProminent)
                    .tint(LCColor.brand)
                    .disabled(!model.canKeep)
                    .keyboardShortcut("k", modifiers: [.command])
                    .accessibilityIdentifier("keepButton")
                Button("Discard changes") { model.discard() }
                    .disabled(!model.canDiscard)
                    .accessibilityIdentifier("discardButton")
                if !model.canKeep && model.snapshot != nil {
                    Text("This result can be inspected, but it cannot be kept as a verified change.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(LCColor.brand.opacity(0.18), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.05), radius: 10, y: 3)
    }

    private var checks: some View {
        let facts = model.snapshot?["facts"] as? [String: Any] ?? [:]
        return VStack(alignment: .leading, spacing: 4) {
            Text("What was actually checked")
                .font(.subheadline.weight(.semibold))
            checkLine("Starting tests", facts["baseline"] as? [String: Any])
            checkLine("Latest check", facts["current"] as? [String: Any])
            checkLine("Final check", facts["final"] as? [String: Any])
            Text("These are recorded check results, not a claim that the request is done.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func checkLine(_ title: String, _ row: [String: Any]?) -> some View {
        let status = row?["status"] as? String ?? "not run"
        let failed = (row?["failed"] as? [String])?.joined(separator: ", ") ?? ""
        return Text("\(title): \(status)\(failed.isEmpty ? "" : " — \(failed)")")
            .font(.callout)
            .textSelection(.enabled)
    }

    private var prerequisiteBox: some View {
        let rows = model.readiness?["rows"] as? [[String: Any]] ?? []
        let blocking = model.readiness?["blocking"] as? [String] ?? []
        return VStack(alignment: .leading, spacing: 6) {
            if !blocking.isEmpty {
                Text("Something on this Mac still needs attention")
                    .font(.headline)
                ForEach(Array(blocking.enumerated()), id: \.offset) { _, item in
                    Text(item)
                        .font(.callout)
                        .textSelection(.enabled)
                }
                HStack {
                    Button("Open Docker Desktop") { model.openDocker() }
                    Button("Build test worker") { model.buildWorker() }
                    Button("Check again") { model.refreshReadiness() }
                }
            } else if !rows.isEmpty && model.projectPath == nil {
                Text("This Mac can use the selected model. Isolated tests are optional and need Docker only for the older Python engine.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
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

struct DiffView: View {
    let text: String

    var body: some View {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        LazyVStack(alignment: .leading, spacing: 0) {
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                Text(line.isEmpty ? " " : line)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(color(for: line))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(background(for: line))
                    .textSelection(.enabled)
            }
        }
        .padding(8)
    }

    private func color(for line: String) -> Color {
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return Color.green }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return Color.red }
        return .primary
    }

    private func background(for line: String) -> Color {
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return Color.green.opacity(0.12) }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return Color.red.opacity(0.12) }
        return .clear
    }
}
