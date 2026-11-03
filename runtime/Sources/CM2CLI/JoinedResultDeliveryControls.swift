import Foundation
import Dispatch
import Synchronization
import RuntimeCore
import MLXTransport

// CPU ordering fixtures, not a performance model or an independent audit.
// Every invocation owns one real compiler/CPU-worker/production execute session.
private struct JRDControlFailure: Error { let message: String }
private func jrdField(_ value: JSONValue, _ key: String) -> JSONValue {
    guard case .object(let fields) = value else { return .null }
    return fields[key] ?? .null
}
private func jrdDirectory(_ url: URL) throws {
    guard !FileManager.default.fileExists(atPath: url.path) else { throw JRDControlFailure(message: "exclusive directory: " + url.path) }
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
}
private func jrdSave(_ value: JSONValue, _ url: URL) throws {
    try (value.encoded() + Data([10])).write(to: url, options: .withoutOverwriting)
}
private func jrdEvents(_ rows: [JSONValue], _ name: String) -> [JSONValue] {
    rows.filter { jrdField($0, "name") == .string(name) }
}
private func jrdOne(_ rows: [JSONValue], _ name: String) throws -> JSONValue {
    guard rows.count == 1 else { throw JRDControlFailure(message: name + " population " + String(rows.count)) }
    return rows[0]
}
private func jrdWord(_ value: UInt64) -> JSONValue { .object(["kind": .string("Word"), "value": .string(String(value))]) }
private func jrdPair(_ a: JSONValue, _ b: JSONValue) -> JSONValue { .object(["kind": .string("Pair"), "left": a, "right": b]) }
private func jrdPoll(_ name: String, seconds: Double, _ ready: @Sendable () -> Bool) async throws {
    let end = DispatchTime.now().uptimeNanoseconds + UInt64(seconds * 1_000_000_000)
    while DispatchTime.now().uptimeNanoseconds < end {
        if ready() { return }
        try await Task.sleep(for: .milliseconds(2))
    }
    throw JRDControlFailure(message: "bounded observation unavailable: " + name)
}
private struct JRDChecks {
    var rows: [JSONValue] = []
    mutating func equal(_ name: String, _ observed: JSONValue, _ expected: JSONValue) throws {
        let passed = observed == expected
        rows.append(.object(["check": .string(name), "observed": observed, "expected": expected, "passed": .bool(passed)]))
        if !passed { throw JRDControlFailure(message: name) }
    }
    mutating func truth(_ name: String, _ condition: Bool) throws { try equal(name, .bool(condition), .bool(true)) }
    mutating func before(_ name: String, _ left: JSONValue, _ right: JSONValue) throws {
        let a = try left.field("sequence").text(), b = try right.field("sequence").text()
        guard let x = UInt64(a), let y = UInt64(b) else { throw JRDControlFailure(message: "Core sequence type") }
        try truth(name, x < y)
    }
}

private struct JRDFixture: Sendable {
    let name: String
    var legacy: Bool { name == "J1" || name == "J7" }
    var policy: ExternalResultDeliveryPolicy { legacy ? .afterBackendClose : .afterSourceJoin }
    var source: String { name == "J5" ? "a_cleanup_failure" : "external_request" }
    var scenario: String { name == "J6" ? "lost_release_ack" : "success" }
    var holdsClose: Bool { !["J5", "J6"].contains(name) }
    var injectsCloseFailure: Bool { name == "J3" || name == "J7" }
    var expectsReply: Bool { ["J1", "J2", "J3", "J4"].contains(name) }
    var earlyReply: Bool { ["J2", "J3", "J4"].contains(name) }
    var coreExit: Int { name == "J4" ? 124 : name == "J5" ? 1 : ["J3", "J6", "J7"].contains(name) ? 2 : 0 }
    var coreStatus: String { ["J1", "J2", "J5"].contains(name) ? "completed" : "incomplete" }
}

