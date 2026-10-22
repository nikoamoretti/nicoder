import Foundation

enum EngineError: LocalizedError {
    case notStarted
    case helperFailed(String)
    case badResponse(String)
    case pythonMissing

    var errorDescription: String? {
        switch self {
        case .notStarted:
            return "The Local Coder helper is not running."
        case .helperFailed(let detail):
            return detail
        case .badResponse(let detail):
            return detail
        case .pythonMissing:
            return "Python 3.11+ was not found. Install Python at /usr/local/bin/python3, then reopen the app."
        }
    }
}

struct EngineEvent {
    let type: String
    let phrase: String?
    let payload: [String: Any]
}

/// JSON-lines client for `python -m local_coder.app_rpc`.
final class EngineClient {
    private var process: Process?
    private var stdinHandle: FileHandle?
    private let lock = NSLock()
    private var buffer = Data()
    private var nextID = 1
    private var pending: [String: (Result<[String: Any], Error>) -> Void] = [:]
    var onEvent: ((EngineEvent) -> Void)?

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start() throws {
        if process?.isRunning == true {
            return
        }
        let python = try Self.resolvePython()
        let process = Process()
        process.executableURL = python
        process.arguments = ["-m", "local_coder.app_rpc"]
        process.environment = Self.helperEnvironment()
        process.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser

        let stdin = Pipe()
        let stdout = Pipe()
        let stderr = Pipe()
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = stderr
        stdinHandle = stdin.fileHandleForWriting

        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.consume(handle.availableData)
        }
        stderr.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if !data.isEmpty, let text = String(data: data, encoding: .utf8), !text.isEmpty {
                FileHandle.standardError.write(data)
            }
        }
        try process.run()
        self.process = process
    }

    func call(_ method: String, _ params: [String: Any] = [:], timeoutSeconds: Double = 12) async throws -> [String: Any] {
        try start()
        let id: String = lock.withLock {
            nextID += 1
            return String(nextID)
        }
        let payload: [String: Any] = ["id": id, "method": method, "params": params]
        let data = try JSONSerialization.data(withJSONObject: payload)
        guard var line = String(data: data, encoding: .utf8) else {
            throw EngineError.badResponse("Could not encode a helper request.")
        }
        line.append("\n")
        return try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            pending[id] = { continuation.resume(with: $0) }
            lock.unlock()
            do {
                try stdinHandle?.write(contentsOf: Data(line.utf8))
            } catch {
                lock.lock()
                pending.removeValue(forKey: id)
                lock.unlock()
                continuation.resume(throwing: error)
                return
            }
            Task {
                try await Task.sleep(nanoseconds: UInt64(timeoutSeconds * 1_000_000_000))
                self.lock.lock()
                let callback = self.pending.removeValue(forKey: id)
                self.lock.unlock()
                callback?(.failure(EngineError.helperFailed("The local engine did not answer in time.")))
            }
        }
    }

    func shutdown() {
        if process?.isRunning == true {
            let payload = "{\"id\":\"0\",\"method\":\"shutdown\",\"params\":{}}\n"
            try? stdinHandle?.write(contentsOf: Data(payload.utf8))
            // The helper serves until its stdin closes; a busy one gets three seconds, then SIGTERM.
            try? stdinHandle?.close()
            let deadline = Date().addingTimeInterval(3)
            while process?.isRunning == true && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.05)
            }
            if process?.isRunning == true {
                process?.terminate()
            }
            process?.waitUntilExit()
        }
        process = nil
        stdinHandle = nil
    }

    private func consume(_ data: Data) {
        guard !data.isEmpty else { return }
        lock.lock()
        buffer.append(data)
        let newline = Data([UInt8(ascii: "\n")])
        while let range = buffer.range(of: newline) {
            let line = buffer.subdata(in: buffer.startIndex..<range.lowerBound)
            buffer.removeSubrange(buffer.startIndex..<range.upperBound)
            process(line: line)
        }
        lock.unlock()
    }

    private func process(line: Data) {
        guard !line.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
            return
        }
        if let event = object["event"] as? String, event == "progress" {
            let payload = object["payload"] as? [String: Any] ?? [:]
            let type = payload["type"] as? String ?? "progress"
            let phrase = payload["phrase"] as? String
            DispatchQueue.main.async {
                self.onEvent?(EngineEvent(type: type, phrase: phrase, payload: payload))
            }
            return
        }
        let id: String?
        if let value = object["id"] as? String {
            id = value
        } else if let value = object["id"] as? Int {
            id = String(value)
        } else {
            id = nil
        }
        guard let id else {
            return
        }
        let callback = pending.removeValue(forKey: id)
        if let ok = object["ok"] as? Bool, ok {
            callback?(.success(object["result"] as? [String: Any] ?? [:]))
        } else {
            let error = object["error"] as? [String: Any]
            let detail = error?["detail"] as? String ?? "The helper refused that request."
            callback?(.failure(EngineError.helperFailed(detail)))
        }
    }

    static func resolvePython() throws -> URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let durable = home.appendingPathComponent(".local/share/local-coder-venv/bin/python")
        if FileManager.default.isExecutableFile(atPath: durable.path) {
            try upgradeFromBundleIfNeeded(python: durable)
            return durable
        }
        let system = URL(fileURLWithPath: "/usr/local/bin/python3")
        guard FileManager.default.isExecutableFile(atPath: system.path) else {
            throw EngineError.pythonMissing
        }
        try bootstrapVenv(python: system, destination: durable.deletingLastPathComponent().deletingLastPathComponent())
        return durable
    }

    static func bundledWheel() -> URL? {
        Bundle.main.resourceURL?.appendingPathComponent("local_coder-0.4.0-py3-none-any.whl")
    }

    static func bundledDockerDir() -> URL? {
        let url = Bundle.main.resourceURL?.appendingPathComponent("docker")
        if let url, FileManager.default.fileExists(atPath: url.path) {
            return url
        }
        return nil
    }

    private static func helperEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let extras = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/Applications/Docker.app/Contents/Resources/bin",
            NSHomeDirectory() + "/.local/bin",
        ]
        let path = (extras + (env["PATH"] ?? "").split(separator: ":").map(String.init))
            .filter { !$0.isEmpty }
        env["PATH"] = path.joined(separator: ":")
        env["HOME"] = NSHomeDirectory()
        env["TMPDIR"] = env["TMPDIR"] ?? NSTemporaryDirectory()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env
    }

    private static func bootstrapVenv(python: URL, destination: URL) throws {
        try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
        let create = Process()
        create.executableURL = python
        create.arguments = ["-m", "venv", destination.path]
        try create.run()
        create.waitUntilExit()
        guard create.terminationStatus == 0 else {
            throw EngineError.helperFailed("Could not create the Local Coder Python environment.")
        }
        try installWheel(python: destination.appendingPathComponent("bin/python"))
    }

    private static func upgradeFromBundleIfNeeded(python: URL) throws {
        guard FileManager.default.fileExists(atPath: bundledWheel()?.path ?? "") else { return }
        if installedVersion(python: python) == "0.4.0" {
            return
        }
        try installWheel(python: python)
    }

    private static func installedVersion(python: URL) -> String? {
        let process = Process()
        process.executableURL = python
        process.arguments = ["-c", "import local_coder; print(local_coder.__version__)"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return nil
        }
        guard process.terminationStatus == 0 else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func installWheel(python: URL) throws {
        guard let wheel = bundledWheel(), FileManager.default.fileExists(atPath: wheel.path) else {
            return
        }
        let pip = Process()
        pip.executableURL = python
        pip.arguments = ["-m", "pip", "install", "--upgrade", wheel.path]
        pip.environment = helperEnvironment()
        try pip.run()
        pip.waitUntilExit()
        if pip.terminationStatus != 0 {
            throw EngineError.helperFailed("Could not install the Local Coder engine into the app environment.")
        }
    }
}

private extension NSLock {
    func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}
