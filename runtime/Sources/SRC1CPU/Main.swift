import CM2CLI

/// Fixed CPU source-response fixtures; no public checked-entry gate selection.
@main struct SRC1CPU {
    static func main() async { await CM2Entrypoint.runSourceResponseCPU() }
}
