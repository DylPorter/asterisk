import Foundation

/// Every tunable the extension uses, fetched from `GET /v2/config` at
/// broadcast start so a wrong guess costs a server restart, not an EAS build.
/// Decoding is lenient: a missing or malformed field keeps its default.
public struct CaptureConfig: Codable, Equatable, Sendable {
    public var dwellMs: Int64 = 5_000
    public var chunkMs: Int64 = 5_000
    public var ringSeconds: Int = 60
    /// Upload chunks after the dwell, before any tap (Android's behaviour).
    /// iOS cannot restrict capture to the three video apps the way Android's
    /// addMatchingUid does, so with this on, any app's audio that plays for
    /// 5 s while broadcasting is transcribed. Off = upload only on tap.
    public var prefetch = false
    public var silentDb = -55.0
    public var frameIntervalMs: Int64 = 250
    public var strip = Region(x: 0.0, y: 0.70, w: 0.72, h: 0.18)
    public var gridW = 24
    public var gridH = 6
    public var bright = 215
    public var pixelStride = 4
    public var changeThreshold: Float = 0.06
    public var stableThreshold: Float = 0.03
    public var stableFrames = 3
    public var deadlineS = 20.0
    /// Log the strip distance every N analysed frames (0 = never). Tuning aid.
    public var diagEveryFrames = 8

    public init() {}

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = CaptureConfig()
        func v<T: Decodable>(_ k: CodingKeys, _ def: T) -> T { (try? c.decodeIfPresent(T.self, forKey: k)) ?? def }
        dwellMs = v(.dwellMs, d.dwellMs)
        chunkMs = v(.chunkMs, d.chunkMs)
        ringSeconds = v(.ringSeconds, d.ringSeconds)
        prefetch = v(.prefetch, d.prefetch)
        silentDb = v(.silentDb, d.silentDb)
        frameIntervalMs = v(.frameIntervalMs, d.frameIntervalMs)
        strip = v(.strip, d.strip)
        gridW = v(.gridW, d.gridW)
        gridH = v(.gridH, d.gridH)
        bright = v(.bright, d.bright)
        pixelStride = v(.pixelStride, d.pixelStride)
        changeThreshold = v(.changeThreshold, d.changeThreshold)
        stableThreshold = v(.stableThreshold, d.stableThreshold)
        stableFrames = v(.stableFrames, d.stableFrames)
        deadlineS = v(.deadlineS, d.deadlineS)
        diagEveryFrames = v(.diagEveryFrames, d.diagEveryFrames)
    }
}
