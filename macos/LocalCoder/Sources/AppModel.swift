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
    @Published var showSettings = false

    private let engine = EngineClient()
    private var activeEpisode: String?
    private var quitting = false

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

    var changedFiles: [String] {
        ((snapshot?["facts"] as? [String: Any])?["files"] as? [String]) ?? []
    }

    var complete: Bool {
        ((snapshot?["facts"] as? [String: Any])?["complete"] as? Bool) ?? false
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
                recent = Self.parseRecent(prefs["recent_projects"])
                readiness = try await engine.call("readiness")
                ready = true
            } catch {
                startupError = error.localizedDescription
                let log = FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent("Library/Logs/local-coder-app.log")
                try? (error.localizedDescription + "\n").write(to: log, atomically: true, encoding: .utf8)
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
        snapshot = nil
        requestText = ""
        Task {
            do {
                inspection = try await engine.call("inspect_project", ["path": path])
                try await refreshSidebars()
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func submitRequest() {
        let text = requestText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let path = projectPath, !text.isEmpty else { return }
        banner = nil
        explainSummary = nil
        explainHits = []
        Task {
            do {
                let classified = try await engine.call("classify_request", ["path": path, "text": text])
                let kind = classified["kind"] as? String
                if kind == "unsupported" {
                    banner = classified["summary"] as? String
                    return
                }
                if kind == "explain" {
                    let result = try await engine.call("explain_request", ["path": path, "text": text])
                    explainSummary = result["summary"] as? String
                    explainHits = result["hits"] as? [[String: Any]] ?? []
                    return
                }
                running = true
                progress = "Starting the local coding engine."
                snapshot = nil
                let started = try await engine.call("run_request", [
                    "path": path,
                    "text": text,
                    "profile": profile,
                ])
                activeEpisode = started["episode_id"] as? String
            } catch {
                running = false
                banner = error.localizedDescription
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
                snapshot = try await engine.call("snapshot", ["episode_id": episode])
                banner = "Changes were kept in the project."
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
                _ = try await engine.call("discard", ["episode_id": episode])
                snapshot = try await engine.call("snapshot", ["episode_id": episode])
                banner = "Proposed changes were discarded. The original project is unchanged."
                try await refreshSidebars()
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func openHistory(_ row: HistoryRow) {
        Task {
            do {
                snapshot = try await engine.call("snapshot", ["episode_id": row.episodeID])
                requestText = (snapshot?["ticket"] as? String) ?? row.request
                explainSummary = nil
            } catch {
                banner = error.localizedDescription
            }
        }
    }

    func refreshReadiness() {
        Task {
            readiness = try? await engine.call("readiness")
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
            _ = try? await engine.call("save_prefs", ["profile": profile])
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

    private func handle(_ event: EngineEvent) {
        if let phrase = event.phrase, !phrase.isEmpty {
            progress = phrase
        }
        if event.type == "app_error" {
            running = false
            banner = event.payload["detail"] as? String ?? "The request failed."
        }
        if event.type == "handoff_written" || event.type == "cancelled" {
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
                snapshot = try await engine.call("snapshot", ["episode_id": episode])
                progress = (snapshot?["progress"] as? [String: Any])?["phrase"] as? String ?? progress
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
