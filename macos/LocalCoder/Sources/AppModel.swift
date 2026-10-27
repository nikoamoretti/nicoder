import AppKit
import SwiftUI

struct RecentProject: Identifiable, Hashable {
    let path: String
    let name: String
    /// When a request or a chat last ran in it.
    var active: Date?
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
    var id = UUID()
    let tool: String
    let detail: String
    let target: String
    let ok: Bool
    let started: Date
    /// A command step: the command itself, for the tooltip (the line shows the model's words for it).
    var command = ""
    /// The command is still running; the step it sends when it ends takes this one's place.
    var running = false
    /// Ties a running command to the step it sends when it ends.
    var call = ""
    /// The command ran on the Mac itself, outside the sandbox (an install, a service, a setting).
    var mac = false
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
    /// The work for this prompt is running now.
    var live = false
    /// Sent while the same kind of work ran elsewhere: it starts by itself when that ends.
    var waiting: UUID?
}

/// Where a conversation lives: a project folder, or a chat started without one.
enum Place: Hashable {
    case project(String)
    case chat(String)

    var path: String? {
        if case .project(let path) = self { return path }
        return nil
    }
}

/// A chat started without a project, as the sidebar lists it.
struct SavedChat: Identifiable, Hashable {
    let id: String
    let title: String
    let updated: Date?
}

/// Work in flight, tied to the conversation it was asked in, so the window can show another one meanwhile
/// (Nico, 2026-10-26: "creating a new chat while another one is running is super buggy").
struct Job {
    let place: Place
    let prompt: String
    var started = Date()
    var steps: [TraceStep] = []
    var episode: String?
}

extension Job {
    mutating func add(_ step: TraceStep) {
        // A command sends a step when it starts and one when it ends: the end takes the start's place, under the
        // same id so the live line updates in place instead of turning the wheel.
        if !step.call.isEmpty, let last = steps.last, last.running, last.call == step.call {
            var ended = step
            ended.id = last.id
            steps[steps.count - 1] = ended
            return
        }
        if step.call.isEmpty, let last = steps.last, last.detail == step.detail, last.tool == step.tool { return }
        steps.append(step)
        if steps.count > 400 { steps.removeFirst(steps.count - 400) }
    }
}

/// A message sent while the same kind of work runs elsewhere (one change and one question at a time).
struct Waiting: Identifiable {
    let id = UUID()
    let place: Place
    let text: String
    let change: Bool
    var files: [Attachment] = []
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
    /// Something runs, in this conversation or another one.
    @Published private(set) var running = false {
        didSet {
            // The Dock's hominin walks while a request runs and sits down to meditate when it ends.
            if running && !oldValue {
                DockFigure.shared.walk()
            } else if !running && oldValue {
                DockFigure.shared.meditate()
            }
        }
    }
    /// The change being made (one at a time) and the question being answered (one at a time).
    @Published private(set) var changeJob: Job?
    @Published private(set) var questionJob: Job?
    @Published private(set) var waiting: [Waiting] = []
    /// Conversations whose work ended while the window showed another one.
    @Published private(set) var unseen: Set<Place> = []
    /// The chat shown when no project is open; saved by the engine once it has a turn.
    @Published private(set) var chatID = AppModel.newChatID()
    @Published var chats: [SavedChat] = []
    /// Files and images attached to the next message (chips above the composer).
    @Published var attachments: [Attachment] = []
    /// Attachments still being copied and read.
    @Published private(set) var attaching = 0
    /// Counts "show me" answers that started the preview, so the panel turns to it even when its address is the same.
    @Published private(set) var previewAsked = 0
    @Published var snapshot: [String: Any]?
    @Published var readiness: [String: Any]?
    @Published var profile = "d2"
    @Published var banner: String?
    @Published var chatTurns: [ChatTurn] = []
    @Published var previewURL: String?
    @Published var engineName = "general"
    @Published var models: [CatalogModel] = []
    @Published var selectedModelId = ""
    /// The finished work behind each answer, per conversation.
    @Published private var traceRecords: [Place: [String: TraceRecord]] = [:]
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
    /// Change runs may run commands on the Mac itself, outside the sandbox (Settings > Advanced).
    @Published var macAccess = true

