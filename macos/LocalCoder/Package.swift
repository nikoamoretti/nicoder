// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LocalCoder",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "LocalCoder",
            path: "Sources"
        ),
    ]
)
