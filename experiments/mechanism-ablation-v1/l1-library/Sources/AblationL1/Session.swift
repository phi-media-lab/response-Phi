import Foundation
import Synchronization
import Dispatch
import Darwin

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
        throw BridgeFault("pipe_read", "\(label), fd \(descriptor), errno \(code)")
    }
}

struct RequestSpec: Sendable {
    let name: String, role: String, operation: String, digest: String
    let epoch: Int, tokens: [JSON]
    var json: JSON { .object(["request_id": .string(name), "role": .string(role), "operation": .string(operation), "lease_epoch": .n(epoch), "token_ids": .array(tokens), "input_sha256": .string(digest)]) }
}
struct Inputs: Sendable {
    // MA1 L1 port: cm1-inputs-v1 (shared with the canonical worker/checked PL),
    // not SMWO1's smwo1-inputs-v1. A = a32/Prefill32/epoch1; B = b_prefix7 plus
    // this session's actual A token, Prefill8/epoch2 (built after A completes).
    let path: String, sha256: String, raw: JSON
    let aSpec: RequestSpec
    let bPrefix7: [JSON]
    init(_ path: String) throws {
        let bytes = try Data(contentsOf: URL(fileURLWithPath: path))
        let raw = try JSON.parse(bytes)
        try require(raw["schema"].string == "cm1-inputs-v1", "inputs schema cm1-inputs-v1")
        try require(raw["model_snapshot"].string == "173234aa840d113125e9f2271100ddbaf16c9620", "frozen model snapshot")
        try require(raw["layers"].integer == 28 && raw["page_size"].integer == 128 && raw["vocab_size"].integer == 151936, "model dimensions")
        guard let a32 = raw["a32"].array, a32.count == 32 else { throw BridgeFault("inputs", "a32 must have 32 tokens") }
        guard let b7 = raw["b_prefix7"].array, b7.count == 7 else { throw BridgeFault("inputs", "b_prefix7 must have 7 tokens") }
        for tok in a32 + b7 { try require(tok.integer.map { (0..<151936).contains($0) } ?? false, "token range") }
        let aDigest = digest(try JSON.array(a32).data())
        self.aSpec = RequestSpec(name: "A-" + UUID().uuidString, role: "A", operation: "Prefill32", digest: aDigest, epoch: 1, tokens: a32)
        self.bPrefix7 = b7
        self.path = path; self.sha256 = digest(bytes); self.raw = raw
    }
    // B's eighth token is this session's actual A result token; never precomputed.
    func bSpec(aToken: Int) throws -> RequestSpec {
        let tokens = bPrefix7 + [JSON.n(aToken)]
        let d = digest(try JSON.array(tokens).data())
        return RequestSpec(name: "B-" + UUID().uuidString, role: "B", operation: "Prefill8", digest: d, epoch: 2, tokens: tokens)
    }
}

