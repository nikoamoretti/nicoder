import AppKit
import SwiftUI

@main
enum LocalCoderMain {
    static var delegate: AppDelegate?

    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        Self.delegate = delegate
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow?
    private var model: AppModel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let log = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/local-coder-app.log")
        try? "didFinishLaunching\n".write(to: log, atomically: true, encoding: .utf8)
        let model = AppModel()
        self.model = model
        let root = RootView()
            .environmentObject(model)
            .frame(minWidth: 920, minHeight: 640)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 980, height: 680),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Local Coder"
        window.titleVisibility = .visible
        window.titlebarAppearsTransparent = false
        window.backgroundColor = NSColor(calibratedRed: 0.96, green: 0.96, blue: 0.94, alpha: 1)
        let host = NSHostingController(rootView: root)
        host.view.frame = NSRect(x: 0, y: 0, width: 980, height: 680)
        window.contentViewController = host
        window.center()
        window.setFrameAutosaveName("LocalCoderMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        installMenu()
        model.start()
        self.window = window
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model else {
            return .terminateNow
        }
        model.prepareToQuit {
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    private func installMenu() {
        let main = NSMenu()
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "About Local Coder",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        )
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(
            withTitle: "Quit Local Coder",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        let appItem = NSMenuItem()
        appItem.submenu = appMenu
        main.addItem(appItem)

        let file = NSMenu(title: "File")
        let open = file.addItem(withTitle: "Open Project…", action: #selector(openProject(_:)), keyEquivalent: "o")
        open.target = self
        let created = file.addItem(withTitle: "New Project…", action: #selector(newProject(_:)), keyEquivalent: "n")
        created.target = self
        let fileItem = NSMenuItem()
        fileItem.submenu = file
        main.addItem(fileItem)
        NSApp.mainMenu = main
    }

    @objc private func openProject(_ sender: Any?) {
        Task { @MainActor in
            self.model?.openProject()
        }
    }

    @objc private func newProject(_ sender: Any?) {
        Task { @MainActor in
            self.model?.newProject()
        }
    }
}
