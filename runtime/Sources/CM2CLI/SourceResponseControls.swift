import Foundation
import Dispatch
import Synchronization
import CryptoKit
import RuntimeCore
import MLXTransport

// CPU ordering fixtures, not a performance model or an independent audit.
// Every invocation owns one real compiler/CPU-worker/production execute session.
private struct SRCControlFailure: Error { let message: String }
private func srcField(_ value: JSONValue, _ key: String) -> JSONValue {
    guard case .object(let fields) = value else { return .null }
    return fields[key] ?? .null
}
private func srcDirectory(_ url: URL) throws {
    guard !FileManager.default.fileExists(atPath: url.path) else { throw SRCControlFailure(message: "exclusive directory: " + url.path) }
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
}
private func srcSave(_ value: JSONValue, _ url: URL) throws {
    try (value.encoded() + Data([10])).write(to: url, options: .withoutOverwriting)
}
private func srcEvents(_ rows: [JSONValue], _ name: String) -> [JSONValue] {
    rows.filter { srcField($0, "name") == .string(name) }
}
private func srcOne(_ rows: [JSONValue], _ name: String) throws -> JSONValue {
    guard rows.count == 1 else { throw SRCControlFailure(message: name + " population " + String(rows.count)) }
    return rows[0]
}
private func srcPoll(_ name: String, seconds: Double, _ ready: @Sendable () -> Bool) async throws {
    let end = DispatchTime.now().uptimeNanoseconds + UInt64(seconds * 1_000_000_000)
    while DispatchTime.now().uptimeNanoseconds < end {
        if ready() { return }
        try await Task.sleep(for: .milliseconds(2))
    }
    throw SRCControlFailure(message: "bounded observation unavailable: " + name)
}
private struct SRCChecks {
    var rows: [JSONValue] = []
    mutating func equal(_ name: String, _ observed: JSONValue, _ expected: JSONValue) throws {
        let passed = observed == expected
        rows.append(.object(["check": .string(name), "observed": observed, "expected": expected, "passed": .bool(passed)]))
        if !passed { throw SRCControlFailure(message: name) }
    }
    mutating func truth(_ name: String, _ condition: Bool) throws { try equal(name, .bool(condition), .bool(true)) }
    mutating func before(_ name: String, _ left: JSONValue, _ right: JSONValue) throws {
        let a = try left.field("sequence").text(), b = try right.field("sequence").text()
        guard let x = UInt64(a), let y = UInt64(b) else { throw SRCControlFailure(message: "Core sequence type") }
        try truth(name, x < y)
    }
}

private struct SRCFixture: Sendable {
    let name: String
    var policy: ExternalResultDeliveryPolicy { ["S03", "S10", "S11"].contains(name) ? .afterBackendClose : .afterSourceJoin }
    var source: String {
        switch name {
        case "S01": return "inside_b_run"
        case "S06": return "source_failure_after_offer"
        case "S07": return "duplicate_uncaught"
        case "S08": return "duplicate_caught"
        case "S09", "S10", "S11", "S12": return "missing_intent"
        case "S13": return "wrong_token"
        case "S14": return "wrong_owner"
        case "S15": return "offer_before_receive"
        default: return "unit_after_b"
        }
    }
    var scenario: String { "success" }
    var holdsRelease: Bool { name == "S01" }
    var holdsClose: Bool { ["S02", "S03", "S04", "S05", "S09", "S10", "S11", "S12"].contains(name) }
    var injectsCloseFailure: Bool { ["S04", "S11"].contains(name) }
    var timesOut: Bool { ["S05", "S12"].contains(name) }
    var missingIntent: Bool { ["S09", "S10", "S11", "S12"].contains(name) }
    var sourceFails: Bool { ["S06", "S07", "S13", "S14", "S15"].contains(name) }
    var hasIntent: Bool { ["S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"].contains(name) }
    var expectsReply: Bool { ["S01", "S02", "S03", "S04", "S05", "S08"].contains(name) }
    var earlyReply: Bool { expectsReply && policy == .afterSourceJoin }
    var coreExit: Int { timesOut ? 124 : injectsCloseFailure || missingIntent ? 2 : sourceFails ? 1 : 0 }
    var coreStatus: String { coreExit == 0 || coreExit == 1 ? "completed" : "incomplete" }
    var closeCompletes: Bool { !timesOut && !injectsCloseFailure }
    var faultCode: String? {
        switch name {
        case "S06": return "BodyRequestedFailure"
        case "S07", "S08": return "HostResponseAlreadyOffered"
        case "S13": return "HostResponseToken"
        case "S14": return "BackendOwnerBusy"
        case "S15": return "HostResponseRequest"
        default: return nil
        }
    }
}

