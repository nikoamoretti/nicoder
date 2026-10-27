import SwiftUI

/// How one agent step reads in the log: an icon, a verb, and what it touched.
struct StepStyle {
    let symbol: String
    let verb: String
    let subject: String
    let isNote: Bool
    /// A command shown as the model's words for it ("run the tests"), which carry their own verb (Nico, 2026-10-27:
    /// "something more descriptive or qualitative about what the model is doing" than "ran cd /Users/…").
    let isDescribed: Bool

    init(_ step: TraceStep) {
        let target = step.target.isEmpty ? Self.subject(from: step.detail) : step.target
        let described = step.tool == "bash" && !step.command.isEmpty && !step.target.isEmpty
        switch step.tool {
        case "find_project": (symbol, verb) = ("scope", "Looked for")
        case "list": (symbol, verb) = ("folder", "Looked in")
        case "glob": (symbol, verb) = ("doc.text.magnifyingglass", "Found")
        case "grep": (symbol, verb) = ("magnifyingglass", "Searched")
        case "read": (symbol, verb) = ("doc.text", "Read")
        case "write": (symbol, verb) = ("doc.badge.plus", "Wrote")
        case "edit": (symbol, verb) = ("pencil.line", "Edited")
        case "bash": (symbol, verb) = ("terminal", described ? "" : "Ran")
        case "open_project": (symbol, verb) = ("arrow.up.forward.app", "Opened")
        case "note": (symbol, verb) = ("text.bubble", "")
        default: (symbol, verb) = ("circle.dotted", "")
        }
        isNote = step.tool == "note"
        isDescribed = described
        if described {
            subject = Self.lowered(step.target)
        } else if verb.isEmpty {
            subject = step.detail
        } else if step.tool == "find_project" && !target.hasPrefix("“") {
            subject = "“\(target)”"
        } else {
            subject = target
        }
    }

    /// "Run the tests" as "run the tests", to sit with the lowercase verbs of the other steps ("README" stays).
    private static func lowered(_ text: String) -> String {
        guard let first = text.first, first.isUppercase, text.dropFirst().first?.isLowercase == true else { return text }
        return first.lowercased() + text.dropFirst()
    }

    /// The object of a phrase like "Reading src/app.js" when no target came with it.
    private static func subject(from detail: String) -> String {
        let words = detail.split(separator: " ", maxSplits: 1).map(String.init)
        return words.count == 2 ? words[1] : detail
    }
}

enum Durations {
    static func label(_ seconds: TimeInterval) -> String {
        if seconds < 1 { return String(format: "%.1fs", max(0, seconds)) }
        if seconds < 60 { return "\(Int(seconds.rounded()))s" }
        let total = Int(seconds.rounded())
        return "\(total / 60)m \(String(format: "%02d", total % 60))s"
    }

    static func clock(_ seconds: TimeInterval) -> String {
        let total = max(0, Int(seconds))
        return "\(total / 60):\(String(format: "%02d", total % 60))"
    }
}

/// The steps of one run on a hairline, each with the time it took to get there.
struct TraceTimeline: View {
    let steps: [TraceStep]
    let start: Date
    let live: Bool
    /// What the live row says while the model works on its next step.
    var waiting = "Working"

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(steps.enumerated()), id: \.element.id) { index, step in
                TraceRow(
                    step: step,
                    duration: step.started.timeIntervalSince(index == 0 ? start : steps[index - 1].started),
                    first: index == 0,
                    last: index == steps.count - 1 && !live
                )
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
            if live {
                ThinkingRow(since: steps.last?.started ?? start, first: steps.isEmpty, label: waiting)
            }
        }
        .animation(.snappy(duration: 0.25), value: steps.count)
    }
}

private struct Rail: View {
    let first: Bool
    let last: Bool
    let icon: AnyView

    var body: some View {
        VStack(spacing: 0) {
            Rectangle()
                .fill(.separator)
                .frame(width: 1, height: 5)
                .opacity(first ? 0 : 1)
            icon
            Rectangle()
                .fill(.separator)
                .frame(width: 1)
                .frame(maxHeight: .infinity)
                .opacity(last ? 0 : 1)
        }
        .frame(width: 22)
    }
}

private struct TraceRow: View {
    let step: TraceStep
    let duration: TimeInterval
    let first: Bool
    let last: Bool