/// A native host-owned continuation at the real production shutdown callsite.
/// It cannot authorize source work or fabricate a closed backend/ACK.
private final class JRDShutdownGate: HostRetainedRoot, Sendable {
    private struct State {
        var entered = false, open = false, exited = false
        var delegated = false, delegateCompleted = false
        var waiters: [CheckedContinuation<Void, Never>] = []
        var events: [JSONValue] = []
        var closedEvidence: JSONValue = .null
    }
    private let state = Mutex(State())
    let holds: Bool, throwsBeforeDelegate: Bool
    init(holds: Bool, throwsBeforeDelegate: Bool) { self.holds = holds; self.throwsBeforeDelegate = throwsBeforeDelegate }
    private func append(_ name: String, _ s: inout State) {
        s.events.append(.object(["event": .string(name), "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
                                "append_seq": .integer(s.events.count + 1)]))
    }
    func enter() throws {
        try state.withLock { s in
            guard !s.entered else { throw JRDControlFailure(message: "duplicate production shutdown") }
            s.entered = true; append("shutdown.enter", &s)
        }
    }
    func wait() async {
        guard holds else { return }
        await withCheckedContinuation { continuation in
            let immediate = state.withLock { s in
                if s.open { return true }
                s.waiters.append(continuation)
                append("shutdown.gate.registered", &s); return false
            }
            if immediate { continuation.resume() }
        }
    }
    func release() {
        let waiters = state.withLock { s -> [CheckedContinuation<Void, Never>] in
            guard !s.open else { return [] }
            s.open = true; append("shutdown.gate.open", &s)
            let saved = s.waiters; s.waiters.removeAll(); return saved
        }
        for waiter in waiters { waiter.resume() }
    }
    func delegated() { state.withLock { s in s.delegated = true; append("shutdown.delegate.begin", &s) } }
    func completed(_ evidence: MLXClosedEvidence) {
        state.withLock { s in s.delegateCompleted = true; s.closedEvidence = evidence.json; append("shutdown.delegate.return", &s) }
    }
    func injected() { state.withLock { s in append("shutdown.fixture-error.before-delegate", &s) } }
    func exited() { state.withLock { s in s.exited = true; append("shutdown.proxy.exit", &s) } }
    func snapshot() -> JSONValue {
        state.withLock { s in .object(["kind": .string("native-production-shutdown-gate"),
            "holds_close": .bool(holds), "injected_before_delegate": .bool(throwsBeforeDelegate),
            "entered": .bool(s.entered), "open": .bool(s.open), "waiter_registered": .bool(!s.waiters.isEmpty),
            "exited": .bool(s.exited), "delegated": .bool(s.delegated), "delegate_completed": .bool(s.delegateCompleted),
            "closed_evidence": s.closedEvidence, "events": .array(s.events), "clock": .string("swift-dispatch-uptime-ns")]) }
    }
}

/// Every ordinary method and snapshot is the original driver. Only shutdown
/// has a gate; its successful result is the actual driver's complete evidence.
private final class JRDBackend: MLXBackendDriver, HostRetainedRoot, Sendable {
    let original: MLXTransportDriver, gate: JRDShutdownGate
    init(_ original: MLXTransportDriver, gate: JRDShutdownGate) { self.original = original; self.gate = gate }
    var identity: MLXSessionIdentity { original.identity }
    var serviceObservation: BRSServiceTrace { original.serviceObservation }
    func withHealthyAdmission<T>(_ body: () throws -> T) throws -> T { try original.withHealthyAdmission(body) }
    func withHealthyHostAction<T>(_ body: () throws -> T) throws -> T { try original.withHealthyHostAction(body) }
    func checkHealthy() throws { try original.checkHealthy() }
    func evaluate(_ request: MLXEvalRequest) async throws -> MLXHostReceipt { try await original.evaluate(request) }
    func release(_ request: MLXReturnRequest) async throws -> MLXReturnACK { try await original.release(request) }
    func snapshot() -> JSONValue { original.snapshot() }
    func shutdown() async throws -> MLXClosedEvidence {
        try gate.enter()
        defer { gate.exited() }
        await gate.wait()
        if gate.throwsBeforeDelegate {
            gate.injected()
            throw MLXBackendFailure(faultID: "jrd1-fixture-close-error", code: "jrd1_fixture_close_error",
                raw: .object(["origin": .string("JRD1-CPU-fixture"), "boundary": .string("after-gate-open-before-original-shutdown"),
                              "worker_shutdown_attempted": .bool(false), "synthetic_ACK": .bool(false)]))
        }
        gate.delegated()
        let evidence = try await original.shutdown()
        gate.completed(evidence)
        return evidence
    }
}

