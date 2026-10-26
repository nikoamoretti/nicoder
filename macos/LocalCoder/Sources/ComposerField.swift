import AppKit
import SwiftUI

/// Multiline composer: Enter sends, Shift+Enter inserts a newline. A pasted or dropped image or file becomes an
/// attachment instead of text.
struct ComposerField: NSViewRepresentable {
    @Binding var text: String
    var isEnabled: Bool = true
    var onSubmit: () -> Void
    var onAttachFiles: ([URL]) -> Void = { _ in }
    var onAttachImage: (NSImage) -> Void = { _ in }

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        let textView = AttachingTextView(frame: NSRect(origin: .zero, size: scroll.contentSize))
        textView.minSize = .zero
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.containerSize = NSSize(width: scroll.contentSize.width,
                                                       height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = true
        textView.onAttachFiles = { context.coordinator.parent.onAttachFiles($0) }
        textView.onAttachImage = { context.coordinator.parent.onAttachImage($0) }
        scroll.documentView = textView
        textView.delegate = context.coordinator
        textView.isRichText = false
        textView.drawsBackground = false
        textView.allowsUndo = true
        textView.font = NSFont.monospacedSystemFont(ofSize: 13, weight: .regular)
        textView.insertionPointColor = NSColor(Console.green)
        textView.textContainerInset = NSSize(width: 2, height: 6)
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.string = text
        textView.setAccessibilityIdentifier("requestEditor")
        context.coordinator.textView = textView
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let textView = scroll.documentView as? NSTextView else { return }
        context.coordinator.parent = self
        textView.isEditable = isEnabled
        textView.isSelectable = true
        if textView.string != text {
            let selected = textView.selectedRanges
            textView.string = text
            textView.selectedRanges = selected
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ComposerField
        weak var textView: NSTextView?

        init(_ parent: ComposerField) {
            self.parent = parent
        }

        func textDidChange(_ notification: Notification) {
            guard let textView = notification.object as? NSTextView else { return }
            parent.text = textView.string
        }

        func textView(_ textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
            if commandSelector == #selector(NSResponder.insertNewline(_:)) {
                let flags = NSApp.currentEvent?.modifierFlags ?? []
                if flags.contains(.shift) {
                    textView.insertNewlineIgnoringFieldEditor(nil)
                    parent.text = textView.string
                    return true
                }
                // Flush the NSTextView into the binding before submit — Enter does
                // not fire textDidChange, so requestText can still be empty otherwise.
                parent.text = textView.string
                parent.onSubmit()
                return true
            }
            return false
        }
    }
}

/// The composer's text view: files and images on the pasteboard (⌘V) or dropped on it are attached, not typed.
final class AttachingTextView: NSTextView {
    var onAttachFiles: ([URL]) -> Void = { _ in }
    var onAttachImage: (NSImage) -> Void = { _ in }

    override func paste(_ sender: Any?) {
        if attach(from: NSPasteboard.general) { return }
        super.paste(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        if attach(from: sender.draggingPasteboard) { return true }
        return super.performDragOperation(sender)
    }

    /// Files first (a copied Finder file also carries its icon as an image), then a bare image; plain text is left
    /// to the text view.
    private func attach(from pasteboard: NSPasteboard) -> Bool {
        if let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL],
           !urls.isEmpty {
            onAttachFiles(urls)
            return true
        }
        let imageTypes: [NSPasteboard.PasteboardType] = [.png, .tiff]
        if pasteboard.availableType(from: imageTypes) != nil, pasteboard.string(forType: .string) == nil,
           let image = NSImage(pasteboard: pasteboard) {
            onAttachImage(image)
            return true
        }
        return false
    }
}
