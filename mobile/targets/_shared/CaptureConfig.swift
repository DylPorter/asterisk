import Foundation

/// Every tunable the extension uses, fetched from `GET /v2/config` at
/// broadcast start so a wrong guess costs a server restart, not an EAS build.
/// Decoding is lenient: a missing or malformed field keeps its default.
///
/// The server is not trusted. Every numeric field is clamped to a safe range,
/// since the strip region and grid drive pixel indexing in the extension.
/// Nor is it trusted with the privacy gate. Whatever it sends,
/// `dwellMs` and the buffer bounds are clamped here, and `prefetch` is never
/// decoded at all: it comes only from the user's own setting on the phone.
public struct CaptureConfig: Codable, Equatable, Sendable {
    public var dwellMs: Int64 = 5_000
    public var chunkMs: Int64 = 5_000
    public var ringSeconds: Int = 60
    /// Upload chunks after the dwell, before any tap (Android's behaviour).
    /// iOS cannot restrict capture to the three video apps the way Android's
    /// addMatchingUid does, so with this on, any app's audio that plays for
    /// 5 s while broadcasting is transcribed. Off = upload only on tap.
    /// Local only -- see `applying(userPrefetch:)`; never read from the server.
    public var prefetch = false

    public static let minDwellMs: Int64 = 3_000
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

    private static func unit(_ x: Float, _ def: Float) -> Float { x.isFinite ? min(1, max(0, x)) : def }

    /// The user's own choice, stored on the phone.
    public func applying(userPrefetch: Bool) -> CaptureConfig {
        var c = self
        c.prefetch = userPrefetch
        return c
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = CaptureConfig()
        func v<T: Decodable>(_ k: CodingKeys, _ def: T) -> T { (try? c.decodeIfPresent(T.self, forKey: k)) ?? def }
        dwellMs = min(60_000, max(Self.minDwellMs, v(.dwellMs, d.dwellMs)))
        chunkMs = min(15_000, max(2_000, v(.chunkMs, d.chunkMs)))
        ringSeconds = min(120, max(10, v(.ringSeconds, d.ringSeconds)))
        prefetch = false
        silentDb = min(0, max(-120, v(.silentDb, d.silentDb)))
        frameIntervalMs = min(2_000, max(50, v(.frameIntervalMs, d.frameIntervalMs)))
        strip = v(.strip, d.strip).clamped()
        // Grid and stride bound the per-frame work and allocation in a 50 MB process.
        gridW = min(64, max(1, v(.gridW, d.gridW)))
        gridH = min(32, max(1, v(.gridH, d.gridH)))
        bright = min(255, max(0, v(.bright, d.bright)))
        pixelStride = min(32, max(1, v(.pixelStride, d.pixelStride)))
        changeThreshold = Self.unit(v(.changeThreshold, d.changeThreshold), d.changeThreshold)
        stableThreshold = Self.unit(v(.stableThreshold, d.stableThreshold), d.stableThreshold)
        stableFrames = min(30, max(1, v(.stableFrames, d.stableFrames)))
        deadlineS = min(60, max(5, v(.deadlineS, d.deadlineS)))
        diagEveryFrames = min(1_000, max(0, v(.diagEveryFrames, d.diagEveryFrames)))
    }
}