/// The fixture retains the actual execute caller. body_done is a notification;
/// the only Task.value is after that notification and does not wait on service.
private final class JRDExecuteRoot: HostRetainedRoot, Sendable {
    private struct State {
        var task: Task<Void, Never>?, report: ExecutionReport?
        var bodyDone = false, actualJoined = false
    }
    private let state = Mutex(State())
    func start(_ operation: @escaping @Sendable () async -> ExecutionReport) {
        let gate = HostStartGate()
        let task = Task.detached { [self] in
            await gate.wait()
            let report = await operation()
            state.withLock { $0.report = report; $0.bodyDone = true }
        }
        state.withLock { precondition($0.task == nil); $0.task = task }
        gate.open()
    }
    func joinAfterBodyDone() async throws -> ExecutionReport {
        let observed = state.withLock { ($0.bodyDone, $0.task, $0.report) }
        guard observed.0, let task = observed.1, let report = observed.2 else { throw JRDControlFailure(message: "execute Task.value before body_done") }
        await task.value
        state.withLock { $0.actualJoined = true; $0.task = nil }
        return report
    }
    func snapshot() -> JSONValue {
        state.withLock { .object(["role": .string("fixture-caller-of-production-execute"), "body_done": .bool($0.bodyDone),
                                 "actual_joined": .bool($0.actualJoined), "retained_task": .bool($0.task != nil),
                                 "report_available": .bool($0.report != nil),
                                 "not_the_Core_host_observer": .bool(true)]) }
    }
}

