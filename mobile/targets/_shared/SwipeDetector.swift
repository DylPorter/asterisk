import Foundation

/// A tiny summary of the caption/handle strip of one video frame.
///
/// iOS gives the broadcast extension pixels but no view tree, so "which reel
/// is on screen" has to come from the frames. The bottom-left strip where
/// TikTok, Reels and Shorts all draw the @handle and caption stays put while a
/// reel plays and changes on a swipe. The video behind it moves, so each cell
/// stores the share of near-white pixels (caption text is white) rather than
/// mean brightness, which keeps a scene cut from looking like a swipe.
public struct StripSignature: Equatable, Sendable {
    public let cells: [Float]       // gridW * gridH, each 0...1

    public init(cells: [Float]) { self.cells = cells }

    /// Mean absolute difference per cell, 0...1.
    public func distance(to o: StripSignature) -> Float {
        guard cells.count == o.cells.count, !cells.isEmpty else { return 1 }
        var acc: Float = 0
        for i in cells.indices { acc += abs(cells[i] - o.cells[i]) }
        return acc / Float(cells.count)
    }

    /// Build from an 8-bit luma plane (plane 0 of the 420 bi-planar buffers ReplayKit delivers).
    /// `region` is fractional (x, y, w, h) of the frame; pixels are sampled every `stride`.
    public static func from(
        luma: UnsafeRawBufferPointer, width: Int, height: Int, bytesPerRow: Int,
        region: Region, gridW: Int, gridH: Int, bright: UInt8, stride: Int = 4
    ) -> StripSignature {
        let gridW = max(1, gridW), gridH = max(1, gridH)
        let region = region.clamped()
        let x0 = Int(Double(width) * region.x), y0 = Int(Double(height) * region.y)
        let rw = max(1, Int(Double(width) * region.w)), rh = max(1, Int(Double(height) * region.h))
        let st = max(1, stride)
        var cells = [Float](repeating: 0, count: gridW * gridH)
        for gy in 0..<gridH {
            for gx in 0..<gridW {
                let cx0 = x0 + rw * gx / gridW, cx1 = x0 + rw * (gx + 1) / gridW
                let cy0 = y0 + rh * gy / gridH, cy1 = y0 + rh * (gy + 1) / gridH
                var hit = 0, n = 0
                var y = cy0
                while y < min(cy1, height) {
                    var x = cx0
                    while x < min(cx1, width) {
                        let i = y * bytesPerRow + x
                        // UnsafeRawBufferPointer is unchecked in release builds; check both ends.
                        if i >= 0, i < luma.count { if luma[i] >= bright { hit += 1 }; n += 1 }
                        x += st
                    }
                    y += st
                }
                cells[gy * gridW + gx] = n == 0 ? 0 : Float(hit) / Float(n)
            }
        }
        return StripSignature(cells: cells)
    }
}

public struct Region: Codable, Equatable, Sendable {
    public var x: Double, y: Double, w: Double, h: Double
    public init(x: Double, y: Double, w: Double, h: Double) { self.x = x; self.y = y; self.w = w; self.h = h }

    /// Inside the unit square with a non-zero area; non-finite input falls back to the whole frame.
    public func clamped() -> Region {
        func f(_ v: Double, _ def: Double) -> Double { v.isFinite ? min(1, max(0, v)) : def }
        let cx = f(x, 0), cy = f(y, 0)
        return Region(x: min(cx, 0.99), y: min(cy, 0.99),
                      w: max(0.01, min(f(w, 1), 1 - min(cx, 0.99))),
                      h: max(0.01, min(f(h, 1), 1 - min(cy, 0.99))))
    }
}

/// Turns a stream of strip signatures into reel keys: `reel-1`, `reel-2`, ...
///
/// A new key needs the strip to (a) differ from the current reel's by more
/// than `changeThreshold`, and (b) then hold still for `stableFrames` frames,
/// so the swipe animation itself (several frames of sliding text) counts once.
public final class SwipeDetector {
    public private(set) var key: String?
    public private(set) var lastDistance: Float = 0

    private let changeThreshold: Float
    private let stableThreshold: Float
    private let stableFrames: Int
    private var anchor: StripSignature?
    private var candidate: StripSignature?
    private var candidateRun = 0
    private var counter = 0

    public init(changeThreshold: Float, stableThreshold: Float, stableFrames: Int) {
        self.changeThreshold = changeThreshold
        self.stableThreshold = stableThreshold
        self.stableFrames = max(1, stableFrames)
    }

    /// Feed one frame's signature. Returns the reel key in effect after it.
    @discardableResult
    public func feed(_ sig: StripSignature) -> String? {
        if let a = anchor {
            lastDistance = sig.distance(to: a)
            if lastDistance < changeThreshold {
                candidate = nil
                candidateRun = 0
                return key
            }
        }
        if let c = candidate, sig.distance(to: c) < stableThreshold {
            candidateRun += 1
        } else {
            candidate = sig
            candidateRun = 1
        }
        if candidateRun >= stableFrames {
            anchor = candidate
            candidate = nil
            candidateRun = 0
            counter += 1
            key = "reel-\(counter)"
        }
        return key
    }

    public func reset() {
        anchor = nil; candidate = nil; candidateRun = 0; key = nil
    }
}
