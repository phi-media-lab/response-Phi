import Foundation
import Dispatch
import Synchronization
import CryptoKit
import Darwin
import RuntimeCore

/// This driver creates the compiler process itself. No public path accepts an
/// already-checked artifact, compiler executable, proof, or runtime state.
/// The fixed frontend must not create subprocesses. Only that direct child is
/// managed here; this bridge does not claim arbitrary process-tree cleanup.
final class CompilerBridge {
    static let root = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    static let python = URL(fileURLWithPath:
        "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin/python3.14")
    static let sourceLimit = 1 * 1024 * 1024
    static let responseLimit = 64 * 1024 * 1024

    let evidenceDirectory: URL
    private let cpuHost: CPUFrontendHost?
    private(set) var provenance: JSONValue = .object([:])

    init(evidenceDirectory: URL, cpuHost: CPUFrontendHost? = nil) {
        self.evidenceDirectory = evidenceDirectory; self.cpuHost = cpuHost
    }

    func checkClosedSource(_ source: Data) async throws -> CheckedProgram {
        let exchange = try runCompiler(source)
        let program = try decodeCompilerExchange(source: source, response: exchange.stdout,
                                                actualExitCode: exchange.exitCode)
        try hostRequire(program.executionDomain == .mlxLeaf, "child frontend must produce the child execution domain")
        return program
    }

    /// Parent sources use an explicit, separately named frontend. The old
    /// entry never infers a domain from an unrecognized source command.
    func checkParentSource(_ source: Data) async throws -> CheckedProgram {
        let exchange = try runCompiler(source, parent: true)
        let program = try decodeCompilerExchange(source: source, response: exchange.stdout,
                                                actualExitCode: exchange.exitCode)
        try hostRequire(program.executionDomain == .hostParent, "parent frontend must produce hostParent")
        try validateParentServiceConfiguration(program)
        return program
    }

