import Foundation

/// What the tap shows: parsed from last-result.json, which the broadcast
/// extension writes as {"kind": verdict|not_ready|silent|failed, "body": <the
/// /v2/check response>, "detail": ...}. Lenient on purpose -- a missing field
/// shows less, it never fails the whole card.
public struct VerdictSummary: Equatable, Sendable {
    public struct Claim: Equatable, Sendable {
        public let claim: String, rating: String, note: String, quote: String
        public let sources: [String]
    }
    public let title: String
    public let overall: String          // clear | caution | misleading | unverifiable | none
    public let score: Int?
    public let claims: [Claim]
    public let seconds: Double?
    public let cached: Bool

    public static func parse(_ data: Data) -> VerdictSummary {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .message("Couldn't read the result")
        }
        switch root["kind"] as? String {
        case "not_ready": return .message(root["detail"] as? String ?? "Not ready yet")
        case "silent": return .message("Couldn't hear this video", overall: "none")
        case "failed": return .message(root["detail"] as? String ?? "Check failed")
        case "verdict": break
        default: return .message("Couldn't read the result")
        }
        let body = root["body"] as? [String: Any] ?? [:]
        let status = body["status"] as? String ?? "error"
        if status == "no_transcript" { return .message("No speech in this video", overall: "none") }
        if status == "captured_silent" { return .message("Couldn't hear this video", overall: "none") }
        guard status == "ok", let v = body["verdict"] as? [String: Any] else {
            return .message(body["detail"] as? String ?? "Check failed")
        }
        let claims = (v["claims"] as? [[String: Any]] ?? []).map {
            Claim(claim: $0["claim"] as? String ?? "", rating: $0["rating"] as? String ?? "",
                  note: $0["note"] as? String ?? "", quote: $0["quote"] as? String ?? "",
                  sources: $0["sources"] as? [String] ?? [])
        }
        return VerdictSummary(
            title: (v["headline"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "Checked",
            overall: v["overall"] as? String ?? "unverifiable",
            score: v["score"] as? Int, claims: claims,
            seconds: body["seconds"] as? Double, cached: body["cached"] as? Bool ?? false)
    }

    static func message(_ s: String, overall: String = "none") -> VerdictSummary {
        VerdictSummary(title: s, overall: overall, score: nil, claims: [], seconds: nil, cached: false)
    }

    /// One line for Siri-style dialog output.
    public var spoken: String {
        guard !claims.isEmpty else { return title }
        let bad = claims.filter { ["contradicted", "misleading"].contains($0.rating) }.count
        return bad == 0 ? "\(title). \(claims.count) claim\(claims.count == 1 ? "" : "s") checked."
                        : "\(title). \(bad) of \(claims.count) claims don't hold up."
    }
}
