import Foundation
import Synchronization
import Dispatch
import Darwin
import RuntimeCore

// A protocol pipe must return a positive short read immediately. Do not fill
// the requested 4096-byte capacity before exposing a HELLO/permit frame. These
// calls run only on the existing dedicated reader queues; existing session
// deadlines and outer process cleanup continue to bound blocked IO.
private func readPipeChunk(_ descriptor: Int32, _ label: String) throws -> Data {
    var bytes = [UInt8](repeating: 0, count: 4096)
    while true {
        let count = bytes.withUnsafeMutableBytes { Darwin.read(descriptor, $0.baseAddress!, $0.count) }
        if count > 0 { return Data(bytes.prefix(count)) }
        if count == 0 { return Data() }
        let code = errno
        if code == EINTR { continue }
        throw TransportFault("pipe_read", "\(label), fd \(descriptor), errno \(code)")
    }
}

// Pipe handles are confined to their dedicated queues after launch. Spawn uses
// explicit POSIX file actions and inherits the wrapper-controlled Swift group;
// neither SETPGROUP nor SETSID is set. waitpid is the sole actual worker reap.
private final class ProcessHandles: @unchecked Sendable {
    let input = Pipe(), output = Pipe(), error = Pipe()
    let ends: [String: HostFileRoot]
    init() {
        ends = [
            "stdin-read": HostFileRoot(input.fileHandleForReading, role: "stdin-read"),
            "stdin-write": HostFileRoot(input.fileHandleForWriting, role: "stdin-write"),
            "stdout-read": HostFileRoot(output.fileHandleForReading, role: "stdout-read"),
            "stdout-write": HostFileRoot(output.fileHandleForWriting, role: "stdout-write"),
            "stderr-read": HostFileRoot(error.fileHandleForReading, role: "stderr-read"),
            "stderr-write": HostFileRoot(error.fileHandleForWriting, role: "stderr-write")
        ]
    }
    func close(_ role: String) throws { try ends[role]!.close() }
    var allClosed: Bool { ends.values.allSatisfy(\.closed) }
    var closeFailures: [TransportFault] { ends.keys.sorted().compactMap { ends[$0]!.failure } }
    func snapshot() -> WireJSON { .array(ends.keys.sorted().map { ends[$0]!.snapshot() }) }
    func spawn(_ executable: String, arguments: [String], environment: [String: String]) throws -> pid_t {
        var actions: posix_spawn_file_actions_t? = nil
        var attributes: posix_spawnattr_t? = nil
        func checked(_ code: Int32, _ operation: String) throws {
            if code != 0 { throw TransportFault("spawn", "\(operation): errno \(code)") }
        }
        try checked(posix_spawn_file_actions_init(&actions), "file-actions init")
        defer { posix_spawn_file_actions_destroy(&actions) }
        try checked(posix_spawnattr_init(&attributes), "attributes init")
        defer { posix_spawnattr_destroy(&attributes) }
        // Close every unrelated inherited fd. Only the three dup2 targets are
        // inherited; explicit source/unused pipe ends are all closed in child.
        try checked(posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT)), "CLOEXEC_DEFAULT")
        let child = [(input.fileHandleForReading.fileDescriptor, STDIN_FILENO), (output.fileHandleForWriting.fileDescriptor, STDOUT_FILENO), (error.fileHandleForWriting.fileDescriptor, STDERR_FILENO)]
        for (source, target) in child { try checked(posix_spawn_file_actions_adddup2(&actions, source, target), "dup2") }
        for handle in [input.fileHandleForReading, input.fileHandleForWriting, output.fileHandleForReading, output.fileHandleForWriting, error.fileHandleForReading, error.fileHandleForWriting] {
            try require(handle.fileDescriptor > STDERR_FILENO, "standard descriptors must already exist")
            try checked(posix_spawn_file_actions_addclose(&actions, handle.fileDescriptor), "child close")
        }
        var argv = ([executable] + arguments).map { strdup($0) } + [nil]
        var envp = environment.keys.sorted().map { strdup("\($0)=\(environment[$0]!)") } + [nil]
        defer { argv.forEach { free($0) }; envp.forEach { free($0) } }
        try require(argv.dropLast().allSatisfy { $0 != nil } && envp.dropLast().allSatisfy { $0 != nil }, "spawn C strings")
        var pid: pid_t = 0
        let code = argv.withUnsafeMutableBufferPointer { av in envp.withUnsafeMutableBufferPointer { ev in
            posix_spawn(&pid, executable, &actions, &attributes, av.baseAddress!, ev.baseAddress!)
        } }
        try checked(code, "posix_spawn")
        // The parent retains only stdin-write/stdout-read/stderr-read. These
        // closes are essential for actual EOF after worker termination.
        // Keep the spawned PID even if a parent-side close fails. Session must
        // start its real reap/read queues and retain both process and fd roots.
        for role in ["stdin-read", "stdout-write", "stderr-write"] {
            do { try close(role) } catch { /* exact failure retained in ends */ }
        }
        return pid
    }
}

