// MA1 level L0: the cancel/return/admit scenario written by hand against the
// worker wire protocol, with no owner, ledger, lease or obligation mechanism.
// Everything a careful engineer would type directly: spawn the worker, forward
// the permit, exchange JSON lines, cancel the waiter, join the producer, send
// RELEASE, send the next EVAL. Validation depth is whatever this file chooses.
import Foundation
import Dispatch
import CryptoKit

// MARK: - Small utilities

struct Failure: Error, CustomStringConvertible {
    let code: String, detail: String
    init(_ code: String, _ detail: String) { self.code = code; self.detail = detail }
    var description: String { "\(code): \(detail)" }
}

func require(_ condition: Bool, _ code: String, _ detail: String) throws {
    if !condition { throw Failure(code, detail) }
}

// FileHandle.readData(ofLength:) blocks filling its buffer until EOF on a pipe;
// a protocol pipe must expose each line as it arrives. Read raw, first short read.
func readChunk(_ fd: Int32) -> Data {
    var bytes = [UInt8](repeating: 0, count: 65536)
    while true {
        let n = bytes.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress!, $0.count) }
        if n > 0 { return Data(bytes.prefix(n)) }
        if n == 0 { return Data() }
        if errno == EINTR { continue }
        return Data()
    }
}

func jsonLine(_ object: [String: Any]) throws -> Data {
    var data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
    data.append(10)
    return data
}

func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

func tokenDigest(_ tokens: [Int]) -> String {
    // Must equal the worker's token_hash: compact JSON of the integer array.
    let text = "[" + tokens.map(String.init).joined(separator: ",") + "]"
    return sha256Hex(Data(text.utf8))
}

// MARK: - Options

struct Options {
    let inputs: String, session: String, outputDir: URL
    let workerPython: String, workerScript: String
    let scenario: String, stubScenario: String?
    var cpu: Bool { workerScript.hasSuffix("stub_worker.py") }
    init(_ argv: [String]) throws {
        var values: [String: String] = [:]
        var i = 1
        while i + 1 < argv.count { values[argv[i]] = argv[i + 1]; i += 2 }
        func need(_ key: String) throws -> String {
            guard let value = values[key] else { throw Failure("cli", "missing \(key)") }
            return value
        }
        inputs = try need("--inputs"); session = try need("--session")
        outputDir = URL(fileURLWithPath: try need("--output-dir"))
        workerPython = try need("--worker-python"); workerScript = try need("--worker-script")
        scenario = values["--scenario"] ?? "N"; stubScenario = values["--stub-scenario"]
        try require(values["--pgid"] == "0" && getpgrp() == getpid(), "cli", "must lead the wrapper-controlled process group")
    }
}

// MARK: - Trace: an event list plus one JSON line per event on stdout

final class Trace: @unchecked Sendable {
    private let lock = NSLock()
    private let origin = DispatchTime.now().uptimeNanoseconds
    private let out = DispatchQueue(label: "l0.stdout")
    private(set) var events: [[String: Any]] = []
    let session: String
    init(session: String) { self.session = session }
    func record(_ event: String, _ fields: [String: Any] = [:]) {
        lock.lock()
        var row = fields
        row["v"] = 1; row["session"] = session; row["event"] = event
        if row["kind"] == nil { row["kind"] = event }
        row["swift_ns"] = Int(DispatchTime.now().uptimeNanoseconds - origin)
        row["swift_seq"] = events.count + 1
        events.append(row)
        lock.unlock()
        if let data = try? jsonLine(row) { out.async { FileHandle.standardOutput.write(data) } }
    }
    func ns(of event: String) -> Int? {
        lock.lock(); defer { lock.unlock() }
        return events.first { $0["event"] as? String == event }?["swift_ns"] as? Int
    }
    func flush() { out.sync {} }
}

// MARK: - A result box with a cancellable wait (hand-written, no evidence)

