import SwiftUI

/// The working indicator: a tiny Australopithecus walking in place with a big stick, drawn in the
/// spinner's style (monochrome strokes, about 20 pt). Nico asked for it in place of the spinner,
/// 2026-10-22. Like the system spinner it replaces, it keeps moving with Reduce Motion on: it is a
/// small in-place loop, not motion across the screen (and Nico's Mac has Reduce Motion on).
struct HomininSpinner: View {
    var size = 20.0

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
    static let period = 1.0           // seconds per two steps
    static let thigh = 15.0, shin = 14.0, foot = 6.0
    static let upperArm = 13.0, forearm = 12.5
    static let torso = 21.0, lean = 0.22
    static let swing = 0.42, kneeRest = 0.12, kneeLift = 0.62
    static let stick = 64.0
    /// Units from the ground to the top of the stick; the drawing scales this to the frame.
    static let height = 70.0

    /// A leg at gait phase ψ, relative to the hip (x forward, y up).
    static func leg(_ psi: Double) -> (knee: CGVector, ankle: CGVector, toe: CGVector) {
        let hip = swing * sin(psi)
        let bend = kneeRest + kneeLift * max(0, cos(psi))
        let knee = CGVector(dx: thigh * sin(hip), dy: -thigh * cos(hip))
        let ankle = CGVector(dx: knee.dx + shin * sin(hip - bend), dy: knee.dy - shin * cos(hip - bend))
        let tilt = -0.35 * max(0, cos(psi))
        return (knee, ankle, CGVector(dx: ankle.dx + foot * cos(tilt), dy: ankle.dy + foot * sin(tilt)))
    }

    /// Draws solid strokes into one layer, then fades the layer as a whole (like the spinner's gray),
    /// so overlapping strokes never brighten the joints; the far limbs sit dimmer inside it.
    static func draw(_ context: inout GraphicsContext, size: CGSize, phase phi: Double) {
        var group = context
        group.opacity = 0.62
        group.drawLayer { layer in
            drawFigure(&layer, size: size, phase: phi)
        }
    }

    private static func drawFigure(_ context: inout GraphicsContext, size: CGSize, phase phi: Double) {
        let scale = size.height / height
        let width = max(1.1, 2.0 * scale / 0.29)
        let ink = GraphicsContext.Shading.color(.primary)
        // The figure spans about 12 units behind the hips and 26 in front (the stick).
        let originX = size.width / 2 - 7 * scale
        func p(_ x: Double, _ y: Double) -> CGPoint {
            CGPoint(x: originX + x * scale, y: size.height - y * scale)
        }
        func stroke(_ points: [CGPoint], _ shading: GraphicsContext.Shading, _ lineWidth: Double, dim: Bool = false) {
            var path = Path()
            path.addLines(points)
            var target = context
            if dim { target.opacity = 0.4 }
            target.stroke(path, with: shading, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round,
                                                                    lineJoin: .round))
        }

        // Hips sit so the planted foot touches the ground; the figure walks in place.
        let near = leg(phi), far = leg(phi + .pi)
        let hipY = -min(near.ankle.dy, far.ankle.dy) + 1.5
        let shoulder = (x: torso * sin(lean), y: hipY + torso * cos(lean))
        let head = (x: shoulder.x + 5.5, y: shoulder.y + 8.5)

        func legPoints(_ leg: (knee: CGVector, ankle: CGVector, toe: CGVector), hipX: Double) -> [CGPoint] {
            [p(hipX, hipY), p(hipX + leg.knee.dx, hipY + leg.knee.dy), p(hipX + leg.ankle.dx, hipY + leg.ankle.dy),
             p(hipX + leg.toe.dx, hipY + leg.toe.dy)]
        }
        func armPoints(swing: Double, bend: Double) -> [CGPoint] {
            let elbow = (x: shoulder.x + upperArm * sin(swing), y: shoulder.y - upperArm * cos(swing))
            let hand = (x: elbow.x + forearm * sin(swing + bend), y: elbow.y - forearm * cos(swing + bend))
            return [p(shoulder.x, shoulder.y), p(elbow.x, elbow.y), p(hand.x, hand.y)]
        }

        // Far limbs dimmer, then the body, the near leg, the stick and the arm holding it.
        stroke(legPoints(far, hipX: -1), ink, width, dim: true)
        stroke(armPoints(swing: 0.32 * sin(phi) + 0.05, bend: 0.3), ink, width * 0.9, dim: true)
        stroke([p(0, hipY), p(shoulder.x, shoulder.y)], ink, width * 1.55)
        stroke([p(shoulder.x + 1, shoulder.y + 1), p(head.x - 2, head.y - 3)], ink, width * 1.2)
        let radius = 6.0 * scale
        let headPoint = p(head.x, head.y)
        context.fill(Path(ellipseIn: CGRect(x: headPoint.x - radius, y: headPoint.y - radius, width: radius * 2,
                                            height: radius * 2)), with: ink)
        // A short muzzle: the forward jaw of an early hominin.
        stroke([p(head.x + 4, head.y - 2.5), p(head.x + 8, head.y - 3.5)], ink, width * 1.1)
        stroke(legPoints(near, hipX: 1), ink, width)

        let arm = armPoints(swing: 0.25 - 0.12 * sin(phi), bend: 0.95)
        let hand = (x: (arm[2].x - originX) / scale, y: (size.height - arm[2].y) / scale)
        let tilt = 0.18 + 0.05 * sin(phi)
        let grip = min(0.42, (hand.y - 2) / (stick * cos(tilt)))
        let bottom = p(hand.x - grip * stick * sin(tilt), hand.y - grip * stick * cos(tilt))
        let top = p(hand.x + (1 - grip) * stick * sin(tilt), hand.y + (1 - grip) * stick * cos(tilt))
        stroke([bottom, top], ink, width * 0.85)
        stroke(arm, ink, width)
    }
}
