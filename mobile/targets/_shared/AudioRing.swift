import Foundation

/// In-memory audio for the reel on screen, addressed by absolute sample
/// position since `begin()` so the uploader can ask for "everything since
/// offset N" across many appends. Oldest samples are overwritten past
/// `capacity`; `drain(since:)` then starts at the oldest it still holds.
///
/// Nothing here touches disk: audio from before the dwell gate must never
/// leave memory, and the broadcast extension has a 50 MB ceiling.
public final class AudioRing {
    public let capacity: Int
    private var buf: [Int16]
    private var written: Int64 = 0     // total samples appended since begin()

    public init(capacity: Int) {
        self.capacity = max(1, capacity)
        buf = [Int16](repeating: 0, count: self.capacity)
    }

    /// Absolute position one past the newest sample.
    public var end: Int64 { written }
    /// Absolute position of the oldest sample still held.
    public var start: Int64 { max(0, written - Int64(capacity)) }

    /// Forget everything: a new reel starts at position 0.
    public func begin() { written = 0 }

    public func append(_ s: [Int16]) {
        guard !s.isEmpty else { return }
        // Only the newest `capacity` samples of an oversized append can survive.
        let src = s.count > capacity ? s[(s.count - capacity)...] : s[...]
        written += Int64(s.count - src.count)
        for v in src {
            buf[Int(written % Int64(capacity))] = v
            written += 1
        }
    }

    /// Samples from `since` (clamped to what is still held) to `end`.
    public func drain(since: Int64) -> (samples: [Int16], end: Int64) {
        let from = max(since, start)
        guard from < written else { return ([], written) }
        var out = [Int16]()
        out.reserveCapacity(Int(written - from))
        var p = from
        while p < written {
            out.append(buf[Int(p % Int64(capacity))])
            p += 1
        }
        return (out, written)
    }
}
