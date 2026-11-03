import CM2CLI

@main struct SRC1BChecked {
    static func main() async { await ParentEntrypoint.runChecked() }
}
