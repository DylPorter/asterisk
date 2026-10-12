import ExpoModulesCore
import ReplayKit

// Duplicated from targets/_shared/Shared.swift: that folder is linked into the
// app and extension targets, not into this pod. Keep the two in step.
private enum Names {
  static let appGroup = "group.dev.tdporter.asterisk"
  static let serverKey = "server"
  static let statusFile = "broadcast-status.json"
  static let resultFile = "last-result.json"
  static let diagFiles = ["diag.jsonl", "diag-intent.jsonl", "diag-app.jsonl"]
}

private func container() -> URL? {
  FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Names.appGroup)
}

private func read(_ name: String) -> String? {
  guard let url = container()?.appendingPathComponent(name) else { return nil }
  return try? String(contentsOf: url, encoding: .utf8)
}

/// The system "start broadcast" button, preset to our extension. It has to be
/// Apple's own view: a broadcast can only be started by the user tapping it.
class BroadcastPickerView: ExpoView {
  private let picker = RPSystemBroadcastPickerView(frame: .zero)

  required init(appContext: AppContext? = nil) {
    super.init(appContext: appContext)
    picker.showsMicrophoneButton = false
    if let id = Bundle.main.bundleIdentifier {
      picker.preferredExtension = id + ".broadcast"
    }
    addSubview(picker)
  }

  override func layoutSubviews() {
    super.layoutSubviews()
    picker.frame = bounds
    // Stretch Apple's inner button over the whole view so the JS label on top is tappable.
    for case let b as UIButton in picker.subviews { b.frame = picker.bounds }
  }
}

public class AsteriskBridgeModule: Module {
  public func definition() -> ModuleDefinition {
    Name("AsteriskBridge")

    Function("setServer") { (url: String) in
      UserDefaults(suiteName: Names.appGroup)?.set(url, forKey: Names.serverKey)
    }

    Function("getServer") { () -> String in
      UserDefaults(suiteName: Names.appGroup)?.string(forKey: Names.serverKey) ?? ""
    }

    Function("status") { () -> String? in read(Names.statusFile) }

    Function("lastResult") { () -> String? in read(Names.resultFile) }

    /// The newest `n` diagnostic lines across the extension, intents and app logs, oldest first.
    Function("diagTail") { (n: Int) -> [String] in
      var lines: [String] = []
      for f in Names.diagFiles {
        lines += (read(f) ?? "").split(separator: "\n").suffix(n).map(String.init)
      }
      func t(_ s: String) -> Int64 {
        guard let r = s.range(of: "\"t\":\"") else { return 0 }
        return Int64(s[r.upperBound...].prefix { $0.isNumber }) ?? 0
      }
      return Array(lines.sorted { t($0) < t($1) }.suffix(n))
    }

    Function("clearDiag") {
      for f in Names.diagFiles + [Names.resultFile] {
        if let url = container()?.appendingPathComponent(f) { try? FileManager.default.removeItem(at: url) }
      }
    }

    View(BroadcastPickerView.self) {}
  }
}