/// A native host-owned continuation at the real production shutdown callsite.
/// It cannot authorize source work or fabricate a closed backend/ACK.
private final class SRCShutdownGate: HostRetainedRoot, Sendable {
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
            guard !s.entered else { throw SRCControlFailure(message: "duplicate production shutdown") }
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

/// S01 only: retain the actual B return request at the original release call.
/// Opening the gate merely allows the original driver to obtain the real ACK.
private final class SRCReleaseGate: HostRetainedRoot, Sendable {
    private struct State {
        var entered = false, open = false, delegated = false, exited = false
        var waiters: [CheckedContinuation<Void, Never>] = []
        var request: JSONValue = .null, ack: JSONValue = .null
        var events: [JSONValue] = []
    }
    private let state = Mutex(State())
    let enabled: Bool
    init(enabled: Bool) { self.enabled = enabled }
    private func append(_ name: String, _ s: inout State) {
        s.events.append(.object(["event": .string(name), "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
                                "append_seq": .integer(s.events.count + 1)]))
    }
    func enter(_ request: MLXReturnRequest) throws {
        try state.withLock { s in
            guard enabled, !s.entered, request.receipt.request.operation == "Prefill8" else {
                throw SRCControlFailure(message: "B release gate identity/once")
            }
            s.entered = true; s.request = request.json; append("release.enter", &s)
        }
    }
    func wait() async {
        await withCheckedContinuation { continuation in
            let immediate = state.withLock { s in
                if s.open { return true }
                s.waiters.append(continuation)
                append("release.gate.registered", &s); return false
            }
            if immediate { continuation.resume() }
        }
    }
    func release() {
        let waiters = state.withLock { s -> [CheckedContinuation<Void, Never>] in
            guard !s.open else { return [] }
            s.open = true; append("release.gate.open", &s)
            let saved = s.waiters; s.waiters.removeAll(); return saved
        }
        for waiter in waiters { waiter.resume() }
    }
    func delegated() { state.withLock { s in s.delegated = true; append("release.delegate.begin", &s) } }
    func completed(_ ack: MLXReturnACK) { state.withLock { s in s.ack = ack.json; append("release.delegate.return", &s) } }
    func exited() { state.withLock { s in s.exited = true; append("release.proxy.exit", &s) } }
    func snapshot() -> JSONValue {
        state.withLock { s in .object(["kind": .string("native-original-B-release-gate"), "enabled": .bool(enabled),
            "entered": .bool(s.entered), "open": .bool(s.open), "delegated": .bool(s.delegated), "exited": .bool(s.exited),
            "waiter_registered": .bool(!s.waiters.isEmpty), "request": s.request, "ack": s.ack,
            "events": .array(s.events), "clock": .string("swift-dispatch-uptime-ns")]) }
    }
}

/// Every method delegates to the real driver. Only the two declared native
/// ordering gates intervene; neither constructs receipt, ACK or close evidence.
private final class SRCBackend: MLXBackendDriver, HostRetainedRoot, Sendable {
    let original: MLXTransportDriver, gate: SRCShutdownGate
    let releaseGate: SRCReleaseGate
    init(_ original: MLXTransportDriver, gate: SRCShutdownGate, releaseGate: SRCReleaseGate) {
        self.original = original; self.gate = gate; self.releaseGate = releaseGate
    }
    var identity: MLXSessionIdentity { original.identity }
    var serviceObservation: BRSServiceTrace { original.serviceObservation }
    func withHealthyAdmission<T>(_ body: () throws -> T) throws -> T { try original.withHealthyAdmission(body) }
    func withHealthyHostAction<T>(_ body: () throws -> T) throws -> T { try original.withHealthyHostAction(body) }
    func checkHealthy() throws { try original.checkHealthy() }
    func evaluate(_ request: MLXEvalRequest) async throws -> MLXHostReceipt { try await original.evaluate(request) }
    func release(_ request: MLXReturnRequest) async throws -> MLXReturnACK {
        guard releaseGate.enabled && request.receipt.request.operation == "Prefill8" else { return try await original.release(request) }
        try releaseGate.enter(request)
        defer { releaseGate.exited() }
        await releaseGate.wait()
        releaseGate.delegated()
        let ack = try await original.release(request)
        releaseGate.completed(ack)
        return ack
    }
    func snapshot() -> JSONValue { original.snapshot() }
    func shutdown() async throws -> MLXClosedEvidence {
        try gate.enter()
        defer { gate.exited() }
        await gate.wait()
        if gate.throwsBeforeDelegate {
            gate.injected()
            throw MLXBackendFailure(faultID: "src1-fixture-close-error", code: "src1_fixture_close_error",
                raw: .object(["origin": .string("SRC1-CPU-fixture"), "boundary": .string("after-gate-open-before-original-shutdown"),
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
private final class SRCExecuteRoot: HostRetainedRoot, Sendable {
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
        guard observed.0, let task = observed.1, let report = observed.2 else { throw SRCControlFailure(message: "execute Task.value before body_done") }
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

private func srcNS(_ value: JSONValue) throws -> UInt64 {
    guard let n = UInt64(try value.text()) else { throw SRCControlFailure(message: "decimal uptime nanoseconds") }
    return n
}
private func srcCheckCore(_ f: SRCFixture, report: ExecutionReport, demand: JSONValue,
                          gate: JSONValue, releaseGate: JSONValue, heldRelease: JSONValue,
                          request: JSONValue, sourceCase: JSONValue, a32: [UInt64], checks: inout SRCChecks) throws {
    try checks.equal("original production Core status", try report.json.field("status"), .string(f.coreStatus))
    try checks.equal("original production Core exit", .integer(Int(report.exitCode)), .integer(f.coreExit))
    let trial = try srcOne(report.json.field("trials").elements(), "Core trial")
    let ledger = try trial.field("ledger"), events = try trial.field("events").elements()
    let jobs = try ledger.field("J").members()
    let main = try srcOne(Array(jobs.values).filter { srcField($0, "role") == .string("main") }, "original main Job")
    let delivery = try ledger.field("external_result_delivery")
    try checks.equal("actual runtime delivery policy", try delivery.field("policy"), .string(f.policy.rawValue))
    try checks.equal("actual successful commit", try delivery.field("success_committed"), .bool(f.expectsReply))
    try checks.equal("source failure abort state", try delivery.field("source_failure_abort_requested"), .bool(f.sourceFails))
    try checks.equal("backend remains production owned", try ledger.field("backend_close_owner"), .string("production-host"))
    try checks.equal("control complete original request", try demand.field("request"), request)
    try checks.equal("legacy demand priority unchanged", try demand.field("policy"), .string("unchanged"))
    let requests = srcEvents(events, "mlx.eval_requested"), receipts = srcEvents(events, "mlx.receipt_accepted")
    let count = f.name == "S15" ? 1 : 2
    try checks.equal("real operation population", .integer(requests.count), .integer(count))
    try checks.equal("real adopted receipt population", .integer(receipts.count), .integer(count))
    try checks.equal("A complete original input", try requests[0].field("details").field("tokens"), .array(a32.map(JSONValue.unsigned)))
    let sites = try sourceCase.field("sites"), originalSites = try sites.field("original_sites")
    try checks.equal("A actual generated source site", try requests[0].field("details").field("site"), try originalSites.field("a_produce"))
    try checks.equal("A CPU sum oracle", try receipts[0].field("details").field("worker_frame").field("token_id"), .unsigned(a32.reduce(0, +) % 151936))
    if count == 2 {
        let b = try requests[1].field("details"), receipt = try receipts[1].field("details")
        try checks.equal("B full external input", try b.field("tokens"), .array(srcPayload.map(JSONValue.unsigned)))
        try checks.equal("B operation", try b.field("operation"), .string("Prefill8"))
        try checks.equal("B actual generated source site", try b.field("site"), try originalSites.field("b_produce"))
        try checks.equal("control original B request", try demand.field("b_request"), b)
        try checks.equal("control original B receipt", try demand.field("b_receipt"), receipt)
        try checks.equal("B independent sum oracle", try receipt.field("worker_frame").field("token_id"), .unsigned(3650))
        let input = try srcOne(Array(ledger.field("host_input_waits").members().values), "one original received input")
        try checks.equal("complete consumed B8", try input.field("tokens"), .array(srcPayload.map(JSONValue.unsigned)))
        for key in ["returned", "received"] { try checks.equal("source input " + key, try input.field(key), .bool(true)) }
        try checks.equal("source input not rejected", try input.field("rejected"), .bool(false))
        try checks.equal("receive actual generated source site", try input.field("site"), try originalSites.field("b_receive"))
        try checks.equal("source holder actual history returned", try input.field("kont").field("held"), .bool(false))
        try checks.equal("source holder actual history owner", try input.field("kont").field("owner"), .string("returned"))
        let args = try srcOne(srcEvents(events, "external.b-source-arguments"), "real source B arguments")
        try checks.equal("source B binds original receive", try args.field("details").field("receive"), try input.field("id"))
        try checks.equal("source B full values", try args.field("details").field("tokens"), try b.field("tokens"))
    } else {
        try checks.equal("pre-receive failure never consumed input", try ledger.field("host_input_waits"), .object([:]))
        try checks.equal("pre-receive failure no B request", try demand.field("b_request"), .null)
        try checks.equal("pre-receive failure no B receipt", try demand.field("b_receipt"), .null)
    }
    let safes = srcEvents(events, "backend-return.safe")
    try checks.equal("all real release ACKs", .integer(safes.count), .integer(count))
    for (index, event) in safes.enumerated() {
        let beta = try event.field("details"), receipt = try beta.field("receipt"), ack = try beta.field("ack").field("worker_frame")
        let original = try receipt.field("worker_frame"), layers = try ack.field("layers").elements()
        let originals = try original.field("layers").elements(), q = try requests[index].field("details")
        try checks.equal("ACK original adopted receipt " + String(index), receipt, try receipts[index].field("details"))
        try checks.equal("ACK original request " + String(index), try receipt.field("request"), q)
        try checks.equal("ACK complete return request " + String(index), try beta.field("ack").field("request"),
                         .object(["backend_return": try q.field("backend_return"), "scope": try q.field("scope"), "eval_receipt": receipt]))
        let producer = try jobs[q.field("job").text()]
        try checks.truth("original request Job retained in ledger " + String(index), producer != nil)
        try checks.equal("original request actual Task " + String(index), try producer!.field("caller").field("task"), try q.field("task"))
        try checks.equal("ACK original receipt ID " + String(index), try ack.field("receipt_id"), try original.field("receipt_id"))
        try checks.equal("ACK original eval sequence " + String(index), try ack.field("eval_seq"), try original.field("seq"))
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
    let joined = try srcOne(srcEvents(events, "host.join"), "actual main join")
    let ready = try srcOne(srcEvents(events, "host.source-join-safe-boundary"), "original Safe boundary")
    let close = try srcOne(srcEvents(events, "host.backend-close-callsite"), "original shutdown call")
    let detail = try ready.field("details"), safeState = try detail.field("state")
    try checks.equal("boundary same publication", try detail.field("publication"), try main.field("publication_identity"))
    try checks.equal("boundary same source outcome", try detail.field("outcome"), try main.field("published"))
    try checks.equal("boundary active", try detail.field("host_final_state"), .string("active"))
    try checks.equal("boundary roots exact", try detail.field("roots"), try safeState.field("roots"))
    try checks.equal("boundary source main joined", try safeState.field("host_main_joined"), .bool(true))
    try checks.equal("boundary backend not closed", try safeState.field("backend_closed"), .bool(false))
    try checks.equal("boundary observer retained", try safeState.field("host_join_observer").field("actual_handle_retained"), .bool(true))
    try checks.equal("boundary observer unjoined", try safeState.field("host_join_observer").field("actual_joined"), .bool(false))
    try checks.truth("all original source obligations Safe", try safeState.field("Omega").members().values.allSatisfy { $0 == .string("Safe") })
    try checks.truth("all original scopes Joined", try safeState.field("S").members().values.allSatisfy { srcField($0, "state") == .string("Joined") })
    try checks.truth("all original Owner epochs released", try safeState.field("O").members().values.allSatisfy { srcField($0, "source_epoch") == .null })
    for (id, job) in jobs {
        for key in ["actual_task_exit_observed", "sourceRetired", "terminalObserved"] {
            try checks.equal("original Job " + id + " " + key, try job.field(key), .bool(true))
        }
        try checks.equal("original Job handle no longer retained " + id, try job.field("actualTaskRetained"), .bool(false))
    }
    try checks.before("main join before common source boundary", joined, ready)
    try checks.before("source boundary before actual close", ready, close)
    let offers = srcEvents(events, "external.response-offered"), intent = try ledger.field("external_response_intent")
    try checks.equal("successful immutable offer population", .integer(offers.count), .integer(f.hasIntent ? 1 : 0))
    if f.hasIntent {
        let offered = offers[0], offeredIntent = try offered.field("details").field("intent")
        try checks.equal("first intent survives every later action", intent, offeredIntent)
        try checks.equal("intent names first actual source offer", try intent.field("site"), try sites.field("offer_sites").elements().first ?? .null)
        try checks.equal("offer snapshot same intent", try offered.field("details").field("state").field("external_response_intent"), intent)
        try checks.equal("intent original external request", try intent.field("external_request"), request)
        try checks.equal("intent original B request", try intent.field("request"), try requests[1].field("details"))
        try checks.equal("intent original B receipt", try intent.field("receipt"), try receipts[1].field("details"))
        try checks.equal("intent exact token", try intent.field("token"), .unsigned(3650))
        try checks.equal("intent main job", try intent.field("caller").field("job"), try main.field("caller").field("job"))
        try checks.equal("intent main task", try intent.field("caller").field("task"), try main.field("caller").field("task"))
        try checks.equal("offering did not join main", try offered.field("details").field("state").field("host_main_joined"), .bool(false))
        try checks.equal("offering did not attempt delivery", try offered.field("details").field("state").field("external_result_delivery").field("response_attempted"), .bool(false))
        try checks.before("intent only before original main join", offered, joined)
    } else { try checks.equal("no valid intent manufactured", intent, .null) }
    if let code = f.faultCode {
        let faults = try ledger.field("faults").members()
        let matching = faults.filter { srcField($0.value, "code") == .string(code) }
        try checks.equal("exact intended source fault once", .integer(matching.count), .integer(1))
        let id = matching.keys.sorted()[0]
        if f.name == "S08" {
            try checks.truth("duplicate was actually handled", srcEvents(events, "fault.handled").contains {
                (try? $0.field("details").field("ids").elements().contains(.string(id))) == true
            })
        } else {
            try checks.equal("original main fault identities preserved", try main.field("published").field("faults"), .array([.string(id)]))
        }
        if f.name == "S13" { try checks.truth("wrong zero token cannot accidentally match real B", try receipts[1].field("details").field("worker_frame").field("token_id") != .unsigned(0)) }
    }
    if f.sourceFails {
        try checks.equal("main source failure stays source failure", try main.field("published").field("status"), .string("fault"))
        try checks.equal("original failure propagated", try trial.field("outcome"), try main.field("published"))
        try checks.equal("source failure notification once", .integer(srcEvents(events, "external.source-failure").count), .integer(1))
    } else {
        try checks.equal("main Unit success preserved independently of host outcome", try main.field("published"),
                         .object(["status": .string("ok"), "value": .object(["kind": .string("Unit")])]))
    }
    let replies = srcEvents(events, "external.source-reply")
    try checks.equal("successful response population", .integer(replies.count), .integer(f.expectsReply ? 1 : 0))
    if f.expectsReply {
        let reply = replies[0]
        try checks.equal("reply binds first intent", try reply.field("details").field("response_intent"), try intent.field("id"))
        try checks.equal("reply source outcome includes Unit", try reply.field("details").field("source_outcome"), try main.field("published"))
        try checks.equal("reply original main publication", try demand.field("reply_publication"), try main.field("publication_identity"))
        try checks.equal("reply original INIT host Caller", try demand.field("reply_caller"), try main.field("caller"))
        try checks.equal("reply actual client result", try demand.field("delivered"), .object(["status": .string("success"), "token": .unsigned(3650)]))
        try checks.equal("reply actual backend closure", try reply.field("details").field("backend_closed"), .bool(!f.earlyReply))
        try checks.before("safe boundary before response", ready, reply)
        if f.earlyReply { try checks.before("early commit before close call", reply, close) }
        else { try checks.before("legacy close before commit", try srcOne(srcEvents(events, "host.backend-closed"), "closed"), reply) }
    } else { try checks.equal("no success reply history", try demand.field("reply"), .null) }
    let contractEvents = srcEvents(events, "external.response-contract-failed")
    try checks.equal("missing intent contract failure population", .integer(contractEvents.count), .integer(f.missingIntent ? 1 : 0))
    if f.missingIntent {
        let failure = try ledger.field("external_response_failure"), contract = contractEvents[0]
        try checks.equal("separate exact response failure kind", try failure.field("kind"), .string("ResponseContractFailure"))
        try checks.equal("separate exact response failure code", try failure.field("code"), .string("MissingResponseIntent"))
        try checks.equal("response failure original publication", try failure.field("publication"), try main.field("publication_identity"))
        try checks.equal("response failure retains original main ok", try failure.field("source_outcome"), try main.field("published"))
        try checks.equal("response state not rewritten by close", try delivery.field("response_state"), .string("contract-failed"))
        try checks.equal("response diagnostic same nested field", try delivery.field("response_failure"), failure)
        try checks.equal("failure early endpoint honest in both policies", try contract.field("details").field("endpoint"), .string("source-joined-contract-check-before-backend-close"))
        try checks.equal("failure sent before closure", try contract.field("details").field("backend_closed"), .bool(false))
        try checks.equal("client actual missing-intent failure", try demand.field("delivered"),
                         .object(["status": .string("failure"), "reason": .string("source-response-contract:MissingResponseIntent")]))
        try checks.before("missing check after all source Safe", ready, contract)
        try checks.before("missing failure cannot skip close", contract, close)
        try checks.equal("only closed response failure reaches final contract event", .integer(srcEvents(events, "host.response-contract-failed").count), .integer(f.closeCompletes ? 1 : 0))
        if f.closeCompletes { try checks.equal("response failure does not manufacture backend failure", try ledger.field("backend_failure"), .null) }
    } else { try checks.equal("no spurious response contract failure", try ledger.field("external_response_failure"), .null) }
    try checks.equal("real close alone clears backend", try ledger.field("backend_closed"), .bool(f.closeCompletes))
    try checks.equal("original observer actual join", try ledger.field("host_join_observer").field("actual_joined"), .bool(!f.timesOut))
    if f.timesOut {
        try checks.equal("timeout retains original observer", try ledger.field("host_join_observer").field("actual_handle_retained"), .bool(true))
        try checks.truth("timeout retains original BackendSession", try ledger.field("roots").elements().contains { ((try? $0.text()) ?? "").hasPrefix("BackendSession:") })
        try checks.equal("one original watchdog event", .integer(srcEvents(events, "watchdog.incomplete").count), .integer(1))
        try checks.equal("watchdog budget remains three seconds", try trial.field("config").field("watchdog_seconds"), .integer(3))
    }
    if f.injectsCloseFailure {
        try checks.equal("exact native close injection", try ledger.field("backend_failure").field("code"), .string("src1_fixture_close_error"))
        try checks.equal("close injection before real shutdown", try gate.field("delegated"), .bool(false))
        try checks.equal("close error boundary", try ledger.field("backend_failure").field("raw").field("boundary"), .string("after-gate-open-before-original-shutdown"))
    }
    try checks.equal("host return only for original completed session", .integer(srcEvents(events, "host.return").count), .integer(f.coreStatus == "completed" ? 1 : 0))
    if f.holdsRelease {
        let offered = offers[0], state = try offered.field("details").field("state"), b = try requests[1].field("details")
        let scope = try b.field("scope").text(), beta = try b.field("backend_return").text()
        try checks.equal("offer in still-open B RUN", try state.field("S").field(scope).field("state"), .string("Open"))
        try checks.equal("offer B return still occupied", try state.field("backend_returns").field(beta).field("phase"), .string("occupied"))
        try checks.equal("offer did not discharge B return", try state.field("Omega").field("BackendReturn:" + beta), .string("Active"))
        try checks.equal("held release is complete original B", try heldRelease.field("release_gate").field("request").field("eval_receipt"), try receipts[1].field("details"))
        try checks.equal("real ACK returned through gate", try releaseGate.field("ack"), try safes[1].field("details").field("ack"))
        let gateRows = try releaseGate.field("events").elements()
        let entered = try srcOne(gateRows.filter { srcField($0, "event") == .string("release.enter") }, "B gate entered")
        let opened = try srcOne(gateRows.filter { srcField($0, "event") == .string("release.gate.open") }, "B gate opened")
        let ack = try srcOne(srcEvents(events, "backend-return.ack_observed").filter { srcField($0, "scope") == .string(scope) }, "B actual ACK")
        let origin = try srcNS(trial.field("config").field("host_uptime_origin_ns"))
        let offerTime = try origin.addingReportingOverflow(srcNS(offered.field("t_ns")))
        let ackTime = try origin.addingReportingOverflow(srcNS(ack.field("t_ns")))
        try checks.truth("Core clock addition has no overflow", !offerTime.overflow && !ackTime.overflow)
        try checks.truth("original offer precedes actual held release", try offerTime.partialValue <= srcNS(entered.field("capture_ns")))
        try checks.truth("live held snapshot precedes opening", try srcNS(heldRelease.field("capture_ns")) <= srcNS(opened.field("capture_ns")))
        try checks.truth("real ACK follows gate opening", try srcNS(opened.field("capture_ns")) <= ackTime.partialValue)
        try checks.before("real ACK still precedes joined Safe response boundary", ack, ready)
    }
}

private let srcPayload: [UInt64] = [101, 203, 307, 409, 503, 607, 709, 811]

func runSourceResponseControls(fixture: String, sourcesDirectory: URL, evidenceDirectory: URL,
                                    context: TransportContext, frontendHost: CPUFrontendHost,
                                    evidence: HostEvidence) async throws -> JSONValue {
    guard (1...15).map({ String(format: "S%02d", $0) }).contains(fixture) else { throw SRCControlFailure(message: "fixed S01...S15 fixture required") }
    let f = SRCFixture(name: fixture)
    try srcDirectory(evidenceDirectory)
    let compilerDirectory = evidenceDirectory.appendingPathComponent("compiler")
    try srcDirectory(compilerDirectory)
    var checks = SRCChecks(), failure: JSONValue = .null, core: JSONValue = .null
    var sourceCase: JSONValue = .null
    var held: JSONValue = .null, heldRelease: JSONValue = .null, beforeCleanup: JSONValue = .null, afterCleanup: JSONValue = .null
    var unexpectedBeforeCleanup: JSONValue = .null, unexpectedPrefixSaveFailure: JSONValue = .null
    var unexpectedPrefixSaved = false
    var backend: SRCBackend?, demand: ExternalDemandControl?, work: SRCExecuteRoot?
    var coreSaved = false
    do {
        let bytes = try readHostSource(sourcesDirectory.appendingPathComponent(f.source + ".json"))
        let manifestBytes = try readHostSource(sourcesDirectory.appendingPathComponent("manifest.json"))
        try manifestBytes.write(to: evidenceDirectory.appendingPathComponent("source-manifest.json"), options: .withoutOverwriting)
        let manifest = try ExactJSON.decode(manifestBytes)
        try checks.equal("source generation schema", try manifest.field("schema"), .string("src1-a-source-generation-v1"))
        sourceCase = try srcOne(manifest.field("cases").elements().filter { srcField($0, "case") == .string(f.source) }, "selected generated source")
        try srcSave(sourceCase, evidenceDirectory.appendingPathComponent("source-selection.json"))
        try checks.equal("selected source filename", try sourceCase.field("file"), .string(f.source + ".json"))
        try checks.equal("selected source exact bytes", try sourceCase.field("bytes"), .integer(bytes.count))
        let sourceSHA = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        try checks.equal("selected source exact SHA256", try sourceCase.field("sha256"), .string(sourceSHA))
        let compiler = CompilerBridge(evidenceDirectory: compilerDirectory, cpuHost: frontendHost)
        let program = try await compiler.checkClosedSource(bytes)
        try validateExternalDemandConfiguration(program, configured: true)
        evidence.put("src_compiler", compiler.provenance)
        let requestPath = evidenceDirectory.appendingPathComponent("request.json")
        let raw: JSONValue = .object(["schema": .string("ebd1-request-v1"), "session": .string(context.session),
            "request_id": .string("B"), "port": .string("B"), "operation": .string("Prefill8"),
            "tokens": .array(srcPayload.map(JSONValue.unsigned))])
        let requestBytes = try raw.encoded(); try requestBytes.write(to: requestPath, options: .withoutOverwriting)
        let request = try ExternalDemandRequest(data: requestBytes, path: requestPath.path, expectedSession: context.session)
        let control = ExternalDemandControl(request: request, policy: .unchanged); demand = control
        evidence.retain("src_external_requester", ExternalDemandHostRoot(control))
        let actual = try await context.startCPU(scenario: f.scenario)
        let gate = SRCShutdownGate(holds: f.holdsClose, throwsBeforeDelegate: f.injectsCloseFailure)
        let proxy = SRCBackend(actual, gate: gate, releaseGate: SRCReleaseGate(enabled: f.holdsRelease)); backend = proxy
        let executeRoot = SRCExecuteRoot(); work = executeRoot
        evidence.retain("src_shutdown_gate", gate); evidence.retain("src_release_gate", proxy.releaseGate); evidence.retain("src_actual_driver", proxy)
        evidence.retain("src_execute_caller", executeRoot)
        control.prepare()
        executeRoot.start {
            await execute(program, backend: proxy, hostBudgetSeconds: 3, returnService: context.returnService,
                          externalDemand: control, externalResultDelivery: f.policy)
        }
        if f.holdsRelease {
            try await srcPoll("original B release continuation registered", seconds: 2) {
                srcField(proxy.releaseGate.snapshot(), "waiter_registered") == .bool(true)
            }
            heldRelease = .object(["release_gate": proxy.releaseGate.snapshot(), "demand": control.snapshot(complete: false),
                "execute_caller": executeRoot.snapshot(), "worker": actual.snapshot(), "transport": context.snapshot(),
                "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
                "clock": .string("swift-dispatch-uptime-ns"), "private_Ledger_read_at_hold": .bool(false)])
            try srcSave(heldRelease, evidenceDirectory.appendingPathComponent("held-release.json"))
            try checks.equal("B release actually held", try heldRelease.field("release_gate").field("waiter_registered"), .bool(true))
            try checks.equal("B release not delegated before opening", try heldRelease.field("release_gate").field("delegated"), .bool(false))
            try checks.equal("no synthetic or already returned ACK", try heldRelease.field("release_gate").field("ack"), .null)
            try checks.equal("held B has complete actual operation", try heldRelease.field("release_gate").field("request").field("eval_receipt").field("request").field("operation"), .string("Prefill8"))
            try checks.equal("intent alone has not replied", try heldRelease.field("demand").field("reply"), .null)
            try checks.equal("intent alone has no delivered client result", try heldRelease.field("demand").field("delivered"), .null)
            try checks.equal("requester is still actually retained", try heldRelease.field("demand").field("retained_task"), .bool(true))
            try checks.equal("requester not done at B release hold", try heldRelease.field("demand").field("body_done"), .bool(false))
            try checks.equal("requester not joined at hold", try heldRelease.field("demand").field("actual_joined"), .bool(false))
            try checks.equal("production execute remains pending at hold", try heldRelease.field("execute_caller").field("body_done"), .bool(false))
            try checks.equal("real worker retained at B release hold", try heldRelease.field("worker").field("process_root"), .bool(true))
            proxy.releaseGate.release()
        }
        if f.holdsClose {
            try await srcPoll("actual production shutdown continuation registered", seconds: 2) {
                srcField(gate.snapshot(), "waiter_registered") == .bool(true)
            }
            if f.earlyReply || f.missingIntent {
                try await srcPoll("actual external requester terminal body_done while shutdown held", seconds: 1) {
                    srcField(control.snapshot(complete: false), "body_done") == .bool(true)
                }
            }
            held = .object(["gate": gate.snapshot(), "release_gate": proxy.releaseGate.snapshot(), "demand": control.snapshot(complete: false),
                            "execute_caller": executeRoot.snapshot(), "worker": actual.snapshot(), "transport": context.snapshot()])
            try srcSave(held, evidenceDirectory.appendingPathComponent("held.json"))
            try checks.equal("real close is held", try held.field("gate").field("waiter_registered"), .bool(true))
            try checks.equal("original backend shutdown not yet called", try held.field("gate").field("delegated"), .bool(false))
            try checks.equal("production execute has not returned", try held.field("execute_caller").field("body_done"), .bool(false))
            try checks.equal("real worker still retained", try held.field("worker").field("process_root"), .bool(true))
            try checks.equal("global transport is not finished", try held.field("transport").field("finished"), .bool(false))
            try checks.equal("reply presence while held distinguishes policy", try held.field("demand").field("reply"), f.earlyReply ? .unsigned(3650) : .null)
            try checks.equal("real requester completion while held", try held.field("demand").field("body_done"), .bool(f.earlyReply || f.missingIntent))
            try checks.equal("requester handle has not been joined to induce donation", try held.field("demand").field("actual_joined"), .bool(false))
            if f.earlyReply {
                try checks.equal("held actual client success", try held.field("demand").field("delivered"), .object(["status": .string("success"), "token": .unsigned(3650)]))
            } else if f.missingIntent {
                try checks.equal("missing intent delivers real contract failure before close", try held.field("demand").field("delivered").field("status"), .string("failure"))
            } else { try checks.equal("legacy held has no client result", try held.field("demand").field("delivered"), .null) }
            if !f.timesOut { gate.release() }
        }
        try await srcPoll("production execute body_done", seconds: 5) { srcField(executeRoot.snapshot(), "body_done") == .bool(true) }
        let report = try await executeRoot.joinAfterBodyDone(); core = report.json
        try srcSave(core, evidenceDirectory.appendingPathComponent("core-report.json")); coreSaved = true
        beforeCleanup = .object(["gate": gate.snapshot(), "release_gate": proxy.releaseGate.snapshot(), "demand": control.snapshot(complete: false),
                                 "execute_caller": executeRoot.snapshot(), "worker": actual.snapshot(), "transport": context.snapshot()])
        try srcSave(beforeCleanup, evidenceDirectory.appendingPathComponent("before-cleanup.json"))
        // Keep the immediate original prefix above. Publication resumes the
        // client independently, so client receipt is checked only after its
        // own bounded notification, never by adding a production Task.value.
        try await srcPoll("actual requester result notification after original Core return", seconds: 2) {
            srcField(control.snapshot(complete: false), "body_done") == .bool(true)
        }
        // Check the original immutable report before any test cleanup can hide it.
        try srcCheckCore(f, report: report, demand: control.snapshot(complete: false), gate: gate.snapshot(), releaseGate: proxy.releaseGate.snapshot(), heldRelease: heldRelease, request: request.json, sourceCase: sourceCase, a32: context.a32, checks: &checks)
        if f.timesOut {
            try checks.equal("watchdog really won while close remained held", srcField(gate.snapshot(), "waiter_registered"), .bool(true))
            gate.release()
            try await srcPoll("late original shutdown completes after saved timeout", seconds: 4) { srcField(gate.snapshot(), "exited") == .bool(true) }
            try checks.equal("late cleanup is actual original shutdown", srcField(gate.snapshot(), "delegate_completed"), .bool(true))
            // No access to or fabricated join of the private Core observer.
        } else if f.injectsCloseFailure {
            await actual.closeFailedCPUFixture()
        }
        if report.exitCode != 0 { control.abort(reason: "src-fixture-original-core-nonsuccess") }
        try await srcPoll("requester own body_done before its join", seconds: 2) {
            srcField(control.snapshot(complete: false), "body_done") == .bool(true)
        }
        await control.finish()
        _ = try await context.finish()
        let finalDemand = control.snapshot(complete: true), finalWorker = actual.snapshot()
        try checks.equal("requester actual original Task joined", try finalDemand.field("actual_joined"), .bool(true))
        try checks.equal("requester own close completed", try finalDemand.field("closed"), .bool(true))
        try checks.equal("requester handle discharged", try finalDemand.field("retained_task"), .bool(false))
        try checks.equal("execute caller actually joined", try executeRoot.snapshot().field("actual_joined"), .bool(true))
        try checks.equal("execute caller handle discharged", try executeRoot.snapshot().field("retained_task"), .bool(false))
        try checks.equal("shutdown gate no live continuation", try gate.snapshot().field("waiter_registered"), .bool(false))
        try checks.equal("release gate no live continuation", try proxy.releaseGate.snapshot().field("waiter_registered"), .bool(false))
        if f.holdsRelease {
            try checks.equal("original B release proxy actually returned", try proxy.releaseGate.snapshot().field("exited"), .bool(true))
        }
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
        afterCleanup = .object(["gate": gate.snapshot(), "release_gate": proxy.releaseGate.snapshot(), "demand": finalDemand, "execute_caller": executeRoot.snapshot(),
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
            "gate": backend?.gate.snapshot() ?? .null, "release_gate": backend?.releaseGate.snapshot() ?? .null,
            "demand": demand?.snapshot(complete: false) ?? .null,
            "execute_caller": work?.snapshot() ?? .null, "worker": backend?.snapshot() ?? .null,
            "transport": context.snapshot(), "fixture_cleanup_not_started": .bool(true)])
        do {
            try srcSave(unexpectedBeforeCleanup, evidenceDirectory.appendingPathComponent("unexpected-failure-before-cleanup.json"))
            unexpectedPrefixSaved = true
        } catch {
            // Archival failure is distinct from the original failure. Neither
            // replaces it nor permits us to skip bounded actual cleanup.
            unexpectedPrefixSaveFailure = context.fault(error)
        }
        demand?.abort(reason: "src-fixture-unexpected-failure")
        backend?.gate.release(); backend?.releaseGate.release()
        if let backend { await backend.original.closeFailedCPUFixture() }
        else { await context.closeFailedCPUWorkers() }
        if let demand {
            do {
                try await srcPoll("failed fixture requester body_done", seconds: 2) { srcField(demand.snapshot(complete: false), "body_done") == .bool(true) }
                await demand.finish()
            } catch { /* Original failure remains; actual retained roots below show incomplete cleanup. */ }
        }
        if let work {
            do {
                try await srcPoll("failed fixture execute caller body_done", seconds: 4) { srcField(work.snapshot(), "body_done") == .bool(true) }
                if srcField(work.snapshot(), "actual_joined") != .bool(true) {
                    let report = try await work.joinAfterBodyDone()
                    if core == .null { core = report.json }
                }
            } catch { /* Do not invent a join on this failed prefix. */ }
        }
        _ = try? await context.finish()
        afterCleanup = .object(["gate": backend?.gate.snapshot() ?? .null, "release_gate": backend?.releaseGate.snapshot() ?? .null,
                               "demand": demand?.snapshot(complete: false) ?? .null,
                               "execute_caller": work?.snapshot() ?? .null, "worker": backend?.snapshot() ?? .null,
                               "transport": context.snapshot(), "original_Core_report_rewritten": .bool(false)])
    }
    if !coreSaved { try srcSave(core, evidenceDirectory.appendingPathComponent("core-report.json")) }
    try srcSave(afterCleanup, evidenceDirectory.appendingPathComponent("after-cleanup.json"))
    try srcSave(context.events(), evidenceDirectory.appendingPathComponent("transport-events.json"))
    let result: JSONValue = .object(["schema": .string("src1-production-cpu-fixture-v1"), "fixture": .string(f.name),
        "policy": .string(f.policy.rawValue), "source": .string(f.source), "scenario": .string(f.scenario), "missing_intent": .bool(f.missingIntent),
        "source_case": sourceCase,
        "wrong_owner_is_allocation_first_rejection": .bool(f.name == "S14"),
        "passed": .bool(failure == .null), "failure": failure, "checks": .array(checks.rows),
        "core_report": core, "held": held, "held_release": heldRelease, "before_cleanup": beforeCleanup, "after_cleanup": afterCleanup,
        "unexpected_failure_before_cleanup": unexpectedBeforeCleanup,
        "unexpected_failure_prefix_saved": .bool(unexpectedPrefixSaved),
        "unexpected_failure_prefix_save_failure": unexpectedPrefixSaveFailure,
        "expected_Core_exit": .integer(f.coreExit), "protocol_session_success_expected": .bool(f.coreExit == 0),
        "physical_GPU_evidence": .bool(false), "performance_claim": .bool(false), "nativeOwnsBackend": .bool(false),
        "private_Core_observer_late_join_claimed": .bool(false), "original_core_preserved": .bool(true),
        "retained_fixture_roots": evidence.snapshot()])
    try srcSave(result, evidenceDirectory.appendingPathComponent("result.json"))
    return result
}