final class ResultBox<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: T?
    private var waiters: [UUID: CheckedContinuation<T?, Never>] = [:]
    func publish(_ v: T) {
        lock.lock(); value = v; let ws = waiters; waiters.removeAll(); lock.unlock()
        for (_, w) in ws { w.resume(returning: v) }
    }
    /// Returns nil when the waiting task was cancelled before the value arrived.
    func waitCancellable() async -> T? {
        let id = UUID()
        return await withTaskCancellationHandler {
            await withCheckedContinuation { (c: CheckedContinuation<T?, Never>) in
                lock.lock()
                if let value { lock.unlock(); c.resume(returning: value); return }
                if Task.isCancelled { lock.unlock(); c.resume(returning: nil); return }
                waiters[id] = c; lock.unlock()
            }
        } onCancel: {
            lock.lock(); let w = waiters.removeValue(forKey: id); lock.unlock()
            w?.resume(returning: nil)
        }
    }
}

// MARK: - The worker process and its wire

/// A JSON frame crossing a continuation; the dictionary is never mutated after parsing.
struct Frame: @unchecked Sendable { let value: [String: Any] }

final class Worker: @unchecked Sendable {
    let id = UUID().uuidString
    private(set) var pid: pid_t = 0
    private var reaped: Int32?
    let input = Pipe(), output = Pipe(), errors = Pipe()
    private let lock = NSLock()
    private var frames: [Frame] = []
    private var frameWaiters: [CheckedContinuation<Frame, Error>] = []
    private var enterWaiters: [CheckedContinuation<Void, Never>] = []
    private var fault: Error?
    private var seq = 0
    var nextSeq: Int { lock.lock(); defer { lock.unlock() }; seq += 1; return seq }
    var dropNextReleaseACK = false   // M7 host-side frame loss injection
    private(set) var droppedFrames: [[String: Any]] = []
    let trace: Trace, options: Options
    private var stderrFile: FileHandle?
    init(_ options: Options, _ trace: Trace) { self.options = options; self.trace = trace }

