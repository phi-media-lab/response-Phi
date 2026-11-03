import CM2CLI

@main struct SRC1BCPU {
    static func main() async { await ParentEntrypoint.runCPU() }
}
