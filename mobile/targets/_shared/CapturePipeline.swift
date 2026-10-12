import Foundation

/// The network side, behind a protocol so the pipeline is testable without a server.
public protocol Transport: Sendable {
    /// POST /v2/segments. True when the server accepted the chunk.
    func postSegment(sessionId: String, seq: Int, wav: Data) async -> Bool
    /// POST /v2/check. Returns the raw JSON response body, or nil on failure.
    func check(sessionId: String, finalSeq: Int, deadlineS: Double) async -> Data?
}

/// What the tap produces, before the app renders it.
public enum CheckOutcome: Equatable, Sendable {
    case verdict(json: Data)
    case notReady(String)       // tapped before the dwell, or nothing playing
    case silent                 // audio all below silentDb: nothing to transcribe
    case failed(String)
}

/// Glue between frames, audio, the dwell tracker and uploads -- everything the
/// broadcast extension does except talk to ReplayKit.
///
/// Threading: every method except the uploads runs on the caller's one serial
/// queue. Uploads go through a FIFO (`jobs`) drained by a single task, so
/// chunks reach the server in seq order and the tap's final chunk is sent
/// only after every chunk queued before it.
public final class CapturePipeline {
    public let config: CaptureConfig
    private let transport: Transport
    private let log: (String, [String: String]) -> Void

    private let tracker: ReelTracker
    private let swipes: SwipeDetector
    private let ring: AudioRing
    private var session: Session?
    private var framesSeen = 0

    private final class Session: @unchecked Sendable {
        let key: String
        let id = UUID().uuidString
        var offset: Int64 = 0
        var dwellPassed = false
        var tapped = false
        // Owned by the upload task once jobs start flowing.
        var lastSeq = -1
        var loudestDb = -Double.infinity
        init(key: String) { self.key = key }
    }

    private enum Job {
        case segment(Session, [Int16])
        case finish(Session, @Sendable (CheckOutcome) -> Void)
    }
    private let jobs: AsyncStream<Job>
    private let jobSink: AsyncStream<Job>.Continuation
    private var worker: Task<Void, Never>?

    public init(config: CaptureConfig, transport: Transport,
                log: @escaping (String, [String: String]) -> Void = { _, _ in }) {
        self.config = config
        self.transport = transport
        self.log = log
        tracker = ReelTracker(dwellMs: config.dwellMs, chunkMs: config.chunkMs)
        swipes = SwipeDetector(changeThreshold: config.changeThreshold,
                               stableThreshold: config.stableThreshold,
                               stableFrames: config.stableFrames)
        ring = AudioRing(capacity: config.ringSeconds * Int(Resampler.outputRate))
        (jobs, jobSink) = AsyncStream<Job>.makeStream()
        let stream = jobs
        let t = transport
        let silentDb = config.silentDb
        let deadline = config.deadlineS
        let lg = log
        worker = Task {
            for await job in stream {
                await Self.run(job, transport: t, silentDb: silentDb, deadlineS: deadline, log: lg)
            }
        }
    }

    deinit { jobSink.finish(); worker?.cancel() }

    // MARK: inputs (serial queue)

    public var currentKey: String? { tracker.currentKey }
    public var dwellReached: Bool { tracker.dwellReached }

    public func onFrame(_ sig: StripSignature, nowMs: Int64) {
        framesSeen += 1
        let key = swipes.feed(sig)
        if config.diagEveryFrames > 0, framesSeen % config.diagEveryFrames == 0 {
            log("strip", ["d": String(format: "%.3f", swipes.lastDistance), "key": key ?? "-"])
        }
        guard let key else { return }
        handle(tracker.onReel(key, nowMs: nowMs))
    }

    public func onAudio(_ samples: [Int16]) {
        // Audio before the first reel key has no reel to belong to.
        guard session != nil else { return }
        ring.append(samples)
    }

    public func tick(nowMs: Int64) { handle(tracker.tick(nowMs: nowMs)) }

    public func stop(nowMs: Int64) {
        handle(tracker.onNoReel(nowMs: nowMs))
        swipes.reset()
    }

    /// The user asked for a check (Back Tap / Action Button via the App Intent).
    public func tap(_ reply: @escaping @Sendable (CheckOutcome) -> Void) {
        guard let s = session else { reply(.notReady("Nothing playing yet")); return }
        guard s.dwellPassed else { reply(.notReady("Give it a few more seconds")); return }
        if s.tapped { reply(.notReady("Already checking")); return }
        s.tapped = true
        log("tap", ["session": s.id, "prefetched": String(s.offset > 0)])
        enqueueRemaining(s, final: true)
        jobSink.yield(.finish(s, reply))
    }

    // MARK: internals

    private func handle(_ events: [ReelEvent]) {
        for e in events {
            switch e {
            case .started(let key):
                ring.begin()
                session = Session(key: key)
                log("reel", ["key": key])
            case .dwellReached(let key):
                guard let s = session, s.key == key else { continue }
                s.dwellPassed = true
                log("dwell", ["key": key])
                if config.prefetch { enqueueRemaining(s, final: false) }
            case .chunkDue(let key, _):
                guard let s = session, s.key == key, !s.tapped, config.prefetch else { continue }
                enqueueRemaining(s, final: false)
            case .ended(let key):
                if session?.key == key { session = nil }
                log("ended", ["key": key])
            }
        }
    }

    /// Queue everything since the last upload, in chunks of at most `chunkMs`
    /// (prefetch off means the tap sends up to `ringSeconds` at once).
    private func enqueueRemaining(_ s: Session, final: Bool) {
        let (pcm, end) = ring.drain(since: s.offset)
        s.offset = end
        let per = Int(Resampler.outputRate) * Int(config.chunkMs) / 1000
        var i = 0
        while i < pcm.count {
            let j = min(pcm.count, i + per)
            // Fold a short tail into the previous chunk rather than send a sliver.
            let tailShort = pcm.count - j < per / 5
            let hi = tailShort ? pcm.count : j
            jobSink.yield(.segment(s, Array(pcm[i..<hi])))
            i = hi
        }
    }

    private static func run(_ job: Job, transport: Transport, silentDb: Double, deadlineS: Double,
                            log: (String, [String: String]) -> Void) async {
        switch job {
        case .segment(let s, let pcm):
            let seq = s.lastSeq + 1
            let wav = PCM.wav(pcm)
            var ok = await transport.postSegment(sessionId: s.id, seq: seq, wav: wav)
            if !ok { ok = await transport.postSegment(sessionId: s.id, seq: seq, wav: wav) }
            let db = PCM.rmsDb(pcm)
            log("segment", ["seq": String(seq), "ok": String(ok), "db": String(format: "%.1f", db),
                            "samples": String(pcm.count)])
            if ok {
                s.lastSeq = seq
                s.loudestDb = max(s.loudestDb, db)
            }
        case .finish(let s, let reply):
            if s.lastSeq < 0 { reply(.notReady("No audio captured")); return }
            if s.loudestDb < silentDb { reply(.silent); return }
            if let body = await transport.check(sessionId: s.id, finalSeq: s.lastSeq, deadlineS: deadlineS) {
                reply(.verdict(json: body))
            } else {
                reply(.failed("Couldn't reach the server"))
            }
        }
    }
}
