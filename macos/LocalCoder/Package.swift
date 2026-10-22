// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "LocalCoder",
    platforms: [.macOS(.v26)],
    targets: [
        .executableTarget(
            name: "LocalCoder",
            path: "Sources",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
    ]
)