private func jrdCheckCore(_ f: JRDFixture, report: ExecutionReport, demand: JSONValue,
                          gate: JSONValue, a32: [UInt64], checks: inout JRDChecks) throws {
    try checks.equal("original production Core status", try report.json.field("status"), .string(f.coreStatus))
    try checks.equal("original production Core exit", .integer(Int(report.exitCode)), .integer(f.coreExit))
    let trial = try jrdOne(report.json.field("trials").elements(), "Core trial")
    let ledger = try trial.field("ledger"), events = try trial.field("events").elements()
    let main = try jrdOne(Array(ledger.field("J").members().values).filter { jrdField($0, "role") == .string("main") }, "original main Job")
    let delivery = try ledger.field("external_result_delivery")
    try checks.equal("actual runtime delivery policy", try delivery.field("policy"), .string(f.policy.rawValue))
    try checks.equal("original runtime successful commit state", try delivery.field("success_committed"), .bool(f.expectsReply))
    try checks.equal("original runtime source failure state", try delivery.field("source_failure_abort_requested"), .bool(f.name == "J5"))
    try checks.equal("backend responsibility remains production", try ledger.field("backend_close_owner"), .string("production-host"))
    let requests = jrdEvents(events, "mlx.eval_requested"), receipts = jrdEvents(events, "mlx.receipt_accepted")
    let count = f.name == "J5" || f.name == "J6" ? 1 : 2
    try checks.equal("actual worker operation population", .integer(requests.count), .integer(count))
    try checks.equal("actual accepted receipt population", .integer(receipts.count), .integer(count))
    let safe = jrdEvents(events, "backend-return.safe")
    try checks.equal("actual complete return ACK population", .integer(safe.count), .integer(f.name == "J6" ? 0 : count))
    for (index, event) in safe.enumerated() {
        let beta = try event.field("details"), receipt = try beta.field("receipt"), ack = try beta.field("ack").field("worker_frame")
        let original = try receipt.field("worker_frame"), layers = try ack.field("layers").elements()
        let originals = try original.field("layers").elements()
        try checks.equal("ACK original receipt " + String(index), try ack.field("receipt_id"), try original.field("receipt_id"))
        try checks.equal("ACK original evaluation " + String(index), try ack.field("eval_seq"), try original.field("seq"))
        try checks.equal("ACK exact layer counts " + String(index), .array([.integer(layers.count), .integer(originals.count)]), .array([.integer(28), .integer(28)]))
        for layer in 0..<28 {
            let label = "ACK " + String(index) + "/" + String(layer)
            try checks.equal(label + " original before", try layers[layer].field("before"), originals[layer])
            try checks.equal(label + " actual released pages", try layers[layer].field("released_pages"), try originals[layer].field("owned_pages"))
            for key in ["cache_page_ids", "page_lens", "owned_pages", "used_page_ids"] {
                try checks.equal(label + " empty after " + key, try layers[layer].field("after").field(key), .array([]))
            }
            try checks.equal(label + " zero offset", try layers[layer].field("after").field("offset"), .integer(0))
        }
    }
    let boundary = jrdEvents(events, "host.source-join-safe-boundary"), reply = jrdEvents(events, "external.source-reply")
    if f.name != "J6" {
        let joined = try jrdOne(jrdEvents(events, "host.join"), "actual main join")
        let ready = try jrdOne(boundary, "source join and Safe boundary")
        let close = try jrdOne(jrdEvents(events, "host.backend-close-callsite"), "actual production close call")
        let detail = try ready.field("details"), safeState = try detail.field("state")
        try checks.equal("boundary full original publication", try detail.field("publication"), try main.field("publication_identity"))
        try checks.equal("boundary full original Stable outcome", try detail.field("outcome"), try main.field("published"))
        try checks.equal("boundary exact policy", try detail.field("delivery_policy"), .string(f.policy.rawValue))
        try checks.equal("boundary is active before close", try detail.field("host_final_state"), .string("active"))
        try checks.equal("boundary original main already joined", try safeState.field("host_main_joined"), .bool(true))
        try checks.equal("boundary has not invented backend closure", try safeState.field("backend_closed"), .bool(false))
        try checks.equal("boundary actual observer remains retained", try safeState.field("host_join_observer").field("actual_handle_retained"), .bool(true))
        try checks.equal("boundary observer not already joined", try safeState.field("host_join_observer").field("actual_joined"), .bool(false))
        try checks.equal("boundary exact original roots snapshot", try detail.field("roots"), try safeState.field("roots"))
        try checks.truth("boundary all source obligations actually Safe", try safeState.field("Omega").members().values.allSatisfy { $0 == .string("Safe") })
        try checks.truth("boundary retains physical backend responsibility", try safeState.field("roots").elements().contains { ((try? $0.text()) ?? "").hasPrefix("BackendSession:") })
        try checks.before("original main join precedes safe boundary", joined, ready)
        try checks.before("source safe boundary precedes close", ready, close)
        try checks.equal("actual main Task joined", try ledger.field("host_main_joined"), .bool(true))
        try checks.truth("all original source obligations Safe", try ledger.field("Omega").members().values.allSatisfy { $0 == .string("Safe") })
        try checks.truth("all original Owner epochs released", try ledger.field("O").members().values.allSatisfy { jrdField($0, "source_epoch") == .null })
        if f.expectsReply {
            let delivered = try jrdOne(reply, "same source reply")
            let b = try requests[1].field("details"), token = UInt64(3650)
            try checks.equal("all eight external values enter B", try b.field("tokens"), .array(jrdPayload.map(JSONValue.unsigned)))
            try checks.equal("control original B request", try demand.field("b_request"), b)
            try checks.equal("control original B receipt", try demand.field("b_receipt"), try receipts[1].field("details"))
            try checks.equal("CPU independent B sum", try receipts[1].field("details").field("worker_frame").field("token_id"), .unsigned(token))
            let a = a32.reduce(0, +) % 151936
            try checks.equal("original main successful source publication survives later close failure", try main.field("published").field("status"), .string("ok"))
            try checks.equal("full source A/B/historyA result", try main.field("published").field("value").field("right"),
                             jrdPair(jrdWord(a), jrdPair(jrdWord(token), jrdWord(a))))
            try checks.equal("external delivery original main publication identity", try demand.field("reply_publication"), try main.field("publication_identity"))
            try checks.equal("external delivery original main Caller", try demand.field("reply_caller"), try main.field("caller"))
            try checks.equal("reply event actual delivery policy", try delivered.field("details").field("delivery_policy"), .string(f.policy.rawValue))
            try checks.equal("reply event exact endpoint", try delivered.field("details").field("endpoint"),
                             .string(f.earlyReply ? "source-joined-before-backend-close" : "source-joined-and-backend-closed"))
            try checks.equal("reply distinguishes actual backend closure", try delivered.field("details").field("backend_closed"), .bool(!f.earlyReply))
            try checks.equal("real client received same B", try demand.field("delivered"), .object(["status": .string("success"), "token": .unsigned(token)]))
            try checks.before("safe boundary before reply", ready, delivered)
            if f.earlyReply { try checks.before("candidate reply before original shutdown call", delivered, close) }
            else { try checks.before("legacy close completes before reply", try jrdOne(jrdEvents(events, "host.backend-closed"), "backend closed"), delivered) }
        } else {
            try checks.equal("no successful external reply", .integer(reply.count), .integer(0))
            try checks.equal("no success token retained", try demand.field("reply"), .null)
        }
    } else {
        try checks.equal("missing ACK never reaches source-safe boundary", .integer(boundary.count), .integer(0))
        try checks.equal("missing ACK never replies", .integer(reply.count), .integer(0))
        try checks.equal("missing ACK never calls shutdown", try gate.field("entered"), .bool(false))
        try checks.equal("missing ACK does not release Owner", .integer(jrdEvents(events, "resource.released").count), .integer(0))
        try checks.equal("missing ACK never receives B", .integer(try ledger.field("host_input_waits").members().count), .integer(0))
        let beta = try jrdOne(Array(ledger.field("backend_returns").members().values), "Unknown beta")
        try checks.equal("original beta Unknown", try beta.field("phase"), .string("unknown"))
        try checks.equal("no invented ACK", try beta.field("ack"), .null)
        try checks.truth("original Owner still occupied", try ledger.field("O").members().values.allSatisfy { jrdField($0, "source_epoch") != .null })
        try checks.equal("no success result on missing ACK", try demand.field("reply"), .null)
    }
    if f.name == "J5" {
        let faults = try ledger.field("faults").members(), fault = try jrdOne(Array(ledger.field("faults").members().values), "ReadBounds fault")
        try checks.equal("original prejoin source fault", try fault.field("code"), .string("ReadBounds"))
        try checks.equal("same source fault propagated", try trial.field("outcome").field("faults"), .array(faults.keys.sorted().map(JSONValue.string)))
        try checks.equal("failure never opens request ingress", try demand.field("queued"), .bool(false))
        try checks.equal("original source failure notification once", .integer(jrdEvents(events, "external.source-failure").count), .integer(1))
    }
    if f.name == "J7" {
        let b = try requests[1].field("details"), token = UInt64(3650), a = a32.reduce(0, +) % 151936
        try checks.equal("legacy failure still computed original full B8", try b.field("tokens"), .array(jrdPayload.map(JSONValue.unsigned)))
        try checks.equal("legacy failure retained original B request", try demand.field("b_request"), b)
        try checks.equal("legacy failure retained original B receipt", try demand.field("b_receipt"), try receipts[1].field("details"))
        try checks.equal("legacy failure CPU B numerical oracle", try receipts[1].field("details").field("worker_frame").field("token_id"), .unsigned(token))
        try checks.equal("legacy main Stable result existed before close error", try main.field("published").field("value").field("right"),
                         jrdPair(jrdWord(a), jrdPair(jrdWord(token), jrdWord(a))))
        try checks.equal("legacy failure never attempted external delivery", try delivery.field("response_attempted"), .bool(false))
    }
    let completed = ["J1", "J2", "J5"].contains(f.name)
    try checks.equal("only actual successful production close clears backend", try ledger.field("backend_closed"), .bool(completed))
    try checks.equal("original observer actual join (deadline remains retained)", try ledger.field("host_join_observer").field("actual_joined"), .bool(f.name != "J4"))
    if f.name == "J4" {
        try checks.equal("deadline preserves original observer handle", try ledger.field("host_join_observer").field("actual_handle_retained"), .bool(true))
        try checks.truth("deadline keeps original BackendSession root", try ledger.field("roots").elements().contains { ((try? $0.text()) ?? "").hasPrefix("BackendSession:") })
    }
    if f.injectsCloseFailure {
        try checks.equal("late close error exact original diagnostic", try ledger.field("backend_failure").field("code"), .string("jrd1_fixture_close_error"))
        try checks.equal("injected error did not fake original shutdown", try gate.field("delegated"), .bool(false))
    }
    try checks.equal("no fabricated host success after failure", .integer(jrdEvents(events, "host.return").count), .integer(completed ? 1 : 0))
}

