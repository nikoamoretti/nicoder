import SwiftUI

/// How one agent step reads in the log: an icon, a verb, and what it touched.
struct StepStyle {
    let symbol: String
    let verb: String
    let subject: String
    let isNote: Bool

    init(_ step: TraceStep) {
        let target = step.target.isEmpty ? Self.subject(from: step.detail) : step.target
        switch step.tool {
        case "find_project": (symbol, verb) = ("scope", "Looked for")
        case "list": (symbol, verb) = ("folder", "Looked in")
        case "glob": (symbol, verb) = ("doc.text.magnifyingglass", "Found")
        case "grep": (symbol, verb) = ("magnifyingglass", "Searched")
        case "read": (symbol, verb) = ("doc.text", "Read")
        case "write": (symbol, verb) = ("doc.badge.plus", "Wrote")
        case "edit": (symbol, verb) = ("pencil.line", "Edited")
        case "bash": (symbol, verb) = ("terminal", "Ran")
        case "open_project": (symbol, verb) = ("arrow.up.forward.app", "Opened")
        case "note": (symbol, verb) = ("text.bubble", "")
        default: (symbol, verb) = ("circle.dotted", "")
        }
        isNote = step.tool == "note"
        if verb.isEmpty {
            subject = step.detail
        } else if step.tool == "find_project" && !target.hasPrefix("“") {
            subject = "“\(target)”"
        } else {
            subject = target
        }
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
        HStack(alignment: .top, spacing: 10) {
            Rail(first: first, last: last, icon: AnyView(
                Image(systemName: step.ok ? style.symbol : "exclamationmark.triangle.fill")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(step.ok ? AnyShapeStyle(.secondary) : AnyShapeStyle(.orange))
                    .frame(width: 22, height: 22)
                    .background(Circle().fill(.quaternary.opacity(0.6)))
            ))
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                if style.isNote {
                    Text(style.subject)
                        .italic()
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                } else {
                    if !style.verb.isEmpty {
                        Text(style.verb)
                            .foregroundStyle(.secondary)
                    }
                    Text(style.subject)
                        .font(style.verb.isEmpty ? .callout : .system(.callout, design: .monospaced))
                        .foregroundStyle(step.ok ? .primary : .secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer(minLength: 12)
                Text(Durations.label(duration))
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
            .font(.callout)
            .padding(.top, 8)
            .padding(.bottom, 6)
            .help(step.detail)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

private struct ThinkingRow: View {
    let since: Date
    let first: Bool
    let label: String

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            // The walker overhangs the 22 pt rail so the timeline line still meets his head.
            Rail(first: first, last: true, icon: AnyView(
                HomininSpinner(size: 50)
                    .frame(width: 22, height: 50)
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
                .padding(.leading, 12)
            }
        }
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
                    Text("Working")
                        .font(.callout.weight(.semibold))
                    Text(Durations.clock(context.date.timeIntervalSince(start)))
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .contentTransition(.numericText())
                    if let name = model.selectedModel?.name {
                        Text(reasoningNote.isEmpty ? "· \(name)" : "· \(name) · \(reasoningNote)")
                            .font(.callout)
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                    Spacer()
                    Button("Stop") { model.stop() }
                        .controlSize(.small)
                }
            }
            TraceTimeline(steps: model.trace, start: start, live: true, waiting: waiting)
        }
        .padding(14)
        .background(.background.secondary, in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(.separator.opacity(0.5)))
        .accessibilityIdentifier("livingProgress")
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
                    Text(Self.summary(record))
                        .font(.callout.monospacedDigit())
                }
                .foregroundStyle(.secondary)
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
