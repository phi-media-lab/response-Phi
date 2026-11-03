import CM2CLI

@main struct CM2DeterministicMain {
    static func main() async { await CM2Entrypoint.runDeterministic() }
}