    /// Foundation.Process puts the child into a fresh process group, which the
    /// wrapper's OS verification rejects; spawn directly so the worker inherits
    /// this (wrapper-controlled) group.
    func spawn() throws {
        var args = ["-I", "-S", "-B", options.workerScript, "--inputs", options.inputs, "--session", options.session,
                    "--worker-id", id, "--swift-pid", String(getpid()), "--pgid", String(getpgrp())]
        if options.cpu { args += ["--scenario", options.stubScenario ?? "success"] }
        var env = ProcessInfo.processInfo.environment
        env["PYTHONDONTWRITEBYTECODE"] = "1"; env["HF_HUB_OFFLINE"] = "1"; env["TRANSFORMERS_OFFLINE"] = "1"
        var actions: posix_spawn_file_actions_t? = nil
        var attributes: posix_spawnattr_t? = nil
        posix_spawn_file_actions_init(&actions); defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawnattr_init(&attributes); defer { posix_spawnattr_destroy(&attributes) }
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT))
        posix_spawn_file_actions_adddup2(&actions, input.fileHandleForReading.fileDescriptor, STDIN_FILENO)
        posix_spawn_file_actions_adddup2(&actions, output.fileHandleForWriting.fileDescriptor, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(&actions, errors.fileHandleForWriting.fileDescriptor, STDERR_FILENO)
        let argv = ([options.workerPython] + args).map { strdup($0) } + [nil]
        let envp = env.keys.sorted().map { strdup("\($0)=\(env[$0]!)") } + [nil]
        defer { argv.forEach { free($0) }; envp.forEach { free($0) } }
        var child: pid_t = 0
        let code = posix_spawn(&child, options.workerPython, &actions, &attributes, argv, envp)
        try require(code == 0, "spawn", "posix_spawn errno \(code)")
        pid = child
        try? input.fileHandleForReading.close(); try? output.fileHandleForWriting.close(); try? errors.fileHandleForWriting.close()
        trace.record("worker.started", ["worker_id": id, "created_pid": Int(pid)])
        let errURL = options.outputDir.appendingPathComponent("worker-\(id).stderr.log")
        FileManager.default.createFile(atPath: errURL.path, contents: nil)
        stderrFile = try FileHandle(forWritingTo: errURL)
        Thread { self.readStdout() }.start()
        Thread { self.readStderr() }.start()
    }
    private func readStderr() {
        while true {
            let chunk = readChunk(errors.fileHandleForReading.fileDescriptor)
            if chunk.isEmpty { return }
            stderrFile?.write(chunk)
        }
    }
    private func readStdout() {
        var buffer = Data()
        while true {
            let chunk = readChunk(output.fileHandleForReading.fileDescriptor)
            if chunk.isEmpty { fail(Failure("worker_eof", "stdout closed")); return }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 10) {
                let line = buffer[buffer.startIndex..<nl]; buffer.removeSubrange(buffer.startIndex...nl)
                guard let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else {
                    fail(Failure("malformed_frame", String(decoding: line, as: UTF8.self))); return
                }
                deliver(obj)
            }
        }
    }
    private func deliver(_ frame: [String: Any]) {
        let kind = frame["kind"] as? String ?? "?"
        trace.record("wire.received", ["kind": "wire.received", "worker_id": id, "message": frame])
        if kind == "HELLO" {
            // The wrapper verifies this line against the OS and replies with LOAD_PERMIT on our stdin.
            trace.record("worker_hello", ["kind": "worker_hello", "worker_id": id, "created_pid": Int(pid),
                                          "scenario": options.stubScenario ?? "success", "hello": frame])
            return
        }
        if kind == "ERROR" { fail(Failure(frame["code"] as? String ?? "worker_error", frame["message"] as? String ?? "")); return }
        if kind == "RELEASE_ACK" && dropNextReleaseACK {
            dropNextReleaseACK = false
            lock.lock(); droppedFrames.append(frame); lock.unlock()
            trace.record("misuse.ack_dropped_by_host", ["seq": frame["seq"] ?? 0])
            return
        }
        lock.lock()
        var enter: [CheckedContinuation<Void, Never>] = []
        if kind == "EVAL_ENTER" { enter = enterWaiters; enterWaiters.removeAll() }
        let boxed = Frame(value: frame)
        if let w = frameWaiters.first { frameWaiters.removeFirst(); lock.unlock(); w.resume(returning: boxed) }
        else { frames.append(boxed); lock.unlock() }
        for e in enter { e.resume() }
    }
    func fail(_ error: Error) {
        lock.lock()
        if fault == nil { fault = error }
        let ws = frameWaiters; frameWaiters.removeAll()
        let es = enterWaiters; enterWaiters.removeAll()
        lock.unlock()
        trace.record("worker.failed", ["fault": String(describing: error)])
        for w in ws { w.resume(throwing: error) }
        for e in es { e.resume() }
    }
    func next() async throws -> Frame {
        try await withCheckedThrowingContinuation { c in
            lock.lock()
            if let fault { lock.unlock(); c.resume(throwing: fault); return }
            if !frames.isEmpty { let f = frames.removeFirst(); lock.unlock(); c.resume(returning: f); return }
            frameWaiters.append(c); lock.unlock()
        }
    }
    func next(kind expected: String, timeout: Double) async throws -> [String: Any] {
        let deadline = Task { try await Task.sleep(for: .seconds(timeout)); fail(Failure("timeout", "waiting for \(expected)")) }
        defer { deadline.cancel() }
        while true {
            let frame = try await next().value
            if frame["kind"] as? String == expected { return frame }
            // Frames of other kinds on the way (HELLO already handled; READY etc.) are skipped.
        }
    }
    func waitEvalEnter() async {
        await withCheckedContinuation { c in
            lock.lock(); enterWaiters.append(c); lock.unlock()
        }
    }
    func send(_ obj: [String: Any]) throws {
        let data = try jsonLine(obj)
        input.fileHandleForWriting.write(data)
        trace.record("wire.sent", ["kind": "wire.sent", "worker_id": id, "message": obj])
    }
    func command(_ kind: String, _ fields: [String: Any]) throws -> Int {
        let s = nextSeq
        var obj: [String: Any] = ["v": 1, "kind": kind, "session": options.session, "worker_id": id, "seq": s]
        for (k, v) in fields { obj[k] = v }
        try send(obj)
        return s
    }
    func forwardPermit(_ permit: [String: Any]) throws { try send(permit) }
    func reap() -> Int32 {
        if let reaped { return reaped }
        var status: Int32 = 0
        var observed: pid_t
        repeat { observed = waitpid(pid, &status, 0) } while observed == -1 && errno == EINTR
        let signal = status & 0x7f
        let code = signal == 0 ? (status >> 8) & 0xff : 128 + signal
        reaped = code
        return code
    }
    func terminate() { if reaped == nil { kill(pid, SIGTERM) } }
}

