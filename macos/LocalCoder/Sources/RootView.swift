import AppKit
import SwiftUI
import WebKit

struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            sidebar
                .frame(width: 250)
                .padding(16)
                .padding(.top, 20)
                .background(Color(red: 0.93, green: 0.93, blue: 0.90))
            detail
                .padding(24)
                .padding(.top, 20)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .frame(minWidth: 920, minHeight: 640)
        .background(Color(red: 0.96, green: 0.96, blue: 0.94))
        .preferredColorScheme(.light)
        .sheet(isPresented: $model.showSettings) {
            SettingsView()
                .environmentObject(model)
                .frame(width: 440, height: 260)
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Local Coder")
                .font(.title2.weight(.semibold))
                .foregroundStyle(Color(red: 0.18, green: 0.36, blue: 0.28))
            Button {
                model.openProject()
            } label: {
                Label("Open Project…", systemImage: "folder")
            }
            .buttonStyle(.borderedProminent)
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

            if !model.recent.isEmpty {
                Section("Recent projects") {
                    ForEach(model.recent) { project in
                        Button(project.name) { model.selectProject(project.path) }
                            .help(project.path)
                    }
                }
            }

            if !model.history.isEmpty {
                Section("Recent requests") {
                    ForEach(model.history) { row in
                        Button {
                            model.openHistory(row)
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(row.request.isEmpty ? row.episodeID : row.request)
                                    .lineLimit(2)
                                Text(row.outcome)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var detail: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let error = model.startupError {
                ContentUnavailableView(
                    "Local Coder could not start",
                    systemImage: "exclamationmark.triangle",
                    description: Text(error)
                )
            } else if model.projectPath == nil {
                welcome
                if !model.ready {
                    ProgressView("Connecting to the local engine…")
                        .padding(.top, 8)
                }
            } else {
                projectWorkspace
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("A local coding tool for this Mac")
                .font(.largeTitle.weight(.semibold))
                .foregroundStyle(.black)
            Text("Open a project or start a new one, describe what you want, watch progress, try the running result, then keep or discard it. You can ask for the next change without using Terminal.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("New Project…") { model.newProject() }
                    .keyboardShortcut(.defaultAction)
                    .accessibilityIdentifier("welcomeNewProjectButton")
                Button("Open Project…") { model.openProject() }
                    .accessibilityIdentifier("welcomeOpenProjectButton")
            }
            prerequisiteBox
        }
    }

    private var projectWorkspace: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                statusCard
                if let banner = model.banner {
                    Text(banner)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.yellow.opacity(0.18), in: RoundedRectangle(cornerRadius: 8))
                }
                requestBox
                if model.running {
                    runningBox
                }
                if let summary = model.explainSummary {
                    explainBox(summary)
                }
                if model.snapshot != nil {
                    reviewBox
                }
            }
        }
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.projectName)
                .font(.title2.weight(.semibold))
            Text(model.projectPath ?? "")
                .font(.caption)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            Text(model.projectSummary)
                .fixedSize(horizontal: false, vertical: true)
            if !model.supported {
                Text("This project cannot be edited until the issue above is resolved.")
                    .foregroundStyle(.orange)
            }
            prerequisiteBox
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(.separator))
    }

    private var requestBox: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.followUpReady ? "What should change next?" : "What would you like to build or change?")
                .font(.headline)
            TextEditor(text: $model.requestText)
                .font(.body)
                .frame(minHeight: 92)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(.separator))
                .accessibilityIdentifier("requestEditor")
            Text("Examples: “Build a task board.”  “Add due dates.”  “Fix this error.”  “Explain this part of the project.”")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Button(model.running ? "Working…" : "Start") { model.submitRequest() }
                    .disabled(model.running || model.requestText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .keyboardShortcut(.return, modifiers: [.command])
                    .accessibilityIdentifier("startButton")
                if model.running {
                    Button("Stop") { model.stop() }
                        .accessibilityIdentifier("stopButton")
                }
            }
        }
    }

    private var runningBox: some View {
        VStack(alignment: .leading, spacing: 8) {
            ProgressView()
            Text(model.progress)
                .font(.title3)
            Text("This is real work on this Mac. The original project is not being overwritten.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
    }

    private func explainBox(_ summary: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Read-only look at the selected code")
                .font(.headline)
            Text(summary)
            ForEach(Array(model.explainHits.enumerated()), id: \.offset) { _, hit in
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(hit["path"] as? String ?? "")")
                        .font(.caption.monospaced())
                    Text(hit["excerpt"] as? String ?? "")
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                }
                .padding(8)
                .background(Color.gray.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
            }
        }
    }

    private var reviewBox: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(model.complete ? "Ready to review" : "Result")
                .font(.headline)
            Text(model.reviewSummary)
                .fixedSize(horizontal: false, vertical: true)
            if let commentary = model.modelCommentary {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Model commentary (not a fact)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(commentary)
                }
                .padding(8)
                .background(Color.gray.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
            }
            if !model.changedFiles.isEmpty {
                Text("Changed files: \(model.changedFiles.joined(separator: ", "))")
                    .font(.callout)
            }
            checks
            if !model.diffText.isEmpty {
                Text("Proposed difference")
                    .font(.headline)
                ScrollView {
                    DiffView(text: model.diffText)
                }
                .frame(minHeight: 180, maxHeight: 360)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(.separator))
            }
            if let url = model.previewURL, let parsed = URL(string: url) {
                Text("Running preview")
                    .font(.headline)
                PreviewPane(url: parsed)
                    .frame(minHeight: 280)
                    .accessibilityIdentifier("previewView")
                Button("Open in Browser") {
                    NSWorkspace.shared.open(parsed)
                }
                .accessibilityIdentifier("openInBrowserButton")
            }
            HStack {
                Button("Keep changes") { model.keep() }
                    .disabled(!model.canKeep)
                    .keyboardShortcut("k", modifiers: [.command])
                    .accessibilityIdentifier("keepButton")
                Button("Discard changes") { model.discard() }
                    .disabled(!model.canDiscard)
                    .accessibilityIdentifier("discardButton")
                if !model.canKeep && model.snapshot != nil {
                    Text("A blocked or partial result can be inspected, but it cannot be kept as a verified change.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var checks: some View {
        let facts = model.snapshot?["facts"] as? [String: Any] ?? [:]
        return VStack(alignment: .leading, spacing: 4) {
            Text("What was actually checked")
                .font(.headline)
            checkLine("Starting tests", facts["baseline"] as? [String: Any])
            checkLine("Latest check", facts["current"] as? [String: Any])
            checkLine("Final check", facts["final"] as? [String: Any])
            let complete = facts["complete"] as? Bool ?? false
            Text(complete
                 ? "The complete requested change was verified."
                 : "Only the recorded progress above was verified — not a claim that the whole request is done.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func checkLine(_ title: String, _ row: [String: Any]?) -> some View {
        let status = row?["status"] as? String ?? "not run"
        let failed = (row?["failed"] as? [String])?.joined(separator: ", ") ?? ""
        return Text("\(title): \(status)\(failed.isEmpty ? "" : " — \(failed)")")
            .font(.callout)
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
                }
                HStack {
                    Button("Open Docker Desktop") { model.openDocker() }
                    Button("Build test worker") { model.buildWorker() }
                    Button("Check again") { model.refreshReadiness() }
                }
            } else if !rows.isEmpty && model.projectPath == nil {
                Text("This Mac has the local model and test worker needed for ordinary use.")
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
