import Darwin
import MLXTransport

@main enum CWCReplayCLI {
    static func main() async {
        let code = await CWCReplay.run(arguments: Array(CommandLine.arguments.dropFirst()))
        exit(code)
    }
}
