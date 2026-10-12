import Foundation

/// Names shared by the app, the broadcast extension and the intents extension.
public enum Shared {
    public static let appGroup = "group.dev.tdporter.asterisk"
    /// Darwin notifications carry no payload; data goes through the App Group container.
    public static let checkRequested = "dev.tdporter.asterisk.check"
    public static let resultReady = "dev.tdporter.asterisk.result"
    public static let resultFile = "last-result.json"
    public static let diagFile = "diag.jsonl"
    public static let statusFile = "broadcast-status.json"
    public static let serverKey = "server"
    /// No baked-in server: the app asks for the URL on first launch.
    public static let defaultServer = ""
}
