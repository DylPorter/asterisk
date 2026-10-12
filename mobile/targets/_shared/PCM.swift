import Foundation

/// The layout of one block of raw audio, as ReplayKit describes it in the
/// buffer's AudioStreamBasicDescription. Kept as plain values so conversion is
/// testable without CoreMedia.
public struct PCMFormat: Equatable, Sendable {
    public enum Sample: Sendable { case int16, float32 }
    public var sampleRate: Double
    public var channels: Int
    public var sample: Sample
    /// ReplayKit's `.audioApp` buffers have been big-endian Int16 on some iOS
    /// versions while `.audioMic` is little-endian. Read the flag, never assume.
    public var bigEndian: Bool
    /// Interleaved L R L R. Non-interleaved input should be passed one plane at a time with channels = 1.
    public var interleaved: Bool

    public init(sampleRate: Double, channels: Int, sample: Sample, bigEndian: Bool = false, interleaved: Bool = true) {
        self.sampleRate = sampleRate
        self.channels = max(1, channels)
        self.sample = sample
        self.bigEndian = bigEndian
        self.interleaved = interleaved
    }
}

/// Streaming converter: any rate / channel count / sample type -> 16 kHz mono PCM16,
/// which is what the server's /v2/segments expects.
///
/// Box-filter downsampling (average of the input samples each output sample
/// covers). Crude next to a polyphase filter, but speech recognition does not
/// notice and it costs nothing in the 50 MB broadcast extension.
public final class Resampler {
    public static let outputRate = 16_000.0

    private var pending: [Float] = []
    private var emitted: Int64 = 0      // output samples produced since reset
    private var dropped: Int64 = 0      // input samples discarded from the front of `pending`
    private var inputRate = 0.0

    public init() {}

    public func reset() {
        pending.removeAll(keepingCapacity: true)
        emitted = 0
        dropped = 0
    }

    public func convert(_ bytes: Data, format: PCMFormat) -> [Int16] {
        if format.sampleRate != inputRate {
            // A route change mid-broadcast changes the rate; stale fractional state would skew pitch.
            reset()
            inputRate = format.sampleRate
        }
        guard format.sampleRate > 0 else { return [] }
        appendMono(bytes, format: format)

        // Output sample k averages input [k*in/out, (k+1)*in/out), in exact
        // integer arithmetic so the result does not depend on how the stream
        // was split into buffers.
        let inRate = Int64(format.sampleRate.rounded()), outRate = Int64(Self.outputRate)
        var out: [Int16] = []
        out.reserveCapacity(Int(Int64(pending.count) * outRate / max(inRate, 1)) + 1)
        while true {
            let lo = Int(emitted * inRate / outRate - dropped)
            let hi = max(lo + 1, Int((emitted + 1) * inRate / outRate - dropped))
            guard hi <= pending.count else { break }
            var acc: Float = 0
            for i in lo..<hi { acc += pending[i] }
            out.append(Self.toInt16(acc / Float(hi - lo)))
            emitted += 1
        }
        let keep = Int(emitted * inRate / outRate - dropped)
        if keep > 0 {
            pending.removeFirst(keep)
            dropped += Int64(keep)
        }
        return out
    }

    private func appendMono(_ bytes: Data, format: PCMFormat) {
        let width = format.sample == .int16 ? 2 : 4
        let ch = format.channels
        let frames = bytes.count / (width * ch)
        guard frames > 0 else { return }
        pending.reserveCapacity(pending.count + frames)
        bytes.withUnsafeBytes { raw in
            for f in 0..<frames {
                var acc: Float = 0
                for c in 0..<ch {
                    let off = format.interleaved ? (f * ch + c) * width : (c * frames + f) * width
                    acc += Self.read(raw, off, format)
                }
                pending.append(acc / Float(ch))
            }
        }
    }

    private static func read(_ raw: UnsafeRawBufferPointer, _ off: Int, _ f: PCMFormat) -> Float {
        switch f.sample {
        case .int16:
            var v = UInt16(raw[off]) | UInt16(raw[off + 1]) << 8
            if f.bigEndian { v = v.byteSwapped }
            return Float(Int16(bitPattern: v)) / 32768
        case .float32:
            var v = UInt32(raw[off]) | UInt32(raw[off + 1]) << 8 | UInt32(raw[off + 2]) << 16 | UInt32(raw[off + 3]) << 24
            if f.bigEndian { v = v.byteSwapped }
            return Float(bitPattern: v)
        }
    }

    static func toInt16(_ x: Float) -> Int16 {
        let c = min(1, max(-1, x.isFinite ? x : 0))
        return Int16((c * 32767).rounded())
    }
}

public enum PCM {
    /// RMS level in dBFS; -infinity for digital silence. Same scale as the server's is_silent.
    public static func rmsDb(_ s: ArraySlice<Int16>) -> Double {
        guard !s.isEmpty else { return -.infinity }
        var acc = 0.0
        for v in s { let d = Double(v); acc += d * d }
        let rms = (acc / Double(s.count)).squareRoot()
        return rms == 0 ? -.infinity : 20 * log10(rms / 32768)
    }

    public static func rmsDb(_ s: [Int16]) -> Double { rmsDb(s[...]) }

    /// 44-byte RIFF header + little-endian PCM16 mono.
    public static func wav(_ s: [Int16], rate: Int = Int(Resampler.outputRate)) -> Data {
        var d = Data(capacity: 44 + s.count * 2)
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        let dataLen = UInt32(s.count * 2)
        d.append(contentsOf: Array("RIFF".utf8)); u32(36 + dataLen)
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1)
        u32(UInt32(rate)); u32(UInt32(rate * 2)); u16(2); u16(16)
        d.append(contentsOf: Array("data".utf8)); u32(dataLen)
        for v in s { u16(UInt16(bitPattern: v)) }
        return d
    }
}
