import Foundation
import Darwin
import RuntimeCore

/// Native protocol fixtures only. The root wrapper provides the finite budget
/// and preserves actual stdout/stderr/exit; this entry cannot start MLX or GPU.
@main struct EBD1NativeCPU {
    static func main() async {
        let roots=ExternalDemandNativeRoots()
        let report=await runExternalDemandNativeControls(roots)
        do {
            try FileHandle.standardOutput.write(contentsOf:report.encoded()+Data([10]))
            let passed=try report.field("passed").boolean()
            withExtendedLifetime(roots) {}
            Darwin.exit(passed ? 0:1)
        } catch {
            let diagnostic=JSONValue.object(["passed":.bool(false),"output_error":.string(String(describing:error)),
                                             "retained_roots":roots.snapshot()])
            try? FileHandle.standardError.write(contentsOf:diagnostic.encoded()+Data([10]))
            withExtendedLifetime(roots) {}
            Darwin.exit(2)
        }
    }
}
