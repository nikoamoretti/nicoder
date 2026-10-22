import SwiftUI

/// The working indicator: an Australopithecus walking in place with a big stick, drawn in the
/// spinner's style (one gray, no outline) as a filled silhouette: hunched furry back, long arms,
/// bent knees, a low skull with a heavy brow and a jutting jaw. Nico asked for it in place of the
/// spinner (2026-10-22), then bigger so it reads as an Australopithecus. Like the system spinner,
/// it keeps moving with Reduce Motion on: a small in-place loop, not motion across the screen.
struct HomininSpinner: View {
    var size = 50.0

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 60.0)) { context in
            HomininFrame(phase: 2 * .pi * context.date.timeIntervalSinceReferenceDate
                .truncatingRemainder(dividingBy: 3600) / Hominin.period)
        }
        .frame(width: size, height: size)
        .accessibilityElement()
        .accessibilityLabel("Working")
    }
}

/// One pose of the walk at gait phase φ (also used for still previews).
struct HomininFrame: View {
    let phase: Double

    var body: some View {
        Canvas { context, size in
            Hominin.draw(&context, size: size, phase: phase)
        }
    }
}

enum Hominin {
    static let period = 1.1           // seconds per two steps
    static let thigh = 14.0, shin = 13.0, foot = 7.0
    static let upperArm = 13.5, forearm = 13.0
    static let torso = 21.0, lean = 0.24
    static let swing = 0.4, kneeRest = 0.22, kneeLift = 0.6
    static let stick = 66.0
    /// Units from the ground to the top of the stick; the drawing scales this to the frame.
    static let height = 72.0

    /// A leg at gait phase ψ, relative to the hip (x forward, y up). Knees stay a little bent.
    static func leg(_ psi: Double) -> (knee: CGVector, ankle: CGVector, toe: CGVector) {
        let hip = swing * sin(psi)
        let bend = kneeRest + kneeLift * max(0, cos(psi))
        let knee = CGVector(dx: thigh * sin(hip), dy: -thigh * cos(hip))
        let ankle = CGVector(dx: knee.dx + shin * sin(hip - bend), dy: knee.dy - shin * cos(hip - bend))
        let tilt = -0.35 * max(0, cos(psi))
        return (knee, ankle, CGVector(dx: ankle.dx + foot * cos(tilt), dy: ankle.dy + foot * sin(tilt)))
    }

    /// Torso outline around the spine (x forward, y up from the hips): furry hunched back, potbelly.
    static let torsoOutline: [(Double, Double)] = [
        (-6.2, 0.5), (-6.8, 6.0), (-8.4, 11.0), (-9.5, 12.8), (-8.9, 14.3), (-10.0, 16.0), (-9.3, 17.6),
        (-10.1, 19.2), (-8.4, 21.4), (-6.6, 23.6), (-3.2, 25.0), (0.6, 25.6), (4.6, 23.6), (6.9, 19.0),
        (8.7, 13.0), (9.5, 7.5), (7.8, 2.4), (3.9, -1.8), (-0.5, -2.6),
    ]
    /// Head outline around its center (x forward, y up): low crown, brow ridge, snout, small chin.
    static let headOutline: [(Double, Double)] = [
        (-6.2, 0.8), (-6.9, 2.6), (-5.4, 3.6), (-4.2, 5.2), (-1.6, 5.9), (1.6, 5.4), (4.4, 4.0),
        (6.6, 2.3), (6.9, 1.1), (6.0, 0.4), (7.2, -0.6), (8.9, -2.0), (8.8, -3.6), (7.0, -4.6),
        (5.0, -5.6), (2.0, -5.4), (-1.6, -4.2), (-4.6, -2.6),
    ]

    /// Draws solid shapes into one layer, then fades the layer as a whole (the spinner's gray),
    /// so overlaps never brighten; the far limbs sit dimmer inside it.
    static func draw(_ context: inout GraphicsContext, size: CGSize, phase phi: Double) {
        var group = context
        group.opacity = 0.72
        group.drawLayer { layer in
            drawFigure(&layer, size: size, phase: phi)
        }
    }

