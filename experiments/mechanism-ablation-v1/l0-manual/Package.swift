// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "AblationManual",
    platforms: [.macOS(.v15)],
    targets: [
        .executableTarget(name: "ablation-l0", path: "Sources/AblationL0")
    ]
)
