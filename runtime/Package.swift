// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "CoreLanguageMLXForkRuntime",
    platforms: [.macOS(.v15)],
    products: [
        .library(name: "RuntimeCore", targets: ["RuntimeCore"]),
        .executable(name: "cm2-checked", targets: ["CM2Checked"]),
        .executable(name: "cm2-deterministic", targets: ["CM2Deterministic"]),
        .executable(name: "cm2-reference", targets: ["CM2Reference"]),
        .executable(name: "cm2-cpu", targets: ["CM2CPU"]),
        .executable(name: "ebd1-native-cpu", targets: ["EBD1NativeCPU"]),
        .executable(name: "ebd1-source-cpu", targets: ["EBD1SourceCPU"]),
        .executable(name: "jrd1-cpu", targets: ["JRD1CPU"]),
        .executable(name: "src1-cpu", targets: ["SRC1CPU"]),
        .executable(name: "src1-decoder-cpu", targets: ["SRC1DecoderCPU"]),
        .executable(name: "src1b-checked", targets: ["SRC1BChecked"]),
        .executable(name: "src1b-cpu", targets: ["SRC1BCPU"]),
        .executable(name: "src1b-decoder-cpu", targets: ["SRC1BDecoderCPU"]),
        .executable(name: "src1b-parser-cpu", targets: ["SRC1BParserCPU"]),
        .executable(name: "src1b-boundary-cpu", targets: ["SRC1BBoundaryCPU"]),
        .executable(name: "brc-replay", targets: ["BRCReplayCLI"]),
        .executable(name: "cwc-replay", targets: ["CWCReplayCLI"])
    ],
    targets: [
        .target(name: "RuntimeCore"),
        .target(name: "MLXTransport", dependencies: ["RuntimeCore"]),
        .target(name: "CM2CLI", dependencies: ["RuntimeCore", "MLXTransport"], exclude: ["baseline-origin.json"]),
        .executableTarget(name: "CM2Checked", dependencies: ["CM2CLI"]),
        .executableTarget(name: "CM2Deterministic", dependencies: ["CM2CLI"]),
        .executableTarget(name: "CM2Reference", dependencies: ["CM2CLI"]),
        .executableTarget(name: "CM2CPU", dependencies: ["CM2CLI"]),
        .executableTarget(name: "EBD1NativeCPU", dependencies: ["RuntimeCore"]),
        .executableTarget(name: "EBD1SourceCPU", dependencies: ["CM2CLI"]),
        .executableTarget(name: "JRD1CPU", dependencies: ["CM2CLI"]),
        .executableTarget(name: "SRC1CPU", dependencies: ["CM2CLI"]),
        .executableTarget(name: "SRC1DecoderCPU", dependencies: ["RuntimeCore"]),
        .executableTarget(name: "SRC1BChecked", dependencies: ["CM2CLI"]),
        .executableTarget(name: "SRC1BCPU", dependencies: ["CM2CLI"]),
        .executableTarget(name: "SRC1BDecoderCPU", dependencies: ["RuntimeCore"]),
        .executableTarget(name: "SRC1BParserCPU", dependencies: ["CM2CLI"]),
        .executableTarget(name: "SRC1BBoundaryCPU", dependencies: ["CM2CLI"]),
        .executableTarget(name: "BRCReplayCLI", dependencies: ["MLXTransport"]),
        .executableTarget(name: "CWCReplayCLI", dependencies: ["MLXTransport"])
    ]
)
