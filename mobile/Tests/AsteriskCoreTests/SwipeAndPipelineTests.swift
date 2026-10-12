import XCTest
@testable import AsteriskCore

/// A fake 120x240 luma frame: background level `bg` everywhere, white "caption
/// text" blocks in the bottom-left strip at the given columns.
private func frame(bg: UInt8, text: [Int], row: Int = 190, shiftY: Int = 0) -> [UInt8] {
    let w = 120, h = 240
    var p = [UInt8](repeating: bg, count: w * h)
    for col in text {
        for y in (row + shiftY)..<min(h, row + shiftY + 8) {
            for x in col..<min(w, col + 6) { p[y * w + x] = 255 }
        }
    }
    return p
}

private let cfg: CaptureConfig = {
    var c = CaptureConfig()
    c.pixelStride = 1
    return c
}()

private func sig(_ p: [UInt8]) -> StripSignature {
    p.withUnsafeBytes {
        StripSignature.from(luma: $0, width: 120, height: 240, bytesPerRow: 120, region: cfg.strip,
                            gridW: cfg.gridW, gridH: cfg.gridH, bright: UInt8(cfg.bright), stride: cfg.pixelStride)
    }
}

final class SwipeTests: XCTestCase {
    private func detector() -> SwipeDetector {
        SwipeDetector(changeThreshold: cfg.changeThreshold, stableThreshold: cfg.stableThreshold,
                      stableFrames: cfg.stableFrames)
    }

    func testFirstStableStripBecomesReel1() {
        let d = detector()
        let a = sig(frame(bg: 40, text: [5, 20, 35, 50]))
        XCTAssertNil(d.feed(a)); XCTAssertNil(d.feed(a))
        XCTAssertEqual(d.feed(a), "reel-1")
    }

    func testBackgroundSceneCutIsNotASwipe() {
        let d = detector()
        for _ in 0..<3 { d.feed(sig(frame(bg: 40, text: [5, 20, 35, 50]))) }
        // The video behind the caption changes brightness completely, but stays below "white".
        for bg in [200, 10, 150, 90] as [UInt8] {
            XCTAssertEqual(d.feed(sig(frame(bg: bg, text: [5, 20, 35, 50]))), "reel-1")
        }
    }

    func testNewCaptionAfterSwipeAnimationCountsOnce() {
        let d = detector()
        for _ in 0..<3 { d.feed(sig(frame(bg: 40, text: [5, 20, 35, 50]))) }
        // Swipe: the old caption slides up across a few frames...
        for dy in [-6, -14, -22, -30] { d.feed(sig(frame(bg: 40, text: [5, 20, 35, 50], shiftY: dy))) }
        // ...then the new reel's caption holds still.
        let b = frame(bg: 60, text: [8, 30, 70])
        for _ in 0..<5 { d.feed(sig(b)) }
        XCTAssertEqual(d.key, "reel-2")
    }

    func testBrieflyFlickeringCaptionIsNotASwipe() {
        let d = detector()
        let a = frame(bg: 40, text: [5, 20, 35, 50])
        for _ in 0..<3 { d.feed(sig(a)) }
        d.feed(sig(frame(bg: 40, text: [5, 70])))   // one odd frame
        d.feed(sig(a))
        XCTAssertEqual(d.key, "reel-1")
    }
}

// MARK: pipeline

private actor Recorder {
    var segments: [(String, Int, Int)] = []      // session, seq, bytes
    var checks: [(String, Int)] = []
    var failNext = 0
    func seg(_ s: String, _ q: Int, _ n: Int) -> Bool {
        if failNext > 0 { failNext -= 1; return false }
        segments.append((s, q, n)); return true
    }
    func chk(_ s: String, _ f: Int) { checks.append((s, f)) }
    func setFail(_ n: Int) { failNext = n }
}

private struct FakeTransport: Transport {
    let rec: Recorder
    func postSegment(sessionId: String, seq: Int, wav: Data) async -> Bool {
        await rec.seg(sessionId, seq, wav.count)
    }
    func check(sessionId: String, finalSeq: Int, deadlineS: Double) async -> Data? {
        await rec.chk(sessionId, finalSeq)
        return Data("{\"status\":\"ok\"}".utf8)
    }
}

final class PipelineTests: XCTestCase {
    private let reelA = sig(frame(bg: 40, text: [5, 20, 35, 50]))
    private let reelB = sig(frame(bg: 40, text: [8, 30, 70, 100]))
    private let loud = [Int16](repeating: 8_000, count: 16_000)       // 1 s, about -12 dBFS
    private let quiet = [Int16](repeating: 0, count: 16_000)

    private func make(prefetch: Bool, rec: Recorder) -> CapturePipeline {
        var c = cfg
        c.prefetch = prefetch
        return CapturePipeline(config: c, transport: FakeTransport(rec: rec))
    }

