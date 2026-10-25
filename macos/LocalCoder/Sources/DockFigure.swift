import AppKit
import SwiftUI

/// The walker in the Dock: he walks while Nicoder works and sits down to meditate when the work is done
/// (Nico, 2026-10-25). Before the first request the Dock shows the app icon, which is the meditating pose too.
@MainActor
final class DockFigure {
    static let shared = DockFigure()

    private var timer: Timer?
    private let host = NSHostingView(rootView: DockTileView(pose: .meditating))

    private init() {
        host.frame = NSRect(origin: .zero, size: NSApp.dockTile.size)
    }

    func walk() {
        guard timer == nil else { return }
        NSApp.dockTile.contentView = host
        let started = Date()
        let timer = Timer(timeInterval: 1.0 / 12.0, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                let phase = 2 * .pi * Date().timeIntervalSince(started) / Hominin.period
                self?.show(.walking(phase: phase))
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func meditate() {
        timer?.invalidate()
        timer = nil
        NSApp.dockTile.contentView = host
        show(.meditating)
    }

    private func show(_ pose: DockTileView.Pose) {
        host.rootView = DockTileView(pose: pose)
        NSApp.dockTile.display()
    }
}

/// The Dock tile and the app icon: a green macOS squircle with the hominin in light ink.
struct DockTileView: View {
    enum Pose {
        case walking(phase: Double)
        case meditating
    }

    let pose: Pose

    var body: some View {
        GeometryReader { geometry in
            let side = min(geometry.size.width, geometry.size.height)
            let tile = side * 0.805
            ZStack {
                RoundedRectangle(cornerRadius: tile * 0.2237, style: .continuous)
                    .fill(LinearGradient(colors: [Color(red: 0.20, green: 0.40, blue: 0.32),
                                                  Color(red: 0.09, green: 0.20, blue: 0.16)],
                                         startPoint: .top, endPoint: .bottom))
                    .frame(width: tile, height: tile)
                    .shadow(color: .black.opacity(0.25), radius: side * 0.02, y: side * 0.01)
                Canvas { context, size in
                    switch pose {
                    case .walking(let phase):
                        Hominin.draw(&context, size: size, phase: phase)
                    case .meditating:
                        MeditatingHominin.draw(&context, size: size)
                    }
                }
                .environment(\.colorScheme, .dark)  // light ink on the green tile
                .frame(width: tile * 0.74, height: tile * 0.74)
                .offset(y: tile * 0.02)
            }
            .frame(width: geometry.size.width, height: geometry.size.height)
        }
    }
}

/// The hominin sitting cross-legged with his eyes closed, his stick planted beside him. Same units, ink and
/// shapes as the walker (`Hominin`), so the two read as one character.
enum MeditatingHominin {
    static func draw(_ context: inout GraphicsContext, size: CGSize) {
        var group = context
        group.opacity = 0.85
        group.drawLayer { layer in
            drawFigure(&layer, size: size)
        }
    }

    /// He fills more of the frame than the walker (his stick is shorter), centered on his lap.
    private static let height = 50.0

    private static func drawFigure(_ context: inout GraphicsContext, size: CGSize) {
        let scale = size.height / height
        let ink = GraphicsContext.Shading.color(context.environment.colorScheme == .dark ? .white : .black)
        let originX = size.width / 2 - 3.5 * scale  // centered between the stick and his knee
        func p(_ x: Double, _ y: Double) -> CGPoint { CGPoint(x: originX + x * scale, y: size.height - y * scale) }
        func disc(_ center: CGPoint, _ radius: Double, in target: GraphicsContext? = nil) {
            let r = radius * scale
            (target ?? context).fill(Path(ellipseIn: CGRect(x: center.x - r, y: center.y - r, width: r * 2,
                                                            height: r * 2)), with: ink)
        }
        func stroke(_ points: [CGPoint], _ width: Double) {
            var path = Path()
            path.addLines(points)
            context.stroke(path, with: ink, style: StrokeStyle(lineWidth: width * scale, lineCap: .round,
                                                               lineJoin: .round))
        }
        func taper(_ a: CGPoint, _ b: CGPoint, _ startWidth: Double, _ endWidth: Double,
                   in target: GraphicsContext? = nil) {
            let dx = b.x - a.x, dy = b.y - a.y
            let length = max(0.001, (dx * dx + dy * dy).squareRoot())
            let nx = -dy / length, ny = dx / length
            let ra = startWidth * scale / 2, rb = endWidth * scale / 2
            var body = Path()
            body.addLines([CGPoint(x: a.x + nx * ra, y: a.y + ny * ra), CGPoint(x: b.x + nx * rb, y: b.y + ny * rb),
                           CGPoint(x: b.x - nx * rb, y: b.y - ny * rb), CGPoint(x: a.x - nx * ra, y: a.y - ny * ra)])
            body.closeSubpath()
            let surface = target ?? context
            surface.fill(body, with: ink)
            surface.fill(Path(ellipseIn: CGRect(x: a.x - ra, y: a.y - ra, width: ra * 2, height: ra * 2)), with: ink)
            surface.fill(Path(ellipseIn: CGRect(x: b.x - rb, y: b.y - rb, width: rb * 2, height: rb * 2)), with: ink)
        }
        func shape(_ outline: [(Double, Double)], angle: Double, at origin: (x: Double, y: Double)) -> Path {
            let points = outline.map { x, y in
                p(origin.x + x * cos(angle) + y * sin(angle), origin.y - x * sin(angle) + y * cos(angle))
            }
            func mid(_ a: CGPoint, _ b: CGPoint) -> CGPoint { CGPoint(x: (a.x + b.x) / 2, y: (a.y + b.y) / 2) }
            var path = Path()
            path.move(to: mid(points[points.count - 1], points[0]))
            for index in points.indices {
                path.addQuadCurve(to: mid(points[index], points[(index + 1) % points.count]), control: points[index])
            }
            path.closeSubpath()
            return path
        }

        let hipY = 6.0
        let lean = -0.03
        let shoulder = (x: Hominin.torso * sin(lean), y: hipY + Hominin.torso * cos(lean))
        let head = (x: shoulder.x + 6.0, y: shoulder.y + 6.6)

        // The stick, planted upright just behind him.
        stroke([p(-17, 0), p(-16.3, 22), p(-15.6, 44)], 3.0)
        stroke([p(-15.9, 35), p(-19.7, 40), p(-20.2, 42.5)], 2.0)
        disc(p(-15.2, 16), 1.5)

        // Far side, dimmed as one layer: the other folded leg and the other hand on its knee.
        var farSide = context
        farSide.opacity = 0.42
        farSide.drawLayer { far in
            taper(p(-1.0, hipY), p(20.0, 4.6), 9.0, 6.4, in: far)
            taper(p(20.0, 4.6), p(7.0, 1.4), 6.4, 4.6, in: far)
            taper(p(shoulder.x - 1.5, shoulder.y - 1), p(4.0, 15.0), 6.4, 5.0, in: far)
            taper(p(4.0, 15.0), p(18.0, 7.8), 5.0, 4.2, in: far)
            disc(p(18.0, 7.8), 2.9, in: far)
        }

        // The near leg folded flat in front: thigh out to the knee, shin tucked back under it.
        taper(p(7.0, 2.2), p(15.0, 1.6), 4.6, 3.6)
        taper(p(22.0, 6.4), p(7.0, 2.2), 6.4, 4.6)
        taper(p(2.0, hipY), p(22.0, 6.4), 9.4, 6.4)

        // Back straight, neck, head level with the eyes closed.
        context.fill(shape(Hominin.torsoOutline, angle: lean, at: (x: 0, y: hipY)), with: ink)
        stroke([p(shoulder.x + 1.2, shoulder.y + 1.5), p(head.x - 2.4, head.y - 2.6)], 7.0)
        context.fill(shape(Hominin.headOutline, angle: 0.08, at: head), with: ink)
        var eye = context
        eye.blendMode = .clear
        var lid = Path()
        lid.addLines([p(head.x + 3.0, head.y - 0.1), p(head.x + 5.3, head.y - 0.5)])
        eye.stroke(lid, with: ink, style: StrokeStyle(lineWidth: 0.9 * scale, lineCap: .round))

        // The near arm hangs, then rests its hand on the knee.
        taper(p(shoulder.x + 1, shoulder.y - 0.5), p(6.0, 14.5), 7.0, 5.4)
        taper(p(6.0, 14.5), p(20.5, 9.8), 5.4, 4.4)
        disc(p(20.5, 9.8), 3.3)
    }
}