// One independently rooted stdin reader for the whole Swift process. CPU
// workers use distinct routing IDs. Only an OS-checking outer wrapper can send
// LOAD_PERMIT; Swift never creates one. A control EOF is not a permit.
final class TransportPermitRouter: Sendable {
    private struct State {
        var receivers: [String: @Sendable (Result<WireJSON, TransportFault>) -> Void] = [:]
        var started = false, ended = false, closing = false, readerJoined = false
        var firstFault: TransportFault?
        var closeFailures: [WireJSON] = []
    }
    private let state = Mutex(State())
    private let reader = DispatchQueue(label: "cm1.wrapper-control-reader")
    private let trace: TransportTrace
    private let ended = TransportPromise<Bool>()
    init(_ trace: TransportTrace) { self.trace = trace }
    func register(_ id: String, _ receive: @escaping @Sendable (Result<WireJSON, TransportFault>) -> Void) {
        let ended = state.withLock { s in s.receivers[id] = receive; return s.ended }
        if ended { receive(.failure(TransportFault("control_eof", "wrapper control already closed"))) }
    }
    func unregister(_ id: String) { state.withLock { $0.receivers.removeValue(forKey: id) } }
    func start() throws {
        try state.withLock { s in
            try require(!s.started && !s.closing, "permit reader activation once before close")
            s.started = true
        }
        reader.async { [self] in
            var buffer = Data()
            while true {
                let chunk: Data
                do { chunk = try readPipeChunk(STDIN_FILENO, "wrapper control") }
                catch { end((error as? TransportFault) ?? TransportFault("pipe_read", String(describing: error))); return }
                if chunk.isEmpty { end(TransportFault("control_eof", buffer.isEmpty ? "wrapper stdin EOF" : "truncated wrapper permit")); return }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 10) {
                    let line = Data(buffer[..<newline]); buffer.removeSubrange(...newline)
                    if line.count + 1 > 1_048_576 { end(TransportFault("control_frame", "wrapper frame including LF exceeds bound")); return }
                    do {
                        let value = try WireJSON.parse(line)
                        guard let id = value["worker_id"].string, let receive = state.withLock({ $0.receivers[id] }) else { throw TransportFault("control_identity", "unknown permit worker") }
                        receive(.success(value))
                    } catch { end((error as? TransportFault) ?? TransportFault("control_frame", String(describing: error))); return }
                }
                if buffer.count >= 1_048_576 { end(TransportFault("control_frame", "unterminated wrapper frame leaves no room for LF")); return }
            }
        }
    }
    private func end(_ fault: TransportFault) {
        let observed = state.withLock { s -> ([ @Sendable (Result<WireJSON, TransportFault>) -> Void ], Bool) in
            s.ended = true
            let normal = s.closing && fault.code == "control_eof" && fault.detail == "wrapper stdin EOF"
            if !normal && s.firstFault == nil { s.firstFault = fault }
            return (Array(s.receivers.values), normal)
        }
        trace.record("control.reader_ended", ["fault": fault.json])
        ended.resolve(observed.1 ? .success(true) : .failure(fault))
        observed.0.forEach { $0(.failure(fault)) }
    }
    func finish() async throws {
        let observed = try state.withLock { s -> (Bool, Bool) in
            try require(s.receivers.isEmpty, "control close only after all worker routes retired")
            let first = !s.closing; s.closing = true; return (s.started, first)
        }
        guard observed.0 else { return } // No reader was created; not a join.
        if observed.1 { trace.record("control_close", ["kind": .string("control_close")]) }
        ended.deadline(3, TransportFault("control_close_timeout", "wrapper stdin EOF"))
        var first: TransportFault?
        do { _ = try await ended.wait() }
        catch { first = retainCloseFailure(error, operation: "original-end-wait") }
        do { try await observeOriginalReaderReturn() }
        catch {
            // observeOriginalReaderReturn has already retained this distinct
            // fence failure, even when an earlier end-wait failure wins return.
            if first == nil { first = (error as? TransportFault) ?? TransportFault("control_fence", String(describing: error)) }
        }
        if let first { throw first }
    }
    private func retainCloseFailure(_ error: any Error, operation: String) -> TransportFault {
        let fault = (error as? TransportFault) ?? TransportFault("control_close", String(describing: error))
        let row: WireJSON = .object(["operation": .string(operation), "observation_id": .string(UUID().uuidString),
            "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)), "fault": fault.json])
        state.withLock { $0.closeFailures.append(row) }
        trace.record("control.close_observation_failed", ["observation": row])
        return fault
    }
    /// Separate from the original end promise, which can have permanently
    /// reported timeout/error. The fence records actual late reader return.
    func observeOriginalReaderReturn() async throws {
        let flags = state.withLock { ($0.started, $0.readerJoined) }
        guard flags.0 && !flags.1 else { return }
        let fence = TransportPromise<Bool>()
        reader.async { [self] in state.withLock { $0.readerJoined = true }; fence.resolve(.success(true)) }
        fence.deadline(3, TransportFault("control_fence_timeout", "original permit reader"))
        do { _ = try await fence.wait() }
        catch { throw retainCloseFailure(error, operation: "original-reader-fence") }
        trace.record("control.reader_joined")
    }
    func snapshot() -> WireJSON { state.withLock { .object([
        "reader_started": .bool($0.started), "reader_ended": .bool($0.ended), "reader_joined": .bool($0.readerJoined),
        "closing": .bool($0.closing), "first_fault": $0.firstFault?.json ?? .null, "close_failures": .array($0.closeFailures),
        "registered_workers": .strings(Array($0.receivers.keys).sorted())]) } }
}

final class TransportSession: Sendable {
    private struct Pending: Sendable {
        let kind: String, seq: Int, command: WireJSON, lease: WireLease?, spec: WireRequest?
        let reply: TransportPromise<WireJSON>
        var sawEnter = false
        var enterLayers: WireJSON?
    }
    private struct State {
        var pid = 0, eventSeq = 0, nextSeq = 1
        var startClaimed = false, startReturned = false, spawnAttempted = false, routeRegistered = false
        var stdoutStarted = false, stderrStarted = false, stdinStarted = false, reapStarted = false
        var stdoutFenced = false, stderrFenced = false, stdinFenced = false, reapFenced = false
        var startupAbortClaimed = false
        var outputFiles: [HostFileRoot] = []
        var fenceFailures: [WireJSON] = []
        var phase = "created", permit = false, ready: WireJSON?, pending: Pending?, receipt: WireReceipt?
        var fault: TransportFault?
        var processExited = false, exitCode: Int?, stdoutEOF = false, stderrEOF = false, shutdownACK = false
        var waitStatus: Int?, terminationSignal: Int?
        var stdoutJoined = false, stderrJoined = false, stdinClosed = false, stdinCloseIssued = false
        var terminalError: WireJSON?, trailingBytes = 0
        var rawFrames = 0, commands = 0
        var evalClaims = 0, completedReleases = 0
        var requestIDs: Set<String> = []
        var pageEpochs: [String: [Int: Int]] = [:]
        var validatedReceipts: [WireReceipt] = [], validatedACKs: [WireJSON] = []
        var cpuEnter: WireJSON?, cpuContinue: WireJSON?, cpuContinueACK: WireJSON?
        var cpuEnterWaitCalled = false
    }
    private let state = Mutex(State())
    private let handles = ProcessHandles()
    private let stdoutQueue = DispatchQueue(label: "cm1.worker-stdout")
    private let stderrQueue = DispatchQueue(label: "cm1.worker-stderr")
    private let writeQueue = DispatchQueue(label: "cm1.worker-stdin")
    private let reapQueue = DispatchQueue(label: "cm1.worker-reap")
    private let readyPromise = TransportPromise<WireJSON>(), reapPromise = TransportPromise<Int>(), stdoutDone = TransportPromise<Bool>(), stderrDone = TransportPromise<Bool>(), stdinDone = TransportPromise<Bool>()
    private let cpuEntered = TransportPromise<WireJSON>(), cpuContinued = TransportPromise<WireJSON>()
    let id = UUID().uuidString
    let inputs: TransportInputs, trace: TransportTrace, deadlines: TransportDeadlines, scenario: String
    private let router: TransportPermitRouter, directory: URL
    private let service: BRSServiceTrace
    private let python: String, script: String, cpu: Bool
    init(inputs: TransportInputs, trace: TransportTrace, router: TransportPermitRouter, directory: URL, service: BRSServiceTrace, python: String, script: String, cpu: Bool, scenario: String = "success") {
        self.inputs = inputs; self.trace = trace; self.router = router; self.directory = directory; self.service = service; self.python = python; self.script = script; self.cpu = cpu; self.scenario = scenario; self.deadlines = cpu ? .cpu : .gpu
    }
    func start() async throws {
        try state.withLock { s in
            try require(!s.startClaimed && !s.startupAbortClaimed, "worker start once before abort")
            s.startClaimed = true
        }
        defer { state.withLock { $0.startReturned = true } }
        router.register(id) { [self] permit in
            switch permit { case .success(let value): acceptPermit(value); case .failure(let fault): fail(fault) }
        }
        state.withLock { $0.routeRegistered = true }
        try checkHealthy()
        var args = ["-I", "-S", "-B", script, "--inputs", inputs.path, "--session", trace.session, "--worker-id", id, "--swift-pid", String(getpid()), "--pgid", String(getpgrp())]
        if cpu { args += ["--scenario", scenario] }
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"; environment["HF_HUB_OFFLINE"] = "1"; environment["TRANSFORMERS_OFFLINE"] = "1"
        let pid: Int
        state.withLock { $0.spawnAttempted = true }
        do { pid = Int(try handles.spawn(python, arguments: args, environment: environment)) }
        catch { let fault = (error as? TransportFault) ?? TransportFault("spawn", String(describing: error)); fail(fault); throw fault }
        state.withLock { $0.pid = pid; if $0.fault == nil { $0.phase = "hello" } }
        trace.record("worker.started", ["worker_id": .string(id), "created_pid": .n(pid), "argv": .strings([python] + args), "scenario": .string(scenario)])
        state.withLock { $0.stdoutStarted = true }
        stdoutQueue.async { [self] in
            readStdout(); state.withLock { $0.stdoutJoined = true }
            trace.record("worker.stdout_reader_returned", ["worker_id": .string(id)]); stdoutDone.resolve(.success(true))
        }
        state.withLock { $0.stderrStarted = true }
        stderrQueue.async { [self] in
            readStderr(); state.withLock { $0.stderrJoined = true }
            trace.record("worker.stderr_reader_returned", ["worker_id": .string(id)]); stderrDone.resolve(.success(true))
        }
        state.withLock { $0.reapStarted = true }
        reapQueue.async { [self] in
            var status: Int32 = 0
            var observed: pid_t
            repeat { observed = Darwin.waitpid(pid_t(pid), &status, 0) } while observed == -1 && errno == EINTR
            guard observed == pid_t(pid) else {
                let fault = TransportFault("waitpid", "pid \(pid), errno \(errno)"); fail(fault); reapPromise.resolve(.failure(fault)); return
            }
            // Darwin wait.h WIFEXITED/WEXITSTATUS/WTERMSIG encodings. waitpid
            // without WUNTRACED/WCONTINUED returns a terminal status here.
            let signal = Int(status & 0x7f)
            guard signal != 0x7f else { let fault = TransportFault("waitpid", "unexpected nonterminal wait status \(status)"); fail(fault); reapPromise.resolve(.failure(fault)); return }
            let code = signal == 0 ? Int((status >> 8) & 0xff) : 128 + signal
            let expected = state.withLock { s in
                s.processExited = true; s.exitCode = code; s.waitStatus = Int(status); s.terminationSignal = signal == 0 ? nil : signal
                return s.shutdownACK && code == 0
            }
            // Nonzero OS termination immediately blocks commands/admission.
            // A later pipe ERROR remains diagnostic raw, never replaces first fault.
            if code != 0 { fail(TransportFault("worker_exit", "exit \(code)")) }
            trace.record("worker.reaped", ["worker_id": .string(id), "pid": .n(pid), "exit_code": .n(code), "wait_status": .n(Int(status)), "termination_signal": signal == 0 ? .null : .n(signal), "shutdown_ack_already_observed": .bool(expected)])
            reapPromise.resolve(.success(code))
        }
        if let closeFailure = handles.closeFailures.first { fail(closeFailure) }
        readyPromise.deadline(deadlines.ready, TransportFault("ready_timeout", scenario)) { [self] in fail($0) }
        _ = try await readyPromise.wait()
    }
    private func emitWrite(_ value: WireJSON) {
        let observed = brsObservedCommand(value)
        state.withLock { s in
            guard s.fault == nil && !s.processExited && !s.startupAbortClaimed else { return }
            s.stdinStarted = true
            if observed { service.record("write.enqueue", origin: brsWireOrigin(value), key: brsWireKey(value),
                details: .object(["queue": .string("cm1.worker-stdin"), "execution_binding_reason": .string("transport-has-no-source-Caller")])) }
            // Health check, reservation and enqueue share the stop mutex.
            // The queued IO itself executes after releasing that mutex.
            writeQueue.async { [self] in
            if observed { service.record("write.enter", domain: "dispatch", origin: brsWireOrigin(value), key: brsWireKey(value),
                details: .object(["queue": .string("cm1.worker-stdin")])) }
            do {
                let allowed = state.withLock { $0.fault == nil && !$0.processExited }
                guard allowed else { return }
                try handles.input.fileHandleForWriting.write(contentsOf: try value.data() + Data([10]))
                if observed { service.record("write.return", domain: "dispatch", origin: brsWireOrigin(value), key: brsWireKey(value),
                    details: .object(["queue": .string("cm1.worker-stdin"), "result": .string("success")])) }
                trace.record("wire.sent", ["worker_id": .string(id), "message": value])
            } catch { fail(TransportFault("broken_pipe", String(describing: error))) }
            }
        }
    }
    private func acceptPermit(_ permit: WireJSON) {
        do {
            try state.withLock { s in
                try checkCommonCommand(permit)
                try require(s.phase == "permit" && !s.permit, "permit phase/once")
                try require(permit["kind"].string == "LOAD_PERMIT" && permit["pid"].integer == s.pid && permit["pgid"].integer == Int(getpgrp()) && permit["swift_pid"].integer == Int(getpid()), "permit process identity")
                try require(permit["inputs_sha256"].string == inputs.sha256 && !(permit["permit_id"].string ?? "").isEmpty, "permit input/nonce")
                s.permit = true; s.phase = "loading"
            }
            trace.record("worker.permit_forwarded", ["worker_id": .string(id), "permit": permit]); emitWrite(permit)
        } catch { fail((error as? TransportFault) ?? TransportFault("permit", String(describing: error))) }
    }
    private func checkCommonCommand(_ value: WireJSON) throws {
        try require(value["v"].integer == 1 && value["session"].string == trace.session && value["worker_id"].string == id, "wire version/session/worker")
    }
    private func openRawFile(_ url: URL, role: String) throws -> HostFileRoot {
        let descriptor = Darwin.open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, mode_t(0o600))
        guard descriptor >= 0 else { throw TransportFault("raw_open", "\(url.path): errno \(errno)") }
        let root = HostFileRoot(FileHandle(fileDescriptor: descriptor, closeOnDealloc: true), role: role)
        state.withLock { $0.outputFiles.append(root) }
        trace.record("worker.output_file_opened", ["worker_id": .string(id), "path": .string(url.path), "handle": root.snapshot()])
        return root
    }
    private func closeRawFile(_ root: HostFileRoot) {
        do { try root.close() }
        catch { fail((error as? TransportFault) ?? TransportFault("raw_close", String(describing: error))) }
        trace.record("worker.output_file_close_observed", ["worker_id": .string(id), "handle": root.snapshot()])
    }
    private func closePipe(_ role: String) {
        do { try handles.close(role) }
        catch { fail((error as? TransportFault) ?? TransportFault("pipe_close", String(describing: error))) }
        trace.record("worker.pipe_close_observed", ["worker_id": .string(id), "role": .string(role), "handles": handles.snapshot()])
    }
    private func unregisterRoute() {
        let registered = state.withLock { s in
            let old = s.routeRegistered; s.routeRegistered = false; return old
        }
        if registered { router.unregister(id) }
    }

    /// A queue-body promise is not an actual queue fence. These observations
    /// append separately, including when an earlier deadline promise failed.
    private func fence(_ queue: DispatchQueue, name: String, seconds: Double,
                       mark: @escaping @Sendable () -> Void) async throws {
        let promise = TransportPromise<Bool>()
        queue.async { [self] in
            mark()
            trace.record("worker.queue_fenced", ["worker_id": .string(id), "queue": .string(name)])
            promise.resolve(.success(true))
        }
        promise.deadline(seconds, TransportFault("queue_fence_timeout", "\(id)/\(name)"))
        _ = try await promise.wait()
    }
    private func retainFenceFailure(_ error: any Error, queue: String) -> TransportFault {
        let fault = (error as? TransportFault) ?? TransportFault("queue_fence", String(describing: error))
        let row: WireJSON = .object(["worker_id": .string(id), "queue": .string(queue),
            "observation_id": .string(UUID().uuidString), "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
            "fault": fault.json])
        state.withLock { $0.fenceFailures.append(row) }
        trace.record("worker.queue_fence_failed", ["observation": row])
        return fault
    }
    private func joinIOQueues(seconds: Double? = nil) async throws {
        let flags = state.withLock { ($0.stdoutStarted, $0.stderrStarted, $0.stdinCloseIssued, $0.reapStarted) }
        let budget = seconds ?? deadlines.command
        var failures: [TransportFault] = []
        if flags.0 {
            do { try await fence(stdoutQueue, name: "stdout", seconds: budget) { [self] in state.withLock { $0.stdoutFenced = true } } }
            catch { failures.append(retainFenceFailure(error, queue: "stdout")) }
        }
        if flags.1 {
            do { try await fence(stderrQueue, name: "stderr", seconds: budget) { [self] in state.withLock { $0.stderrFenced = true } } }
            catch { failures.append(retainFenceFailure(error, queue: "stderr")) }
        }
        if flags.2 {
            do { try await fence(writeQueue, name: "stdin", seconds: budget) { [self] in state.withLock { $0.stdinFenced = true } } }
            catch { failures.append(retainFenceFailure(error, queue: "stdin")) }
        }
        if flags.3 {
            do { try await fence(reapQueue, name: "reap", seconds: budget) { [self] in state.withLock { $0.reapFenced = true } } }
            catch { failures.append(retainFenceFailure(error, queue: "reap")) }
        }
        if let first = failures.first { throw first }
    }
    private func readStdout() {
        defer { closePipe("stdout-read") }
        let url = directory.appendingPathComponent("worker-\(id).stdout.jsonl")
        let file: HostFileRoot
        do { file = try openRawFile(url, role: "stdout-log") }
        catch { fail((error as? TransportFault) ?? TransportFault("raw_output", String(describing: error))); return }
        let raw = file.handle
        defer { closeRawFile(file) }
        var buffer = Data()
        while true {
            let chunk: Data
            do { chunk = try readPipeChunk(handles.output.fileHandleForReading.fileDescriptor, "worker \(id) stdout") }
            catch { fail((error as? TransportFault) ?? TransportFault("pipe_read", String(describing: error))); return }
            if chunk.isEmpty {
                let complete = state.withLock { $0.shutdownACK }
                if !buffer.isEmpty { fail(TransportFault("truncated_frame", "worker EOF mid-line")) }
                else if !complete { fail(TransportFault("worker_eof", "EOF before SHUTDOWN_ACK")) }
                state.withLock { $0.stdoutEOF = true; $0.trailingBytes = buffer.count }
                trace.record("worker.stdout_eof", ["worker_id": .string(id), "trailing_bytes": .n(buffer.count)])
                return
            }
            do { try raw.write(contentsOf: chunk) } catch { fail(TransportFault("raw_output", String(describing: error))); return }
            buffer.append(chunk)
            while let newline = buffer.firstIndex(of: 10) {
                let line = Data(buffer[..<newline]); buffer.removeSubrange(...newline)
                if line.count + 1 > 1_048_576 { fail(TransportFault("oversize_frame", "worker frame including LF >1MiB")); return }
                let extracted = BRSServiceStamp.capture(domain: "dispatch")
                do {
                    let frame = try WireJSON.parse(line)
                    if ["EVAL_ENTER", "EVAL_RECEIPT", "RELEASE_ACK"].contains(frame["kind"].string ?? "") {
                        service.record("frame.extracted", domain: "dispatch", stamp: extracted,
                            origin: brsWireOrigin(frame), key: brsWireKey(frame),
                            details: .object(["queue": .string("cm1.worker-stdout"), "line_bytes": .integer(line.count + 1)]))
                    }
                    try consume(frame)
                }
                catch { fail((error as? TransportFault) ?? TransportFault("malformed_frame", String(describing: error))) }
            }
            if buffer.count >= 1_048_576 { fail(TransportFault("oversize_frame", "unterminated worker frame leaves no room for LF")); return }
        }
    }
    private func readStderr() {
        defer { closePipe("stderr-read") }
        let url = directory.appendingPathComponent("worker-\(id).stderr.log")
        let file: HostFileRoot
        do { file = try openRawFile(url, role: "stderr-log") }
        catch { fail((error as? TransportFault) ?? TransportFault("raw_stderr", String(describing: error))); return }
        let raw = file.handle
        defer { closeRawFile(file) }
        while true {
            let chunk: Data
            do { chunk = try readPipeChunk(handles.error.fileHandleForReading.fileDescriptor, "worker \(id) stderr") }
            catch { fail((error as? TransportFault) ?? TransportFault("pipe_read", String(describing: error))); return }
            if chunk.isEmpty { state.withLock { $0.stderrEOF = true }; trace.record("worker.stderr_eof", ["worker_id": .string(id)]); return }
            do { try raw.write(contentsOf: chunk) } catch { fail(TransportFault("raw_stderr", String(describing: error))); return }
        }
    }
    private func consume(_ value: WireJSON) throws {
        var result: (TransportPromise<WireJSON>, WireJSON)?
        var hello = false, ready = false, cpuEnter = false, cpuACK = false
        var ackStamp: BRSServiceStamp?
        try state.withLock { s in
            s.rawFrames += 1
            if value["kind"].string == "ERROR" { s.terminalError = value }
            guard s.fault == nil && !(s.processExited && s.exitCode != 0) else { return }
            try checkCommonCommand(value)
            try require(value["pid"].integer == s.pid && value["pgid"].integer == Int(getpgrp()), "worker PID/PGID")
            try require(value["event_seq"].integer == s.eventSeq + 1 && value["worker_ns"].integer != nil, "worker event sequence/clock")
            s.eventSeq += 1
            guard let kind = value["kind"].string else { throw TransportFault("wire_kind", "missing kind") }
            trace.record("wire.received", ["worker_id": .string(id), "message": value])
            if kind == "ERROR" {
                try require(value["unresolved"].bool == true && value["fault_id"].string == "\(trace.session)/\(id)/error/\(s.eventSeq)" && value["phase"].string != nil, "ERROR identity/phase")
                if let pending = s.pending {
                    try require(value["seq"].integer == pending.seq, "ERROR pending command")
                    if let lease = pending.lease { try require(value["request_id"].string == lease.request && value["lease_epoch"].integer == lease.epoch, "ERROR lease") }
                }
                throw TransportFault(value["code"].string ?? "worker_error", value.text(), id: value["fault_id"].string ?? "missing-worker-fault-id")
            }
            if kind == "HELLO" {
                try require(s.phase == "hello" && s.eventSeq == 1 && value["swift_pid"].integer == Int(getpid()) && value["inputs_sha256"].string == inputs.sha256 && value["backend"].string == (cpu ? "stub" : "mlx"), "HELLO phase/identity")
                s.phase = "permit"; hello = true; return
            }
            if kind == "READY" {
                try require(s.phase == "loading" && s.permit, "READY before permit or duplicate")
                try validateReady(value)
                guard readyPromise.reserve() else { return }
                s.ready = value; s.phase = "ready"; ready = true; return
            }
            guard var pending = s.pending else { throw TransportFault("unexpected_reply", "\(kind) with no pending command") }
            try require(value["seq"].integer == pending.seq, "reply command sequence")
            if let lease = pending.lease { try require(value["request_id"].string == lease.request && value["lease_epoch"].integer == lease.epoch, "reply lease identity") }
            if pending.lease != nil {
                for key in ["operation", "purpose", "source_context", "native_context"] {
                    try require(value[key] == pending.command[key], "reply preserves original request context \(key)")
                }
            }
            switch (pending.kind, kind) {
            case ("EVAL", "EVAL_ENTER"):
                try require(!pending.sawEnter && value["input_sha256"].string == pending.spec!.digest && value["token_count"].integer == pending.spec!.tokens.count, "EVAL_ENTER once/input")
                try validateLiveLayers(value["layers"], count: pending.spec!.tokens.count, ready: s.ready!)
                s.pageEpochs = try nextPageEpochs(value["layers"], previous: s.pageEpochs)
                pending.sawEnter = true; pending.enterLayers = value["layers"]; s.pending = pending
                if cpu && scenario == "cm2_eval_hold" && s.evalClaims == 1 {
                    try require(s.cpuEnter == nil && s.phase == "eval", "CPU first ENTER is unique/pending")
                    guard cpuEntered.reserve() else { return }
                    s.cpuEnter = value; cpuEnter = true
                }
            case ("EVAL", "CONTINUE_EVAL_ACK"):
                try require(cpu && scenario == "cm2_eval_hold" && s.evalClaims == 1 &&
                    s.phase == "eval" && pending.sawEnter && s.cpuEnter != nil &&
                    s.cpuContinue != nil && s.cpuContinueACK == nil &&
                    value["control_kind"].string == "CONTINUE_EVAL", "CPU continuation ACK phase/once")
                guard cpuContinued.reserve() else { return }
                s.cpuContinueACK = value; cpuACK = true
            case ("EVAL", "EVAL_RECEIPT"):
                if cpu && scenario == "cm2_eval_hold" && s.evalClaims == 1 {
                    try require(s.cpuContinueACK != nil, "CPU held EVAL cannot return before actual continuation ACK")
                }
                try require(pending.sawEnter && value["sealed"].bool == true, "receipt requires ENTER/sealed")
                try require(pending.enterLayers == value["layers"], "receipt preserves original EVAL_ENTER page identities")
                try require(value["receipt_id"].string == "\(trace.session)/\(id)/eval/\(pending.seq)" && value["input_sha256"].string == pending.spec!.digest && value["token_count"].integer == pending.spec!.tokens.count, "receipt identity/input")
                try require(value["token_id"].integer.map { (0..<151936).contains($0) } ?? false, "receipt token")
                guard let begin = value["eval_begin_ns"].integer, let end = value["eval_end_ns"].integer, let item = value["item_end_ns"].integer else { throw TransportFault("receipt", "missing worker clocks") }
                try require(begin <= end && end <= item, "worker-local eval/item order")
                try validateLiveLayers(value["layers"], count: pending.spec!.tokens.count, ready: s.ready!)
                // Reserve the same promise while holding health/phase. A
                // previously reserved timeout is not a validated receipt;
                // once this wins, a later fault cannot replace this success.
                guard pending.reply.reserve() else { return }
                let receipt = WireReceipt(lease: pending.lease!, raw: value)
                s.receipt = receipt; s.validatedReceipts.append(receipt)
                s.phase = "leased"; s.pending = nil; result = (pending.reply, value)
                trace.record("transport.receipt_validated", ["worker_id": .string(id), "receipt": value])
            case ("RELEASE", "RELEASE_ACK"):
                try Self.validateRelease(value, receipt: s.receipt!, ready: s.ready!)
                guard pending.reply.reserve() else { return }
                ackStamp = .capture(domain: "dispatch")
                s.validatedACKs.append(value); s.completedReleases += 1
                s.receipt = nil; s.phase = "ready"; s.pending = nil; result = (pending.reply, value)
                trace.record("transport.ack_validated", ["worker_id": .string(id), "ack": value])
            case ("SHUTDOWN", "SHUTDOWN_ACK"):
                try require(value["all_released"].bool == true, "shutdown release evidence")
                try require(value["completed_evaluations"].integer == s.completedReleases && s.completedReleases == s.evalClaims, "shutdown exact completed claims")
                try Self.validateEmptyPools(value["pools"], ready: s.ready!)
                guard pending.reply.reserve() else { return }
                s.shutdownACK = true; s.phase = "shutdown_ack"; s.pending = nil; result = (pending.reply, value)
            default: throw TransportFault("reply_phase", "\(kind) for \(pending.kind)")
            }
        }
        if hello { trace.record("worker_hello", ["kind": .string("worker_hello"), "worker_id": .string(id), "created_pid": .n(state.withLock { $0.pid }), "scenario": .string(scenario), "hello": value]) }
        if ready, !readyPromise.publishReserved(.success(value)) { fail(TransportFault("promise_protocol", "READY promise refused publication")) }
        if cpuEnter, !cpuEntered.publishReserved(.success(value)) { fail(TransportFault("promise_protocol", "CPU enter promise refused publication")) }
        if cpuACK, !cpuContinued.publishReserved(.success(value)) { fail(TransportFault("promise_protocol", "CPU continue promise refused publication")) }
        if let ackStamp {
            service.record("ack.reserved", domain: "dispatch", stamp: ackStamp,
                origin: brsWireOrigin(value), key: brsWireKey(value),
                details: .object(["queue": .string("cm1.worker-stdout")]))
        }
        if let result {
            if ["EVAL_RECEIPT", "RELEASE_ACK"].contains(value["kind"].string ?? "") {
                service.record("reply.publish", domain: "dispatch", origin: brsWireOrigin(value), key: brsWireKey(value),
                    details: .object(["queue": .string("cm1.worker-stdout")]))
            }
            if !result.0.publishReserved(.success(result.1)) { fail(TransportFault("promise_protocol", "reply promise refused publication")) }
        }
    }
    // BRC1 historical payload entry only; deliberately does not call consume,
    // create Session/Pipe/router, or assert live phase/health/PID/once evidence.
    static func brcValidateRelease(_ value: WireJSON, receipt: WireReceipt, ready: WireJSON) throws {
        try Self.validateRelease(value, receipt: receipt, ready: ready)
    }
    private static func pools(_ ready: WireJSON) throws -> [WireJSON] {
        guard let pools = ready["pools"].array, pools.count == 28 else { throw TransportFault("pools", "28 pools required") }; return pools
    }
    private func validateReady(_ ready: WireJSON) throws {
        try require(ready["backend"].string == (cpu ? "stub" : "mlx") && ready["layers"].integer == 28 && ready["page_size"].integer == 128 && ready["vocab_size"].integer == 151936, "READY backend/geometry")
        let pools = try Self.pools(ready)
        for (i, pool) in pools.enumerated() {
            try require(pool["layer"].integer == i && pool["pool_id"].string == "\(trace.session)/\(id)/L\(i)" && !(pool["pool_object_id"].string ?? "").isEmpty, "READY pool identity/order")
        }
        try require(Set(pools.compactMap { $0["pool_object_id"].string }).count == 28, "distinct pool objects")
    }
    private static func validatePartition(_ layer: WireJSON, pool: WireJSON) throws {
        try require(layer["layer"] == pool["layer"] && layer["pool_id"] == pool["pool_id"] && layer["pool_object_id"] == pool["pool_object_id"], "layer/pool identity")
        guard let used = layer["used_page_ids"].array, let free = layer["free_page_ids"].array,
              let count = layer["num_pages"].integer, let capacity = layer["capacity"].integer, let bytes = layer["storage_nbytes"].integer else { throw TransportFault("partition", "missing pool geometry") }
        let u = used.compactMap(\.integer), f = free.compactMap(\.integer)
        try require(count >= 0 && count == u.count + f.count && capacity >= count && bytes >= 0 && u.count == used.count && f.count == free.count, "pool bounds")
        try require(Set(u).count == u.count && Set(f).count == f.count && Set(u).isDisjoint(with: Set(f)) && Set(u + f) == Set(0..<count), "exact used/free partition")
        try require(u == u.sorted(), "used IDs sorted")
    }
    private func validateLiveLayers(_ value: WireJSON, count: Int, ready: WireJSON) throws {
        guard let layers = value.array, layers.count == 28 else { throw TransportFault("layers", "28 live layers required") }
        let pools = try Self.pools(ready)
        for (i, layer) in layers.enumerated() {
            try Self.validatePartition(layer, pool: pools[i])
            guard let pages = layer["cache_page_ids"].array, let owned = layer["owned_pages"].array, pages.count == 1, owned.count == 1 else { throw TransportFault("live_pages", "one logical page per layer") }
            try require(layer["page_lens"] == .array([.n(count)]) && layer["offset"].integer == count, "cache length/offset")
            try require(layer["used_page_ids"] == .array(pages) && owned[0]["page_id"] == pages[0] && (owned[0]["allocation_epoch"].integer ?? 0) > 0, "active page and positive independent allocation generation")
        }
    }
    private func nextPageEpochs(_ value: WireJSON, previous: [String: [Int: Int]]) throws -> [String: [Int: Int]] {
        var result = previous
        for layer in value.array! {
            let pool = layer["pool_id"].string!
            for owned in layer["owned_pages"].array! {
                guard let page = owned["page_id"].integer, let epoch = owned["allocation_epoch"].integer else { throw TransportFault("page_epoch", "integer page/generation") }
                try require(epoch == (previous[pool]?[page] ?? 0) + 1, "per-pool/page next allocation generation")
                result[pool, default: [:]][page] = epoch
            }
        }
        return result
    }
    private static func validateEmptyPools(_ value: WireJSON, ready: WireJSON) throws {
        guard let layers = value.array, layers.count == 28 else { throw TransportFault("empty_pools", "28 empty pools required") }
        let pools = try Self.pools(ready)
        for (i, layer) in layers.enumerated() {
            try Self.validatePartition(layer, pool: pools[i])
            try require(layer["cache_page_ids"] == .array([]) && layer["page_lens"] == .array([]) && layer["owned_pages"] == .array([]) && layer["offset"].integer == 0 && layer["used_page_ids"] == .array([]), "empty pool cache/used")
        }
    }
    private static func validateRelease(_ value: WireJSON, receipt: WireReceipt, ready: WireJSON) throws {
        try require(value["released"].bool == true && value["receipt_id"] == receipt.raw["receipt_id"] && value["eval_seq"] == receipt.raw["seq"], "release original receipt identity")
        guard let layers = value["layers"].array, layers.count == 28, let original = receipt.raw["layers"].array else { throw TransportFault("release", "full layer set missing") }
        var after: [WireJSON] = []
        for (i, layer) in layers.enumerated() {
            try require(layer["layer"].integer == i && layer["pool_id"] == original[i]["pool_id"] && layer["before"] == original[i] && layer["released_pages"] == original[i]["owned_pages"], "release exact original pages/epochs/before")
            for field in ["pool_object_id", "num_pages", "capacity", "storage_nbytes"] { try require(layer["after"][field] == original[i][field], "release unchanged pool geometry \(field)") }
            after.append(layer["after"])
        }
        try Self.validateEmptyPools(.array(after), ready: ready)
    }
    private func command(_ kind: String, lease: WireLease? = nil, spec: WireRequest? = nil, prior: WireReceipt? = nil, extra: [String: WireJSON] = [:]) async throws -> WireJSON {
        let registered: (WireJSON, TransportPromise<WireJSON>, BRSServiceStamp?) = try state.withLock { s in
            if let fault = s.fault { throw fault }
            try require(s.pending == nil && !s.processExited, "one pending command and live worker")
            try require((kind == "EVAL" && s.phase == "ready") || (kind == "RELEASE" && s.phase == "leased") || (kind == "SHUTDOWN" && s.phase == "ready"), "command admission phase")
            var currentLease = lease
            if kind == "EVAL" {
                guard let spec else { throw TransportFault("request", "EVAL requires actual operation/tokens") }
                try require(s.evalClaims < Int.max && !s.requestIDs.contains(spec.name), "fresh EVAL identity and representable wire lease")
                s.evalClaims += 1; s.requestIDs.insert(spec.name)
                currentLease = WireLease(session: trace.session, worker: id, request: spec.name, task: spec.task, epoch: s.evalClaims)
            }
            if kind == "RELEASE" { try require(s.receipt == prior && prior != nil && s.receipt?.lease == lease, "release exact validated current receipt") }
            if kind == "SHUTDOWN" { try require(s.receipt == nil && s.evalClaims == s.completedReleases, "all actual EVAL claims fully returned; zero or any completed count allowed") }
            var body = extra
            body["v"] = .n(1); body["kind"] = .string(kind); body["session"] = .string(trace.session); body["worker_id"] = .string(id); body["seq"] = .n(s.nextSeq)
            if let currentLease { body["request_id"] = .string(currentLease.request); body["lease_epoch"] = .n(currentLease.epoch) }
            let value = WireJSON.object(body)
            let observation: BRSObservedPromise? = brsObservedCommand(value) ? BRSObservedPromise(
                sink: service, key: brsWireKey(value), origin: brsWireOrigin(value),
                promiseID: "command/" + trace.session + "/" + id + "/" + String(s.nextSeq)) : nil
            let reply = TransportPromise<WireJSON>(observation: observation)
            s.pending = Pending(kind: kind, seq: s.nextSeq, command: value, lease: currentLease, spec: spec, reply: reply)
            s.nextSeq += 1; s.commands += 1; s.phase = kind.lowercased()
            let stamp = observation == nil ? nil : BRSServiceStamp.capture(domain: "swift_task")
            trace.record("wire.pending_registered", ["worker_id": .string(id), "command": value])
            return (value, reply, stamp)
        }
        let value = registered.0, reply = registered.1
        if let stamp = registered.2 {
            service.record("command.registered", stamp: stamp, origin: brsWireOrigin(value), key: brsWireKey(value),
                details: .object(["execution_binding_reason": .string("transport-has-no-source-Caller")]))
        }
        reply.deadline(deadlines.command, TransportFault("command_timeout", "\(kind) \(spec?.name ?? lease?.request ?? "shutdown")")) { [self] in fail($0) }
        emitWrite(value)
        return try await reply.wait()
    }
    func eval(_ spec: WireRequest) async throws -> WireReceipt {
        var extra = spec.context; extra["token_ids"] = .array(spec.tokens); extra["input_sha256"] = .string(spec.digest)
        let raw = try await command("EVAL", spec: spec, extra: extra)
        let lease = WireLease(session: trace.session, worker: id, request: spec.name, task: spec.task, epoch: raw["lease_epoch"].integer!)
        return WireReceipt(lease: lease, raw: raw)
    }
    /// C1's original parent source Task waits here outside all health locks.
    /// Only the validated real EVAL_ENTER frame can satisfy this promise.
    func waitForEvaluationEntered(job: String) async throws -> WireJSON {
        try state.withLock { s in
            if let fault = s.fault { throw fault }
            try require(cpu && scenario == "cm2_eval_hold" && !job.isEmpty &&
                !s.cpuEnterWaitCalled && !s.processExited, "CPU ENTER waiter role/once")
            s.cpuEnterWaitCalled = true
        }
        cpuEntered.deadline(deadlines.gate, TransportFault("cpu_enter_timeout", job)) { [self] in fail($0) }
        let frame = try await cpuEntered.wait()
        try state.withLock { s in
            if let fault = s.fault { throw fault }
            try require(s.cpuEnter == frame && s.phase == "eval" && s.cpuContinue == nil &&
                frame["source_context"]["job"].string == job && s.pending?.command["source_context"]["job"].string == job,
                "CPU ENTER original source Job/pending identity")
            trace.record("cpu.eval_enter_returned", ["worker_id": .string(id), "job": .string(job), "enter": frame])
        }
        return frame
    }
    /// This extra CPU-only frame resumes the same pending request. It creates
    /// neither a model command nor a resource completion/release obligation.
    func continueEvaluation(job: String) async throws -> WireJSON {
        let frame: WireJSON = try state.withLock { s in
            if let fault = s.fault { throw fault }
            guard let pending = s.pending else { throw TransportFault("cpu_continue", "no original pending EVAL") }
            try require(cpu && scenario == "cm2_eval_hold" && s.evalClaims == 1 &&
                s.phase == "eval" && !s.processExited && s.cpuEnterWaitCalled &&
                s.cpuEnter != nil && s.cpuContinue == nil && pending.kind == "EVAL" && pending.sawEnter &&
                pending.command["source_context"]["job"].string == job,
                "CPU continuation original Job/phase/once")
            var fields: [String: WireJSON] = ["kind": .string("CONTINUE_EVAL")]
            for key in ["v", "session", "worker_id", "seq", "request_id", "lease_epoch", "operation", "purpose", "source_context", "native_context"] {
                fields[key] = pending.command[key]
            }
            let value = WireJSON.object(fields); s.cpuContinue = value
            trace.record("cpu.eval_continue_registered", ["worker_id": .string(id), "job": .string(job), "control": value])
            return value
        }
        cpuContinued.deadline(deadlines.command, TransportFault("cpu_continue_timeout", job)) { [self] in fail($0) }
        emitWrite(frame)
        let ack = try await cpuContinued.wait()
        try state.withLock { s in
            if let fault = s.fault { throw fault }
            try require(s.cpuContinueACK == ack && ack["source_context"]["job"].string == job,
                "CPU continuation original validated ACK")
            trace.record("cpu.eval_continue_returned", ["worker_id": .string(id), "job": .string(job), "ack": ack])
        }
        return ack
    }
    func release(_ receipt: WireReceipt) async throws -> WireJSON {
        var extra: [String: WireJSON] = ["receipt_id": receipt.raw["receipt_id"], "eval_seq": receipt.raw["seq"]]
        for key in ["operation", "purpose", "source_context", "native_context"] { extra[key] = receipt.raw[key] }
        return try await command("RELEASE", lease: receipt.lease, prior: receipt, extra: extra)
    }
    func shutdown() async throws -> WireJSON {
        let ack = try await command("SHUTDOWN")
        // EOF on command input is graceful only after the validated ACK.
        closeInput()
        reapPromise.deadline(deadlines.command, TransportFault("reap_timeout", id)) { [self] in fail($0) }
        stdoutDone.deadline(deadlines.command, TransportFault("stdout_timeout", id)) { [self] in fail($0) }
        stderrDone.deadline(deadlines.command, TransportFault("stderr_timeout", id)) { [self] in fail($0) }
        stdinDone.deadline(deadlines.command, TransportFault("stdin_close_timeout", id)) { [self] in fail($0) }
        let exit = try await reapPromise.wait(); _ = try await stdoutDone.wait(); _ = try await stderrDone.wait(); _ = try await stdinDone.wait()
        try require(exit == 0, "normal worker exit")
        try checkHealthy()
        try await joinIOQueues()
        try require(handles.allClosed && state.withLock { $0.outputFiles.allSatisfy(\.closed) },
                    "normal shutdown requires all original pipe and output file closes")
        unregisterRoute()
        trace.record("worker.normal_closed", ["worker_id": .string(id), "roots": snapshot()])
        return ack
    }
    func checkHealthy() throws {
        let fault = state.withLock { s -> TransportFault? in
            if let fault = s.fault { return fault }
            if s.processExited && (!s.shutdownACK || s.exitCode != 0) { return TransportFault("worker_exit", "observed worker termination before successful shutdown") }
            return nil
        }
        if let fault { fail(fault); throw fault }
    }
    func initializedIdentity() throws -> MLXSessionIdentity {
        let observed = state.withLock { ($0.pid, $0.ready) }
        guard let ready = observed.1 else { throw TransportFault("identity", "no validated READY") }
        let pools = try Self.pools(ready).map { row -> MLXPoolIdentity in
            guard let layer = row["layer"].integer, let id = row["pool_id"].string, let object = row["pool_object_id"].string else { throw TransportFault("identity", "incomplete pool identity") }
            return MLXPoolIdentity(layer: layer, poolID: id, objectID: object)
        }
        return try MLXSessionIdentity(session: trace.session, worker: id, pid: observed.0, pgid: Int(getpgrp()),
            poolGroup: "\(trace.session)/\(id)/private-pools", pools: pools, provenance: ready.core)
    }
    // Lock order is Session -> synchronous Core commit -> TransportTrace.
    // The closure cannot suspend or call this Session. Admission commits while
    // the same mutex excludes command/reply/fault/process-exit state changes.
    func withAdmission<T>(_ body: () throws -> T) throws -> T {
        try state.withLock { s in
            if let fault = s.fault { throw fault }
            try require(!s.processExited && s.phase == "ready", "session live/ready at owner admission")
            return try body()
        }
    }
    /// Ordinary source registration uses observed session health without
    /// requiring the model/KV to be idle. The original synchronous Core commit
    /// shares this mutex with replies, faults, and process-exit observations.
    /// It must not suspend or call back into this Session.
    func withHostAction<T>(_ body: () throws -> T) throws -> T {
        try state.withLock { s in
            if let fault = s.fault { throw fault }
            try require(!s.processExited && s.ready != nil &&
                ["ready", "eval", "leased", "release"].contains(s.phase),
                "session live/running at ordinary registration")
            let result = try body()
            trace.record("host.action_committed", ["worker_id": .string(id), "phase": .string(s.phase)])
            return result
        }
    }
    private func closeInput() {
        state.withLock { s in
            guard !s.stdinCloseIssued else { return }; s.stdinCloseIssued = true; s.stdinStarted = true
            // A concurrent closer/fence cannot observe issued before enqueue.
            writeQueue.async { [self] in
            do {
                try handles.close("stdin-write"); state.withLock { $0.stdinClosed = true }
                trace.record("worker.stdin_closed", ["worker_id": .string(id)]); stdinDone.resolve(.success(true))
            } catch { let fault = TransportFault("stdin_close", String(describing: error)); fail(fault); stdinDone.resolve(.failure(fault)) }
            }
        }
    }
    func fail(_ fault: TransportFault) {
        let old = state.withLock { s -> (Pending?, Bool) in
            guard s.fault == nil else { return (nil, false) }
            s.fault = fault; s.phase = "unresolved"; return (s.pending, true)
        }
        guard old.1 else { return }
        trace.record("session.unresolved", ["worker_id": .string(id), "fault": fault.json, "roots": snapshot()])
        readyPromise.resolve(.failure(fault)); old.0?.reply.resolve(.failure(fault))
        cpuEntered.resolve(.failure(fault)); cpuContinued.resolve(.failure(fault))
        // Intentionally preserve pending command, receipt/KV obligation and IO
        // roots. No RELEASE, reset, task cancellation or retry occurs here.
    }
    // CPU expected-failure fixtures must not strand a process until suite end.
    // They close stdin (never release) and use bounded OS termination/reap;
    // their unresolved KV root remains in snapshots after process disappearance.
    func closeFailedCPUFixture() async {
        precondition(cpu)
        let closed = state.withLock { $0.processExited && $0.stdoutJoined && $0.stderrJoined && $0.stdinClosed }
        if closed { return }
        closeInput()
        let pid = state.withLock { $0.pid }
        if pid > 0 && !state.withLock({ $0.processExited }) { _ = Darwin.kill(pid_t(pid), SIGTERM) }
        reapPromise.deadline(1, TransportFault("cpu_reap_timeout", id))
        _ = try? await reapPromise.wait()
        if pid > 0 && !state.withLock({ $0.processExited }) { _ = Darwin.kill(pid_t(pid), SIGKILL) }
        // Separate fresh drain observation: the original timeout is retained.
        let done = TransportPromise<Bool>()
        reapQueue.async { done.resolve(.success(true)) }
        done.deadline(1, TransportFault("cpu_final_reap_timeout", id)); _ = try? await done.wait()
        stdoutDone.deadline(1, TransportFault("cpu_stdout_timeout", id)); stderrDone.deadline(1, TransportFault("cpu_stderr_timeout", id))
        _ = try? await stdoutDone.wait(); _ = try? await stderrDone.wait()
        stdinDone.deadline(1, TransportFault("cpu_stdin_close_timeout", id)); _ = try? await stdinDone.wait()
        unregisterRoute()
        trace.record("cpu.failure_fixture_closed", ["worker_id": .string(id), "roots": snapshot()])
    }
    /// Only the prefix before the first EVAL can use OS startup-abort cleanup.
    /// It produces no shutdown ACK, no release evidence and no source Safe.
    /// A missing process does not erase the six already-created pipe handles.
    func closeStartupPrefix() async throws -> WireJSON {
        let stopped: (Int, TransportFault) = try state.withLock { s in
            try require(s.evalClaims == 0 && s.pending == nil, "startup abort cannot clean a submitted EVAL")
            try require(!s.startClaimed || s.startReturned, "original start must return before startup abort")
            try require(!s.startupAbortClaimed, "startup abort once")
            s.startupAbortClaimed = true
            if s.fault == nil { s.fault = TransportFault("startup_aborted", "original pre-EVAL population sealed") }
            s.phase = "unresolved"
            return (s.pid, s.fault!)
        }
        // The same health lock makes this seal and the first EVAL registration
        // exclusive. No already-accepted permit can enqueue beyond the seal.
        let pid = stopped.0
        readyPromise.resolve(.failure(stopped.1))
        cpuEntered.resolve(.failure(stopped.1)); cpuContinued.resolve(.failure(stopped.1))
        trace.record("worker.startup_admission_sealed", ["worker_id": .string(id), "fault": stopped.1.json])
        if pid == 0 {
            for role in handles.ends.keys.sorted() { closePipe(role) }
            // No reader/reaper ever ran. Do not mark their join flags true.
            unregisterRoute()
        } else {
            closeInput()
            if !state.withLock({ $0.processExited }) { _ = Darwin.kill(pid_t(pid), SIGTERM) }
            do {
                try await fence(reapQueue, name: "startup-reap-term", seconds: 1) { [self] in state.withLock { $0.reapFenced = true } }
            } catch {
                _ = retainFenceFailure(error, queue: "startup-reap-term")
            }
            if !state.withLock({ $0.processExited }) { _ = Darwin.kill(pid_t(pid), SIGKILL) }
            do { try await joinIOQueues(seconds: 1) }
            catch { trace.record("worker.startup_fence_failed", ["worker_id": .string(id), "first_fault": ((error as? TransportFault)?.json) ?? .null]) }
            let stopped = state.withLock { $0.processExited && $0.stdoutFenced && $0.stderrFenced && $0.reapFenced }
            if stopped { unregisterRoute() }
        }
        let result = startupSnapshot()
        trace.record("worker.startup_abort_observed", ["worker_id": .string(id), "observation": result])
        return result
    }
    func startupSnapshot() -> WireJSON {
        state.withLock { s in
            let closed = (s.pid == 0 || s.processExited) &&
                (!s.stdoutStarted || s.stdoutFenced) && (!s.stderrStarted || s.stderrFenced) &&
                (!s.reapStarted || s.reapFenced) && (!s.stdinCloseIssued || s.stdinFenced) &&
                !s.routeRegistered && handles.allClosed && s.outputFiles.allSatisfy(\.closed)
            return .object([
                "worker_id": .string(id), "start_claimed": .bool(s.startClaimed), "start_returned": .bool(s.startReturned),
                "spawn_attempted": .bool(s.spawnAttempted), "process_created": .bool(s.pid > 0),
                "pid": .n(s.pid), "process_reaped": .bool(s.processExited),
                "stdout_started": .bool(s.stdoutStarted), "stderr_started": .bool(s.stderrStarted), "reap_started": .bool(s.reapStarted),
                "stdin_started": .bool(s.stdinStarted),
                "stdout_fenced": .bool(s.stdoutFenced), "stderr_fenced": .bool(s.stderrFenced),
                "stdin_close_issued": .bool(s.stdinCloseIssued), "stdin_fenced": .bool(s.stdinFenced), "reap_fenced": .bool(s.reapFenced),
                "route_registered": .bool(s.routeRegistered), "startup_abort_claimed": .bool(s.startupAbortClaimed),
                "eval_claims": .n(s.evalClaims), "pipe_handles": handles.snapshot(),
                "output_files": .array(s.outputFiles.map { $0.snapshot() }), "actual_host_resources_closed": .bool(closed),
                "fence_failures": .array(s.fenceFailures),
                "fault": s.fault?.json ?? .null
            ])
        }
    }
    func hostResourceObservations() throws -> [HostResourceObservation] {
        let population = startupSnapshot()
        var observations: [HostResourceObservation] = []
        func add(_ kind: HostResourceKind, _ suffix: String, created: Bool, started: Bool,
                 closed: Bool, joined: Bool?) throws {
            observations.append(try HostResourceObservation(kind: kind, id: id + "/" + suffix,
                created: created, started: started, closed: closed, joined: joined, originalEvidence: population.core))
        }
        let spawned = population["process_created"].bool == true
        try add(.workerProcess, "process", created: spawned, started: spawned,
            closed: population["process_reaped"].bool == true,
            joined: spawned ? population["reap_fenced"].bool : nil)
        for (kind, name, pipe) in [(HostResourceKind.workerStdin, "stdin", "stdin-write"),
                                  (.workerStdout, "stdout", "stdout-read"), (.workerStderr, "stderr", "stderr-read")] {
            let started = population[name + "_started"].bool == true
            let fenced = population[name + "_fenced"].bool == true
            try add(kind, name + "-queue", created: true, started: started,
                closed: (!started || fenced) && handles.ends[pipe]!.closed,
                joined: started ? fenced : nil)
        }
        for role in handles.ends.keys.sorted() {
            observations.append(try handles.ends[role]!.observation(kind: .pipeEnd))
        }
        for file in state.withLock({ $0.outputFiles }) {
            observations.append(try file.observation(kind: .outputFile))
        }
        return observations
    }
    func snapshot() -> WireJSON { state.withLock { s in .object([
        "worker_id": .string(id), "pid": .n(s.pid), "phase": .string(s.phase), "permit_forwarded": .bool(s.permit),
        "process_root": .bool(!s.processExited), "stdout_root": .bool(!s.stdoutJoined), "stderr_root": .bool(!s.stderrJoined), "stdin_root": .bool(!s.stdinClosed),
        "stdout_eof": .bool(s.stdoutEOF), "stderr_eof": .bool(s.stderrEOF),
        "stdin_close_issued": .bool(s.stdinCloseIssued),
        "terminal_error_diagnostic": s.terminalError ?? .null, "trailing_bytes": .n(s.trailingBytes),
        "pending_root": s.pending?.command ?? .null, "receipt_kv_root": s.receipt?.raw ?? .null,
        "shutdown_ack": .bool(s.shutdownACK), "actual_exit": s.exitCode.map(WireJSON.n) ?? .null, "fault": s.fault?.json ?? .null,
        "wait_status": s.waitStatus.map(WireJSON.n) ?? .null, "termination_signal": s.terminationSignal.map(WireJSON.n) ?? .null,
        "raw_frames": .n(s.rawFrames), "commands": .n(s.commands),
        "eval_claims": .n(s.evalClaims), "completed_releases": .n(s.completedReleases),
        "cpu_phase_control": .object(["selected": .bool(cpu && scenario == "cm2_eval_hold"),
            "enter_wait_called": .bool(s.cpuEnterWaitCalled), "enter": s.cpuEnter ?? .null,
            "continue": s.cpuContinue ?? .null, "continue_ack": s.cpuContinueACK ?? .null]),
        "validated_receipts": .array(s.validatedReceipts.map(\.raw)), "validated_acks": .array(s.validatedACKs)
    ]) } }
}
