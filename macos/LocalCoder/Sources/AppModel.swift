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
    let group: String
    let subtitle: String
    let provider: String
    let free: Bool
    let intelligence: Double?
    let reasoningSwitch: Bool

    /// SF Symbol for where the model runs.
    var placeSymbol: String {
        switch provider {
        case "orcarouter": return "point.3.filled.connected.trianglepath.dotted"
        case "nvidia": return "cloud"
        default: return "laptopcomputer"
        }
    }
}

/// One OrcaRouter model in the full list.
struct RouterModel: Identifiable, Hashable {
    let id: String
    let name: String
    let provider: String
    let summary: String
    let context: Int?
    let priceIn: Double?
    let priceOut: Double?
    let free: Bool
    let reasoning: Bool
    let vision: Bool
    let intelligence: Double?
    let speed: Int?

    var priceLabel: String {
        if free { return "Free" }
        guard let priceIn, let priceOut else { return "Not listed" }
        return "$\(Self.money(priceIn)) in · $\(Self.money(priceOut)) out"
    }

    var contextLabel: String {
        guard let context else { return "–" }
        if context >= 1_000_000 {
            let millions = (Double(context) / 100_000).rounded() / 10
            return millions == millions.rounded() ? "\(Int(millions))M" : String(format: "%.1fM", millions)
        }
        return "\(context / 1000)K"
    }

    private static let smallMoney = moneyFormatter(maxDigits: 3)
    private static let largeMoney = moneyFormatter(maxDigits: 2)

    static func money(_ value: Double) -> String {
        (value < 1 ? smallMoney : largeMoney).string(from: NSNumber(value: value)) ?? String(value)
    }

    private static func moneyFormatter(maxDigits: Int) -> NumberFormatter {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.numberStyle = .decimal
        formatter.usesGroupingSeparator = false
        formatter.minimumFractionDigits = 0
        formatter.maximumFractionDigits = maxDigits
        formatter.minimumIntegerDigits = 1
        return formatter
    }
}

/// One step of the agent's work, as it streams in.
struct TraceStep: Identifiable, Hashable {
    let id = UUID()
    let tool: String
    let detail: String
    let target: String
    let ok: Bool
    let started: Date
}

/// The finished work behind one answer.
struct TraceRecord: Hashable {
    let steps: [TraceStep]
    let started: Date
    let ended: Date
    let cost: Double?
}

struct ChatTurn: Identifiable, Hashable {
    let id: String
    let role: String
    let text: String
}

