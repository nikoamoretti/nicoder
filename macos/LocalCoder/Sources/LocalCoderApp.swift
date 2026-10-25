import AppKit
import SwiftUI

@main
struct LocalCoderApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        Window("Local Coder", id: "main") {
            WindowRoot()
                .environmentObject(model)
        }
        .defaultSize(width: 1220, height: 800)
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Chat") { model.newChat() }
                    .keyboardShortcut("n")
                    .disabled(model.running)
                Button("New Project…") { model.newProject() }
                    .keyboardShortcut("n", modifiers: [.command, .shift])
                    .disabled(model.running)
                Button("Open Project…") { model.openProject() }
                    .keyboardShortcut("o")
                    .disabled(model.running)
            }
            CommandMenu("Model") {
                Button("All OrcaRouter Models…") { model.showBrowser = true }
                    .keyboardShortcut("m", modifiers: [.command, .shift])
                Button(model.orcaConnected ? "OrcaRouter Is Connected" : "Connect OrcaRouter…") {
                    model.showConnect = true
                }
                .disabled(model.orcaConnected)
            }
            InspectorCommands()
        }

        Settings {
            SettingsView()
                .environmentObject(model)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        let log = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/local-coder-app.log")
        try? "didFinishLaunching\n".write(to: log, atomically: true, encoding: .utf8)
        AppModel.shared.start()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        AppModel.shared.prepareToQuit {
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}
