// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ClaudeMenuBar",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "ClaudeMenuBar", targets: ["ClaudeMenuBar"])],
    targets: [
        .target(name: "ClaudeMenuBarKit"),
        .executableTarget(name: "ClaudeMenuBar", dependencies: ["ClaudeMenuBarKit"]),
        .testTarget(name: "ClaudeMenuBarKitTests", dependencies: ["ClaudeMenuBarKit"]),
    ]
)
