import AppKit
import SwiftUI

struct RecentProject: Identifiable, Hashable {
    let path: String
    let name: String
    var id: String { path }
}

struct HistoryRow: Identifiable, Hashable {
    let episodeID: String
    let request: String
    let outcome: String
    let updated: String
    var id: String { episodeID }
}

struct CatalogModel: Identifiable, Hashable {
    let id: String
    let name: String
    let available: Bool
    let note: String
}

struct ChatTurn: Identifiable, Hashable {
    let id: String
    let role: String
    let text: String
}

@MainActor
final class AppModel: ObservableObject {
    @Published var ready = false
    @Published var startupError: String?
    @Published var recent: [RecentProject] = []
    @Published var inspection: [String: Any]?
    @Published var history: [HistoryRow] = []
    @Published var requestText = ""
    @Published var progress = "Ready."
    @Published var running = false
    @Published var snapshot: [String: Any]?
    @Published var readiness: [String: Any]?
    @Published var profile = "d2"
    @Published var banner: String?
    @Published var explainSummary: String?
    @Published var explainHits: [[String: Any]] = []
    @Published var chatTurns: [ChatTurn] = []
    @Published var showSettings = false
    @Published var previewURL: String?
    @Published var followUpReady = false
    @Published var engineName = "general"
    @Published var models: [CatalogModel] = []
    @Published var selectedModelId = ""
    @Published var progressLog: [String] = []
    @Published var elapsedSeconds = 0
    @Published var lastUpdate: Date?
    @Published var lastPulse: Date?

    var updateFreshness: String {
        _ = elapsedSeconds
        if lastPulse == nil && lastUpdate == nil {
            return "Started. Waiting for the first real step."
        }
        let pulseAge = lastPulse.map { Int(Date().timeIntervalSince($0)) } ?? 99
        let stepAge = lastUpdate.map { Int(Date().timeIntervalSince($0)) } ?? 99
        if stepAge <= 2 {
            return "This just updated. The tool is working."
        }
        if pulseAge <= 8 && stepAge < 20 {
            return "The helper is alive. Last new step \(stepAge) seconds ago."
        }
        if pulseAge <= 8 {
            return "The helper is alive. Last new step \(stepAge) seconds ago. Quiet usually means the model is still reading."
        }
        return "No check-in for \(pulseAge) seconds. If this stays here, press Stop."
    }

    private let engine = EngineClient()
    private var activeEpisode: String?
    private var quitting = false
    private var applyingCatalog = false
    private var clockTask: Task<Void, Never>?

    var elapsedLabel: String {
        let minutes = elapsedSeconds / 60
        let seconds = elapsedSeconds % 60
        if minutes == 0 {
            return "\(seconds) seconds so far"
        }
        return "\(minutes) min \(seconds) sec so far"
    }

    var projectPath: String? {
        inspection?["path"] as? String
    }

    var projectName: String {
        inspection?["name"] as? String ?? "Local Coder"
    }

    var supported: Bool {
        inspection?["supported"] as? Bool ?? false
    }

    var projectSummary: String {
        inspection?["summary"] as? String ?? "Open a project folder to begin."
    }

    var canKeep: Bool {
        snapshot?["can_keep"] as? Bool ?? false
    }

    var canDiscard: Bool {
        snapshot?["can_discard"] as? Bool ?? false
    }

    var diffText: String {
        snapshot?["diff"] as? String ?? ""
    }

    var reviewSummary: String {
        snapshot?["summary"] as? String ?? ""
    }

    var modelCommentary: String? {
        snapshot?["model_commentary"] as? String
    }

    var reviewExplanation: String {
        let commentary = modelCommentary?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !commentary.isEmpty {
            return commentary
        }
        return reviewSummary
    }

    var reviewEngine: String {
        (snapshot?["facts"] as? [String: Any])?["engine"] as? String
            ?? snapshot?["engine"] as? String
            ?? ""
    }

    var changedFiles: [String] {
        ((snapshot?["facts"] as? [String: Any])?["files"] as? [String]) ?? []
    }