/// One exchange in the conversation.
struct ConversationEntry: Identifiable {
    let id: String
    let prompt: String?
    var answers: [ChatTurn]
}

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

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
    @Published var chatTurns: [ChatTurn] = []
    @Published var previewURL: String?
    @Published var followUpReady = false
    @Published var engineName = "general"
    @Published var models: [CatalogModel] = []
    @Published var selectedModelId = ""
    @Published var trace: [TraceStep] = []
    @Published var traceStart: Date?
    @Published var traces: [String: TraceRecord] = [:]
    @Published var currentPrompt: String?
    @Published var inspectorShown = false

    // OrcaRouter
    @Published var orcaConnected = false
    @Published var orcaWhere = ""
    @Published var routerModels: [RouterModel] = []
    @Published var routerFetchedAt: Date?
    @Published var routerLoading = false
    @Published var routerError: String?
    @Published var signingIn = false
    @Published var signInStatus: String?
    @Published var connectError: String?
    @Published var showConnect = false
    @Published var showBrowser = false
    @Published var switchingModel = false
    @Published var settingsTab = "models"
    @Published var reasoningOn = true

    private let engine = EngineClient()
    private var activeEpisode: String?
    /// The project the active request runs in (the window may not switch away until it ends).
    private var activeEpisodePath: String?
    private var quitting = false
    private var started = false
    private var pendingModelId: String?

    var projectPath: String? {
        inspection?["path"] as? String
    }

    var projectName: String {
        inspection?["name"] as? String ?? "Local Coder"
    }

    var projectSummary: String {
        inspection?["summary"] as? String ?? ""
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

    var changedFiles: [String] {
        ((snapshot?["facts"] as? [String: Any])?["files"] as? [String]) ?? []
    }

    var changeDelta: [String: Any] {
        ((snapshot?["facts"] as? [String: Any])?["delta"] as? [String: Any]) ?? [:]
    }

    var verification: [String: Any] {
        snapshot?["verification"] as? [String: Any] ?? [:]
    }

    var requestCost: Double? {
        snapshot?["cost_usd"] as? Double
    }

    var hasChanges: Bool {
        !changedFiles.isEmpty || !diffText.isEmpty
    }

    var selectedModel: CatalogModel? {
        models.first(where: { $0.id == selectedModelId })
    }

    /// Models grouped by where they run, in a fixed order.
    var modelGroups: [(String, [CatalogModel])] {
        let order = ["OrcaRouter", "NVIDIA Cloud", "On This Mac"]
        let grouped = Dictionary(grouping: models, by: \.group)
        let known = order.compactMap { name in grouped[name].map { (name, $0) } }
        let others = grouped.keys.filter { !order.contains($0) }.sorted().map { ($0, grouped[$0] ?? []) }
        return known + others
    }

    /// The conversation as entries: a prompt and the answers that followed it.
    var entries: [ConversationEntry] {
        var result: [ConversationEntry] = []
        for turn in chatTurns {
            if turn.role == "user" {
                result.append(ConversationEntry(id: turn.id, prompt: turn.text, answers: []))
            } else if result.isEmpty {
                result.append(ConversationEntry(id: turn.id, prompt: nil, answers: [turn]))
            } else {
                result[result.count - 1].answers.append(turn)
            }
        }
        return result
    }

    var blockingIssues: [String] {
        readiness?["blocking"] as? [String] ?? []
    }

    // MARK: Start and quit

    func start() {
        guard !started else { return }
        started = true
        if ProcessInfo.processInfo.environment["LOCAL_CODER_UI_PROBE"] != nil {
            listenForProbes()
        }
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
                loadRouterModels()
                // Warm the local model now so the first message is not a cold start (no-op for a hosted model).
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

    // MARK: Projects and conversation

    /// Start a fresh conversation (the old one is archived by the engine). Not while a request runs:
    /// clearing the screen mid-run used to leave a blank "running" window and drop the result.
    func newChat() {
        guard !running else { return }
        let path = projectPath
        snapshot = nil
        requestText = ""
        banner = nil
        chatTurns = []
        followUpReady = false
        previewURL = nil
        progress = "Ready."
        activeEpisode = nil
        trace = []
        traces = [:]
        inspectorShown = false
        if let path {
            Task {
                _ = try? await engine.call("clear_chat", ["path": path])
            }
        }
    }

    /// Leave the current project and ask about anything on the Mac. Its chat stays saved; a running
    /// request keeps the project open.
    func closeProject() {
        guard !running else { return }
        snapshot = nil
        requestText = ""
        banner = nil
        chatTurns = []
        followUpReady = false
        previewURL = nil
        activeEpisode = nil
        traces = [:]
        inspectorShown = false
        inspection = nil
        history = []
    }

    func newProject() {
        guard !running else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = "Choose the folder that should contain the new project."
        panel.prompt = "Choose"
        guard panel.runModal() == .OK, let parent = panel.url else { return }
        let alert = NSAlert()
        alert.messageText = "Name the new project"
        alert.informativeText = "Local Coder creates an empty React starter here. The model writes the app after you describe it."
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
        guard !running else { return }
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
        guard !running, path != projectPath || inspection == nil else { return }
        banner = nil
        chatTurns = []
        snapshot = nil
        previewURL = nil
        requestText = ""
        traces = [:]
        inspectorShown = false
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
        guard !text.isEmpty, !running else { return }
        banner = nil
        appendLocalTurn(role: "user", text: text)
        requestText = ""
        currentPrompt = text
        trace = []
        traceStart = Date()
        running = true
        Task {
            do {
                var path = projectPath
                if path == nil {
                    // A question needs no new folder: the model looks around the Mac and may open the project it means.
                    let first = try await engine.call("classify_request", ["text": text])
                    let firstKind = first["kind"] as? String
                    if firstKind == "chat" || firstKind == "explain" {
                        try await answerQuestion(path: nil, text: text)
                        return
                    }
                    noteStatus("Creating a project from what you wrote.")
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
                    finishTrace(cost: nil)
                    banner = "Local Coder could not create a project from that message."
                    return
                }
                let classified = try await engine.call("classify_request", ["path": path, "text": text])
                let kind = classified["kind"] as? String
                if kind == "unsupported" {
                    running = false
                    finishTrace(cost: nil)
                    appendLocalTurn(role: "assistant", text: classified["summary"] as? String ?? "That request is not supported.")
                    return
                }
                if kind == "explain" {
                    let result = try await engine.call("explain_request", ["path": path, "text": text])
                    running = false
                    finishTrace(cost: nil)
                    if let turns = result["turns"] {
                        applyChatTurns(turns)
                    } else if let summary = result["summary"] as? String, !summary.isEmpty {
                        appendLocalTurn(role: "assistant", text: summary)
                    }
                    return
                }
                if kind == "chat" {
                    try await answerQuestion(path: path, text: text)
                    return
                }
                snapshot = nil
                previewURL = nil
                inspectorShown = false
                activeEpisodePath = path
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
                finishTrace(cost: nil)
                appendLocalTurn(role: "assistant", text: error.localizedDescription)
            }
        }
    }

    /// Answer a question in chat. With no project open, `path` is nil; when the model opens a
    /// project, the window switches to it and shows the conversation there.
    private func answerQuestion(path: String?, text: String) async throws {
        var params: [String: Any] = ["text": text]
        if let path {
            params["path"] = path
        }
        let result: [String: Any]
        do {
            result = try await engine.call("chat_request", params, timeoutSeconds: 1200)
        } catch {
            running = false
            progress = "Ready."
            finishTrace(cost: nil)
            throw error
        }
        running = false
        progress = "Ready."
        finishTrace(cost: result["cost_usd"] as? Double)
        if let opened = result["open_project"] as? String, !opened.isEmpty {
            let kept = traces
            selectProject(opened)
            traces = kept
            return
        }
        if let turns = result["turns"] as? [[String: Any]], !turns.isEmpty {
            applyChatTurns(turns)
        } else {
            let reply = (result["model_commentary"] as? String) ?? (result["summary"] as? String)
            if let reply, !reply.isEmpty {
                appendLocalTurn(role: "assistant", text: reply)
            }
        }
        if let preview = result["preview"] as? [String: Any],
           let url = preview["url"] as? String, !url.isEmpty {
            previewURL = url
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
                banner = "The changes are in your project now. Ask for the next one."
                followUpReady = true
                inspectorShown = false
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
                banner = "The changes were thrown away. Your project is as it was."
                followUpReady = false
                inspectorShown = false
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
                inspectorShown = hasChanges
                if !hasChanges {
                    switch snapshot?["outcome"] as? String {
                    case "discarded": banner = "That request was discarded."
                    case "kept": banner = "Those changes are already in your project."
                    default: banner = "That request did not change any files."
                    }
                }
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    // MARK: Models

    func refreshReadiness() {
        Task {
            readiness = try? await engine.call("readiness")
            applyCatalog(from: readiness)
        }
    }

    func selectModel(_ id: String) {
        guard !id.isEmpty, id != selectedModelId || !(selectedModel?.available ?? false) else { return }
        switchingModel = true
        Task {
            defer { switchingModel = false }
            do {
                let result = try await engine.call("select_model", ["id": id], timeoutSeconds: 20)
                if let catalog = result["catalog"] as? [String: Any] {
                    applyCatalog(from: ["models": catalog["models"] as Any, "selected_model": catalog["selected"] as Any])
                }
                refreshReadiness()
                if !id.hasPrefix("orca:") && !id.hasPrefix("nemotron") {
                    _ = try? await engine.call("ensure_server", [:], timeoutSeconds: 300)
                    refreshReadiness()
                }
            } catch let error as EngineError where error.code == "missing_key" && id.hasPrefix("orca:") {
                pendingModelId = id
                connectError = nil
                showConnect = true
            } catch {
                banner = error.localizedDescription
                refreshReadiness()
            }
        }
    }

    /// Turn the selected model's reasoning on or off (remembered for that model).
    func setReasoning(_ on: Bool) {
        guard on != reasoningOn else { return }
        reasoningOn = on
        Task {
            do {
                _ = try await engine.call("set_reasoning", ["mode": on ? "on" : "off"])
            } catch {
                reasoningOn = !on
                banner = error.localizedDescription
            }
        }
    }

    func chooseRouterModel(_ modelID: String) {
        selectModel("orca:\(modelID)")
    }

    private func applyCatalog(from payload: [String: Any]?) {
        let rows = payload?["models"] as? [[String: Any]] ?? []
        if !rows.isEmpty {
            models = rows.compactMap { row in
                guard let id = row["id"] as? String, let name = row["name"] as? String else { return nil }
                return CatalogModel(
                    id: id,
                    name: name,
                    available: row["available"] as? Bool ?? false,
                    note: row["note"] as? String ?? "",
                    group: row["group"] as? String ?? "On This Mac",
                    subtitle: row["subtitle"] as? String ?? "",
                    provider: row["provider"] as? String ?? "local",
                    free: row["free"] as? Bool ?? false,
                    intelligence: row["intelligence"] as? Double,
                    reasoningSwitch: row["reasoning_switch"] as? Bool ?? false
                )
            }
        }
        if let selected = payload?["selected_model"] as? String, !selected.isEmpty {
            selectedModelId = selected
        }
        if let orca = payload?["orcarouter"] as? [String: Any] {
            applyOrcaStatus(orca)
        }
        if let mode = payload?["reasoning"] as? String {
            reasoningOn = mode != "off"
        }
    }

    // MARK: OrcaRouter

    private func applyOrcaStatus(_ status: [String: Any]) {
        orcaConnected = status["connected"] as? Bool ?? false
        orcaWhere = status["where"] as? String ?? ""
    }

    func loadRouterModels(refresh: Bool = false) {
        guard !routerLoading else { return }
        routerLoading = true
        Task {
            defer { routerLoading = false }
            do {
                let result = try await engine.call("orca_models", ["refresh": refresh], timeoutSeconds: 45)
                let rows = result["models"] as? [[String: Any]] ?? []
                routerModels = rows.compactMap(Self.routerModel)
                if let stamp = result["fetched_at"] as? Double, stamp > 0 {
                    routerFetchedAt = Date(timeIntervalSince1970: stamp)
                } else if let stamp = result["fetched_at"] as? Int, stamp > 0 {
                    routerFetchedAt = Date(timeIntervalSince1970: TimeInterval(stamp))
                }
                routerError = result["error"] as? String
                orcaConnected = result["connected"] as? Bool ?? orcaConnected
                if refresh {
                    refreshReadiness()
                }
            } catch {
                routerError = error.localizedDescription
            }
        }
    }

    static func routerModel(_ row: [String: Any]) -> RouterModel? {
        guard let id = row["id"] as? String else { return nil }
        return RouterModel(
            id: id,
            name: row["name"] as? String ?? id,
            provider: row["provider"] as? String ?? "",
            summary: row["description"] as? String ?? "",
            context: row["context"] as? Int,
            priceIn: (row["price_in"] as? Double) ?? (row["price_in"] as? Int).map(Double.init),
            priceOut: (row["price_out"] as? Double) ?? (row["price_out"] as? Int).map(Double.init),
            free: row["free"] as? Bool ?? false,
            reasoning: row["reasoning"] as? Bool ?? false,
            vision: row["vision"] as? Bool ?? false,
            intelligence: (row["intelligence"] as? Double) ?? (row["intelligence"] as? Int).map(Double.init),
            speed: row["speed"] as? Int
        )
    }

    func signInWithOrcaRouter() {
        guard !signingIn else { return }
        signingIn = true
        connectError = nil
        signInStatus = "Opening OrcaRouter in your browser…"
        Task {
            defer { signingIn = false }
            do {
                let status = try await engine.call("orca_sign_in", [:], timeoutSeconds: 330)
                didConnect(status)
            } catch {
                if error.localizedDescription != "Sign-in was cancelled." {
                    connectError = error.localizedDescription
                }
            }
        }
    }

    func cancelSignIn() {
        Task {
            _ = try? await engine.call("orca_sign_in_cancel")
        }
    }

    /// Check a pasted key with OrcaRouter and keep it. Returns whether it was saved.
    func saveOrcaKey(_ key: String) async -> Bool {
        connectError = nil
        do {
            let status = try await engine.call("orca_save_key", ["key": key], timeoutSeconds: 40)
            didConnect(status)
            return true
        } catch {
            connectError = error.localizedDescription
            return false
        }
    }

    func disconnectOrcaRouter() {
        Task {
            do {
                let status = try await engine.call("orca_remove_key")
                applyOrcaStatus(status)
                refreshReadiness()
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    private func didConnect(_ status: [String: Any]) {
        applyOrcaStatus(status)
        signInStatus = nil
        showConnect = false
        loadRouterModels()
        if let pending = pendingModelId {
            pendingModelId = nil
            selectModel(pending)
        } else {
            refreshReadiness()
        }
    }

    // MARK: Engine settings

    func saveProfile() {
        Task {
            _ = try? await engine.call("save_prefs", ["profile": profile, "engine": engineName])
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

    // MARK: Probe (automated screen checks)

    /// Lets a test script open screens without touching the keyboard or mouse. Only active when the
    /// app was launched with LOCAL_CODER_UI_PROBE set.
    private func listenForProbes() {
        DistributedNotificationCenter.default().addObserver(
            forName: Notification.Name("app.localcoder.probe"), object: nil, queue: .main
        ) { [weak self] note in
            let command = note.object as? String ?? ""
            Task { @MainActor in self?.probe(command) }
        }
    }

    /// Views only: a probe never selects, sends, stops or clears anything, and does nothing during a run.
    private func probe(_ command: String) {
        guard !running else { return }
        let parts = command.split(separator: ":", maxSplits: 1).map(String.init)
        let argument = parts.count > 1 ? parts[1] : ""
        switch parts.first ?? "" {
        case "browser": showBrowser = true
        case "connect": showConnect = true
        case "close": showBrowser = false; showConnect = false
        case "settings":
            settingsTab = argument.isEmpty ? "models" : argument
            NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
        case "demo-run" where ProcessInfo.processInfo.environment["LOCAL_CODER_UI_DEMO"] != nil:
            // A test copy only (its own config): shows the live run with no engine call.
            chatTurns = [ChatTurn(id: "demo", role: "user", text: "Show the working indicator")]
            currentPrompt = "Show the working indicator"
            traceStart = Date()
            trace = [TraceStep(tool: "list", detail: "Looking in ~/projects", target: "~/projects", ok: true,
                               started: Date())]
            running = true
        default: break
        }
    }

    // MARK: Events and the live trace

    private func noteStatus(_ phrase: String) {
        guard !phrase.isEmpty else { return }
        progress = phrase
        if running {
            recordStep(tool: "status", detail: phrase, target: "", ok: true)
        }
    }

    private func handle(_ event: EngineEvent) {
        if event.type == "orca_sign_in" {
            signInStatus = event.phrase
            return
        }
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
            progress = phrase
            if running, !Self.quietEvents.contains(event.type) {
                let data = event.payload["data"] as? [String: Any] ?? [:]
                recordStep(
                    tool: data["tool"] as? String ?? (event.type == "activity" ? "status" : event.type),
                    detail: phrase,
                    target: data["target"] as? String ?? "",
                    ok: data["ok"] as? Bool ?? true
                )
            }
        }
        if event.type == "app_error" {
            running = false
            let nested = event.payload["data"] as? [String: Any]
            let detail = (event.payload["detail"] as? String)
                ?? (nested?["detail"] as? String)
                ?? event.phrase
                ?? "The request failed."
            banner = detail.isEmpty ? "The request failed." : detail
            finishTrace(cost: nil)
        }
        if event.type == "handoff_written" || event.type == "cancelled"
            || event.type == "done" || event.type == "stopped" || event.type == "budget" {
            finishActiveEpisode()
        }
    }

    private static let quietEvents: Set<String> = ["working", "done", "stopped", "handoff_written", "cancelled"]
    private static let vaguePhrases = [
        "the model is thinking about the next step.",
        "the model is working.",
        "waiting for the model to start.",
        "waiting for the model’s next tool call.",
        "waiting for the model's next tool call.",
        "still working",
    ]

    private func recordStep(tool: String, detail: String, target: String, ok: Bool) {
        let lowered = detail.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if Self.vaguePhrases.contains(where: { lowered == $0 || lowered.hasPrefix($0) }) { return }
        if let last = trace.last, last.detail == detail, last.tool == tool { return }
        trace.append(TraceStep(tool: tool, detail: detail, target: target, ok: ok, started: Date()))
        if trace.count > 400 { trace.removeFirst(trace.count - 400) }
    }

    private func finishTrace(cost: Double?) {
        if let prompt = currentPrompt, let start = traceStart, !trace.isEmpty || cost != nil {
            traces[prompt] = TraceRecord(steps: trace, started: start, ended: Date(), cost: cost)
        }
        trace = []
        traceStart = nil
        currentPrompt = nil
    }

    private func finishActiveEpisode() {
        guard let episode = activeEpisode else {
            running = false
            return
        }
        activeEpisode = nil
        let episodePath = activeEpisodePath ?? projectPath
        activeEpisodePath = nil
        Task {
            do {
                var params: [String: Any] = ["episode_id": episode]
                if let path = episodePath {
                    params["path"] = path
                }
                snapshot = try await engine.call("snapshot", params, timeoutSeconds: 60)
                finishTrace(cost: requestCost)
                if let preview = snapshot?["preview"] as? [String: Any],
                   let url = preview["url"] as? String, !url.isEmpty,
                   preview["ok"] as? Bool != false {
                    previewURL = url
                } else {
                    previewURL = nil
                }
                inspectorShown = hasChanges || previewURL != nil
                if let path = episodePath {
                    try await loadChat(for: path)
                }
                let reply = (snapshot?["model_commentary"] as? String) ?? (snapshot?["summary"] as? String)
                if let reply, !reply.isEmpty {
                    appendLocalTurn(role: "assistant", text: reply)
                }
                try await refreshSidebars()
            } catch {
                banner = error.localizedDescription
                finishTrace(cost: nil)
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
            history = rows.prefix(30).map { row in
                HistoryRow(
                    episodeID: row["episode_id"] as? String ?? "",
                    request: row["request"] as? String ?? "",
                    outcome: row["outcome"] as? String ?? "",
                    updated: row["updated_ts"] as? String ?? ""
                )
            }
        } else {
            history = []
        }
    }

    private func loadChat(for path: String) async throws {
        let payload = try await engine.call("load_chat", ["path": path])
        applyChatTurns(payload["turns"])
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
