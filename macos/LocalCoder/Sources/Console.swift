import AppKit
import SwiftUI

/// Nicoder's look, design C (Nico picked it on 2026-10-26): graphite, monospaced labels, the icon's green. Light mode
/// keeps the same type and green on pale paper. Answers stay in the system font, which reads better in paragraphs.
enum Console {
    static let background = shade(light: (0.965, 0.965, 0.955), dark: (0.07, 0.075, 0.072))
    static let sidebar = shade(light: (0.925, 0.925, 0.915), dark: (0.095, 0.10, 0.098))
    static let panel = shade(light: (1.0, 1.0, 1.0), dark: (0.11, 0.115, 0.112))
    static let line = shade(light: (0.82, 0.82, 0.80), dark: (0.20, 0.21, 0.205))
    static let ink = shade(light: (0.11, 0.11, 0.11), dark: (0.88, 0.88, 0.88))
    static let mute = shade(light: (0.38, 0.38, 0.38), dark: (0.60, 0.60, 0.60))
    static let faint = shade(light: (0.55, 0.55, 0.55), dark: (0.44, 0.44, 0.44))
    static let green = shade(light: (0.09, 0.47, 0.31), dark: (0.36, 0.77, 0.57))

    /// Labels, prompts, steps and paths.
    static func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    private static func shade(light: (CGFloat, CGFloat, CGFloat), dark: (CGFloat, CGFloat, CGFloat)) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let rgb = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: rgb.0, green: rgb.1, blue: rgb.2, alpha: 1)
        })
    }
}

/// A sidebar entry in the console style: the selected one is green with a "›".
struct ConsoleRow: View {
    let text: String
    var selected = false
    var size: CGFloat = 12.5
    /// Shown on the right, faint: when the row last had activity.
    var date: Date?

    var body: some View {
        HStack(spacing: 6) {
            Text(selected ? "›" : " ")
                .foregroundStyle(Console.green)
            Text(text)
                .foregroundStyle(selected ? Console.green : Console.mute)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 4)
            if let date {
                Text(Self.when(date))
                    .font(Console.mono(10.5))
                    .foregroundStyle(Console.faint)
                    .fixedSize()
            }
        }
        .font(Console.mono(size, selected ? .semibold : .regular))
        .padding(.vertical, 3)
        .contentShape(Rectangle())
    }

    /// "now", "12m", "3h", "yesterday", then "oct 23".
    static func when(_ date: Date, now: Date = Date()) -> String {
        let seconds = now.timeIntervalSince(date)
        if seconds < 60 { return "now" }
        if seconds < 3600 { return "\(Int(seconds / 60))m" }
        let calendar = Calendar.current
        if calendar.isDateInToday(date) { return "\(Int(seconds / 3600))h" }
        if calendar.isDateInYesterday(date) { return "yesterday" }
        return date.formatted(.dateTime.month(.abbreviated).day()).lowercased()
    }
}
