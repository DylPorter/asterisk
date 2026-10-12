import AppIntents
import Foundation
import SwiftUI

@main
struct AsteriskIntentsExtension: AppIntentsExtension {}

private let group = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Shared.appGroup)
private let diag = group.map { DiagLog(url: $0.appendingPathComponent("diag-intent.jsonl")) }

/// Back Tap / Action Button / Siri: "check the video I'm watching".
///
/// Runs in this extension's process, not the broadcast's, so it only signals:
/// a Darwin notification wakes the broadcast extension, which uploads the
/// last chunk, calls /v2/check and writes last-result.json. This waits for
/// that file and shows the verdict as a snippet over whatever app is open.
struct CheckVideoIntent: AppIntent {
    static let title: LocalizedStringResource = "Check this video"
    static let description = IntentDescription("Fact-check the short video playing right now.")
    static let openAppWhenRun = false

    func perform() async throws -> some IntentResult & ProvidesDialog & ShowsSnippetView {
        let t0 = DiagLog.nowMs()
        diag?.write("intent_start", [:], nowMs: t0)

        guard broadcastAlive() else {
            diag?.write("intent_no_broadcast")
            let s = VerdictSummary.parse(Data(#"{"kind":"not_ready","detail":"Start Asterisk first: open the app and tap Start."}"#.utf8))
            return .result(dialog: IntentDialog(stringLiteral: s.title), view: VerdictSnippet(summary: s))
        }

        CFNotificationCenterPostNotification(CFNotificationCenterGetDarwinNotifyCenter(),
                                             CFNotificationName(Shared.checkRequested as CFString), nil, nil, true)

        // Poll rather than observe: Darwin observers need a run loop this
        // process may not spin, and 200 ms granularity is invisible next to a
        // multi-second fact-check.
        let deadline = t0 + 35_000
        while DiagLog.nowMs() < deadline {
            if let data = resultNewer(than: t0) {
                let s = VerdictSummary.parse(data)
                diag?.write("intent_done", ["ms": String(DiagLog.nowMs() - t0), "overall": s.overall])
                return .result(dialog: IntentDialog(stringLiteral: s.spoken), view: VerdictSnippet(summary: s))
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        diag?.write("intent_timeout")
        let s = VerdictSummary.parse(Data(#"{"kind":"failed","detail":"Timed out waiting for the check"}"#.utf8))
        return .result(dialog: IntentDialog(stringLiteral: s.title), view: VerdictSnippet(summary: s))
    }

    private func broadcastAlive() -> Bool {
        guard let url = group?.appendingPathComponent(Shared.statusFile),
              let data = try? Data(contentsOf: url),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              obj["running"] as? Bool == true,
              let beat = (obj["beat"] as? NSNumber)?.int64Value else { return false }
        return DiagLog.nowMs() - beat < 6_000
    }

    private func resultNewer(than t0: Int64) -> Data? {
        guard let url = group?.appendingPathComponent(Shared.resultFile),
              let data = try? Data(contentsOf: url),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let req = (obj["requested"] as? NSNumber)?.int64Value, req >= t0 - 1_000 else { return nil }
        return data
    }
}

struct AsteriskShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: CheckVideoIntent(),
                    phrases: ["Check this video with \(.applicationName)", "\(.applicationName) check this"],
                    shortTitle: "Check video",
                    systemImageName: "checkmark.seal")
    }
}

/// The card shown over TikTok/Reels/Shorts. Kept small: a snippet is not a screen.
struct VerdictSnippet: View {
    let summary: VerdictSummary

    private var tint: Color {
        switch summary.overall {
        case "clear": return .green
        case "caution": return .orange
        case "misleading": return .red
        default: return .gray
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Circle().fill(tint).frame(width: 12, height: 12)
                Text(summary.title).font(.headline)
            }
            ForEach(Array(summary.claims.prefix(3).enumerated()), id: \.offset) { _, c in
                VStack(alignment: .leading, spacing: 2) {
                    Text(c.claim).font(.subheadline).bold()
                    Text("\(c.rating.capitalized): \(c.note)").font(.caption).foregroundStyle(.secondary)
                }
            }
            if summary.claims.count > 3 {
                Text("+\(summary.claims.count - 3) more in the Asterisk app").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding()
    }
}
