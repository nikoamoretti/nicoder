import SwiftUI

/// A model's answer as readable text: paragraphs, lists, quotes and code blocks, with inline
/// bold, italics, `code` and links.
struct ProseView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(Self.blocks(text).enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    enum Block: Hashable {
        case paragraph(String)
        case heading(String)
        case bullet(String, level: Int)
        case numbered(String, String, level: Int)
        case quote(String)
        case code(String)
    }

    @ViewBuilder
    private func view(for block: Block) -> some View {
        switch block {
        case .paragraph(let body):
            Text(Self.inline(body))
                .font(.system(size: 14))
                .lineSpacing(3.5)
                .fixedSize(horizontal: false, vertical: true)
        case .heading(let body):
            Text(Self.inline(body))
                .font(.system(size: 15, weight: .semibold))
                .padding(.top, 4)
        case .bullet(let body, let level):
            ListLine(marker: "•", content: Self.inline(body), level: level)
        case .numbered(let number, let body, let level):
            ListLine(marker: number, content: Self.inline(body), level: level)
        case .quote(let body):
            HStack(alignment: .top, spacing: 10) {
                RoundedRectangle(cornerRadius: 1)
                    .fill(.quaternary)
                    .frame(width: 3)
                Text(Self.inline(body))
                    .font(.system(size: 14))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .fixedSize(horizontal: false, vertical: true)
        case .code(let body):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(body)
                    .font(.system(size: 12, design: .monospaced))
                    .lineSpacing(2)
                    .padding(12)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.45), in: .rect(cornerRadius: 10))
        }
    }

    private struct ListLine: View {
        let marker: String
        let content: AttributedString
        let level: Int

        var body: some View {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(marker)
                    .font(.system(size: 14).monospacedDigit())
                    .foregroundStyle(.secondary)
                    .frame(minWidth: 14, alignment: .trailing)
                Text(content)
                    .font(.system(size: 14))
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.leading, CGFloat(level) * 18)
        }
    }

    static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }

    /// Split plain model text into blocks. Lines inside a paragraph keep their breaks.
    static func blocks(_ text: String) -> [Block] {
        var blocks: [Block] = []
        var paragraph: [String] = []
        var code: [String]?

        func flush() {
            if !paragraph.isEmpty {
                blocks.append(.paragraph(paragraph.joined(separator: "\n")))
                paragraph = []
            }
        }

        for raw in text.components(separatedBy: "\n") {
            let line = raw.replacingOccurrences(of: "\t", with: "    ")
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if let lines = code {
                    blocks.append(.code(lines.joined(separator: "\n")))
                    code = nil
                } else {
                    flush()
                    code = []
                }
                continue
            }
            if code != nil {
                code?.append(line)
                continue
            }
            if trimmed.isEmpty {
                flush()
                continue
            }
            let indent = line.prefix(while: { $0 == " " }).count / 2
            if let match = trimmed.firstMatch(of: #/^#{1,6}\s+(.+)$/#) {
                flush()
                blocks.append(.heading(String(match.1)))
            } else if let match = trimmed.firstMatch(of: #/^[-*•]\s+(.+)$/#) {
                flush()
                blocks.append(.bullet(String(match.1), level: min(indent, 3)))
            } else if let match = trimmed.firstMatch(of: #/^(\d{1,3})[.)]\s+(.+)$/#) {
                flush()
                blocks.append(.numbered("\(match.1).", String(match.2), level: min(indent, 3)))
            } else if let match = trimmed.firstMatch(of: #/^>\s?(.*)$/#) {
                flush()
                blocks.append(.quote(String(match.1)))
            } else {
                paragraph.append(trimmed)
            }
        }
        if let lines = code {
            blocks.append(.code(lines.joined(separator: "\n")))
        }
        flush()
        return blocks
    }
}
