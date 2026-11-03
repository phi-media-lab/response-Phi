import Darwin
import MLXTransport

@main enum BRCReplayCLI {
    static func main() async {
        let code = await BRCReplay.run(arguments: Array(CommandLine.arguments.dropFirst()))
        exit(code)
    }
}
