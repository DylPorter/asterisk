import XCTest
@testable import AsteriskCore

final class VerdictSummaryTests: XCTestCase {
    private func p(_ s: String) -> VerdictSummary { VerdictSummary.parse(Data(s.utf8)) }

    func testVerdict() {
        let v = p(#"{"kind":"verdict","body":{"status":"ok","seconds":4.2,"verdict":{"score":2,"overall":"misleading","headline":"Main claim is false","claims":[{"claim":"X causes Y","rating":"contradicted","note":"Studies say no","quote":"x causes y","sources":["https://a"]},{"claim":"Z","rating":"supported","note":"ok"}]}}}"#)
        XCTAssertEqual(v.overall, "misleading")
        XCTAssertEqual(v.score, 2)
        XCTAssertEqual(v.claims.count, 2)
        XCTAssertEqual(v.claims[0].sources, ["https://a"])
        XCTAssertEqual(v.spoken, "Main claim is false. 1 of 2 claims don't hold up.")
    }

    func testNonVerdicts() {
        XCTAssertEqual(p(#"{"kind":"silent"}"#).title, "Couldn't hear this video")
        XCTAssertEqual(p(#"{"kind":"not_ready","detail":"Give it a few more seconds"}"#).title, "Give it a few more seconds")
        XCTAssertEqual(p(#"{"kind":"verdict","body":{"status":"no_transcript"}}"#).title, "No speech in this video")
        XCTAssertEqual(p(#"{"kind":"verdict","body":{"status":"error","detail":"boom"}}"#).title, "boom")
        XCTAssertEqual(p("not json").title, "Couldn't read the result")
    }
}
