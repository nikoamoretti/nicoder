import AppKit
import SwiftUI

/// A model's answer as readable text: paragraphs, lists, quotes and code blocks, with inline
/// bold, italics, `code` and links. One macOS text view holds the whole answer, so a selection runs across
/// paragraphs and ⌘C or the context menu copies it (Nico, 2026-10-25: "I cant copy anything from the app").
struct ProseView: View {
    let text: String

    var body: some View {
        ProseText(attributed: Self.attributed(text), whole: text)
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

    static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }

    /// The whole answer as one attributed string, in the sizes the answer always had.
    static func attributed(_ text: String) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let parts = blocks(text)
        for (index, block) in parts.enumerated() {
            out.append(rendered(block))
            if index < parts.count - 1 {
                out.append(NSAttributedString(string: "\n"))
            }
        }
        return out
    }

    private static func rendered(_ block: Block) -> NSAttributedString {
        switch block {
        case .paragraph(let body):
            return inlineText(lineBreaks(body), size: 14, style: paragraph(after: 10, lines: 3.5))
        case .heading(let body):
            return inlineText(body, size: 15, weight: .semibold, style: paragraph(before: 4, after: 8))
        case .bullet(let body, let level):
            return listLine(marker: "•", body: body, level: level)
        case .numbered(let number, let body, let level):
            return listLine(marker: number, body: body, level: level)
        case .quote(let body):
            return inlineText(lineBreaks(body), size: 14, color: .secondaryLabelColor,
                              style: paragraph(after: 10, lines: 3, indent: 14))
        case .code(let body):
            return NSAttributedString(string: lineBreaks(body), attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
                .foregroundColor: NSColor.labelColor,
                .backgroundColor: NSColor.textColor.withAlphaComponent(0.07),
                .paragraphStyle: paragraph(after: 10, lines: 2, indent: 10),
            ])
        }
    }

    /// Breaks inside one block stay inside its paragraph, so spacing falls only between blocks.
    private static func lineBreaks(_ text: String) -> String {
        text.replacingOccurrences(of: "\n", with: "\u{2028}")
    }

    private static func paragraph(before: CGFloat = 0, after: CGFloat, lines: CGFloat = 3,
                                  indent: CGFloat = 0) -> NSParagraphStyle {
        let style = NSMutableParagraphStyle()
        style.paragraphSpacingBefore = before
        style.paragraphSpacing = after
        style.lineSpacing = lines
        style.firstLineHeadIndent = indent
        style.headIndent = indent
        return style
    }

    private static func listLine(marker: String, body: String, level: Int) -> NSAttributedString {
        let indent = CGFloat(level) * 18
        let style = NSMutableParagraphStyle()
        style.tabStops = [NSTextTab(textAlignment: .left, location: indent + 22)]
        style.firstLineHeadIndent = indent
        style.headIndent = indent + 22
        style.lineSpacing = 3
        style.paragraphSpacing = 6
        let line = NSMutableAttributedString(string: "\(marker)\t", attributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 14, weight: .regular),
            .foregroundColor: NSColor.secondaryLabelColor,
        ])
        line.append(inlineText(lineBreaks(body), size: 14, style: style))
        line.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: line.length))
        return line
    }

    /// Inline markdown (bold, italics, `code`, links) in AppKit fonts.
    private static func inlineText(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular,
                                   color: NSColor = .labelColor, style: NSParagraphStyle) -> NSAttributedString {
        let parsed = inline(text)
        let out = NSMutableAttributedString()
        for run in parsed.runs {
            let intent = run.inlinePresentationIntent ?? []
            var font = NSFont.systemFont(ofSize: size, weight: intent.contains(.stronglyEmphasized) ? .semibold : weight)
            if intent.contains(.code) {
                font = .monospacedSystemFont(ofSize: size - 1.5, weight: .regular)
            }
            if intent.contains(.emphasized) {
                font = NSFontManager.shared.convert(font, toHaveTrait: .italicFontMask)
            }
            var attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color, .paragraphStyle: style]
            if intent.contains(.code) {
                attributes[.backgroundColor] = NSColor.textColor.withAlphaComponent(0.07)
            }
            if let link = run.link {
                attributes[.link] = link
            }
            out.append(NSAttributedString(string: String(parsed[run.range].characters), attributes: attributes))
        }
        return out
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

/// A read-only macOS text view sized to its text: select any part of an answer, copy it, or copy it all.
struct ProseText: NSViewRepresentable {
    let attributed: NSAttributedString
    let whole: String

    func makeNSView(context: Context) -> AnswerTextView {
        let view = AnswerTextView()
        view.isEditable = false
        view.isSelectable = true
        view.drawsBackground = false
        view.isRichText = true
        view.textContainerInset = .zero
        view.textContainer?.lineFragmentPadding = 0
        view.textContainer?.widthTracksTextView = true
        view.linkTextAttributes = [.foregroundColor: NSColor.linkColor, .cursor: NSCursor.pointingHand]
        view.textStorage?.setAttributedString(attributed)
        view.whole = whole
        return view
    }

    func updateNSView(_ view: AnswerTextView, context: Context) {
        // Only a new answer replaces the text, so a selection survives the window's other updates. Compared by the
        // answer's own text: a text view stores attributes its own way, so its contents never equal a fresh build,
        // and every update used to replace the text and drop the selection (Nico: "still cant copy paste").
        guard view.whole != whole else { return }
        view.whole = whole
        view.textStorage?.setAttributedString(attributed)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView view: AnswerTextView, context: Context) -> CGSize? {
        let width = max(1, proposal.width ?? 640)
        guard let container = view.textContainer, let layout = view.layoutManager else { return nil }
        container.containerSize = NSSize(width: width, height: .greatestFiniteMagnitude)
        layout.ensureLayout(for: container)
        return CGSize(width: width, height: ceil(layout.usedRect(for: container).height))
    }
}

/// The answer's text view, with "Copy Answer" above the usual Copy in its context menu.
final class AnswerTextView: NSTextView {
    var whole = ""

    override func menu(for event: NSEvent) -> NSMenu? {
        let menu = super.menu(for: event) ?? NSMenu()
        let item = NSMenuItem(title: "Copy Answer", action: #selector(copyAnswer), keyEquivalent: "")
        item.target = self
        menu.insertItem(item, at: 0)
        menu.insertItem(.separator(), at: 1)
        return menu
    }

    @objc private func copyAnswer() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(whole, forType: .string)
    }
}
