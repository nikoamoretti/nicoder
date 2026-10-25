// Renders the app icon (the meditating hominin) and the Dock's two poses from the app's own drawing code.
// Usage: macos/scripts/make_icon.sh  (writes macos/AppIcon.icns)
import AppKit
import SwiftUI

@MainActor
func render(_ pose: DockTileView.Pose, size: CGFloat, to url: URL) {
    let renderer = ImageRenderer(content: DockTileView(pose: pose).frame(width: size, height: size))
    renderer.scale = 1
    guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiff),
          let png = bitmap.representation(using: .png, properties: [:]) else {
        fatalError("could not render \(url.lastPathComponent)")
    }
    do { try png.write(to: url) } catch { fatalError("\(error)") }
}

let out = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
MainActor.assumeIsolated {
    render(.meditating, size: 1024, to: out.appendingPathComponent("icon-1024.png"))
    render(.walking(phase: 0.9), size: 256, to: out.appendingPathComponent("dock-walking.png"))
    render(.meditating, size: 256, to: out.appendingPathComponent("dock-meditating.png"))
}
