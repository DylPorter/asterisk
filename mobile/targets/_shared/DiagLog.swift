import Foundation

/// JSON-lines log in the App Group container, readable by the app's
/// diagnostics screen and shipped to the server's /v2/diag.
///
/// This is the test build's instrument: every unknown about the broadcast
/// extension (buffers per second, memory, swipe distances, tap latency) is
/// written here, so one install on a phone answers all of them.
/// Holds no audio and no screen content -- only counts, levels and timings.
public final class DiagLog: @unchecked Sendable {
    private let url: URL
    private let maxBytes: Int
    private let lock = NSLock()
    private var unsent: [String] = []

    public init(url: URL, maxBytes: Int = 512 * 1024) {
        self.url = url
        self.maxBytes = maxBytes
    }

    public func write(_ event: String, _ fields: [String: String] = [:], nowMs: Int64 = DiagLog.nowMs()) {
        var obj = fields
        obj["t"] = String(nowMs)
        obj["ev"] = event
        guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
              let line = String(data: data, encoding: .utf8) else { return }
        lock.lock(); defer { lock.unlock() }
        unsent.append(line)
        if unsent.count > 2_000 { unsent.removeFirst(unsent.count - 2_000) }
        append(line + "\n")
    }

    /// Lines written since the last call, for shipping to the server.
    public func takeUnsent() -> [String] {
        lock.lock(); defer { lock.unlock() }
        let out = unsent
        unsent.removeAll()
        return out
    }

    public func tail(_ n: Int) -> [String] {
        lock.lock(); defer { lock.unlock() }
        guard let s = try? String(contentsOf: url, encoding: .utf8) else { return [] }
        return Array(s.split(separator: "\n").suffix(n).map(String.init))
    }

    public func clear() {
        lock.lock(); defer { lock.unlock() }
        try? FileManager.default.removeItem(at: url)
    }

    private func append(_ s: String) {
        let fm = FileManager.default
        if let attrs = try? fm.attributesOfItem(atPath: url.path),
           let size = attrs[.size] as? Int, size > maxBytes {
            // Keep the newer half rather than lose the whole run.
            if let all = try? Data(contentsOf: url) {
                try? all.suffix(maxBytes / 2).write(to: url)
            }
        }
        if !fm.fileExists(atPath: url.path) {
            _ = fm.createFile(atPath: url.path, contents: nil)
        }
        guard let h = try? FileHandle(forWritingTo: url) else { return }
        defer { try? h.close() }
        _ = try? h.seekToEnd()
        try? h.write(contentsOf: Data(s.utf8))
    }

    public static func nowMs() -> Int64 { Int64(Date().timeIntervalSince1970 * 1000) }
}