    private func runCompiler(_ source: Data, parent: Bool = false) throws -> (stdout: Data, exitCode: Int32) {
        guard source.count <= Self.sourceLimit else {
            throw FrontendRejection(status: "execution-profile-not-supported",
                diagnostic: Self.diagnostic("source-limit", "source exceeds 1 MiB"), exitCode: 3)
        }
        let script = Self.root.appendingPathComponent(parent ? "frontend/export_parent.py" : "frontend/export.py")
        let before = try sourceIdentities()
        try source.write(to: evidenceDirectory.appendingPathComponent("source.json"), options: .withoutOverwriting)
        let command = [Self.python.path, "-I", "-S", "-B", script.path]
        let process = Process()
        process.executableURL = Self.python
        process.arguments = Array(command.dropFirst())
        process.currentDirectoryURL = Self.root
        let input = Pipe(), output = Pipe(), errors = Pipe()
        process.standardInput = input; process.standardOutput = output; process.standardError = errors
        let stdout = StreamCapture(limit: Self.responseLimit)
        let stderr = StreamCapture(limit: 1024 * 1024)
        let group = DispatchGroup(), inputFinished = Mutex(false)
        let control = ChildControl(process, identity: .object([
            "command": .array(command.map(JSONValue.string)), "cwd": .string(Self.root.path),
            "evidence_directory": .string(evidenceDirectory.path), "source_sha256": .string(Self.sha(source))]), streams: group) {
            .object(["stdout": stdout.summary(), "stderr": stderr.summary(),
                "stdin_worker_finished": .bool(inputFinished.withLock { $0 })])
        }
        // CPU readers are the sole raw-file writers. Their group leaves only
        // after closing these files, even if the host deadline bypasses this
        // function's later provenance work. Public compilation retains its
        // original collect-then-save behavior and 60-second budget.
        func rawHandle(_ name: String) throws -> FileHandle? {
            guard cpuHost != nil else { return nil }
            let path = evidenceDirectory.appendingPathComponent(name)
            try Data().write(to: path, options: .withoutOverwriting)
            return try FileHandle(forWritingTo: path)
        }
        var ioStarted = false
        let rawOut = try rawHandle("frontend.stdout")
        defer { if !ioStarted { try? rawOut?.close() } }
        let rawErr = try rawHandle("frontend.stderr")
        defer { if !ioStarted { try? rawErr?.close() } }
        defer {
            if !ioStarted {
                for handle in [input.fileHandleForReading, input.fileHandleForWriting,
                               output.fileHandleForReading, output.fileHandleForWriting,
                               errors.fileHandleForReading, errors.fileHandleForWriting] { try? handle.close() }
            }
        }
        let startIO = {
            try? input.fileHandleForReading.close()
            try? output.fileHandleForWriting.close()
            try? errors.fileHandleForWriting.close()
            self.collect(output.fileHandleForReading, into: stdout, label: "stdout", group: group, control: control, raw: rawOut)
            self.collect(errors.fileHandleForReading, into: stderr, label: "stderr", group: group, control: control, raw: rawErr)
            group.enter()
            DispatchQueue.global().async {
                defer {
                    do { try input.fileHandleForWriting.close() }
                    catch { control.stop("stdin-close: \(error)") }
                    inputFinished.withLock { $0 = true }; group.leave()
                }
                do { try input.fileHandleForWriting.write(contentsOf: source) }
                catch { control.stop("stdin-write: \(error)") }
            }
        }
        let started = DispatchTime.now().uptimeNanoseconds
        let waitBudget: Double
        do {
            if let cpuHost { waitBudget = try cpuHost.launch(control, startIO: startIO) }
            else { try control.launch(); startIO(); waitBudget = 60 }
            ioStarted = true
        } catch {
            let after = observedSourceIdentities()
            provenance = .object(["command": .array(command.map(JSONValue.string)),
                                  "launch_error": .string(String(describing: error)),
                                  "pid": .null, "exit_code": .null,
                                  "child_root": control.snapshot(),
                                  "inputs_before": before, "inputs_after": after,
                                  "inputs_unchanged": .bool(before == after)])
            _ = persistArtifacts([])
            throw ProtocolError(diagnostic: Self.diagnostic("frontend-launch", String(describing: error)))
        }
        let pid = process.processIdentifier
        var observedTermination = control.waitForTermination(until: .now() + waitBudget)
        if !observedTermination {
            control.stop("frontend-timeout")
            observedTermination = control.waitForTermination(until: .now() + 2)
        }
        if !observedTermination {
            control.killIfRunning()
            observedTermination = control.waitForTermination(until: .now() + 5)
        }
        let streamsClosed = control.waitForIO(until: .now() + 5)
        let out = stdout.snapshot(), err = stderr.snapshot()
        let after = observedSourceIdentities()
        let status: Int32? = observedTermination ? process.terminationStatus : nil
        let reason = control.reason
        provenance = .object([
            "command": .array(command.map(JSONValue.string)), "cwd": .string(Self.root.path),
            "pid": .integer(Int(pid)), "exit_code": status.map { .integer(Int($0)) } ?? .null,
            "termination_kind": observedTermination ? .string(process.terminationReason == .exit ? "exit" : "signal") : .null,
            "termination_observed": .bool(observedTermination), "streams_closed": .bool(streamsClosed),
            "wait_budget_seconds": .number(String(waitBudget)), "cpu_streaming_stdio": .bool(cpuHost != nil),
            "child_root": control.snapshot(),
            "stop_reason": reason.map(JSONValue.string) ?? .null,
            "duration_ns": .string(String(DispatchTime.now().uptimeNanoseconds - started)),
            "stdout": streamIdentity(out, file: "frontend.stdout"),
            "stderr": streamIdentity(err, file: "frontend.stderr"),
            "source": .object(["path": .string(evidenceDirectory.appendingPathComponent("source.json").path),
                               "bytes": .integer(source.count), "sha256": .string(Self.sha(source))]),
            "inputs_before": before, "inputs_after": after,
            "inputs_unchanged": .bool(before == after)
        ])
        let saveErrors = persistArtifacts(cpuHost == nil ? [("frontend.stdout", out.bytes), ("frontend.stderr", err.bytes)] : [])
        guard before == after, observedTermination, streamsClosed, reason == nil,
              out.eof, err.eof, !out.truncated, !err.truncated, out.error == nil, err.error == nil,
              saveErrors.isEmpty, let status else {
            throw ProtocolError(diagnostic: Self.diagnostic("frontend-process",
                "frontend did not produce a complete unchanged-input exchange; see frontend-process.json"))
        }
        return (out.bytes, status)
    }

    private func collect(_ handle: FileHandle, into capture: StreamCapture, label: String,
                         group: DispatchGroup, control: ChildControl, raw: FileHandle?) {
        group.enter()
        DispatchQueue.global().async {
            defer {
                do { try raw?.close() }
                catch { capture.recordError("raw-close: \(error)"); control.stop("\(label)-raw-close: \(error)") }
                do { try handle.close() }
                catch { capture.recordError("pipe-close: \(error)"); control.stop("\(label)-pipe-close: \(error)") }
                group.leave()
            }
            do {
                while let data = try handle.read(upToCount: 65_536), !data.isEmpty {
                    let retained = capture.append(data)
                    try raw?.write(contentsOf: retained.bytes)
                    if !retained.complete { control.stop("\(label)-limit") }
                }
                capture.markEOF()
            } catch {
                capture.recordError(String(describing: error))
                control.stop("\(label)-read: \(error)")
            }
        }
    }

