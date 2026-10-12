import AppIntents
import Foundation
import VisualIntelligence

/// Test-build probe for the zero-setup iOS path: does Asterisk show up when
/// the user screenshots a reel and opens Visual Intelligence, and what does
/// the system hand us? Logs what arrives; the real flow (OCR the handle and
/// caption, find the video server-side) is built only if this fires.
///
/// The extension's deployment target is 17.0 (@bacons/apple-targets hardcodes
/// it for app-intent targets), hence the availability gate.
struct ScreenCheckEntity: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Fact-check"
    static let defaultQuery = ScreenCheckQuery()
    let id: String

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "Fact-check this video",
                              subtitle: "Asterisk",
                              image: .init(systemName: "checkmark.seal"))
    }
}

struct ScreenCheckQuery: EntityQuery {
    func entities(for identifiers: [String]) async throws -> [ScreenCheckEntity] {
        identifiers.map { ScreenCheckEntity(id: $0) }
    }
}

@available(iOS 26.0, *)
struct ScreenshotValueQuery: IntentValueQuery {
    func values(for input: SemanticContentDescriptor) async throws -> [ScreenCheckEntity] {
        let url = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Shared.appGroup)?
            .appendingPathComponent("diag-intent.jsonl")
        url.map { DiagLog(url: $0) }?.write("vi_query", [
            "labels": input.labels.prefix(10).joined(separator: "|"),
            "has_pixels": String(input.pixelBuffer != nil),
            "pixels": input.pixelBuffer.map { String(describing: $0) }.map { String($0.prefix(200)) } ?? "-",
        ])
        return [ScreenCheckEntity(id: UUID().uuidString)]
    }
}
