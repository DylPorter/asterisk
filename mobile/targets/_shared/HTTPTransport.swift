import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// The real server. `app=ios`: unlike Android, the extension cannot tell which
/// app is playing, so the server keys the cache on the transcript alone.
public struct HTTPTransport: Transport {
    public let base: URL
    public let app: String
    private let session: URLSession

    public init(base: URL, app: String = "ios", session: URLSession = .shared) {
        self.base = base
        self.app = app
        self.session = session
    }

    public func postSegment(sessionId: String, seq: Int, wav: Data) async -> Bool {
        var c = URLComponents(url: base.appendingPathComponent("v2/segments"), resolvingAgainstBaseURL: false)!
        c.queryItems = [.init(name: "session_id", value: sessionId),
                        .init(name: "seq", value: String(seq)),
                        .init(name: "app", value: app)]
        var r = URLRequest(url: c.url!, timeoutInterval: 15)
        r.httpMethod = "POST"
        r.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
        r.httpBody = wav
        guard let (_, resp) = try? await session.data(for: r) else { return false }
        return (resp as? HTTPURLResponse)?.statusCode == 200
    }

    public func check(sessionId: String, finalSeq: Int, deadlineS: Double) async -> Data? {
        let body: [String: Any] = ["session_id": sessionId, "app": app, "titles": [String](),
                                   "channel": "", "final_seq": finalSeq, "deadline_s": deadlineS]
        return await postJSON("v2/check", body, timeout: deadlineS + 10)
    }

    public func fetchConfig() async -> CaptureConfig? {
        var r = URLRequest(url: base.appendingPathComponent("v2/config"), timeoutInterval: 5)
        r.httpMethod = "GET"
        guard let (data, resp) = try? await session.data(for: r),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(CaptureConfig.self, from: data)
    }

    /// Ship diagnostic lines to the server so a test run on Alan's phone can be
    /// read from here. Best effort; failures are dropped.
    public func postDiag(_ lines: [String], source: String) async {
        _ = await postJSON("v2/diag", ["source": source, "lines": lines], timeout: 10)
    }

    private func postJSON(_ path: String, _ obj: [String: Any], timeout: Double) async -> Data? {
        guard let body = try? JSONSerialization.data(withJSONObject: obj) else { return nil }
        var r = URLRequest(url: base.appendingPathComponent(path), timeoutInterval: timeout)
        r.httpMethod = "POST"
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = body
        guard let (data, resp) = try? await session.data(for: r),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return data
    }
}
