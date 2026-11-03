import CM2CLI

/// Fixed CPU fixtures only; the checked public entry cannot select these gates.
@main struct JRD1CPU {
    static func main() async { await CM2Entrypoint.runJoinedDeliveryCPU() }
}
