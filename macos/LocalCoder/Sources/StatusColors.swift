import AppKit
import SwiftUI

/// Green and red for text. System green is 2.2:1 and system red 3.6:1 on a light window, too faint for a line count
/// or a label. In light mode these are the system colors darkened toward black (so they follow the system's own hue and
/// its Increase Contrast variants) to 4.5:1 or more; in dark mode they are the system colors as they were. Shapes and
/// icons keep `.green` and `.red`.
extension ShapeStyle where Self == Color {
    /// Text that says something passed, was added or is connected.
    static var passText: Color { StatusText.pass }
    /// Text that says something failed or was removed.
    static var failText: Color { StatusText.fail }
}

private enum StatusText {
    static let pass = Color(nsColor: lightDarkened(.systemGreen, by: 0.53))
    static let fail = Color(nsColor: lightDarkened(.systemRed, by: 0.36))

    private static func lightDarkened(_ system: NSColor, by amount: CGFloat) -> NSColor {
        NSColor(name: nil) { appearance in
            guard appearance.bestMatch(from: [.aqua, .darkAqua]) == .aqua else { return system }
            var result = system
            appearance.performAsCurrentDrawingAppearance {
                result = system.usingColorSpace(.sRGB)?.blended(withFraction: amount, of: .black) ?? system
            }
            return result
        }
    }
}