    /// Drive `seconds` of a reel: one frame + 1 s of audio + a tick per second.
    private func play(_ p: CapturePipeline, _ s: StripSignature, from t0: Int64, seconds: Int, audio: [Int16]) -> Int64 {
        var t = t0
        for _ in 0..<seconds {
            for k in 0..<3 { p.onFrame(s, nowMs: t + Int64(k)) }
            p.onAudio(audio)
            t += 1_000
            p.tick(nowMs: t)
        }
        return t
    }

    private func tap(_ p: CapturePipeline) async -> CheckOutcome {
        await withCheckedContinuation { c in p.tap { c.resume(returning: $0) } }
    }

    func testTapBeforeDwellSendsNothing() async {
        let rec = Recorder()
        let p = make(prefetch: true, rec: rec)
        _ = play(p, reelA, from: 0, seconds: 3, audio: loud)
        let out = await tap(p)
        XCTAssertEqual(out, .notReady("Give it a few more seconds"))
        let n = await rec.segments.count
        XCTAssertEqual(n, 0)
    }

    func testPrefetchOffUploadsOnlyOnTapThenChecks() async {
        let rec = Recorder()
        let p = make(prefetch: false, rec: rec)
        _ = play(p, reelA, from: 0, seconds: 12, audio: loud)
        let before = await rec.segments.count
        XCTAssertEqual(before, 0, "prefetch off must not upload before the tap")
        let out = await tap(p)
        guard case .verdict = out else { return XCTFail("\(out)") }
        let segs = await rec.segments, checks = await rec.checks
        XCTAssertEqual(segs.map(\.1), Array(0..<segs.count), "seq contiguous from 0")
        XCTAssertGreaterThanOrEqual(segs.count, 2)
        XCTAssertEqual(checks.count, 1)
        XCTAssertEqual(checks[0].1, segs.count - 1)
    }

    func testPrefetchOnUploadsAtDwellAndEachChunk() async {
        let rec = Recorder()
        let p = make(prefetch: true, rec: rec)
        _ = play(p, reelA, from: 0, seconds: 16, audio: loud)
        let out = await tap(p)
        guard case .verdict = out else { return XCTFail("\(out)") }
        let segs = await rec.segments
        // dwell at ~5 s, chunks at ~10 s and ~15 s, final partial on tap
        XCTAssertEqual(segs.count, 4)
        XCTAssertEqual(segs.map(\.1), [0, 1, 2, 3])
        XCTAssertEqual(Set(segs.map(\.0)).count, 1)
    }

    func testSwipeStartsANewSessionAndDropsOldAudio() async {
        let rec = Recorder()
        let p = make(prefetch: false, rec: rec)
        var t = play(p, reelA, from: 0, seconds: 8, audio: loud)
        t = play(p, reelB, from: t, seconds: 7, audio: loud)
        let out = await tap(p)
        guard case .verdict = out else { return XCTFail("\(out)") }
        let segs = await rec.segments
        let total = segs.reduce(0) { $0 + ($1.2 - 44) / 2 }
        // Only reel B's audio, which started after its strip held still for 3 frames.
        XCTAssertLessThanOrEqual(total, 7 * 16_000)
        XCTAssertEqual(Set(segs.map(\.0)).count, 1)
    }

    func testSilentReelSaysSoWithoutChecking() async {
        let rec = Recorder()
        let p = make(prefetch: false, rec: rec)
        _ = play(p, reelA, from: 0, seconds: 8, audio: quiet)
        let out = await tap(p)
        XCTAssertEqual(out, .silent)
        let n = await rec.checks.count
        XCTAssertEqual(n, 0)
    }

    func testFailedUploadIsRetriedOnceWithSameSeq() async {
        let rec = Recorder()
        await rec.setFail(1)
        let p = make(prefetch: false, rec: rec)
        _ = play(p, reelA, from: 0, seconds: 8, audio: loud)
        _ = await tap(p)
        let segs = await rec.segments
        XCTAssertEqual(segs.first?.1, 0)
    }

    func testSecondTapIsRefused() async {
        let rec = Recorder()
        let p = make(prefetch: false, rec: rec)
        _ = play(p, reelA, from: 0, seconds: 8, audio: loud)
        // Two taps back to back on the extension's queue, before the first finishes.
        let first = expectation(description: "first")
        var second: CheckOutcome?
        p.tap { _ in first.fulfill() }
        p.tap { second = $0 }
        XCTAssertEqual(second, .notReady("Already checking"))
        await fulfillment(of: [first], timeout: 5)
    }

    func testConfigDecodingIsLenient() throws {
        let c = try JSONDecoder().decode(CaptureConfig.self, from: Data(#"{"dwellMs": 3000, "gridW": "oops", "extra": 1}"#.utf8))
        XCTAssertEqual(c.dwellMs, 3_000)
        XCTAssertEqual(c.gridW, CaptureConfig().gridW)
    }
}