    let engine = EngineClient()
    /// Projects where the last change was kept: the next request builds on it.
    private var followUps: Set<String> = []
    /// What went wrong in a conversation while the window showed another one, shown when it is opened.
    private var notes: [Place: String] = [:]
    /// The change's result is being fetched; a second "done" for it is ignored.
    private var finishingChange = false
    /// Runs that ended before their start was answered (request id → answered as a question).
    private var endedEarly: [String: Bool] = [:]
    private var quitting = false
    private var started = false
    private var pendingModelId: String?

    var projectPath: String? {
        inspection?["path"] as? String
    }

    var projectName: String {
        inspection?["name"] as? String ?? "Nicoder"
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

    /// The engine's one sentence about the result, when it has one.
    var snapshotSummary: String? {
        guard let text = (snapshot?["summary"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else { return nil }
        return text
    }

    /// The newer request that has to be decided first, when this result is held back by it.
    var blockedBy: String? {
        snapshot?["blocked_by"] as? String
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

    /// The conversation the window shows.
    var place: Place {
        if let path = projectPath {
            return .project(path)
        }
        return .chat(chatID)
    }

    /// The work running in the conversation the window shows, if any.
    var jobHere: Job? {
        [changeJob, questionJob].compactMap { $0 }.first { $0.place == place }
    }

    var busyHere: Bool {
        jobHere != nil
    }

    var trace: [TraceStep] {
        jobHere?.steps ?? []
    }

    var traceStart: Date? {
        jobHere?.started
    }

    var traces: [String: TraceRecord] {
        traceRecords[place] ?? [:]
    }

    /// Keep and Discard wait while a change runs (an older result can be opened from the requests list meanwhile):
    /// Discard stops the one preview server, which the running change may be using for its checks.
    var decisionWaitsFor: String? {
        changeJob.map { name(of: $0.place) }
    }

    func isWorking(_ place: Place) -> Bool {
        changeJob?.place == place || questionJob?.place == place
    }

    func isWaiting(_ place: Place) -> Bool {
        waiting.contains { $0.place == place }
    }

    func isUnseen(_ place: Place) -> Bool {
        unseen.contains(place)
    }

    /// The conversation as entries: a prompt and the answers that followed it, then the prompt running now
    /// and the ones waiting for their turn.
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
        if let job = jobHere {
            if let last = result.last, last.prompt == job.prompt, last.answers.isEmpty {
                result[result.count - 1].live = true
            } else {
                // Back in a conversation whose work started before the window left it: the saved chat has
                // no turn for it yet.
                result.append(ConversationEntry(id: "running", prompt: job.prompt, answers: [], live: true))
            }
        }
        for item in waiting where item.place == place {
            result.append(ConversationEntry(id: item.id.uuidString, prompt: item.text, answers: [], waiting: item.id))
        }
        return result
    }

    /// The name of the conversation a waiting message waits for, for its line.
    func waitsFor(_ id: UUID) -> String {
        guard let item = waiting.first(where: { $0.id == id }),
              let job = item.change ? changeJob : questionJob else { return "the work before it" }
        return name(of: job.place)
    }

    func name(of place: Place) -> String {
        switch place {
        case .project(let path):
            return recent.first(where: { $0.path == path })?.name ?? URL(fileURLWithPath: path).lastPathComponent
        case .chat(let id):
            return chats.first(where: { $0.id == id })?.title ?? "a new chat"
        }
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
                if let listed = try? await engine.call("list_chats") {
                    chats = Self.parseChats(listed)
                }
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

    static func newChatID() -> String {
        "chat-" + UUID().uuidString.lowercased()
    }

    /// Clear what the window shows before it shows another conversation. Work keeps running in its own.
    private func leaveConversation() {
        snapshot = nil
        requestText = ""
        attachments = []
        banner = nil
        chatTurns = []
        previewURL = nil
        inspectorShown = false
    }

    /// The window now shows `place`: its "done" mark goes, and what went wrong there meanwhile is shown.
    private func arrive(at place: Place) {
        unseen.remove(place)
        if let note = notes.removeValue(forKey: place) {
            banner = note
        }
    }

    /// A new chat with no project (⌘N, the toolbar, the sidebar). Nothing is archived: every chat stays in the
    /// sidebar, and work keeps running in the conversation it was asked in.
    func newChat() {
        if projectPath == nil && chatTurns.isEmpty && !isWorking(place) && !isWaiting(place) {
            return
        }
        leaveConversation()
        inspection = nil
        history = []
        chatID = Self.newChatID()
    }

    /// Open a chat that has no project.
    func selectChat(_ id: String) {
        guard place != .chat(id) else { return }
        leaveConversation()
        inspection = nil
        history = []
        chatID = id
        arrive(at: .chat(id))
        Task {
            do {
                let payload = try await engine.call("load_chat", ["chat_id": id])
                guard place == .chat(id) else { return }
                applyChatTurns(payload["turns"])
            } catch {
                if place == .chat(id) {
                    banner = error.localizedDescription
                }
            }
        }
    }

    /// Saved chats, plus a chat whose first message is still running or waiting (it is saved with its answer).
    var chatRows: [SavedChat] {
        var rows = chats
        let pending = [changeJob, questionJob].compactMap { $0 }.map { ($0.place, $0.prompt) }
            + waiting.map { ($0.place, $0.text) }
        for (place, text) in pending {
            if case .chat(let id) = place, !rows.contains(where: { $0.id == id }) {
                rows.insert(SavedChat(id: id, title: text, updated: nil), at: 0)
            }
        }
        return rows
    }

    /// What a sidebar row says on its right.
    func rowState(_ place: Place) -> RowState {
        if isWorking(place) { return .working }
        if isWaiting(place) { return .waiting }
        if isUnseen(place) { return .done }
        return .idle
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
        alert.informativeText = "Nicoder creates an empty React starter here. The model writes the app after you describe it."
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
        panel.message = "Choose the project folder Nicoder should work in."
        panel.prompt = "Open"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        selectProject(url.path)
    }

    /// Show a project. Allowed while work runs anywhere: that work keeps running and its result lands where it
    /// was asked.
    func selectProject(_ path: String) {
        guard path != projectPath || inspection == nil else { return }
        leaveConversation()
        history = []
        // The window shows the project at once; its details follow.
        inspection = ["path": path, "name": URL(fileURLWithPath: path).lastPathComponent]
        arrive(at: .project(path))
        Task {
            do {
                let report = try await engine.call("inspect_project", ["path": path])
                guard projectPath == path else { return }
                inspection = report
                let shown = projectPath ?? path
                try await refreshSidebars()
                try await loadChat(for: shown)
                try await showWaitingResult(in: shown)
            } catch {
                if projectPath == path {
                    banner = error.localizedDescription
                }
            }
        }
    }

    /// Back in a project whose last change waits for Keep or Discard: show it as when it had just finished.
    private func showWaitingResult(in path: String) async throws {
        guard !isWorking(.project(path)), snapshot == nil,
              let newest = history.first, newest.outcome == "review" else { return }
        let result = try await engine.call("snapshot", ["episode_id": newest.episodeID, "path": path],
                                           timeoutSeconds: 60)
        guard projectPath == path, snapshot == nil, result["can_keep"] as? Bool == true else { return }
        snapshot = result
    }

    func submitRequest() {
        let text = requestText.trimmingCharacters(in: .whitespacesAndNewlines)
        let files = attachments
        guard !text.isEmpty || !files.isEmpty, attaching == 0, !busyHere else { return }
        banner = nil
        requestText = ""
        attachments = []
        let here = place
        Task { await send(text, in: here, files: files) }
    }

    // MARK: Attachments

    /// Files chosen, dropped or pasted: copied and read in the background, then shown as chips.
    func attach(urls: [URL]) {
        for url in urls {
            stage { try Stager.stage(file: url) }
        }
    }

    /// An image with no file (pasted, or dragged out of a browser).
    func attach(image: NSImage) {
        stage { try Stager.stage(image: image) }
    }

    func detach(_ id: UUID) {
        attachments.removeAll { $0.id == id }
    }

    func pickAttachments() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.message = "Choose files or images for Nicoder to look at."
        panel.prompt = "Attach"
        guard panel.runModal() == .OK else { return }
        attach(urls: panel.urls)
    }

    private func stage(_ work: @escaping () throws -> Attachment) {
        attaching += 1
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Result { try work() }
            Task { @MainActor in
                self.attaching -= 1
                switch result {
                case .success(let item): self.attachments.append(item)
                case .failure(let error): self.banner = error.localizedDescription
                }
            }
        }
    }

    /// Start a message in its conversation, or let it wait while the same kind of work runs in another one.
    private func send(_ text: String, in place: Place, files: [Attachment] = []) async {
        // A message that is only files is a question about them.
        let words = text.isEmpty && !files.isEmpty ? "Look at the attached files." : text
        let verdict: [String: Any]
        do {
            if let path = place.path {
                var params: [String: Any] = ["path": path, "text": words]
                // "just do it" right after a reply that said to keep the result keeps it.
                if self.place == place, let last = chatTurns.last, last.role == "assistant" {
                    params["last_reply"] = last.text
                }
                verdict = try await engine.call("classify_request", params)
            } else {
                // No project: a question stays a chat; work runs in the project it names, or in a new one.
                verdict = try await engine.call("classify_request", ["text": words])
            }
        } catch {
            tell(error.localizedDescription, in: place, after: text)
            return
        }
        var kind = verdict["kind"] as? String ?? ""
        if !files.isEmpty && (kind == "keep" || kind == "show") {
            kind = "chat"  // with files the message is about them, not a command
        }
        if kind == "unsupported" {
            tell(verdict["summary"] as? String ?? "That request is not supported.", in: place, after: text)
            return
        }
        if kind == "keep" {
            await keepFromChat(text, in: place, thenShow: verdict["show"] as? Bool == true)
            return
        }
        // "show" runs Nicoder's own preview with no model; it waits its turn like a question.
        let question = kind == "chat" || kind == "explain" || kind == "show"
        if question ? questionJob != nil : changeJob != nil {
            waiting.append(Waiting(place: place, text: text, change: !question, files: files))
            return
        }
        let shown = turnText(text, files)  // as the chat keeps it, so the live entry matches after a reload
        let job = Job(place: place, prompt: shown)
        if question {
            questionJob = job
        } else {
            changeJob = job
        }
        syncRunning()
        if self.place == place {
            appendLocalTurn(role: "user", text: shown)
        }
        if kind == "show" {
            await show(text, in: place, inBrowser: verdict["browser"] as? Bool == true)
        } else if question {
            await ask(text, in: place, explain: kind == "explain" && files.isEmpty, files: files)
        } else {
            await change(text, in: place, project: verdict["project"] as? String, files: files)
        }
    }

    /// "just show me", "its not running": run the project in Nicoder's own preview and open it beside the chat.
    /// No model is asked: a model could only claim it had started the app.
    private func show(_ text: String, in place: Place, inBrowser: Bool = false) async {
        guard let path = place.path else {
            finishQuestion(cost: nil)
            tell("Open a project first: a chat without one has no app to show.", in: place)
            startWaiting()
            return
        }
        do {
            let result = try await engine.call("show_app", ["path": path, "text": text], timeoutSeconds: 180)
            finishQuestion(cost: nil)
            present(result, in: place)
            // "open it in my browser": Nicoder opens it there itself (the agent's sandbox cannot, by design).
            if inBrowser, let preview = result["preview"] as? [String: Any], preview["ok"] as? Bool != false,
               let address = preview["url"] as? String, let url = URL(string: address) {
                NSWorkspace.shared.open(url)
            }
        } catch {
            finishQuestion(cost: nil)
            tell(error.localizedDescription, in: place)
        }
        startWaiting()
    }

    /// A reply from Nicoder itself (show, keep): its turns, and the app in the panel when it started.
    private func present(_ result: [String: Any], in place: Place) {
        guard self.place == place else {
            unseen.insert(place)
            return
        }
        applyChatTurns(result["turns"])
        if let preview = result["preview"] as? [String: Any],
           let url = preview["url"] as? String, !url.isEmpty, preview["ok"] as? Bool != false {
            previewURL = url
            previewAsked += 1  // the same address again still brings the Preview tab forward
            inspectorShown = true
        }
    }

    /// "keep it": keep the result that waits, exactly as the Keep button does. "just do it" after a reply that said
    /// to keep it also shows the app.
    private func keepFromChat(_ text: String, in place: Place, thenShow: Bool) async {
        guard let path = place.path else {
            tell("Nothing is waiting to be kept in a chat without a project.", in: place, after: text)
            return
        }
        if decisionWaitsFor != nil {
            // Keep waits while a change runs (one preview server); it happens by itself when that change ends.
            waiting.append(Waiting(place: place, text: text, change: true))
            return
        }
        do {
            let result = try await engine.call("keep_waiting", ["path": path, "text": text])
            let kept = result["episode_id"] as? String
            if let kept {
                followUps.insert(path)
                if self.place == place {
                    snapshot = try? await engine.call("snapshot", ["episode_id": kept, "path": path])
                    previewURL = nil
                }
            }
            present(result, in: place)
            try? await refreshSidebars()
            if kept != nil, thenShow, questionJob == nil {
                // The app from the folder it now lives in; only Nicoder's reply is added to the chat.
                let shown = try await engine.call("show_app", ["path": path, "text": ""], timeoutSeconds: 180)
                present(shown, in: place)
            }
        } catch {
            tell(error.localizedDescription, in: place)
        }
        startWaiting()  // a Keep that waited behind a change may hold others back
    }

    /// A reply that needs no model: shown when the window shows `place`, kept for when it is opened otherwise.
    private func tell(_ text: String, in place: Place, after prompt: String? = nil) {
        if self.place == place {
            if let prompt {
                appendLocalTurn(role: "user", text: prompt)
            }
            appendLocalTurn(role: "assistant", text: text)
        } else {
            notes[place] = text
            unseen.insert(place)
        }
    }

    /// Answer a question in its conversation. The model may open the project it means: the window follows when
    /// it still shows the question's conversation.
    private func ask(_ text: String, in place: Place, explain: Bool, files: [Attachment] = []) async {
        do {
            let result: [String: Any]
            if explain, let path = place.path {
                result = try await engine.call("explain_request", ["path": path, "text": text], timeoutSeconds: 1200)
            } else {
                var params: [String: Any] = ["text": text]
                switch place {
                case .project(let path): params["path"] = path
                case .chat(let id): params["chat_id"] = id
                }
                if !files.isEmpty {
                    params["attachments"] = files.map(\.payload)
                }
                result = try await engine.call("chat_request", params, timeoutSeconds: 1200)
            }
            let job = finishQuestion(cost: result["cost_usd"] as? Double)
            if let opened = result["open_project"] as? String, !opened.isEmpty {
                if let job, let record = traceRecords[job.place]?[job.prompt] {
                    traceRecords[.project(opened), default: [:]][job.prompt] = record
                }
                if self.place == place {
                    selectProject(opened)
                } else {
                    unseen.insert(.project(opened))
                }
            } else if self.place == place {
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
                    inspectorShown = true
                }
            } else {
                unseen.insert(place)
            }
            try? await refreshSidebars()
        } catch {
            finishQuestion(cost: nil)
            tell(error.localizedDescription, in: place)
            try? await refreshSidebars()  // the question was saved before the answer: its chat is listed
        }
        startWaiting()
    }

    @discardableResult
    private func finishQuestion(cost: Double?) -> Job? {
        guard let job = questionJob else { return nil }
        questionJob = nil
        record(job, cost: cost)
        syncRunning()
        return job
    }

    /// Make a change in its project. Without a project the message names one ("add X to vc-jobs-board") or a new
    /// one is made for it, and the chat moves into it; the window follows when it still shows that chat.
    private func change(_ text: String, in place: Place, project named: String?, files: [Attachment] = []) async {
        do {
            var path = place.path
            if path == nil {
                if let named, !named.isEmpty {
                    path = named
                } else {
                    noteStatus("Creating a project from what you wrote.")
                    var params: [String: Any] = ["text": text.isEmpty ? turnText(text, files) : text]
                    if case .chat(let id) = place {
                        params["chat_id"] = id
                    }
                    let created = try await engine.call("start_chat", params)
                    path = created["path"] as? String
                    if self.place == place {
                        banner = created["detail"] as? String
                    }
                }
            }
            guard let path else {
                throw NSError(domain: "Nicoder", code: 1, userInfo: [
                    NSLocalizedDescriptionKey: "Nicoder could not create a project from that message.",
                ])
            }
            let target = Place.project(path)
            if target != place, var job = changeJob {
                // The work belongs to the project now.
                job = Job(place: target, prompt: job.prompt, started: job.started, steps: job.steps)
                changeJob = job
                if self.place == place {
                    let note = banner
                    selectProject(path)
                    banner = note
                }
                try? await refreshSidebars()
            }
            if self.place == target {
                snapshot = nil
                previewURL = nil
                inspectorShown = false
            }
            var request: [String: Any] = [
                "path": path,
                "text": text,
                "profile": profile,
                "engine": engineName,
                "kind": followUps.contains(path) ? "follow_up" : "create",
            ]
            if !files.isEmpty {
                request["attachments"] = files.map(\.payload)
            }
            let started = try await engine.call("run_request", request)
            let episode = started["episode_id"] as? String
            changeJob?.episode = episode
            if let episode, let answered = endedEarly.removeValue(forKey: episode) {
                finishChange(requestID: episode, answered: answered)
            }
            endedEarly = [:]
        } catch {
            failChange(error.localizedDescription)
        }
    }

    /// Stop the work running in the conversation the window shows; work elsewhere goes on.
    func stop() {
        guard let job = jobHere else { return }
        let what = changeJob?.place == job.place ? "run" : "chat"
        Task {
            _ = try? await engine.call("cancel", ["what": what])
            progress = "Stopping…"
        }
    }

    /// Take back a message that waits for its turn.
    func dropWaiting(_ id: UUID) {
        waiting.removeAll { $0.id == id }
    }

    /// Start the first waiting message whose kind of work is free, in a conversation where nothing runs (a change
    /// that starts a project moves its chat: a question still running there would land in an emptied chat).
    private func startWaiting() {
        guard let index = waiting.firstIndex(where: {
            ($0.change ? changeJob == nil : questionJob == nil) && !isWorking($0.place)
        }) else {
            return
        }
        let item = waiting.remove(at: index)
        Task { await send(item.text, in: item.place, files: item.files) }
    }

    private func syncRunning() {
        running = changeJob != nil || questionJob != nil
    }

    private func record(_ job: Job, cost: Double?) {
        guard !job.steps.isEmpty || cost != nil else { return }
        traceRecords[job.place, default: [:]][job.prompt] = TraceRecord(
            steps: job.steps, started: job.started, ended: Date(), cost: cost
        )
    }

    /// Snapshots and the UI demo only: a run (in the conversation the window shows unless told), no engine call.
    func showDemoRun(prompt: String, started: Date, steps: [TraceStep], in where: Place? = nil) {
        changeJob = Job(place: `where` ?? place, prompt: prompt, started: started, steps: steps)
        syncRunning()
    }

    /// Snapshots only: the finished work behind an answer in the conversation the window shows.
    func showDemoTrace(_ record: TraceRecord, for prompt: String) {
        traceRecords[place, default: [:]][prompt] = record
    }

    /// Snapshots only: results not seen yet, and messages waiting for their turn.
    func showDemoQueue(done: [Place], waiting items: [(Place, String)]) {
        unseen = Set(done)
        waiting = items.map { Waiting(place: $0.0, text: $0.1, change: true) }
    }

    func endDemoRun() {
        changeJob = nil
        questionJob = nil
        waiting = []
        unseen = []
        syncRunning()
    }

    func keep() {
        guard decisionWaitsFor == nil, let path = projectPath, let episode = snapshot?["episode_id"] as? String else {
            return
        }
        let shown = place
        Task {
            do {
                _ = try await engine.call("keep", ["episode_id": episode, "path": path])
                followUps.insert(path)
                let result = try await engine.call("snapshot", ["episode_id": episode, "path": path])
                if place == shown {
                    snapshot = result
                    previewURL = nil
                    banner = "The changes are in your project now. Ask for the next one."
                    inspectorShown = false
                }
                try await refreshSidebars()
            } catch {
                if place == shown {
                    banner = error.localizedDescription
                }
            }
        }
    }

    func discard() {
        guard decisionWaitsFor == nil, let episode = snapshot?["episode_id"] as? String else { return }
        let shown = place
        Task {
            do {
                var params: [String: Any] = ["episode_id": episode]
                if let path = projectPath {
                    params["path"] = path
                    followUps.remove(path)
                }
                _ = try await engine.call("discard", params)
                let result = try await engine.call("snapshot", params)
                if place == shown {
                    snapshot = result
                    previewURL = nil
                    banner = "Discarded. Your project was not changed."
                    inspectorShown = false
                }
                try await refreshSidebars()
            } catch {
                if place == shown {
                    banner = error.localizedDescription
                }
            }
        }
    }

    func openHistory(_ row: HistoryRow) {
        // The last row's notice does not belong over this one.
        banner = nil
        let shown = place
        Task {
            do {
                var params: [String: Any] = ["episode_id": row.episodeID]
                if let path = projectPath {
                    params["path"] = path
                }
                let result = try await engine.call("snapshot", params)
                guard place == shown else { return }
                snapshot = result
                inspectorShown = hasChanges
                // A kept request keeps its files and diff, so it has changes to show and never reaches the notice.
                if !hasChanges {
                    switch snapshot?["outcome"] as? String {
                    case "discarded": banner = "This request was discarded. Your project was not changed."
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

    /// Let change runs run commands on the Mac itself, outside the sandbox, or keep them all in it.
    func setMacAccess(_ on: Bool) {
        guard on != macAccess else { return }
        macAccess = on
        Task {
            do {
                _ = try await engine.call("set_mac_access", ["on": on])
            } catch {
                macAccess = !on
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
        if let mac = payload?["mac_access"] as? Bool {
            macAccess = mac
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
            showDemoRun(prompt: "Show the working indicator", started: Date(), steps: [
                TraceStep(tool: "list", detail: "Looking in ~/projects", target: "~/projects", ok: true, started: Date()),
            ])
        default: break
        }
    }

    // MARK: Events and the live trace

    private func noteStatus(_ phrase: String) {
        guard !phrase.isEmpty else { return }
        progress = phrase
        recordStep(tool: "status", detail: phrase, target: "", ok: true, fromChat: false)
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
        // A question's steps come tagged; the rest belong to the change.
        let fromChat = event.payload["source"] as? String == "chat"
        if let phrase = event.phrase, !phrase.isEmpty {
            progress = phrase
            if !Self.quietEvents.contains(event.type) {
                let data = event.payload["data"] as? [String: Any] ?? [:]
                recordStep(
                    tool: data["tool"] as? String ?? (event.type == "activity" ? "status" : event.type),
                    detail: phrase,
                    target: data["target"] as? String ?? "",
                    ok: data["ok"] as? Bool ?? true,
                    fromChat: fromChat,
                    command: data["command"] as? String ?? "",
                    running: data["running"] as? Bool ?? false,
                    call: data["call"] as? String ?? "",
                    mac: data["mac"] as? Bool ?? false
                )
            }
        }
        if fromChat {
            return  // a question ends when its answer comes back
        }
        if event.type == "app_error" {
            let nested = event.payload["data"] as? [String: Any]
            let detail = (event.payload["detail"] as? String)
                ?? (nested?["detail"] as? String)
                ?? event.phrase
                ?? "The request failed."
            failChange(detail.isEmpty ? "The request failed." : detail)
        }
        // "budget" is not an end: the run still saves its result and then sends "done".
        if event.type == "handoff_written" || event.type == "cancelled"
            || event.type == "done" || event.type == "stopped" {
            let data = event.payload["data"] as? [String: Any]
            finishChange(requestID: data?["request_id"] as? String, answered: data?["answered"] as? Bool == true)
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

    private func recordStep(tool: String, detail: String, target: String, ok: Bool, fromChat: Bool,
                            command: String = "", running: Bool = false, call: String = "", mac: Bool = false) {
        let lowered = detail.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if Self.vaguePhrases.contains(where: { lowered == $0 || lowered.hasPrefix($0) }) { return }
        let step = TraceStep(tool: tool, detail: detail, target: target, ok: ok, started: Date(),
                             command: command, running: running, call: call, mac: mac)
        if fromChat {
            questionJob?.add(step)
        } else {
            changeJob?.add(step)
        }
    }

    /// The change failed before it could finish: say so where it ran.
    private func failChange(_ detail: String) {
        guard let job = changeJob, !finishingChange else { return }
        changeJob = nil
        record(job, cost: nil)
        syncRunning()
        tell(detail, in: job.place)
        startWaiting()
    }

    /// The change ended (done, stopped, out of steps or time): its result shows in its project, now when the
    /// window shows it, or when the project is opened next (the sidebar says "done"). `answered`: the project read
    /// the message as a question and answered it in its chat.
    private func finishChange(requestID: String?, answered: Bool) {
        guard let job = changeJob, !finishingChange else { return }
        if let requestID {
            guard let episode = job.episode else {
                // It ended before its start was answered, or it is a late event of the run before this one.
                endedEarly[requestID] = answered
                return
            }
            guard requestID == episode else { return }
        }
        guard let episode = job.episode, let path = job.place.path else {
            changeJob = nil
            record(job, cost: nil)
            syncRunning()
            startWaiting()
            return
        }
        finishingChange = true
        Task {
            var cost: Double?
            do {
                if answered {
                    if place == job.place {
                        try await loadChat(for: path)
                    } else {
                        unseen.insert(job.place)
                    }
                } else {
                    let result = try await engine.call("snapshot", ["episode_id": episode, "path": path],
                                                       timeoutSeconds: 60)
                    cost = result["cost_usd"] as? Double
                    if place == job.place {
                        snapshot = result
                        if let preview = result["preview"] as? [String: Any],
                           let url = preview["url"] as? String, !url.isEmpty,
                           preview["ok"] as? Bool != false {
                            previewURL = url
                        } else {
                            previewURL = nil
                        }
                        inspectorShown = hasChanges || previewURL != nil
                        try await loadChat(for: path)
                        let reply = (result["model_commentary"] as? String) ?? (result["summary"] as? String)
                        if let reply, !reply.isEmpty, place == job.place {
                            appendLocalTurn(role: "assistant", text: reply)
                        }
                    } else {
                        unseen.insert(job.place)
                    }
                }
                try await refreshSidebars()
            } catch {
                tell(error.localizedDescription, in: job.place)
            }
            record(changeJob ?? job, cost: cost)
            changeJob = nil
            finishingChange = false
            syncRunning()
            startWaiting()
        }
    }

    private func refreshSidebars() async throws {
        let prefs = try await engine.call("prefs")
        recent = Self.parseRecent(prefs["recent_projects"])
        if let listed = try? await engine.call("list_chats") {
            chats = Self.parseChats(listed)  // a slow answer keeps the list it had
        }
        if let path = projectPath {
            let payload = try await engine.call("history", ["path": path])
            guard projectPath == path else { return }
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
        guard projectPath == path else { return }
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

    private static func parseChats(_ payload: [String: Any]) -> [SavedChat] {
        let rows = payload["chats"] as? [[String: Any]] ?? []
        return rows.compactMap { row in
            guard let id = row["id"] as? String else { return nil }
            let updated = (row["updated"] as? Double).map { Date(timeIntervalSince1970: $0) }
            return SavedChat(id: id, title: row["title"] as? String ?? "chat", updated: updated)
        }
    }

    private static func parseRecent(_ raw: Any?) -> [RecentProject] {
        guard let rows = raw as? [[String: Any]] else { return [] }
        return rows.compactMap { row in
            guard let path = row["path"] as? String else { return nil }
            let active = (row["active"] as? Double).map { Date(timeIntervalSince1970: $0) }
            return RecentProject(path: path, name: row["name"] as? String ?? URL(fileURLWithPath: path).lastPathComponent,
                                 active: active)
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
