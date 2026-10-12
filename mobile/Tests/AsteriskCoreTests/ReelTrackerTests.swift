import XCTest
@testable import AsteriskCore

/// Mirrors android/.../ReelTrackerTest.kt (minus the YouTube title/handle cases,
/// which iOS keys never hit).
final class ReelTrackerTests: XCTestCase {
    func testNewReelEmitsStartedOnly() {
        let t = ReelTracker()
        XCTAssertEqual(t.onReel("a", nowMs: 0), [.started(key: "a")])
    }

    func testSwipeBeforeDwellNeverReachesDwell() {
        let t = ReelTracker()
        _ = t.onReel("a", nowMs: 0)
        XCTAssertEqual(t.tick(nowMs: 4_999), [])
        XCTAssertEqual(t.onReel("b", nowMs: 4_999), [.ended(key: "a"), .started(key: "b")])
        XCTAssertEqual(t.tick(nowMs: 5_000), [])
    }

    func testDwellBoundaryInclusive() {
        let t = ReelTracker()
        _ = t.onReel("a", nowMs: 0)
        XCTAssertEqual(t.tick(nowMs: 5_000), [.dwellReached(key: "a")])
        XCTAssertTrue(t.dwellReached)
    }

    func testChunksEvery5sContiguous() {
        let t = ReelTracker()
        _ = t.onReel("a", nowMs: 0)
        _ = t.tick(nowMs: 5_000)
        XCTAssertEqual(t.tick(nowMs: 9_999), [])
        XCTAssertEqual(t.tick(nowMs: 10_000), [.chunkDue(key: "a", seq: 1)])
        XCTAssertEqual(t.tick(nowMs: 15_000), [.chunkDue(key: "a", seq: 2)])
    }

    func testStarvedTimerEmitsOneChunk() {
        let t = ReelTracker()
        _ = t.onReel("a", nowMs: 0)
        _ = t.tick(nowMs: 5_000)
        XCTAssertEqual(t.tick(nowMs: 26_000), [.chunkDue(key: "a", seq: 1)])
        XCTAssertEqual(t.tick(nowMs: 29_999), [])
        XCTAssertEqual(t.tick(nowMs: 30_000), [.chunkDue(key: "a", seq: 2)])
    }

    func testSameKeyOnlyAdvancesTime() {
        let t = ReelTracker()
        _ = t.onReel("a", nowMs: 0)
        XCTAssertEqual(t.onReel("a", nowMs: 1_000), [])
        XCTAssertEqual(t.onReel("a", nowMs: 5_000), [.dwellReached(key: "a")])
    }

    func testKeyChangeAfterDwellResetsSeq() {
        let t = ReelTracker()
        _ = t.onReel("a", nowMs: 0)
        _ = t.tick(nowMs: 5_000)
        _ = t.tick(nowMs: 10_000)
        XCTAssertEqual(t.onReel("b", nowMs: 11_000), [.ended(key: "a"), .started(key: "b")])
        _ = t.tick(nowMs: 16_000)
        XCTAssertEqual(t.tick(nowMs: 21_000), [.chunkDue(key: "b", seq: 1)])
    }

    func testNoReelEndsOnce() {
        let t = ReelTracker()
        _ = t.onReel("a", nowMs: 0)
        XCTAssertEqual(t.onNoReel(nowMs: 1), [.ended(key: "a")])
        XCTAssertEqual(t.onNoReel(nowMs: 2), [])
        XCTAssertEqual(t.tick(nowMs: 10_000), [])
    }
}
