// swift-tools-version:5.9
// Linux-testable core shared by the iOS targets (targets/_shared is linked
// into every Xcode target by @bacons/apple-targets). Run: swift test
import PackageDescription

let package = Package(
    name: "AsteriskCore",
    products: [.library(name: "AsteriskCore", targets: ["AsteriskCore"])],
    targets: [
        .target(name: "AsteriskCore", path: "targets/_shared"),
        .testTarget(name: "AsteriskCoreTests", dependencies: ["AsteriskCore"], path: "Tests/AsteriskCoreTests"),
    ]
)