    var complete: Bool {
        ((snapshot?["facts"] as? [String: Any])?["complete"] as? Bool) ?? false
    }

    var hasRealChecks: Bool {
        let facts = snapshot?["facts"] as? [String: Any] ?? [:]
        for key in ["baseline", "current", "final"] {
            guard let row = facts[key] as? [String: Any] else { continue }
            let status = (row["status"] as? String ?? "").lowercased()
            if !status.isEmpty && status != "not run" && status != "not_run" {
                return true
            }
        }
        return false
    }

    var selectedModelName: String? {
        models.first(where: { $0.id == selectedModelId })?.name
    }

    var selectedModelNote: String? {
        models.first(where: { $0.id == selectedModelId })?.note
    }

    func start() {
        engine.onEvent = { [weak self] event in
            Task { @MainActor in
                self?.handle(event)
            }
        }
        Task {
            do {
                try engine.start()
                let prefs = try await engine.call("prefs")
                if let name = prefs["profile"] as? String, !name.isEmpty {
                    profile = name
                }
                if let enginePref = prefs["engine"] as? String, !enginePref.isEmpty {
                    engineName = enginePref
                }
                recent = Self.parseRecent(prefs["recent_projects"])
                readiness = try await engine.call("readiness")
                applyCatalog(from: readiness)
                ready = true
                // Warm the local model now so the first message is not a cold start.
                Task {
                    _ = try? await engine.call("ensure_server", [:], timeoutSeconds: 300)
                    readiness = try? await engine.call("readiness")
                    applyCatalog(from: readiness)
                }
            } catch {
                startupError = error.localizedDescription
                let log = FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent("Library/Logs/local-coder-app.log")
                try? (error.localizedDescription + "\n").write(to: log, atomically: true, encoding: .utf8)
            }
        }
    }

    func newChat() {
        if running {
            stop()
        }
        let path = projectPath
        let keptInspection = inspection
        snapshot = nil
        requestText = ""
        banner = nil
        explainSummary = nil
        explainHits = []
        chatTurns = []
        followUpReady = false
        previewURL = nil
        progressLog = []
        elapsedSeconds = 0
        lastUpdate = nil
        lastPulse = nil
        progress = "Ready."
        activeEpisode = nil
        if let path, keptInspection != nil {
            // Stay on this project; only start a fresh conversation.
            inspection = keptInspection
            history = history
            Task {
                _ = try? await engine.call("clear_chat", ["path": path])
            }
        } else {
            inspection = nil
            history = []
        }
    }