private let jrdPayload: [UInt64] = [101, 203, 307, 409, 503, 607, 709, 811]

func runJoinedResultDeliveryControls(fixture: String, sourcesDirectory: URL, evidenceDirectory: URL,
                                    context: TransportContext, frontendHost: CPUFrontendHost,
                                    evidence: HostEvidence) async throws -> JSONValue {
    guard (1...7).map({ "J" + String($0) }).contains(fixture) else { throw JRDControlFailure(message: "fixed J1...J7 fixture required") }
    let f = JRDFixture(name: fixture)
    try jrdDirectory(evidenceDirectory)
    let compilerDirectory = evidenceDirectory.appendingPathComponent("compiler")
    try jrdDirectory(compilerDirectory)
    var checks = JRDChecks(), failure: JSONValue = .null, core: JSONValue = .null
    var held: JSONValue = .null, beforeCleanup: JSONValue = .null, afterCleanup: JSONValue = .null
    var unexpectedBeforeCleanup: JSONValue = .null, unexpectedPrefixSaveFailure: JSONValue = .null
    var unexpectedPrefixSaved = false
    var backend: JRDBackend?, demand: ExternalDemandControl?, work: JRDExecuteRoot?
    var coreSaved = false
    do {
        let bytes = try readHostSource(sourcesDirectory.appendingPathComponent(f.source + ".json"))
        let compiler = CompilerBridge(evidenceDirectory: compilerDirectory, cpuHost: frontendHost)
        let program = try await compiler.checkClosedSource(bytes)
        try validateExternalDemandConfiguration(program, configured: true)
        evidence.put("jrd_compiler", compiler.provenance)
        let requestPath = evidenceDirectory.appendingPathComponent("request.json")
        let raw: JSONValue = .object(["schema": .string("ebd1-request-v1"), "session": .string(context.session),
            "request_id": .string("B"), "port": .string("B"), "operation": .string("Prefill8"),
            "tokens": .array(jrdPayload.map(JSONValue.unsigned))])
        let requestBytes = try raw.encoded(); try requestBytes.write(to: requestPath, options: .withoutOverwriting)
        let request = try ExternalDemandRequest(data: requestBytes, path: requestPath.path, expectedSession: context.session)
        let control = ExternalDemandControl(request: request, policy: .unchanged); demand = control
        evidence.retain("jrd_external_requester", ExternalDemandHostRoot(control))
        let actual = try await context.startCPU(scenario: f.scenario)
        let gate = JRDShutdownGate(holds: f.holdsClose, throwsBeforeDelegate: f.injectsCloseFailure)
        let proxy = JRDBackend(actual, gate: gate); backend = proxy
        let executeRoot = JRDExecuteRoot(); work = executeRoot
        evidence.retain("jrd_shutdown_gate", gate); evidence.retain("jrd_actual_driver", proxy)
        evidence.retain("jrd_execute_caller", executeRoot)
        control.prepare()
        executeRoot.start {
            await execute(program, backend: proxy, hostBudgetSeconds: 3, returnService: context.returnService,
                          externalDemand: control, externalResultDelivery: f.policy)
        }
        if f.holdsClose {
            try await jrdPoll("actual production shutdown continuation registered", seconds: 2) {
                jrdField(gate.snapshot(), "waiter_registered") == .bool(true)
            }
            if f.earlyReply {
                try await jrdPoll("actual external requester success body_done while shutdown held", seconds: 1) {
                    jrdField(control.snapshot(complete: false), "body_done") == .bool(true)
                }
            }
            held = .object(["gate": gate.snapshot(), "demand": control.snapshot(complete: false),
                            "execute_caller": executeRoot.snapshot(), "worker": actual.snapshot(), "transport": context.snapshot()])
            try jrdSave(held, evidenceDirectory.appendingPathComponent("held.json"))
            try checks.equal("real close is held", try held.field("gate").field("waiter_registered"), .bool(true))
            try checks.equal("original backend shutdown not yet called", try held.field("gate").field("delegated"), .bool(false))
            try checks.equal("production execute has not returned", try held.field("execute_caller").field("body_done"), .bool(false))
            try checks.equal("real worker still retained", try held.field("worker").field("process_root"), .bool(true))
            try checks.equal("global transport is not finished", try held.field("transport").field("finished"), .bool(false))
            try checks.equal("reply presence while held distinguishes policy", try held.field("demand").field("reply"), f.earlyReply ? .unsigned(3650) : .null)
            try checks.equal("real requester completion while held", try held.field("demand").field("body_done"), .bool(f.earlyReply))
            try checks.equal("requester handle has not been joined to induce donation", try held.field("demand").field("actual_joined"), .bool(false))
            if f.earlyReply {
                try checks.equal("held actual client success", try held.field("demand").field("delivered"), .object(["status": .string("success"), "token": .unsigned(3650)]))
            } else { try checks.equal("legacy held has no client result", try held.field("demand").field("delivered"), .null) }
            if f.name != "J4" { gate.release() }
        }
        try await jrdPoll("production execute body_done", seconds: 5) { jrdField(executeRoot.snapshot(), "body_done") == .bool(true) }
        let report = try await executeRoot.joinAfterBodyDone(); core = report.json
        try jrdSave(core, evidenceDirectory.appendingPathComponent("core-report.json")); coreSaved = true
        beforeCleanup = .object(["gate": gate.snapshot(), "demand": control.snapshot(complete: false),
                                 "execute_caller": executeRoot.snapshot(), "worker": actual.snapshot(), "transport": context.snapshot()])
        try jrdSave(beforeCleanup, evidenceDirectory.appendingPathComponent("before-cleanup.json"))
        // Keep the immediate original prefix above. Publication resumes the
        // client independently, so client receipt is checked only after its
        // own bounded notification, never by adding a production Task.value.
        try await jrdPoll("actual requester result notification after original Core return", seconds: 2) {
            jrdField(control.snapshot(complete: false), "body_done") == .bool(true)
        }
        // Check the original immutable report before any test cleanup can hide it.
        try jrdCheckCore(f, report: report, demand: control.snapshot(complete: false), gate: gate.snapshot(), a32: context.a32, checks: &checks)
        if f.name == "J4" {
            try checks.equal("watchdog really won while close remained held", jrdField(gate.snapshot(), "waiter_registered"), .bool(true))
            gate.release()
            try await jrdPoll("late original shutdown completes after saved timeout", seconds: 4) { jrdField(gate.snapshot(), "exited") == .bool(true) }
            try checks.equal("late cleanup is actual original shutdown", jrdField(gate.snapshot(), "delegate_completed"), .bool(true))
            // No access to or fabricated join of the private Core observer.
        } else if f.injectsCloseFailure || f.name == "J6" {
            await actual.closeFailedCPUFixture()
        }
        if report.exitCode != 0 { control.abort(reason: "jrd-fixture-original-core-nonsuccess") }
        try await jrdPoll("requester own body_done before its join", seconds: 2) {
            jrdField(control.snapshot(complete: false), "body_done") == .bool(true)
        }
        await control.finish()
        _ = try await context.finish()
        let finalDemand = control.snapshot(complete: true), finalWorker = actual.snapshot()
        try checks.equal("requester actual original Task joined", try finalDemand.field("actual_joined"), .bool(true))
        try checks.equal("requester handle discharged", try finalDemand.field("retained_task"), .bool(false))
        try checks.equal("requester continuation roots discharged", try finalDemand.field("retained_waiters"),
                         .object(["trigger": .bool(false), "payload": .bool(false), "result": .bool(false), "done": .integer(0), "close": .integer(0)]))
        if f.expectsReply {
            try checks.equal("late failure cannot retract delivered client result", try finalDemand.field("delivered"), .object(["status": .string("success"), "token": .unsigned(3650)]))
            try checks.equal("same successful reply remains retained as history", try finalDemand.field("reply"), .unsigned(3650))
        } else {
            try checks.equal("failure never manufactures success reply", try finalDemand.field("reply"), .null)
            try checks.equal("real requester receives failure", try finalDemand.field("delivered").field("status"), .string("failure"))
        }
        for name in ["process_root", "stdout_root", "stderr_root", "stdin_root"] {
            try checks.equal("actual worker closure " + name, try finalWorker.field(name), .bool(false))
        }
        try checks.equal("actual router/control finished", try context.snapshot().field("finished"), .bool(true))
        afterCleanup = .object(["gate": gate.snapshot(), "demand": finalDemand, "execute_caller": executeRoot.snapshot(),
                               "worker": finalWorker, "transport": context.snapshot(),
                               "original_Core_report_rewritten": .bool(false),
                               "late_Core_observer_actual_join_claimed": .bool(false)])
    } catch {
        failure = context.fault(error)
        // Preserve the actual failing prefix before abort/open/OS cleanup can
        // change it, including failure during an early bounded poll. These
        // diagnostic snapshots are not a cross-object atomic transaction.
        unexpectedBeforeCleanup = .object([
            "failure": failure, "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
            "clock": .string("swift-dispatch-uptime-ns"), "core_report_already_observed": core,
            "gate": backend?.gate.snapshot() ?? .null,
            "demand": demand?.snapshot(complete: false) ?? .null,
            "execute_caller": work?.snapshot() ?? .null, "worker": backend?.snapshot() ?? .null,
            "transport": context.snapshot(), "fixture_cleanup_not_started": .bool(true)])
        do {
            try jrdSave(unexpectedBeforeCleanup, evidenceDirectory.appendingPathComponent("unexpected-failure-before-cleanup.json"))
            unexpectedPrefixSaved = true
        } catch {
            // Archival failure is distinct from the original failure. Neither
            // replaces it nor permits us to skip bounded actual cleanup.
            unexpectedPrefixSaveFailure = context.fault(error)
        }
        demand?.abort(reason: "jrd-fixture-unexpected-failure")
        backend?.gate.release()
        if let backend { await backend.original.closeFailedCPUFixture() }
        else { await context.closeFailedCPUWorkers() }
        if let demand {
            do {
                try await jrdPoll("failed fixture requester body_done", seconds: 2) { jrdField(demand.snapshot(complete: false), "body_done") == .bool(true) }
                await demand.finish()
            } catch { /* Original failure remains; actual retained roots below show incomplete cleanup. */ }
        }
        if let work {
            do {
                try await jrdPoll("failed fixture execute caller body_done", seconds: 4) { jrdField(work.snapshot(), "body_done") == .bool(true) }
                if jrdField(work.snapshot(), "actual_joined") != .bool(true) {
                    let report = try await work.joinAfterBodyDone()
                    if core == .null { core = report.json }
                }
            } catch { /* Do not invent a join on this failed prefix. */ }
        }
        _ = try? await context.finish()
        afterCleanup = .object(["gate": backend?.gate.snapshot() ?? .null,
                               "demand": demand?.snapshot(complete: false) ?? .null,
                               "execute_caller": work?.snapshot() ?? .null, "worker": backend?.snapshot() ?? .null,
                               "transport": context.snapshot(), "original_Core_report_rewritten": .bool(false)])
    }
    if !coreSaved { try jrdSave(core, evidenceDirectory.appendingPathComponent("core-report.json")) }
    try jrdSave(afterCleanup, evidenceDirectory.appendingPathComponent("after-cleanup.json"))
    try jrdSave(context.events(), evidenceDirectory.appendingPathComponent("transport-events.json"))
    let result: JSONValue = .object(["schema": .string("jrd1-production-cpu-fixture-v1"), "fixture": .string(f.name),
        "policy": .string(f.policy.rawValue), "source": .string(f.source), "scenario": .string(f.scenario),
        "passed": .bool(failure == .null), "failure": failure, "checks": .array(checks.rows),
        "core_report": core, "held": held, "before_cleanup": beforeCleanup, "after_cleanup": afterCleanup,
        "unexpected_failure_before_cleanup": unexpectedBeforeCleanup,
        "unexpected_failure_prefix_saved": .bool(unexpectedPrefixSaved),
        "unexpected_failure_prefix_save_failure": unexpectedPrefixSaveFailure,
        "expected_Core_exit": .integer(f.coreExit), "protocol_session_success_expected": .bool(f.coreExit == 0),
        "physical_GPU_evidence": .bool(false), "performance_claim": .bool(false), "nativeOwnsBackend": .bool(false),
        "private_Core_observer_late_join_claimed": .bool(false), "original_core_preserved": .bool(true),
        "retained_fixture_roots": evidence.snapshot()])
    try jrdSave(result, evidenceDirectory.appendingPathComponent("result.json"))
    return result
}
