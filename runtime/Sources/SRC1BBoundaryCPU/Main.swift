import CM2CLI

@main
enum SRC1BBoundaryMain {
    static func main() async { await ParentEntrypoint.runBoundaryCPU() }
}
