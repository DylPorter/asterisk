import Foundation

/// What the dwell state machine tells the capture/upload side to do.
/// Port of android/.../ReelTracker.kt; keep the two in step.
public enum ReelEvent: Equatable, Sendable {
    /// A new reel is on screen. Start buffering locally; nothing leaves the device yet.
    case started(key: String)
    /// The user has stayed `dwellMs`. Only now may buffered audio be uploaded.
    case dwellReached(key: String)
    /// Another chunk interval has passed since the dwell; upload what accrued. `seq` starts at 1.
    case chunkDue(key: String, seq: Int)
    /// The reel left the screen (swipe, app switch). Drop its buffer.
    case ended(key: String)
}

/// Dwell state machine for "which reel is on screen, and for how long".
///
/// No clock of its own: every call takes `nowMs`, so the privacy rule -- a reel
/// swiped away before the dwell gate uploads nothing -- is testable on Linux.
/// Not thread-safe; the broadcast extension drives it from one serial queue.
public final class ReelTracker {
    public private(set) var currentKey: String?
    public private(set) var dwellReached = false

    private let dwellMs: Int64
    private let chunkMs: Int64
    private var startedAt: Int64 = 0
    private var nextSeq = 1
    private var nextDueAt: Int64 = 0

    public init(dwellMs: Int64 = 5_000, chunkMs: Int64 = 5_000) {
        self.dwellMs = dwellMs
        self.chunkMs = chunkMs
    }

    /// The same key again only advances time. A different key is a swipe: the
    /// old reel ends before the new one starts, so one buffer never holds two reels.
    public func onReel(_ key: String, nowMs: Int64) -> [ReelEvent] {
        if key == currentKey { return tick(nowMs: nowMs) }
        var out: [ReelEvent] = []
        if let old = currentKey { out.append(.ended(key: old)) }
        currentKey = key
        startedAt = nowMs
        dwellReached = false
        nextSeq = 1
        out.append(.started(key: key))
        return out
    }

    /// No reel on screen any more (broadcast stopped, screen locked).
    public func onNoReel(nowMs: Int64) -> [ReelEvent] {
        guard let key = currentKey else { return [] }
        currentKey = nil
        dwellReached = false
        return [.ended(key: key)]
    }

    /// Called on a timer. Emits at most one event per call; a starved timer
    /// skips ahead rather than bursting, so `seq` stays contiguous.
    public func tick(nowMs: Int64) -> [ReelEvent] {
        guard let key = currentKey else { return [] }
        if !dwellReached {
            if nowMs - startedAt < dwellMs { return [] }
            dwellReached = true
            nextDueAt = startedAt + dwellMs + chunkMs
            return [.dwellReached(key: key)]
        }
        if nowMs < nextDueAt { return [] }
        while nextDueAt <= nowMs { nextDueAt += chunkMs }
        let seq = nextSeq
        nextSeq += 1
        return [.chunkDue(key: key, seq: seq)]
    }
}