    private func streamIdentity(_ stream: StreamCapture.State, file: String) -> JSONValue {
        .object(["path": .string(evidenceDirectory.appendingPathComponent(file).path),
                 "bytes": .integer(stream.bytes.count), "sha256": .string(Self.sha(stream.bytes)),
                 "complete": .bool(stream.eof && !stream.truncated && stream.error == nil),
                 "eof": .bool(stream.eof),
                 "hash_scope": .string(stream.eof && !stream.truncated && stream.error == nil ? "complete-stream" : "retained-prefix"),
                 "truncated": .bool(stream.truncated), "error": stream.error.map(JSONValue.string) ?? .null])
    }

    /// Construct the process record before attempting any output. A failed
    /// artifact write must not erase the actual exit or prevent other saves.
    private func persistArtifacts(_ artifacts: [(String, Data)]) -> [String] {
        var failures = [String]()
        var saved = [String: JSONValue]()
        for (name, bytes) in artifacts {
            do {
                try bytes.write(to: evidenceDirectory.appendingPathComponent(name), options: .withoutOverwriting)
                saved[name] = .bool(true)
            } catch {
                saved[name] = .bool(false)
                failures.append("\(name): \(error)")
            }
        }
        guard case .object(var record) = provenance else { return ["internal process record is not an object"] }
        record["artifacts_saved"] = .object(saved)
        record["evidence_errors"] = .array(failures.map(JSONValue.string))
        record["record_saved"] = .bool(true)
        provenance = .object(record)
        do {
            try provenance.encoded().write(to: evidenceDirectory.appendingPathComponent("frontend-process.json"),
                                           options: .withoutOverwriting)
        } catch {
            failures.append("frontend-process.json: \(error)")
            record["evidence_errors"] = .array(failures.map(JSONValue.string))
            record["record_saved"] = .bool(false)
            provenance = .object(record)
        }
        return failures
    }

    private func observedSourceIdentities() -> JSONValue {
        do { return try sourceIdentities() }
        catch { return .object(["observation_error": .string(String(describing: error))]) }
    }

    private func sourceIdentities() throws -> JSONValue {
        let frontend = Self.root.appendingPathComponent("frontend")
        guard let entries = FileManager.default.enumerator(at: frontend,
            includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey], options: [.skipsHiddenFiles]) else {
            throw ProtocolError(diagnostic: Self.diagnostic("frontend-missing", "fixed frontend directory is unavailable"))
        }
        var files = [URL]()
        for case let file as URL in entries where file.pathExtension == "py" {
            let values = try file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            guard values.isRegularFile == true, values.isSymbolicLink != true else {
                throw ProtocolError(diagnostic: Self.diagnostic("frontend-source", "non-regular source file"))
            }
            files.append(file)
        }
        guard files.contains(frontend.appendingPathComponent("export.py")) else {
            throw ProtocolError(diagnostic: Self.diagnostic("frontend-missing", "fixed export.py is unavailable"))
        }
        files.append(Self.python.resolvingSymlinksInPath())
        files.append(Self.python.deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Python").resolvingSymlinksInPath())
        return .array(try files.sorted(by: { $0.path < $1.path }).map { file in
            let data = try Data(contentsOf: file)
            return .object(["path": .string(file.path), "bytes": .integer(data.count), "sha256": .string(Self.sha(data))])
        })
    }

    static func sha(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    static func diagnostic(_ code: String, _ message: String) -> JSONValue {
        .object(["phase": .string("driver"), "code": .string(code), "path": .array([]), "message": .string(message)])
    }
}

private final class StreamCapture: Sendable {
    struct State: Sendable { var bytes = Data(); var truncated = false; var eof = false; var error: String? }
    private let state = Mutex(State())
    private let limit: Int
    init(limit: Int) { self.limit = limit }
    func append(_ data: Data) -> (complete: Bool, bytes: Data) {
        state.withLock { state in
            let remaining = limit - state.bytes.count
            let accepted = Data(data.prefix(remaining))
            state.bytes.append(accepted)
            if data.count > remaining { state.truncated = true }
            return (!state.truncated, accepted)
        }
    }
    func recordError(_ error: String) { state.withLock { $0.error = error } }
    func markEOF() { state.withLock { $0.eof = true } }
    func snapshot() -> State { state.withLock { $0 } }
    func summary() -> JSONValue {
        let s = snapshot()
        return .object(["bytes": .integer(s.bytes.count), "sha256": .string(CompilerBridge.sha(s.bytes)),
            "eof": .bool(s.eof), "truncated": .bool(s.truncated), "error": s.error.map(JSONValue.string) ?? .null])
    }
}

