// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "AblationL1",
    platforms: [.macOS(.v15)],
    targets: [
        .executableTarget(name: "AblationL1", path: "Sources/AblationL1")
    ]
)