// Pipe handles are confined to their dedicated queues after launch. Spawn uses
// explicit POSIX file actions and inherits the wrapper-controlled Swift group;
// neither SETPGROUP nor SETSID is set. waitpid is the sole actual worker reap.
private final class ProcessHandles: @unchecked Sendable {
    let input = Pipe(), output = Pipe(), error = Pipe()
    func spawn(_ executable: String, arguments: [String], environment: [String: String]) throws -> pid_t {
        var actions: posix_spawn_file_actions_t? = nil
        var attributes: posix_spawnattr_t? = nil
        func checked(_ code: Int32, _ operation: String) throws {
            if code != 0 { throw BridgeFault("spawn", "\(operation): errno \(code)") }
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
        try? input.fileHandleForReading.close(); try? output.fileHandleForWriting.close(); try? error.fileHandleForWriting.close()
        return pid
    }
}

// One independently rooted stdin reader for the whole Swift process. CPU
// workers use distinct routing IDs. Only an OS-checking outer wrapper can send
// LOAD_PERMIT; Swift never creates one. A control EOF is not a permit.
final class PermitRouter: Sendable {
    private struct State { var receivers: [String: @Sendable (Result<JSON, BridgeFault>) -> Void] = [:]; var ended = false; var closing = false; var readerJoined = false }
    private let state = Mutex(State())
    private let reader = DispatchQueue(label: "smwo1.wrapper-control-reader")
    private let trace: Trace
    private let ended = Promise<Bool>()
    init(_ trace: Trace) { self.trace = trace }
    func register(_ id: String, _ receive: @escaping @Sendable (Result<JSON, BridgeFault>) -> Void) {
        let ended = state.withLock { s in s.receivers[id] = receive; return s.ended }
        if ended { receive(.failure(BridgeFault("control_eof", "wrapper control already closed"))) }
    }
    func unregister(_ id: String) { state.withLock { $0.receivers.removeValue(forKey: id) } }
    func start() {
        reader.async { [self] in
            var buffer = Data()
            while true {
                let chunk: Data
                do { chunk = try readPipeChunk(STDIN_FILENO, "wrapper control") }
                catch { end((error as? BridgeFault) ?? BridgeFault("pipe_read", String(describing: error))); return }
                if chunk.isEmpty { end(BridgeFault("control_eof", buffer.isEmpty ? "wrapper stdin EOF" : "truncated wrapper permit")); return }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 10) {
                    let line = Data(buffer[..<newline]); buffer.removeSubrange(...newline)
                    if line.count + 1 > 1_048_576 { end(BridgeFault("control_frame", "wrapper frame including LF exceeds bound")); return }
                    do {
                        let value = try JSON.parse(line)
                        guard let id = value["worker_id"].string, let receive = state.withLock({ $0.receivers[id] }) else { throw BridgeFault("control_identity", "unknown permit worker") }
                        receive(.success(value))
                    } catch { end((error as? BridgeFault) ?? BridgeFault("control_frame", String(describing: error))); return }
                }
                if buffer.count >= 1_048_576 { end(BridgeFault("control_frame", "unterminated wrapper frame leaves no room for LF")); return }
            }
        }
    }
    private func end(_ fault: BridgeFault) {
        let receivers = state.withLock { s in s.ended = true; return Array(s.receivers.values) }
        trace.record("control.reader_ended", ["fault": fault.json])
        let normal = state.withLock { $0.closing } && fault.code == "control_eof" && fault.detail == "wrapper stdin EOF"
        ended.resolve(normal ? .success(true) : .failure(fault))
        receivers.forEach { $0(.failure(fault)) }
    }
    func finish() async throws {
        try state.withLock { s in try require(s.receivers.isEmpty && !s.ended, "control close only after all worker routes retired"); s.closing = true }
        trace.record("control_close", ["kind": .string("control_close")])
        ended.deadline(3, BridgeFault("control_close_timeout", "wrapper stdin EOF"))
        _ = try await ended.wait()
        await withCheckedContinuation { c in reader.async { [self] in state.withLock { $0.readerJoined = true }; c.resume() } }
        trace.record("control.reader_joined")
    }
    func snapshot() -> JSON { state.withLock { .object(["reader_ended": .bool($0.ended), "reader_joined": .bool($0.readerJoined), "registered_workers": .strings(Array($0.receivers.keys).sorted())]) } }
}

