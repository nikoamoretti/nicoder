import AppKit
import PDFKit
import UniformTypeIdentifiers
import Vision

/// A file or image attached to the next message (Nico, 2026-10-26: "i need nicoder to be able to accept files and
/// images and be able to process them"). It is copied where the agent's tools can read it, with the text macOS can
/// take from it and, for an image, a smaller copy the model is shown.
struct Attachment: Identifiable {
    let id = UUID()
    let name: String
    /// "image", "pdf" or "file".
    let kind: String
    let staged: URL
    var text: URL?
    var preview: URL?
    var thumbnail: NSImage?

    /// What the engine gets (`local_coder.attachments.parse`).
    var payload: [String: Any] {
        ["path": staged.path, "name": name, "kind": kind, "text": text?.path ?? "", "preview": preview?.path ?? ""]
    }
}

enum Stager {
    /// The largest file that is copied (a bigger one is refused with one sentence).
    static let maxBytes = 200 * 1024 * 1024
    /// The longest side of the copy of an image the model is shown.
    static let previewSide: CGFloat = 1600

    /// `<data root>/attachments`, the folder the engine accepts attachments from.
    static var root: URL {
        if let override = ProcessInfo.processInfo.environment["LOCAL_CODER_DATA_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath).appendingPathComponent("attachments")
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/share/local-coder/attachments")
    }

    enum Problem: LocalizedError {
        case tooBig(String), unreadable(String), folder(String)
        var errorDescription: String? {
            switch self {
            case .tooBig(let name): return "\(name) is over 200 MB, so it was not attached."
            case .unreadable(let name): return "\(name) could not be read, so it was not attached."
            case .folder(let name): return "\(name) is a folder: open it as a project instead (open folder…)."
            }
        }
    }

    /// Copy a file the user chose, dropped or pasted, and take what macOS can read from it.
    static func stage(file source: URL) throws -> Attachment {
        let name = source.lastPathComponent
        let values = try? source.resourceValues(forKeys: [.fileSizeKey, .isDirectoryKey])
        if values?.isDirectory == true { throw Problem.folder(name) }
        if (values?.fileSize ?? 0) > maxBytes { throw Problem.tooBig(name) }
        let folder = root.appendingPathComponent(UUID().uuidString.lowercased(), isDirectory: true)
        let target = folder.appendingPathComponent(name)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try FileManager.default.copyItem(at: source, to: target)
        } catch {
            throw Problem.unreadable(name)
        }
        return finish(Attachment(name: name, kind: kind(of: target), staged: target))
    }

    /// An image with no file (pasted, or dragged out of a browser): saved as a PNG first.
    static func stage(image: NSImage, name: String = "pasted image.png") throws -> Attachment {
        guard let png = pngData(image) else { throw Problem.unreadable(name) }
        let folder = root.appendingPathComponent(UUID().uuidString.lowercased(), isDirectory: true)
        let target = folder.appendingPathComponent(name)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try png.write(to: target)
        } catch {
            throw Problem.unreadable(name)
        }
        return finish(Attachment(name: name, kind: "image", staged: target))
    }

    static func kind(of url: URL) -> String {
        guard let type = UTType(filenameExtension: url.pathExtension) else { return "file" }
        if type.conforms(to: .pdf) { return "pdf" }
        if type.conforms(to: .image) { return "image" }
        return "file"
    }

    private static func finish(_ item: Attachment) -> Attachment {
        var item = item
        switch item.kind {
        case "pdf":
            if let text = PDFDocument(url: item.staged)?.string?.trimmingCharacters(in: .whitespacesAndNewlines),
               !text.isEmpty {
                item.text = write(text, beside: item.staged)
            }
            item.thumbnail = NSWorkspace.shared.icon(forFile: item.staged.path)
        case "image":
            if let image = NSImage(contentsOf: item.staged) {
                item.thumbnail = image
                item.preview = smallerCopy(of: image, beside: item.staged)
                if let words = recognizedText(in: image), !words.isEmpty {
                    item.text = write(words, beside: item.staged)
                }
            }
        default:
            item.thumbnail = NSWorkspace.shared.icon(forFile: item.staged.path)
        }
        return item
    }

    private static func write(_ text: String, beside file: URL) -> URL? {
        let target = file.deletingLastPathComponent().appendingPathComponent(file.lastPathComponent + ".txt")
        return (try? text.write(to: target, atomically: true, encoding: .utf8)) == nil ? nil : target
    }

    /// A JPEG no longer than `previewSide` on its longest side: what the model is shown.
    private static func smallerCopy(of image: NSImage, beside file: URL) -> URL? {
        guard let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
        let width = CGFloat(cgImage.width), height = CGFloat(cgImage.height)
        let scale = min(1, previewSide / max(width, height))
        let size = NSSize(width: (width * scale).rounded(), height: (height * scale).rounded())
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height),
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSColor.white.setFill()
        NSRect(origin: .zero, size: size).fill()
        NSGraphicsContext.current?.cgContext.draw(cgImage, in: CGRect(origin: .zero, size: size))
        NSGraphicsContext.restoreGraphicsState()
        guard let jpeg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.82]) else { return nil }
        let target = file.deletingLastPathComponent().appendingPathComponent(file.lastPathComponent + ".preview.jpg")
        return (try? jpeg.write(to: target)) == nil ? nil : target
    }

    /// The words in an image, read on this Mac (Vision), line by line.
    private static func recognizedText(in image: NSImage) -> String? {
        guard let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return nil }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        try? VNImageRequestHandler(cgImage: cgImage).perform([request])
        let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        return lines.isEmpty ? nil : lines.joined(separator: "\n")
    }

    private static func pngData(_ image: NSImage) -> Data? {
        guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) else { return nil }
        return rep.representation(using: .png, properties: [:])
    }
}

/// The user's turn as the chat keeps it: the message, then the attached names (`attachments.turn_text`).
func turnText(_ text: String, _ files: [Attachment]) -> String {
    let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !files.isEmpty else { return body }
    let names = files.map(\.name).joined(separator: ", ")
    return body.isEmpty ? "attached: \(names)" : "\(body)\n\nattached: \(names)"
}
