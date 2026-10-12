import XCTest
@testable import AsteriskCore

final class AudioTests: XCTestCase {
    // MARK: helpers
    private func sine(_ hz: Double, rate: Double, seconds: Double, amp: Double = 0.5) -> [Double] {
        (0..<Int(rate * seconds)).map { amp * sin(2 * .pi * hz * Double($0) / rate) }
    }
    private func int16Bytes(_ x: [Double], channels: Int, bigEndian: Bool = false) -> Data {
        var d = Data()
        for v in x {
            let s = Int16((v * 32767).rounded())
            for _ in 0..<channels {
                let u = bigEndian ? UInt16(bitPattern: s).bigEndian : UInt16(bitPattern: s).littleEndian
                withUnsafeBytes(of: u) { d.append(contentsOf: $0) }
            }
        }
        return d
    }
    private func float32Bytes(_ x: [Double]) -> Data {
        var d = Data()
        for v in x { withUnsafeBytes(of: Float(v).bitPattern.littleEndian) { d.append(contentsOf: $0) } }
        return d
    }
    private func rms(_ s: [Int16]) -> Double {
        (s.map { Double($0) / 32768 }.map { $0 * $0 }.reduce(0, +) / Double(s.count)).squareRoot()
    }

    // MARK: resampler
    func testStereo48kInt16To16kMonoKeepsLengthAndLevel() {
        let x = sine(440, rate: 48_000, seconds: 1)
        let out = Resampler().convert(int16Bytes(x, channels: 2), format: PCMFormat(sampleRate: 48_000, channels: 2, sample: .int16))
        XCTAssertEqual(Double(out.count), 16_000, accuracy: 2)
        XCTAssertEqual(rms(out), 0.5 / 2.squareRoot(), accuracy: 0.02)
    }

    func testBigEndianIsDecodedNotNoise() {
        let x = sine(440, rate: 44_100, seconds: 0.5)
        let le = Resampler().convert(int16Bytes(x, channels: 1), format: PCMFormat(sampleRate: 44_100, channels: 1, sample: .int16))
        let be = Resampler().convert(int16Bytes(x, channels: 1, bigEndian: true),
                                     format: PCMFormat(sampleRate: 44_100, channels: 1, sample: .int16, bigEndian: true))
        XCTAssertEqual(le, be)
        // Reading big-endian as little-endian is the bug this flag exists for: it comes out as loud noise.
        let wrong = Resampler().convert(int16Bytes(x, channels: 1, bigEndian: true),
                                        format: PCMFormat(sampleRate: 44_100, channels: 1, sample: .int16))
        XCTAssertNotEqual(wrong, le)
    }

    func testFloat32Input() {
        let x = sine(300, rate: 48_000, seconds: 0.5, amp: 0.25)
        let out = Resampler().convert(float32Bytes(x), format: PCMFormat(sampleRate: 48_000, channels: 1, sample: .float32))
        XCTAssertEqual(Double(out.count), 8_000, accuracy: 2)
        XCTAssertEqual(rms(out), 0.25 / 2.squareRoot(), accuracy: 0.01)
    }

    func testStreamingMatchesOneShot() {
        let x = sine(500, rate: 44_100, seconds: 1)
        let f = PCMFormat(sampleRate: 44_100, channels: 1, sample: .int16)
        let whole = Resampler().convert(int16Bytes(x, channels: 1), format: f)
        let r = Resampler()
        var parts: [Int16] = []
        var i = 0
        for n in [1024, 333, 4096, 7, 2048] + Array(repeating: 4410, count: 20) where i < x.count {
            let j = min(x.count, i + n)
            parts += r.convert(int16Bytes(Array(x[i..<j]), channels: 1), format: f)
            i = j
        }
        if i < x.count { parts += r.convert(int16Bytes(Array(x[i...]), channels: 1), format: f) }
        XCTAssertEqual(parts, whole)
    }

    func testRateChangeResets() {
        let r = Resampler()
        _ = r.convert(int16Bytes(sine(440, rate: 48_000, seconds: 0.1), channels: 1),
                      format: PCMFormat(sampleRate: 48_000, channels: 1, sample: .int16))
        let out = r.convert(int16Bytes(sine(440, rate: 16_000, seconds: 0.1), channels: 1),
                            format: PCMFormat(sampleRate: 16_000, channels: 1, sample: .int16))
        XCTAssertEqual(Double(out.count), 1_600, accuracy: 1)
    }

    // MARK: PCM helpers
    func testRmsDb() {
        XCTAssertEqual(PCM.rmsDb([Int16](repeating: 0, count: 100)), -.infinity)
        XCTAssertEqual(PCM.rmsDb([Int16](repeating: 16384, count: 100)), -6.02, accuracy: 0.05)
    }

    func testWavHeader() {
        let w = PCM.wav([1, -1, 300])
        XCTAssertEqual(w.count, 44 + 6)
        XCTAssertEqual(String(data: w[0..<4], encoding: .ascii), "RIFF")
        XCTAssertEqual(String(data: w[8..<12], encoding: .ascii), "WAVE")
        let rate = w[24..<28].enumerated().reduce(0) { $0 | Int($1.element) << (8 * $1.offset) }
        XCTAssertEqual(rate, 16_000)
        XCTAssertEqual(w[44], 1); XCTAssertEqual(w[45], 0)
        XCTAssertEqual(w[46], 0xFF); XCTAssertEqual(w[47], 0xFF)
    }

    // MARK: ring
    func testRingDrainSince() {
        let r = AudioRing(capacity: 10)
        r.append([1, 2, 3])
        var (s, end) = r.drain(since: 0)
        XCTAssertEqual(s, [1, 2, 3]); XCTAssertEqual(end, 3)
        r.append([4, 5])
        (s, end) = r.drain(since: end)
        XCTAssertEqual(s, [4, 5]); XCTAssertEqual(end, 5)
        (s, _) = r.drain(since: end)
        XCTAssertEqual(s, [])
    }

    func testRingOverwritesOldest() {
        let r = AudioRing(capacity: 4)
        r.append([1, 2, 3, 4, 5, 6])
        XCTAssertEqual(r.start, 2)
        XCTAssertEqual(r.drain(since: 0).samples, [3, 4, 5, 6])
        r.append(Array(10...20).map(Int16.init))
        XCTAssertEqual(r.drain(since: 0).samples, [17, 18, 19, 20])
        XCTAssertEqual(r.end, 17)
    }

    func testRingBeginForgets() {
        let r = AudioRing(capacity: 4)
        r.append([1, 2])
        r.begin()
        XCTAssertEqual(r.drain(since: 0).samples, [])
        r.append([9])
        XCTAssertEqual(r.drain(since: 0).samples, [9])
    }
}