final class WorkerSession: Sendable {
    private struct Pending: Sendable {
        let kind: String, seq: Int, command: JSON, lease: LeaseID?, spec: RequestSpec?
        let reply: Promise<JSON>
        let entered: Promise<JSON>?
        var sawEnter = false
        var enterLayers: JSON?
    }
    private struct State {
        var pid = 0, eventSeq = 0, nextSeq = 1
        var phase = "created", permit = false, ready: JSON?, pending: Pending?, receipt: HostReceipt?
        var fault: BridgeFault?
        var processExited = false, exitCode: Int?, stdoutEOF = false, stderrEOF = false, shutdownACK = false
        var waitStatus: Int?, terminationSignal: Int?
        var stdoutJoined = false, stderrJoined = false, stdinClosed = false
        var terminalError: JSON?, trailingBytes = 0
        var rawFrames = 0, commands = 0
    }
    private let state = Mutex(State())
    private let handles = ProcessHandles()
    private let stdoutQueue = DispatchQueue(label: "smwo1.worker-stdout")
    private let stderrQueue = DispatchQueue(label: "smwo1.worker-stderr")
    private let writeQueue = DispatchQueue(label: "smwo1.worker-stdin")
    private let reapQueue = DispatchQueue(label: "smwo1.worker-reap")
    private let readyPromise = Promise<JSON>(), reapPromise = Promise<Int>(), stdoutDone = Promise<Bool>(), stderrDone = Promise<Bool>(), stdinDone = Promise<Bool>()
    let id = UUID().uuidString
    let inputs: Inputs, trace: Trace, deadlines: Deadlines, scenario: String
    private let router: PermitRouter, directory: URL
    private let python: String, script: String, cpu: Bool
    init(inputs: Inputs, trace: Trace, router: PermitRouter, directory: URL, python: String, script: String, cpu: Bool, scenario: String = "success") {
        self.inputs = inputs; self.trace = trace; self.router = router; self.directory = directory; self.python = python; self.script = script; self.cpu = cpu; self.scenario = scenario; self.deadlines = cpu ? .cpu : .gpu
    }
    func start() async throws {
        router.register(id) { [self] permit in
            switch permit { case .success(let value): acceptPermit(value); case .failure(let fault): fail(fault) }
        }
        try checkHealthy()
        var args = ["-I", "-S", "-B", script, "--inputs", inputs.path, "--session", trace.session, "--worker-id", id, "--swift-pid", String(getpid()), "--pgid", String(getpgrp())]
        if cpu { args += ["--scenario", scenario] }
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"; environment["HF_HUB_OFFLINE"] = "1"; environment["TRANSFORMERS_OFFLINE"] = "1"
        let pid: Int
        do { pid = Int(try handles.spawn(python, arguments: args, environment: environment)) }
        catch { let fault = (error as? BridgeFault) ?? BridgeFault("spawn", String(describing: error)); fail(fault); throw fault }
        state.withLock { $0.pid = pid; if $0.fault == nil { $0.phase = "hello" } }
        trace.record("worker.started", ["worker_id": .string(id), "created_pid": .n(pid), "argv": .strings([python] + args), "scenario": .string(scenario)])
        stdoutQueue.async { [self] in
            readStdout(); state.withLock { $0.stdoutJoined = true }
            trace.record("worker.stdout_reader_returned", ["worker_id": .string(id)]); stdoutDone.resolve(.success(true))
        }
        stderrQueue.async { [self] in
            readStderr(); state.withLock { $0.stderrJoined = true }
            trace.record("worker.stderr_reader_returned", ["worker_id": .string(id)]); stderrDone.resolve(.success(true))
        }
        reapQueue.async { [self] in
            var status: Int32 = 0
            var observed: pid_t
            repeat { observed = Darwin.waitpid(pid_t(pid), &status, 0) } while observed == -1 && errno == EINTR
            guard observed == pid_t(pid) else {
                let fault = BridgeFault("waitpid", "pid \(pid), errno \(errno)"); fail(fault); reapPromise.resolve(.failure(fault)); return
            }
            // Darwin wait.h WIFEXITED/WEXITSTATUS/WTERMSIG encodings. waitpid
            // without WUNTRACED/WCONTINUED returns a terminal status here.
            let signal = Int(status & 0x7f)
            guard signal != 0x7f else { let fault = BridgeFault("waitpid", "unexpected nonterminal wait status \(status)"); fail(fault); reapPromise.resolve(.failure(fault)); return }
            let code = signal == 0 ? Int((status >> 8) & 0xff) : 128 + signal
            let expected = state.withLock { s in
                s.processExited = true; s.exitCode = code; s.waitStatus = Int(status); s.terminationSignal = signal == 0 ? nil : signal
                return s.shutdownACK && code == 0
            }
            // Nonzero OS termination immediately blocks commands/admission.
            // A later pipe ERROR remains diagnostic raw, never replaces first fault.
            if code != 0 { fail(BridgeFault("worker_exit", "exit \(code)")) }
            trace.record("worker.reaped", ["worker_id": .string(id), "pid": .n(pid), "exit_code": .n(code), "wait_status": .n(Int(status)), "termination_signal": signal == 0 ? .null : .n(signal), "shutdown_ack_already_observed": .bool(expected)])
            reapPromise.resolve(.success(code))
        }
        readyPromise.deadline(deadlines.ready, BridgeFault("ready_timeout", scenario)) { [self] in fail($0) }
        _ = try await readyPromise.wait()
    }
    private func emitWrite(_ value: JSON) {
        writeQueue.async { [self] in
            do {
                let allowed = state.withLock { $0.fault == nil && !$0.processExited }
                guard allowed else { return }
                try handles.input.fileHandleForWriting.write(contentsOf: try value.data() + Data([10]))
                trace.record("wire.sent", ["worker_id": .string(id), "message": value])
            } catch { fail(BridgeFault("broken_pipe", String(describing: error))) }
        }
    }
    private func acceptPermit(_ permit: JSON) {
        do {
            try state.withLock { s in
                try checkCommonCommand(permit)
                try require(s.phase == "permit" && !s.permit, "permit phase/once")
                try require(permit["kind"].string == "LOAD_PERMIT" && permit["pid"].integer == s.pid && permit["pgid"].integer == Int(getpgrp()) && permit["swift_pid"].integer == Int(getpid()), "permit process identity")
                try require(permit["inputs_sha256"].string == inputs.sha256 && !(permit["permit_id"].string ?? "").isEmpty, "permit input/nonce")
                s.permit = true; s.phase = "loading"
            }
            trace.record("worker.permit_forwarded", ["worker_id": .string(id), "permit": permit]); emitWrite(permit)
        } catch { fail((error as? BridgeFault) ?? BridgeFault("permit", String(describing: error))) }
    }
    private func checkCommonCommand(_ value: JSON) throws {
        try require(value["v"].integer == 1 && value["session"].string == trace.session && value["worker_id"].string == id, "wire version/session/worker")
    }
    private func readStdout() {
        defer { try? handles.output.fileHandleForReading.close() }
        let url = directory.appendingPathComponent("worker-\(id).stdout.jsonl")
        FileManager.default.createFile(atPath: url.path, contents: nil)
        guard let raw = try? FileHandle(forWritingTo: url) else { fail(BridgeFault("raw_output", url.path)); return }
        defer { try? raw.close() }
        var buffer = Data()
        while true {
            let chunk: Data
            do { chunk = try readPipeChunk(handles.output.fileHandleForReading.fileDescriptor, "worker \(id) stdout") }
            catch { fail((error as? BridgeFault) ?? BridgeFault("pipe_read", String(describing: error))); return }
            if chunk.isEmpty {
                let complete = state.withLock { $0.shutdownACK }
                if !buffer.isEmpty { fail(BridgeFault("truncated_frame", "worker EOF mid-line")) }
                else if !complete { fail(BridgeFault("worker_eof", "EOF before SHUTDOWN_ACK")) }
                state.withLock { $0.stdoutEOF = true; $0.trailingBytes = buffer.count }
                trace.record("worker.stdout_eof", ["worker_id": .string(id), "trailing_bytes": .n(buffer.count)])
                return
            }
            do { try raw.write(contentsOf: chunk) } catch { fail(BridgeFault("raw_output", String(describing: error))); return }
            buffer.append(chunk)
            while let newline = buffer.firstIndex(of: 10) {
                let line = Data(buffer[..<newline]); buffer.removeSubrange(...newline)
                if line.count + 1 > 1_048_576 { fail(BridgeFault("oversize_frame", "worker frame including LF >1MiB")); return }
                do { try consume(try JSON.parse(line)) }
                catch { fail((error as? BridgeFault) ?? BridgeFault("malformed_frame", String(describing: error))) }
            }
            if buffer.count >= 1_048_576 { fail(BridgeFault("oversize_frame", "unterminated worker frame leaves no room for LF")); return }
        }
    }
    private func readStderr() {
        defer { try? handles.error.fileHandleForReading.close() }
        let url = directory.appendingPathComponent("worker-\(id).stderr.log")
        FileManager.default.createFile(atPath: url.path, contents: nil)
        guard let raw = try? FileHandle(forWritingTo: url) else { fail(BridgeFault("raw_stderr", url.path)); return }
        defer { try? raw.close() }
        while true {
            let chunk: Data
            do { chunk = try readPipeChunk(handles.error.fileHandleForReading.fileDescriptor, "worker \(id) stderr") }
            catch { fail((error as? BridgeFault) ?? BridgeFault("pipe_read", String(describing: error))); return }
            if chunk.isEmpty { state.withLock { $0.stderrEOF = true }; trace.record("worker.stderr_eof", ["worker_id": .string(id)]); return }
            do { try raw.write(contentsOf: chunk) } catch { fail(BridgeFault("raw_stderr", String(describing: error))); return }
        }
    }
    private func consume(_ value: JSON) throws {
        var result: (Promise<JSON>, JSON)?
        var enter: (Promise<JSON>, JSON)?
        var hello = false, ready = false
        try state.withLock { s in
            s.rawFrames += 1
            if value["kind"].string == "ERROR" { s.terminalError = value }
            guard s.fault == nil && !(s.processExited && s.exitCode != 0) else { return }
            try checkCommonCommand(value)
            try require(value["pid"].integer == s.pid && value["pgid"].integer == Int(getpgrp()), "worker PID/PGID")
            try require(value["event_seq"].integer == s.eventSeq + 1 && value["worker_ns"].integer != nil, "worker event sequence/clock")
            s.eventSeq += 1
            guard let kind = value["kind"].string else { throw BridgeFault("wire_kind", "missing kind") }
            trace.record("wire.received", ["worker_id": .string(id), "message": value])
            if kind == "ERROR" {
                try require(value["unresolved"].bool == true && value["fault_id"].string == "\(trace.session)/\(id)/error/\(s.eventSeq)" && value["phase"].string != nil, "ERROR identity/phase")
                if let pending = s.pending {
                    try require(value["seq"].integer == pending.seq, "ERROR pending command")
                    if let lease = pending.lease { try require(value["request_id"].string == lease.request && value["lease_epoch"].integer == lease.epoch, "ERROR lease") }
                }
                throw BridgeFault(value["code"].string ?? "worker_error", value.text(), id: value["fault_id"].string ?? "missing-worker-fault-id")
            }
            if kind == "HELLO" {
                try require(s.phase == "hello" && s.eventSeq == 1 && value["swift_pid"].integer == Int(getpid()) && value["inputs_sha256"].string == inputs.sha256 && value["backend"].string == (cpu ? "stub" : "mlx"), "HELLO phase/identity")
                s.phase = "permit"; hello = true; return
            }
            if kind == "READY" {
                try require(s.phase == "loading" && s.permit, "READY before permit or duplicate")
                try validateReady(value); s.ready = value; s.phase = "ready"; ready = true; return
            }
            guard var pending = s.pending else { throw BridgeFault("unexpected_reply", "\(kind) with no pending command") }
            try require(value["seq"].integer == pending.seq, "reply command sequence")
            if let lease = pending.lease { try require(value["request_id"].string == lease.request && value["lease_epoch"].integer == lease.epoch, "reply lease identity") }
            switch (pending.kind, kind) {
            case ("EVAL", "EVAL_ENTER"):
                try require(!pending.sawEnter && value["input_sha256"].string == pending.spec!.digest && value["token_count"].integer == pending.spec!.tokens.count, "EVAL_ENTER once/input")
                try validateLiveLayers(value["layers"], lease: pending.lease!, count: pending.spec!.tokens.count, ready: s.ready!)
                pending.sawEnter = true; pending.enterLayers = value["layers"]; s.pending = pending
                if let entered = pending.entered { enter = (entered, value) }
            case ("EVAL", "EVAL_RECEIPT"):
                try require(pending.sawEnter && value["sealed"].bool == true, "receipt requires ENTER/sealed")
                try require(pending.enterLayers == value["layers"], "receipt preserves original EVAL_ENTER page identities")
                try require(value["receipt_id"].string == "\(trace.session)/\(id)/eval/\(pending.seq)" && value["input_sha256"].string == pending.spec!.digest && value["token_count"].integer == pending.spec!.tokens.count, "receipt identity/input")
                try require(value["token_id"].integer.map { (0..<151936).contains($0) } ?? false, "receipt token")
                guard let begin = value["eval_begin_ns"].integer, let end = value["eval_end_ns"].integer, let item = value["item_end_ns"].integer else { throw BridgeFault("receipt", "missing worker clocks") }
                try require(begin <= end && end <= item, "worker-local eval/item order")
                try validateLiveLayers(value["layers"], lease: pending.lease!, count: pending.spec!.tokens.count, ready: s.ready!)
                s.receipt = HostReceipt(lease: pending.lease!, raw: value); s.phase = "leased"; s.pending = nil; result = (pending.reply, value)
            case ("RELEASE", "RELEASE_ACK"):
                try validateRelease(value, receipt: s.receipt!, ready: s.ready!)
                s.receipt = nil; s.phase = "ready"; s.pending = nil; result = (pending.reply, value)
            case ("SHUTDOWN", "SHUTDOWN_ACK"):
                try require(value["all_released"].bool == true, "shutdown release evidence")
                try validateEmptyPools(value["pools"], ready: s.ready!)
                s.shutdownACK = true; s.phase = "shutdown_ack"; s.pending = nil; result = (pending.reply, value)
            default: throw BridgeFault("reply_phase", "\(kind) for \(pending.kind)")
            }
        }
        if hello { trace.record("worker_hello", ["kind": .string("worker_hello"), "worker_id": .string(id), "created_pid": .n(state.withLock { $0.pid }), "scenario": .string(scenario), "hello": value]) }
        if ready { readyPromise.resolve(.success(value)) }
        if let enter { enter.0.resolve(.success(enter.1)) }
        if let result { result.0.resolve(.success(result.1)) }
    }
    private func pools(_ ready: JSON) throws -> [JSON] {
        guard let pools = ready["pools"].array, pools.count == 28 else { throw BridgeFault("pools", "28 pools required") }; return pools
    }
    private func validateReady(_ ready: JSON) throws {
        try require(ready["backend"].string == (cpu ? "stub" : "mlx") && ready["layers"].integer == 28 && ready["page_size"].integer == 128 && ready["vocab_size"].integer == 151936, "READY backend/geometry")
        let pools = try pools(ready)
        for (i, pool) in pools.enumerated() {
            try require(pool["layer"].integer == i && pool["pool_id"].string == "\(trace.session)/\(id)/L\(i)" && !(pool["pool_object_id"].string ?? "").isEmpty, "READY pool identity/order")
        }
        try require(Set(pools.compactMap { $0["pool_object_id"].string }).count == 28, "distinct pool objects")
    }
    private func validatePartition(_ layer: JSON, pool: JSON) throws {
        try require(layer["layer"] == pool["layer"] && layer["pool_id"] == pool["pool_id"] && layer["pool_object_id"] == pool["pool_object_id"], "layer/pool identity")
        guard let used = layer["used_page_ids"].array, let free = layer["free_page_ids"].array,
              let count = layer["num_pages"].integer, let capacity = layer["capacity"].integer, let bytes = layer["storage_nbytes"].integer else { throw BridgeFault("partition", "missing pool geometry") }
        let u = used.compactMap(\.integer), f = free.compactMap(\.integer)
        try require(count >= 0 && count == u.count + f.count && capacity >= count && bytes >= 0 && u.count == used.count && f.count == free.count, "pool bounds")
        try require(Set(u).count == u.count && Set(f).count == f.count && Set(u).isDisjoint(with: Set(f)) && Set(u + f) == Set(0..<count), "exact used/free partition")
        try require(u == u.sorted(), "used IDs sorted")
    }
    private func validateLiveLayers(_ value: JSON, lease: LeaseID, count: Int, ready: JSON) throws {
        guard let layers = value.array, layers.count == 28 else { throw BridgeFault("layers", "28 live layers required") }
        let pools = try pools(ready)
        for (i, layer) in layers.enumerated() {
            try validatePartition(layer, pool: pools[i])
            guard let pages = layer["cache_page_ids"].array, let owned = layer["owned_pages"].array, pages.count == 1, owned.count == 1 else { throw BridgeFault("live_pages", "one logical page per layer") }
            try require(layer["page_lens"] == .array([.n(count)]) && layer["offset"].integer == count, "cache length/offset")
            try require(layer["used_page_ids"] == .array(pages) && owned[0]["page_id"] == pages[0] && owned[0]["allocation_epoch"].integer == lease.epoch, "active page/epoch")
        }
    }
    private func validateEmptyPools(_ value: JSON, ready: JSON) throws {
        guard let layers = value.array, layers.count == 28 else { throw BridgeFault("empty_pools", "28 empty pools required") }
        let pools = try pools(ready)
        for (i, layer) in layers.enumerated() {
            try validatePartition(layer, pool: pools[i])
            try require(layer["cache_page_ids"] == .array([]) && layer["page_lens"] == .array([]) && layer["owned_pages"] == .array([]) && layer["offset"].integer == 0 && layer["used_page_ids"] == .array([]), "empty pool cache/used")
        }
    }
    private func validateRelease(_ value: JSON, receipt: HostReceipt, ready: JSON) throws {
        try require(value["released"].bool == true && value["receipt_id"] == receipt.raw["receipt_id"] && value["eval_seq"] == receipt.raw["seq"], "release original receipt identity")
        guard let layers = value["layers"].array, layers.count == 28, let original = receipt.raw["layers"].array else { throw BridgeFault("release", "full layer set missing") }
        var after: [JSON] = []
        for (i, layer) in layers.enumerated() {
            try require(layer["layer"].integer == i && layer["pool_id"] == original[i]["pool_id"] && layer["before"] == original[i] && layer["released_pages"] == original[i]["owned_pages"], "release exact original pages/epochs/before")
            for field in ["pool_object_id", "num_pages", "capacity", "storage_nbytes"] { try require(layer["after"][field] == original[i][field], "release unchanged pool geometry \(field)") }
            after.append(layer["after"])
        }
        try validateEmptyPools(.array(after), ready: ready)
    }
    private func command(_ kind: String, lease: LeaseID? = nil, spec: RequestSpec? = nil, entered: Promise<JSON>? = nil, extra: [String: JSON] = [:]) async throws -> JSON {
        let reply = Promise<JSON>()
        let value: JSON = try state.withLock { s in
            if let fault = s.fault { throw fault }
            try require(s.pending == nil && !s.processExited, "one pending command and live worker")
            try require((kind == "EVAL" && s.phase == "ready") || (kind == "RELEASE" && s.phase == "leased") || (kind == "SHUTDOWN" && s.phase == "ready"), "command admission phase")
            if kind == "RELEASE" { try require(s.receipt?.lease == lease, "release current lease") }
            var body = extra
            body["v"] = .n(1); body["kind"] = .string(kind); body["session"] = .string(trace.session); body["worker_id"] = .string(id); body["seq"] = .n(s.nextSeq)
            if let lease { body["request_id"] = .string(lease.request); body["lease_epoch"] = .n(lease.epoch) }
            let value = JSON.object(body)
            s.pending = Pending(kind: kind, seq: s.nextSeq, command: value, lease: lease, spec: spec, reply: reply, entered: entered)
            s.nextSeq += 1; s.commands += 1; s.phase = kind.lowercased()
            trace.record("wire.pending_registered", ["worker_id": .string(id), "command": value])
            return value
        }
        reply.deadline(deadlines.command, BridgeFault("command_timeout", "\(kind) \(lease?.request ?? "shutdown")")) { [self] in fail($0) }
        emitWrite(value)
        return try await reply.wait()
    }
    func eval(_ lease: LeaseID, spec: RequestSpec, entered: Promise<JSON>) async throws -> HostReceipt {
        let raw = try await command("EVAL", lease: lease, spec: spec, entered: entered, extra: [
            "operation": .string(spec.operation), "purpose": .string("native-reference"), "source_context": .null,
            "native_context": .object(["request_id": .string(lease.request), "task_id": .string(lease.task), "purpose": .string("native-reference")]),
            "token_ids": .array(spec.tokens), "input_sha256": .string(spec.digest)])
        return HostReceipt(lease: lease, raw: raw)
    }
    func release(_ receipt: HostReceipt) async throws -> JSON {
        try await command("RELEASE", lease: receipt.lease, extra: [
            "operation": receipt.raw["operation"], "purpose": .string("native-reference"), "source_context": .null,
            "native_context": .object(["request_id": .string(receipt.lease.request), "task_id": .string(receipt.lease.task), "purpose": .string("native-reference")]),
            "receipt_id": receipt.raw["receipt_id"], "eval_seq": receipt.raw["seq"]])
    }
    func shutdown() async throws -> JSON {
        let ack = try await command("SHUTDOWN")
        // EOF on command input is graceful only after the validated ACK.
        closeInput()
        reapPromise.deadline(deadlines.command, BridgeFault("reap_timeout", id)) { [self] in fail($0) }
        stdoutDone.deadline(deadlines.command, BridgeFault("stdout_timeout", id)) { [self] in fail($0) }
        stderrDone.deadline(deadlines.command, BridgeFault("stderr_timeout", id)) { [self] in fail($0) }
        stdinDone.deadline(deadlines.command, BridgeFault("stdin_close_timeout", id)) { [self] in fail($0) }
        let exit = try await reapPromise.wait(); _ = try await stdoutDone.wait(); _ = try await stderrDone.wait(); _ = try await stdinDone.wait()
        try require(exit == 0, "normal worker exit")
        try checkHealthy(); router.unregister(id)
        trace.record("worker.normal_closed", ["worker_id": .string(id), "roots": snapshot()])
        return ack
    }
    func checkHealthy() throws {
        let fault = state.withLock { s -> BridgeFault? in
            if let fault = s.fault { return fault }
            if s.processExited && (!s.shutdownACK || s.exitCode != 0) { return BridgeFault("worker_exit", "observed worker termination before successful shutdown") }
            return nil
        }
        if let fault { fail(fault); throw fault }
    }
    // Lock order is Session -> Owner -> Hub -> Trace. No Owner-held operation
    // acquires Session. The actual admission event is atomic with session health.
    func withAdmission<T>(_ body: () throws -> T) throws -> T {
        try state.withLock { s in
            if let fault = s.fault { throw fault }
            try require(!s.processExited && s.phase == "ready", "session live/ready at owner admission")
            return try body()
        }
    }
    private func closeInput() {
        writeQueue.async { [self] in
            do {
                try handles.input.fileHandleForWriting.close(); state.withLock { $0.stdinClosed = true }
                trace.record("worker.stdin_closed", ["worker_id": .string(id)]); stdinDone.resolve(.success(true))
            } catch { let fault = BridgeFault("stdin_close", String(describing: error)); fail(fault); stdinDone.resolve(.failure(fault)) }
        }
    }
    func fail(_ fault: BridgeFault) {
        let old = state.withLock { s -> (Pending?, Bool) in
            guard s.fault == nil else { return (nil, false) }
            s.fault = fault; s.phase = "unresolved"; return (s.pending, true)
        }
        guard old.1 else { return }
        trace.record("session.unresolved", ["worker_id": .string(id), "fault": fault.json, "roots": snapshot()])
        readyPromise.resolve(.failure(fault)); old.0?.reply.resolve(.failure(fault)); old.0?.entered?.resolve(.failure(fault))
        // Intentionally preserve pending command, receipt/KV obligation and IO
        // roots. No RELEASE, reset, task cancellation or retry occurs here.
    }
    // CPU expected-failure fixtures must not strand a process until suite end.
    // They close stdin (never release) and use bounded OS termination/reap;
    // their unresolved KV root remains in snapshots after process disappearance.
    func closeFailedCPUFixture() async {
        precondition(cpu)
        closeInput()
        let pid = state.withLock { $0.pid }
        if pid > 0 && !state.withLock({ $0.processExited }) { _ = Darwin.kill(pid_t(pid), SIGTERM) }
        reapPromise.deadline(1, BridgeFault("cpu_reap_timeout", id))
        _ = try? await reapPromise.wait()
        if pid > 0 && !state.withLock({ $0.processExited }) { _ = Darwin.kill(pid_t(pid), SIGKILL) }
        // Separate fresh drain observation: the original timeout is retained.
        let done = Promise<Bool>()
        reapQueue.async { done.resolve(.success(true)) }
        done.deadline(1, BridgeFault("cpu_final_reap_timeout", id)); _ = try? await done.wait()
        stdoutDone.deadline(1, BridgeFault("cpu_stdout_timeout", id)); stderrDone.deadline(1, BridgeFault("cpu_stderr_timeout", id))
        _ = try? await stdoutDone.wait(); _ = try? await stderrDone.wait()
        stdinDone.deadline(1, BridgeFault("cpu_stdin_close_timeout", id)); _ = try? await stdinDone.wait()
        router.unregister(id)
        trace.record("cpu.failure_fixture_closed", ["worker_id": .string(id), "roots": snapshot()])
    }
    func snapshot() -> JSON { state.withLock { s in .object([
        "worker_id": .string(id), "pid": .n(s.pid), "phase": .string(s.phase), "permit_forwarded": .bool(s.permit),
        "process_root": .bool(!s.processExited), "stdout_root": .bool(!s.stdoutJoined), "stderr_root": .bool(!s.stderrJoined), "stdin_root": .bool(!s.stdinClosed),
        "stdout_eof": .bool(s.stdoutEOF), "stderr_eof": .bool(s.stderrEOF),
        "terminal_error_diagnostic": s.terminalError ?? .null, "trailing_bytes": .n(s.trailingBytes),
        "pending_root": s.pending?.command ?? .null, "receipt_kv_root": s.receipt?.raw ?? .null,
        "shutdown_ack": .bool(s.shutdownACK), "actual_exit": s.exitCode.map(JSON.n) ?? .null, "fault": s.fault?.json ?? .null,
        "wait_status": s.waitStatus.map(JSON.n) ?? .null, "termination_signal": s.terminationSignal.map(JSON.n) ?? .null,
        "raw_frames": .n(s.rawFrames), "commands": .n(s.commands)
    ]) } }
}