// MARK: - Permit forwarding from the wrapper (our stdin)

func startPermitReader(_ worker: Worker, _ trace: Trace) {
    Thread {
        var buffer = Data()
        while true {
            let chunk = readChunk(FileHandle.standardInput.fileDescriptor)
            if chunk.isEmpty { trace.record("control.eof"); return }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 10) {
                let line = buffer[buffer.startIndex..<nl]; buffer.removeSubrange(buffer.startIndex...nl)
                guard let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { continue }
                if obj["kind"] as? String == "LOAD_PERMIT", obj["worker_id"] as? String == worker.id {
                    try? worker.forwardPermit(obj)
                    trace.record("worker.permit_forwarded", ["worker_id": worker.id])
                }
            }
        }
    }.start()
}

// MARK: - Requests

struct Receipt: @unchecked Sendable {
    let request: String, operation: String, lease: Int, seq: Int, raw: [String: Any]
    var token: Int { raw["token_id"] as? Int ?? -1 }
    var receiptID: String { raw["receipt_id"] as? String ?? "" }
}

struct Inputs {
    let a32: [Int], bPrefix7: [Int]
    init(path: String) throws {
        let obj = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as? [String: Any] ?? [:]
        guard let a = obj["a32"] as? [Int], let b = obj["b_prefix7"] as? [Int], a.count == 32, b.count == 7 else {
            throw Failure("inputs", "cm1-inputs-v1 a32/b_prefix7 required")
        }
        a32 = a; bPrefix7 = b
    }
}

func evalFields(request: String, lease: Int, operation: String, tokens: [Int], task: String) -> [String: Any] {
    ["request_id": request, "lease_epoch": lease, "operation": operation, "purpose": "native-reference",
     "source_context": NSNull(), "native_context": ["request_id": request, "task_id": task, "purpose": "native-reference"],
     "token_ids": tokens, "input_sha256": tokenDigest(tokens)]
}
func releaseFields(_ r: Receipt, task: String) -> [String: Any] {
    ["request_id": r.request, "lease_epoch": r.lease, "operation": r.operation, "purpose": "native-reference",
     "source_context": NSNull(), "native_context": ["request_id": r.request, "task_id": task, "purpose": "native-reference"],
     "receipt_id": r.receiptID, "eval_seq": r.seq]
}