    func newProject() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = "Choose the folder that should contain the new project."
        panel.prompt = "Choose"
        guard panel.runModal() == .OK, let parent = panel.url else { return }
        let alert = NSAlert()
        alert.messageText = "Name the new project"
        alert.informativeText = "Local Coder will create an empty React starter here. The local model writes the real application after you describe it."
        alert.addButton(withTitle: "Create")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(string: "my-app")
        field.frame = NSRect(x: 0, y: 0, width: 240, height: 24)
        field.setAccessibilityIdentifier("newProjectName")
        alert.accessoryView = field
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let name = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return }
        Task {
            do {
                let result = try await engine.call("new_project", ["parent": parent.path, "name": name])
                if let path = result["path"] as? String {
                    selectProject(path)
                    banner = "New project created. Describe what you want to build."
                }
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func openProject() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = "Choose the project folder Local Coder should work in."
        panel.prompt = "Open"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        selectProject(url.path)
    }

    func selectProject(_ path: String) {
        banner = nil
        explainSummary = nil
        explainHits = []
        chatTurns = []
        snapshot = nil
        requestText = ""
        Task {
            do {
                inspection = try await engine.call("inspect_project", ["path": path])
                try await refreshSidebars()
                try await loadChat(for: path)
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func submitRequest() {
        let text = requestText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        banner = nil
        explainSummary = nil
        explainHits = []
        progressLog = []
        elapsedSeconds = 0
        lastUpdate = nil
        lastPulse = nil
        // Show the user message in the transcript immediately (before the reply).
        appendLocalTurn(role: "user", text: text)
        requestText = ""
        Task {
            do {
                var path = projectPath
                if path == nil {
                    running = true
                    startProgressClock()
                    noteProgress("Creating a project from what you wrote.")
                    let created = try await engine.call("start_chat", ["text": text])
                    path = created["path"] as? String
                    if let report = created["inspection"] as? [String: Any] {
                        inspection = report
                    } else if let createdPath = path {
                        inspection = try await engine.call("inspect_project", ["path": createdPath])
                    }
                    banner = created["detail"] as? String
                    try await refreshSidebars()
                }
                guard let path else {
                    running = false
                    banner = "Local Coder could not create a project from that message."
                    return
                }
                let classified = try await engine.call("classify_request", ["path": path, "text": text])
                let kind = classified["kind"] as? String
                if kind == "unsupported" {
                    running = false
                    banner = classified["summary"] as? String
                    appendLocalTurn(
                        role: "assistant",
                        text: classified["summary"] as? String ?? "That request is not supported."
                    )
                    return
                }
                if kind == "explain" {
                    running = false
                    let result = try await engine.call("explain_request", ["path": path, "text": text])
                    let summary = result["summary"] as? String
                    explainSummary = nil
                    explainHits = result["hits"] as? [[String: Any]] ?? []
                    if let turns = result["turns"] {
                        applyChatTurns(turns)
                    } else if let summary, !summary.isEmpty {
                        appendLocalTurn(role: "assistant", text: summary)
                    }
                    return
                }
                if kind == "chat" {
                    running = true
                    startProgressClock()
                    noteProgress("Answering…")
                    snapshot = nil
                    let result = try await engine.call(
                        "chat_request",
                        ["path": path, "text": text],
                        timeoutSeconds: 600
                    )
                    clockTask?.cancel()
                    running = false
                    progress = "Ready."
                    applyChatTurns(result["turns"])
                    if chatTurns.isEmpty {
                        let reply = (result["model_commentary"] as? String)
                            ?? (result["summary"] as? String)
                        if let reply, !reply.isEmpty {
                            appendLocalTurn(role: "assistant", text: reply)
                        }
                    }
                    explainSummary = nil
                    explainHits = result["hits"] as? [[String: Any]] ?? []
                    if let preview = result["preview"] as? [String: Any],
                       let url = preview["url"] as? String, !url.isEmpty {
                        previewURL = url
                    }
                    return
                }
                running = true
                startProgressClock()
                noteProgress("Starting the local coding engine.")
                snapshot = nil
                previewURL = nil
                let started = try await engine.call("run_request", [
                    "path": path,
                    "text": text,
                    "profile": profile,
                    "engine": engineName,
                    "kind": followUpReady ? "follow_up" : "create",
                ])
                activeEpisode = started["episode_id"] as? String
            } catch {
                running = false
                banner = error.localizedDescription
                appendLocalTurn(role: "assistant", text: error.localizedDescription)
            }
        }
    }

    func stop() {
        Task {
            _ = try? await engine.call("cancel")
            progress = "Stopping…"
        }
    }

    func keep() {
        guard let path = projectPath, let episode = snapshot?["episode_id"] as? String else { return }
        Task {
            do {
                _ = try await engine.call("keep", ["episode_id": episode, "path": path])
                snapshot = try await engine.call("snapshot", ["episode_id": episode, "path": path])
                banner = "Changes were kept in the project. You can ask for the next change."
                followUpReady = true
                requestText = ""
                try await refreshSidebars()
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func discard() {
        guard let episode = snapshot?["episode_id"] as? String else { return }
        Task {
            do {
                var params: [String: Any] = ["episode_id": episode]
                if let path = projectPath {
                    params["path"] = path
                }
                _ = try await engine.call("discard", params)
                snapshot = try await engine.call("snapshot", params)
                banner = "Proposed changes were discarded. The original project is unchanged."
                followUpReady = false
                try await refreshSidebars()
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func openHistory(_ row: HistoryRow) {
        Task {
            do {
                var params: [String: Any] = ["episode_id": row.episodeID]
                if let path = projectPath {
                    params["path"] = path
                }
                snapshot = try await engine.call("snapshot", params)
                requestText = (snapshot?["ticket"] as? String) ?? row.request
                let commentary = snapshot?["model_commentary"] as? String
                let files = ((snapshot?["facts"] as? [String: Any])?["files"] as? [String]) ?? []
                if files.isEmpty, let commentary, !commentary.isEmpty {
                    explainSummary = commentary
                } else {
                    explainSummary = nil
                }
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func refreshReadiness() {
        Task {
            readiness = try? await engine.call("readiness")
            applyCatalog(from: readiness)
        }
    }

    func selectModel(_ id: String) {
        guard !applyingCatalog, !id.isEmpty else { return }
        Task {
            do {
                let result = try await engine.call("select_model", ["id": id], timeoutSeconds: 20)
                banner = result["detail"] as? String
                showSettings = false
                refreshReadiness()
                _ = try await engine.call("ensure_server", [:], timeoutSeconds: 300)
                refreshReadiness()
            } catch {
                banner = error.localizedDescription
                refreshReadiness()
            }
        }
    }

    private func applyCatalog(from payload: [String: Any]?) {
        applyingCatalog = true
        defer { applyingCatalog = false }
        let rows = payload?["models"] as? [[String: Any]] ?? []
        models = rows.compactMap { row in
            guard let id = row["id"] as? String, let name = row["name"] as? String else { return nil }
            return CatalogModel(
                id: id,
                name: name,
                available: row["available"] as? Bool ?? false,
                note: row["note"] as? String ?? ""
            )
        }
        if let selected = payload?["selected_model"] as? String, !selected.isEmpty {
            selectedModelId = selected
        }
    }

    func openDocker() {
        Task {
            do {
                let result = try await engine.call("open_docker")
                banner = result["detail"] as? String
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func buildWorker() {
        guard let docker = EngineClient.bundledDockerDir()?.path else {
            banner = "The worker Dockerfile is not bundled with this app."
            return
        }
        Task {
            do {
                let result = try await engine.call("build_worker", ["docker_dir": docker])
                banner = result["detail"] as? String
                refreshReadiness()
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func saveProfile() {
        Task {
            _ = try? await engine.call("save_prefs", ["profile": profile, "engine": engineName])
        }
    }

    func prepareToQuit(done: @escaping () -> Void) {
        guard !quitting else {
            done()
            return
        }
        quitting = true
        let once = Once()
        Task {
            _ = try? await engine.call("shutdown")
            engine.shutdown()
            once.run(done)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
            once.run(done)
        }
    }

    private func noteProgress(_ phrase: String) {
        guard !phrase.isEmpty else { return }
        progress = phrase
        let lowered = phrase.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let vague = [
            "the model is thinking about the next step.",
            "the model is working.",
            "waiting for the model to start.",
            "waiting for the model’s next tool call.",
            "waiting for the model's next tool call.",
            "still working",
        ]
        if vague.contains(where: { lowered == $0 || lowered.hasPrefix($0) }) {
            return
        }
        if progressLog.last != phrase {
            progressLog.append(phrase)
            if progressLog.count > 40 {
                progressLog.removeFirst(progressLog.count - 40)
            }
        }
    }

    private func startProgressClock() {
        clockTask?.cancel()
        let already = elapsedSeconds
        clockTask = Task { @MainActor [weak self] in
            let start = Date().addingTimeInterval(TimeInterval(-already))
            while !Task.isCancelled {
                guard let self, self.running else { return }
                self.elapsedSeconds = max(0, Int(Date().timeIntervalSince(start)))
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    private func handle(_ event: EngineEvent) {
        lastPulse = Date()
        if event.type == "working" {
            let data = event.payload["data"] as? [String: Any]
            if let last = data?["last"] as? String, !last.isEmpty {
                progress = last
            } else if let phrase = event.phrase, !phrase.isEmpty {
                progress = phrase
            }
            return
        }
        if let phrase = event.phrase, !phrase.isEmpty {
            lastUpdate = Date()
            noteProgress(phrase)
        }
        if event.type == "app_error" {
            running = false
            let nested = event.payload["data"] as? [String: Any]
            let detail = (event.payload["detail"] as? String)
                ?? (nested?["detail"] as? String)
                ?? event.phrase
                ?? "The request failed."
            banner = detail.isEmpty ? "The request failed." : detail
        }
        if event.type == "handoff_written" || event.type == "cancelled"
            || event.type == "done" || event.type == "stopped" || event.type == "budget" {
            finishActiveEpisode()
        }
    }

    private func finishActiveEpisode() {
        guard let episode = activeEpisode else {
            running = false
            return
        }
        Task {
            do {
                var params: [String: Any] = ["episode_id": episode]
                if let path = projectPath {
                    params["path"] = path
                }
                snapshot = try await engine.call("snapshot", params)
                if let preview = snapshot?["preview"] as? [String: Any],
                   let url = preview["url"] as? String, !url.isEmpty,
                   preview["ok"] as? Bool != false {
                    previewURL = url
                } else {
                    previewURL = nil
                }
                progress = (snapshot?["progress"] as? [String: Any])?["phrase"] as? String ?? progress
                if let path = projectPath {
                    try await loadChat(for: path)
                }
                let reply = (snapshot?["model_commentary"] as? String)
                    ?? (snapshot?["summary"] as? String)
                if let reply, !reply.isEmpty {
                    appendLocalTurn(role: "assistant", text: reply)
                }
                try await refreshSidebars()
            } catch {
                banner = error.localizedDescription
            }
            running = false
        }
    }

    private func refreshSidebars() async throws {
        let prefs = try await engine.call("prefs")
        recent = Self.parseRecent(prefs["recent_projects"])
        if let path = projectPath {
            let payload = try await engine.call("history", ["path": path])
            let rows = payload["episodes"] as? [[String: Any]] ?? []
            history = rows.prefix(20).map { row in
                HistoryRow(
                    episodeID: row["episode_id"] as? String ?? "",
                    request: row["request"] as? String ?? "",
                    outcome: row["outcome"] as? String ?? "",
                    updated: row["updated_ts"] as? String ?? ""
                )
            }
        }
    }

    private func loadChat(for path: String) async throws {
        let payload = try await engine.call("load_chat", ["path": path])
        applyChatTurns(payload["turns"])
        if let last = chatTurns.last(where: { $0.role == "assistant" }) {
            explainSummary = nil
            _ = last
        }
    }

    private func applyChatTurns(_ raw: Any?) {
        let rows = raw as? [[String: Any]] ?? []
        chatTurns = rows.compactMap { row in
            guard let role = row["role"] as? String,
                  let text = row["text"] as? String,
                  !text.isEmpty else { return nil }
            let id = row["id"] as? String ?? "\(role)-\(text.hashValue)"
            return ChatTurn(id: id, role: role, text: text)
        }
    }

    private func appendLocalTurn(role: String, text: String) {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { return }
        if let last = chatTurns.last, last.role == role, last.text == body {
            return
        }
        chatTurns.append(ChatTurn(id: "local-\(UUID().uuidString)", role: role, text: body))
    }

    private static func parseRecent(_ raw: Any?) -> [RecentProject] {
        guard let rows = raw as? [[String: Any]] else { return [] }
        return rows.compactMap { row in
            guard let path = row["path"] as? String else { return nil }
            return RecentProject(path: path, name: row["name"] as? String ?? URL(fileURLWithPath: path).lastPathComponent)
        }
    }
}

private final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var finished = false

    func run(_ body: () -> Void) {
        lock.lock()
        let first = !finished
        finished = true
        lock.unlock()
        if first {
            body()
        }
    }
}