    var body: some View {
        let style = StepStyle(step)
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(step.running ? "…" : step.ok ? "✓" : "!")
                .foregroundStyle(step.running ? Console.faint : step.ok ? Console.green : .orange)
            if style.isNote {
                Text(style.subject)
                    .foregroundStyle(Console.mute)
                    .lineLimit(2)
            } else {
                if !style.verb.isEmpty {
                    Text(style.verb.lowercased())
                        .foregroundStyle(Console.faint)
                }
                Text(style.subject)
                    .foregroundStyle(step.ok ? Console.mute : Console.faint)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 12)
            Text(Durations.label(duration))
                .foregroundStyle(Console.faint)
        }
        .font(Console.mono(12))
        .padding(.vertical, 2.5)
        .help(step.command.isEmpty ? step.detail : step.command)
        .fixedSize(horizontal: false, vertical: true)
    }
}

private struct ThinkingRow: View {
    let since: Date
    let first: Bool
    let label: String

    var body: some View {
        // The row is as tall as a step's: the same rail, the same text padding and the label in the steps' column. The
        // walker is bigger than his 22 pt slot and overhangs it, so his head reaches the end of the line from the step
        // above (that step's rail already runs to the top of this row) and his stick rises beside it.
        HStack(alignment: .top, spacing: 10) {
            Rail(first: true, last: true, icon: AnyView(
                Color.clear
                    .frame(width: 22, height: 22)
                    .overlay { HomininSpinner(size: 50).offset(y: 1.5) }
            ))
            TimelineView(.periodic(from: .now, by: 1)) { context in
                HStack(alignment: .firstTextBaseline) {
                    Text(first ? "Starting" : label)
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 12)
                    Text(Durations.label(context.date.timeIntervalSince(since)))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.tertiary)
                        .contentTransition(.numericText())
                }
                .font(.callout)
                .padding(.top, 8)
                .padding(.bottom, 6)
            }
        }
        // With no step above him his stick would touch the header's text.
        .padding(.top, first ? 4 : 0)
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// The run in progress: elapsed time, Stop, and every step as it lands.
struct LiveTrace: View {
    @EnvironmentObject private var model: AppModel

    /// "Thinking" only when the model reasons; with reasoning off it is writing its next step.
    private var waiting: String {
        guard model.selectedModel?.reasoningSwitch == true else { return "Working" }
        return model.reasoningOn ? "Thinking" : "Writing"
    }

    private var reasoningNote: String {
        model.selectedModel?.reasoningSwitch == true && !model.reasoningOn ? "reasoning off" : ""
    }

    var body: some View {
        let start = model.traceStart ?? Date()
        VStack(alignment: .leading, spacing: 6) {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                HStack(spacing: 8) {
                    Text("●")
                        .foregroundStyle(Console.green)
                    Text("working")
                        .foregroundStyle(Console.ink)
                    Text(Durations.clock(context.date.timeIntervalSince(start)))
                        .foregroundStyle(Console.mute)
                        .contentTransition(.numericText())
                    if let name = model.selectedModel?.name {
                        Text(reasoningNote.isEmpty ? "· \(name.lowercased())" : "· \(name.lowercased()) · \(reasoningNote)")
                            .foregroundStyle(Console.faint)
                            .lineLimit(1)
                    }
                    Spacer()
                    Button("stop") { model.stop() }
                        .controlSize(.small)
                }
                .font(Console.mono(12))
            }
            StepWheel(steps: model.trace, start: start, waiting: waiting)
                .padding(.leading, 2)
        }
        .padding(.vertical, 4)
        .accessibilityIdentifier("livingProgress")
    }
}

/// The run in progress, one action at a time: the walker keeps walking, the new action turns in from the right
/// and the one before turns out to the left, like a wheel (Nico, 2026-10-25). Every step stays in the summary
/// once the run ends.
struct StepWheel: View {
    let steps: [TraceStep]
    let start: Date
    /// What the row says while the model works on its first step.
    var waiting = "Working"