final class Session: @unchecked Sendable {
    let options: Options, trace: Trace, worker: Worker, inputs: Inputs
    let commandTimeout: Double
    var misuse: [String: Any] = [:]
    init(_ options: Options, _ trace: Trace) throws {
        self.options = options; self.trace = trace
        worker = Worker(options, trace); inputs = try Inputs(path: options.inputs)
        commandTimeout = options.cpu ? 5 : 30
    }
    func start() async throws {
        try worker.spawn()
        startPermitReader(worker, trace)
        let ready = try await worker.next(kind: "READY", timeout: options.cpu ? 10 : 90)
        try require((ready["layers"] as? Int) == 28, "ready", "28 layers expected")
        trace.record("worker.ready")
    }
    /// EVAL on the calling task: send, observe ENTER, observe RECEIPT.
    func eval(label: String, request: String, lease: Int, operation: String, tokens: [Int], task: String) async throws -> Receipt {
        trace.record("\(label).eval_requested", ["request_id": request])
        let seq = try worker.command("EVAL", evalFields(request: request, lease: lease, operation: operation, tokens: tokens, task: task))
        let enter = try await worker.next(kind: "EVAL_ENTER", timeout: commandTimeout)
        try require((enter["seq"] as? Int) == seq, "wire", "EVAL_ENTER seq")
        trace.record("\(label).eval_entered")
        let receipt = try await worker.next(kind: "EVAL_RECEIPT", timeout: commandTimeout)
        try require((receipt["seq"] as? Int) == seq && receipt["token_id"] is Int, "wire", "EVAL_RECEIPT seq/token")
        trace.record("\(label).receipt_validated", ["token_id": receipt["token_id"] ?? -1])
        return Receipt(request: request, operation: operation, lease: lease, seq: seq, raw: receipt)
    }
    func release(label: String, _ r: Receipt, task: String, validateLease: Bool = true) async throws -> [String: Any] {
        trace.record("\(label).release_requested")
        _ = try worker.command("RELEASE", releaseFields(r, task: task))
        let ack: [String: Any]
        do { ack = try await worker.next(kind: "RELEASE_ACK", timeout: commandTimeout) }
        catch let e as Failure where e.code == "timeout" && options.scenario == "misuse:M7" {
            // The sloppy manual pattern: log and move on.
            trace.record("misuse.ack_timeout_ignored", ["label": label])
            misuse["ack_evidence_for_\(label)"] = false
            return [:]
        }
        try require((ack["released"] as? Bool) == true && (ack["layers"] as? [Any])?.count == 28, "ack", "released/28 layers")
        if validateLease { try require((ack["lease_epoch"] as? Int) == r.lease, "ack", "lease epoch") }
        else if (ack["lease_epoch"] as? Int) != r.lease { misuse["ack_lease_mismatch_ignored_for_\(label)"] = ack["lease_epoch"] ?? -1 }
        trace.record("\(label).release_acked")
        return ack
    }
    func shutdown() async throws {
        _ = try worker.command("SHUTDOWN", [:])
        let ack = try await worker.next(kind: "SHUTDOWN_ACK", timeout: commandTimeout)
        try require((ack["all_released"] as? Bool) == true, "shutdown", "all_released")
        trace.record("session.shutdown_acked")
        try? worker.input.fileHandleForWriting.close()
        let code = worker.reap()
        trace.record("worker.reaped", ["exit": Int(code)])
    }
    func abandon() {
        // Failure path: close the pipe and reap whatever the worker does; never a RELEASE.
        try? worker.input.fileHandleForWriting.close()
        worker.terminate()
        let code = worker.reap()
        trace.record("worker.reaped", ["exit": Int(code), "after_failure": true])
    }
}

// MARK: - Scenario N and the misuse variants

