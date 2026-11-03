import CM2CLI

/// This separate native entry is the sole selector for the fixed EBD1 source
/// fixtures. The public checked entry cannot select a stub or a test policy.
@main struct EBD1SourceCPU {
    static func main() async { await CM2Entrypoint.runExternalCPU() }
}