    var body: some View {
        let current = steps.last
        HStack(alignment: .center, spacing: 12) {
            HomininSpinner(size: 40)
                .frame(width: 30, height: 40)
            ZStack(alignment: .leading) {
                if let current {
                    WheelLabel(step: current)
                        .id(current.id)
                        .transition(.wheelTurn)
                } else {
                    Text(waiting.lowercased())
                        .foregroundStyle(Console.mute)
                        .id("waiting")
                        .transition(.wheelTurn)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .clipped()
            if !steps.isEmpty {
                Text("\(steps.count)")
                    .font(Console.mono(11))
                    .foregroundStyle(Console.faint)
                    .contentTransition(.numericText())
                    .help("\(steps.count) step\(steps.count == 1 ? "" : "s") so far")
            }
            TimelineView(.periodic(from: .now, by: 1)) { context in
                Text(Durations.label(context.date.timeIntervalSince(current?.started ?? start)))
                    .font(Console.mono(11))
                    .foregroundStyle(Console.faint)
                    .contentTransition(.numericText())
            }
        }
        .font(Console.mono(12.5))
        .frame(height: 40)
        .animation(.snappy(duration: 0.4), value: current?.id)
    }
}

/// One action on the wheel: its icon, verb and what it touched, on one line.
private struct WheelLabel: View {
    let step: TraceStep

    var body: some View {
        let style = StepStyle(step)
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            if style.isNote || (style.verb.isEmpty && !style.isDescribed) {
                Text(style.subject)
                    .foregroundStyle(Console.mute)
                    .lineLimit(1)
            } else {
                if !style.verb.isEmpty {
                    Text(style.verb.lowercased())
                        .foregroundStyle(step.ok ? Console.green.opacity(0.75) : .orange)
                }
                Text(step.running ? style.subject + "…" : style.subject)
                    .foregroundStyle(step.ok ? Console.green : .orange)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .help(step.command.isEmpty ? step.detail : step.command)
    }
}

/// A turn of the wheel: in from the right, out to the left, tipped a little as it goes.
private struct WheelTurn: ViewModifier {
    let angle: Double
    let shift: CGFloat
    let opacity: Double

    func body(content: Content) -> some View {
        content
            .rotation3DEffect(.degrees(angle), axis: (x: 0, y: 1, z: 0), perspective: 0.5)
            .offset(x: shift)
            .opacity(opacity)
    }
}

private extension AnyTransition {
    static var wheelTurn: AnyTransition {
        .asymmetric(
            insertion: .modifier(active: WheelTurn(angle: -35, shift: 90, opacity: 0),
                                 identity: WheelTurn(angle: 0, shift: 0, opacity: 1)),
            removal: .modifier(active: WheelTurn(angle: 35, shift: -90, opacity: 0),
                               identity: WheelTurn(angle: 0, shift: 0, opacity: 1))
        )
    }
}

/// One line that says what a finished run did; click to see every step.
struct TraceSummary: View {
    let record: TraceRecord
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                withAnimation(.snappy(duration: 0.22)) { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .bold))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                    Text("✓")
                        .foregroundStyle(Console.green)
                    Text(Self.summary(record).lowercased())
                }
                .font(Console.mono(12))
                .foregroundStyle(Console.mute)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                TraceTimeline(steps: record.steps, start: record.started, live: false)
                    .padding(.leading, 2)
                    .transition(.opacity)
            }
        }
    }

    static func summary(_ record: TraceRecord) -> String {
        var parts = ["Worked \(Durations.label(record.ended.timeIntervalSince(record.started)))"]
        let counts = Dictionary(grouping: record.steps, by: \.tool).mapValues(\.count)
        let reads = counts["read", default: 0]
        let searches = counts["grep", default: 0] + counts["glob", default: 0] + counts["find_project", default: 0]
        let edits = counts["edit", default: 0] + counts["write", default: 0]
        let commands = counts["bash", default: 0]
        if reads > 0 { parts.append("\(reads) file\(reads == 1 ? "" : "s") read") }
        if searches > 0 { parts.append("\(searches) search\(searches == 1 ? "" : "es")") }
        if edits > 0 { parts.append("\(edits) edit\(edits == 1 ? "" : "s")") }
        if commands > 0 { parts.append("\(commands) command\(commands == 1 ? "" : "s")") }
        if let cost = record.cost { parts.append(Self.cost(cost)) }
        return parts.joined(separator: " · ")
    }

    static func cost(_ value: Double) -> String {
        if value == 0 { return "free" }
        if value < 0.01 { return "<$0.01" }
        return String(format: "$%.2f", value)
    }
}