    private static func drawFigure(_ context: inout GraphicsContext, size: CGSize, phase phi: Double) {
        let scale = size.height / height
        // Fully opaque ink (the label colors are slightly translucent and would brighten overlaps);
        // the whole figure is faded once, in `draw`.
        let ink = GraphicsContext.Shading.color(context.environment.colorScheme == .dark ? .white : .black)
        let originX = size.width / 2 - 8 * scale
        func p(_ x: Double, _ y: Double) -> CGPoint { CGPoint(x: originX + x * scale, y: size.height - y * scale) }
        func stroke(_ points: [CGPoint], _ width: Double, in target: GraphicsContext? = nil) {
            var path = Path()
            path.addLines(points)
            (target ?? context).stroke(path, with: ink, style: StrokeStyle(lineWidth: width * scale, lineCap: .round,
                                                                             lineJoin: .round))
        }
        func disc(_ center: CGPoint, _ radius: Double, in target: GraphicsContext? = nil) {
            let r = radius * scale
            (target ?? context).fill(Path(ellipseIn: CGRect(x: center.x - r, y: center.y - r, width: r * 2,
                                                            height: r * 2)), with: ink)
        }
        /// A limb segment that narrows from one joint to the next, with round ends that match its widths.
        func taper(_ a: CGPoint, _ b: CGPoint, _ startWidth: Double, _ endWidth: Double,
                   in target: GraphicsContext? = nil) {
            let dx = b.x - a.x, dy = b.y - a.y
            let length = max(0.001, (dx * dx + dy * dy).squareRoot())
            let nx = -dy / length, ny = dx / length
            let ra = startWidth * scale / 2, rb = endWidth * scale / 2
            // Three separate fills: one combined path would cancel where the outlines wind opposite ways.
            var body = Path()
            body.addLines([CGPoint(x: a.x + nx * ra, y: a.y + ny * ra), CGPoint(x: b.x + nx * rb, y: b.y + ny * rb),
                           CGPoint(x: b.x - nx * rb, y: b.y - ny * rb), CGPoint(x: a.x - nx * ra, y: a.y - ny * ra)])
            body.closeSubpath()
            let surface = target ?? context
            surface.fill(body, with: ink)
            surface.fill(Path(ellipseIn: CGRect(x: a.x - ra, y: a.y - ra, width: ra * 2, height: ra * 2)), with: ink)
            surface.fill(Path(ellipseIn: CGRect(x: b.x - rb, y: b.y - rb, width: rb * 2, height: rb * 2)), with: ink)
        }
        /// A closed outline smoothed through its points, placed by a rotation and an offset.
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

        // Hips sit so the planted foot touches the ground; the figure walks in place.
        let near = leg(phi), farLeg = leg(phi + .pi)
        let hipY = -min(near.ankle.dy, farLeg.ankle.dy) + 2.0
        let shoulder = (x: torso * sin(lean), y: hipY + torso * cos(lean))
        let head = (x: shoulder.x + 7.0, y: shoulder.y + 6.5)

        func drawLeg(_ leg: (knee: CGVector, ankle: CGVector, toe: CGVector), hipX: Double,
                     in target: GraphicsContext? = nil) {
            let hip = p(hipX, hipY)
            let knee = p(hipX + leg.knee.dx, hipY + leg.knee.dy)
            let ankle = p(hipX + leg.ankle.dx, hipY + leg.ankle.dy)
            taper(hip, knee, 9.0, 6.4, in: target)
            taper(knee, ankle, 6.4, 4.6, in: target)
            taper(ankle, p(hipX + leg.toe.dx, hipY + leg.toe.dy), 4.6, 3.6, in: target)
        }
        func arm(swing: Double, bend: Double) -> (elbow: (x: Double, y: Double), hand: (x: Double, y: Double)) {
            let elbow = (x: shoulder.x + upperArm * sin(swing), y: shoulder.y - upperArm * cos(swing))
            return (elbow, (x: elbow.x + forearm * sin(swing + bend), y: elbow.y - forearm * cos(swing + bend)))
        }

        // Far side first, solid in its own layer that is then dimmed as a whole, so its joints never glow.
        var farSide = context
        farSide.opacity = 0.42
        farSide.drawLayer { far in
            drawLeg(farLeg, hipX: -1.5, in: far)
            let farArm = arm(swing: 0.34 * sin(phi) + 0.08, bend: 0.28)
            taper(p(shoulder.x - 1.5, shoulder.y - 1), p(farArm.elbow.x, farArm.elbow.y), 6.4, 5.0, in: far)
            taper(p(farArm.elbow.x, farArm.elbow.y), p(farArm.hand.x, farArm.hand.y), 5.0, 4.2, in: far)
            disc(p(farArm.hand.x, farArm.hand.y), 2.9, in: far)
        }

        context.fill(shape(torsoOutline, angle: lean, at: (x: 0, y: hipY)), with: ink)
        stroke([p(shoulder.x + 1.5, shoulder.y + 1.5), p(head.x - 2.5, head.y - 2.5)], 7.0)
        context.fill(shape(headOutline, angle: 0.06, at: head), with: ink)
        // The eye, under the brow ridge: cut out of the silhouette.
        var eye = context
        eye.blendMode = .clear
        let eyeCenter = p(head.x + 4.3, head.y + 0.2)
        let eyeRadius = 1.05 * scale
        eye.fill(Path(ellipseIn: CGRect(x: eyeCenter.x - eyeRadius, y: eyeCenter.y - eyeRadius, width: eyeRadius * 2,
                                        height: eyeRadius * 2)), with: ink)

        drawLeg(near, hipX: 1.5)

        let nearArm = arm(swing: 0.3 - 0.12 * sin(phi), bend: 0.95)
        let hand = nearArm.hand
        let tilt = 0.17 + 0.05 * sin(phi)
        let grip = min(0.4, (hand.y - 2.5) / (stick * cos(tilt)))
        func along(_ t: Double) -> (Double, Double) {
            (hand.x + (t - grip) * stick * sin(tilt), hand.y + (t - grip) * stick * cos(tilt))
        }
        let (bx, by) = along(0), (mx, my) = along(0.55), (tx, ty) = along(1), (fx, fy) = along(0.8)
        stroke([p(bx, by), p(mx + 1.2, my), p(tx, ty)], 3.6)
        stroke([p(fx, fy), p(fx - 5, fy + 6.5), p(fx - 5.6, fy + 9.5)], 2.2)
        let (kx, ky) = along(0.32)
        disc(p(kx + 1.6, ky), 1.7)

        taper(p(shoulder.x + 1, shoulder.y - 0.5), p(nearArm.elbow.x, nearArm.elbow.y), 7.0, 5.4)
        taper(p(nearArm.elbow.x, nearArm.elbow.y), p(hand.x, hand.y), 5.4, 4.4)
        disc(p(hand.x, hand.y), 3.3)
    }
}