func runScenario(_ s: Session) async throws -> [String: Any] {
    let scenario = s.options.scenario
    let misuse = scenario.hasPrefix("misuse:") ? String(scenario.dropFirst(7)) : nil
    let box = ResultBox<Receipt>()
    let taskA = "A-task-" + UUID().uuidString
    let requestA = "A-" + UUID().uuidString
    // Producer A: one Task sends EVAL, observes receipt, publishes, returns.
    let producer = Task<Receipt, Error> {
        let receipt = try await s.eval(label: "a", request: requestA, lease: 1, operation: "Prefill32", tokens: s.inputs.a32, task: taskA)
        if misuse == "M1" {
            // Release on receipt observation, before this Task has published or returned.
            _ = try await s.release(label: "a", receipt, task: taskA)
            s.misuse["release_before_join"] = true
        }
        box.publish(receipt)
        return receipt
    }
    // Waiter W: an independent task waiting for A's result.
    let waiter = Task<Receipt?, Never> { await box.waitCancellable() }
    await s.worker.waitEvalEnter()
    s.trace.record("cancel.requested")
    waiter.cancel()
    let waited = await waiter.value
    s.trace.record("wait.returned", ["outcome": waited == nil ? "cancelled" : "value"])
    if misuse == "M2" || misuse == "M4" {
        // B while A's lease is still live (M2), or right after the waiter cancel as if A were done (M4).
        s.trace.record("b.requested"); s.trace.record("b.admitted")
        _ = try await s.eval(label: "b", request: "B-" + UUID().uuidString, lease: 2, operation: "Prefill8",
                             tokens: s.inputs.bPrefix7 + [0], task: "B-task")
        return ["misuse": misuse!, "note": "worker accepted B while A live"]
    }
    if misuse == "M5" {
        try await s.shutdown()
        return ["misuse": "M5", "note": "worker accepted shutdown with A live"]
    }
    let receiptA = try await producer.value
    s.trace.record("a.join_returned", ["token_id": receiptA.token])
    if misuse == "M7" { s.worker.dropNextReleaseACK = true }
    if misuse != "M1" {
        _ = try await s.release(label: "a", receiptA, task: taskA, validateLease: misuse != "M6")
    }
    if misuse == "M3" {
        _ = try await s.release(label: "a", receiptA, task: taskA)
        return ["misuse": "M3", "note": "worker accepted a second RELEASE"]
    }
    if misuse == "M8" {
        _ = try await s.eval(label: "stale", request: "stale-" + UUID().uuidString, lease: 1, operation: "Prefill8",
                             tokens: s.inputs.bPrefix7 + [receiptA.token], task: "stale-task")
        return ["misuse": "M8", "note": "worker accepted an old lease"]
    }
    s.trace.record("b.requested")
    s.trace.record("b.admitted")   // no admission concept at L0: B goes as soon as this line runs
    let taskB = "B-task-" + UUID().uuidString
    let receiptB = try await s.eval(label: "b", request: "B-" + UUID().uuidString, lease: 2, operation: "Prefill8",
                                    tokens: s.inputs.bPrefix7 + [receiptA.token], task: taskB)
    _ = try await s.release(label: "b", receiptB, task: taskB)
    try await s.shutdown()
    var result: [String: Any] = ["scenario": scenario, "a_token": receiptA.token, "b_token": receiptB.token,
                                 "wait_outcome": waited == nil ? "cancelled" : "value"]
    if let misuse { result["misuse"] = misuse }
    return result
}

// MARK: - Main

let trace: Trace
let options: Options
do {
    options = try Options(CommandLine.arguments)
    trace = Trace(session: options.session)
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8)); exit(2)
}
signal(SIGPIPE, SIG_IGN)
try? FileManager.default.createDirectory(at: options.outputDir, withIntermediateDirectories: true)
trace.record("session.started", ["level": "L0", "scenario": options.scenario, "swift_pid": Int(getpid())])

let semaphore = DispatchSemaphore(value: 0)
nonisolated(unsafe) var document: [String: Any] = ["schema": "ma1-l0-result-v1", "level": "L0", "session": options.session, "scenario": options.scenario]
nonisolated(unsafe) var exitStatus: Int32 = 0
Task.detached {
    var session: Session?
    do {
        let s = try Session(options, trace); session = s
        try await s.start()
        let value = try await runScenario(s)
        document["result"] = value; document["passed"] = true
        document["misuse_observations"] = s.misuse
    } catch {
        document["passed"] = false; document["fault"] = String(describing: error)
        if let s = session { document["misuse_observations"] = s.misuse; s.abandon() }
        exitStatus = 1
    }
    semaphore.signal()
}
semaphore.wait()
document["events"] = trace.events
if let data = try? JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]) {
    try? data.write(to: options.outputDir.appendingPathComponent("result.json"))
}
trace.record("control_close", ["kind": "control_close"])
trace.flush()
exit(exitStatus)
