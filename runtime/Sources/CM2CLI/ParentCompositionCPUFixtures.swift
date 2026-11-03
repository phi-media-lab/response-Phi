import Foundation
import Dispatch
import Synchronization
import RuntimeCore
import MLXTransport

struct ParentCPUFixture: Sendable {
    let name: String
    init(_ name: String) throws {
        try hostRequire((1...10).map { String(format: "B-C%02d", $0) }.contains(name), "fixed B-C01...B-C10 fixture")
        self.name = name
    }
    var child: String {
        switch name { case "B-C06": return "source_failure_after_offer"; case "B-C07": return "missing_intent"; default: return "unit_after_b" }
    }
    var workerScenario: String {
        switch name { case "B-C03": return "src1b_added_token"; case "B-C04": return "src1b_missing_token"; default: return "success" }
    }
    var token: UInt64 { name == "B-C03" ? 151643 : name == "B-C04" ? 151669 : 64288 }
    var cached: Bool { name == "B-C02" }
    var timesOut: Bool { name == "B-C09" }
    var closeFault: Bool { name == "B-C08" }
    var parentFault: Bool { name == "B-C10" }
    var hasResponse: Bool { !["B-C06", "B-C07"].contains(name) }
    var hasTool: Bool { hasResponse && !parentFault }
    var toolSucceeds: Bool { hasTool && !["B-C03", "B-C04", "B-C05"].contains(name) }
    var parentExit: Int { timesOut ? 124 : closeFault || parentFault ? 2 : ["B-C03", "B-C04", "B-C05", "B-C06", "B-C07"].contains(name) ? 1 : 0 }
    var childExit: Int { timesOut ? 124 : closeFault || name == "B-C07" ? 2 : name == "B-C06" ? 1 : 0 }
    var parentFaultCode: String? {
        switch name {
        case "B-C03": return "ToolAddedTokenUnsupported"
        case "B-C04": return "ToolTokenMissing"
        case "B-C05": return "ToolOpenFailure"
        case "B-C06": return "ServiceChildSourceFailure"
        case "B-C07": return "ServiceResponseContractFailure"
        default: return nil
        }
    }
}

/// One original native continuation. Opening it supplies no source result,
/// ACK or close evidence; it only resumes the actual caller of a real method.
private final class ParentCPUGate: Sendable {
    private struct State { var entered = false, open = false, returned = false; var waiter: CheckedContinuation<Void, Never>?; var events: [JSONValue] = [] }
    private let state = Mutex(State())
    let name: String, enabled: Bool
    init(_ name: String, enabled: Bool) { self.name = name; self.enabled = enabled }
    private func row(_ event: String, _ state: inout State) {
        state.events.append(.object(["event": .string(event), "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
            "append_seq": .integer(state.events.count + 1)]))
    }
    func wait() async {
        guard enabled else { return }
        await withCheckedContinuation { continuation in
            let ready = state.withLock { s in
                precondition(!s.entered); s.entered = true; row("gate.enter", &s)
                if s.open { return true }
                s.waiter = continuation; row("gate.registered", &s); return false
            }
            if ready { continuation.resume() }
        }
        state.withLock { s in s.returned = true; row("gate.returned", &s) }
    }
    func release() {
        let waiter = state.withLock { s -> CheckedContinuation<Void, Never>? in
            guard !s.open else { return nil }; s.open = true; row("gate.open", &s)
            let result = s.waiter; s.waiter = nil; return result
        }
        waiter?.resume()
    }
    func snapshot() -> JSONValue {
        state.withLock { .object(["name": .string(name), "enabled": .bool(enabled), "entered": .bool($0.entered),
            "open": .bool($0.open), "returned": .bool($0.returned), "waiter_registered": .bool($0.waiter != nil), "events": .array($0.events)]) }
    }
}

/// Installed only by the separate fixed CPU entry. The production entry
/// rejects this capability. All outcome authority stays in the original tool
/// and backend, and all three gates have actual continuations and owned roots.
final class ParentCompositionCPUControl: HostRetainedRoot, ParentServiceCPUControl, VocabSymbolTool, Sendable {
    private struct State {
        var original: MLXTransportDriver?, wrapperInstalled = false
        var evaluateCalls = 0, shutdownCalls = 0, shutdownReturned = false
        var shutdownEvidence: JSONValue = .null, injectedCloseFailure: JSONValue = .null
        var toolInvocation: JSONValue = .null, toolRoot: ToolReadRoot?, toolOutcome: ToolCallOutcome?
        var events: [JSONValue] = []
    }
    let fixture: ParentCPUFixture
    private let state = Mutex(State())
    private let evaluation: ParentCPUGate, parentWait: ParentCPUGate, shutdown: ParentCPUGate
    init(fixture: String) throws {
        self.fixture = try ParentCPUFixture(fixture)
        evaluation = ParentCPUGate("first-original-evaluate-before-delegate", enabled: fixture != "B-C02")
        parentWait = ParentCPUGate("parent-before-response-cell-wait", enabled: fixture == "B-C02")
        shutdown = ParentCPUGate("original-shutdown-before-delegate", enabled: fixture != "B-C02")
    }
    private func record(_ name: String, detail: JSONValue = .null) {
        state.withLock { s in s.events.append(.object(["event": .string(name), "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
            "append_seq": .integer(s.events.count + 1), "details": detail])) }
    }
    func wrapBackend(_ original: MLXTransportDriver) -> any MLXBackendDriver {
        state.withLock { s in precondition(!s.wrapperInstalled); s.wrapperInstalled = true; s.original = original }
        record("native.backend-wrapper-installed")
        return ParentCPUBackend(original: original, control: self)
    }
    fileprivate func beforeEvaluate(_ request: MLXEvalRequest) async throws {
        let first = state.withLock { s in s.evaluateCalls += 1; return s.evaluateCalls == 1 }
        record("native.original-evaluate-enter", detail: request.json)
        if first { await evaluation.wait() }
    }
    fileprivate func beforeShutdown() async throws {
        let once = state.withLock { s in s.shutdownCalls += 1; return s.shutdownCalls == 1 }
        try hostRequire(once, "native control observes only original shutdown invocation")
        record("native.original-shutdown-enter")
        await shutdown.wait()
        record("native.original-shutdown-delegate")
    }
    fileprivate func afterShutdown(_ evidence: MLXClosedEvidence) throws {
        state.withLock { s in s.shutdownReturned = true; s.shutdownEvidence = evidence.json }
        record("native.original-shutdown-return", detail: evidence.json)
        if fixture.closeFault {
            let failure = MLXBackendFailure(faultID: "src1b-close-result-delivery-failure", code: "src1b_close_result_delivery_failure",
                raw: .object(["origin": .string("SRC1B-native-CPU"),
                    "boundary": .string("after-original-shutdown-return-before-Core-receive"),
                    "actual_closed_evidence": evidence.json, "second_shutdown": .bool(false),
                    "physical_close_failure_claimed": .bool(false)]))
            state.withLock { $0.injectedCloseFailure = failure.raw }
            record("native.close-result-delivery-failure", detail: failure.raw)
            throw failure
        }
    }
    func beforeResponseWait(serviceID: String) async {
        record("native.parent-before-response-wait", detail: .string(serviceID))
        await parentWait.wait()
    }
    func afterResponseConsumed(serviceID: String) throws {
        record("native.parent-after-response-consumed", detail: .string(serviceID))
        if fixture.parentFault {
            throw hostFault("src1b-parent-post-consume", "fixed native infrastructure after actual response consume; service " + serviceID)
        }
    }
    func perform(_ invocation: ToolInvocation, root: ToolReadRoot) -> ToolCallOutcome {
        state.withLock { s in precondition(s.toolRoot == nil); s.toolRoot = root; s.toolInvocation = invocation.diagnostic }
        record("native.original-tool-enter", detail: invocation.diagnostic)
        let outcome = TokenizerVocabSymbolTool().perform(invocation, root: root)
        let detail = outcome.diagnostic
        // The readiness observation and its actual timestamp are published at
        // the same lock boundary; the harness cannot open close in between.
        state.withLock { s in
            s.events.append(.object(["event": .string("native.original-tool-return"),
                "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
                "append_seq": .integer(s.events.count + 1), "details": detail]))
            s.toolOutcome = outcome
        }
        return outcome
    }
    func releaseEvaluation() { evaluation.release() }
    func releaseParentWait() { parentWait.release() }
    func releaseShutdown() { shutdown.release() }
    func releaseAll() { evaluation.release(); parentWait.release(); shutdown.release() }
    func snapshot() -> JSONValue {
        let s = state.withLock { ($0.wrapperInstalled, $0.evaluateCalls, $0.shutdownCalls, $0.shutdownReturned, $0.shutdownEvidence,
            $0.injectedCloseFailure, $0.toolInvocation, $0.toolRoot, $0.toolOutcome, $0.events, $0.original) }
        return .object(["schema": .string("src1b-native-composition-control-v1"), "fixture": .string(fixture.name),
            "evaluation_gate": evaluation.snapshot(), "parent_wait_gate": parentWait.snapshot(), "shutdown_gate": shutdown.snapshot(),
            "wrapper_installed": .bool(s.0), "evaluate_calls": .integer(s.1), "shutdown_calls": .integer(s.2),
            "shutdown_returned": .bool(s.3), "original_shutdown_evidence": s.4, "injected_close_failure": s.5,
            "tool_invocation": s.6, "tool_root": s.7?.snapshot() ?? .null, "tool_outcome": s.8?.diagnostic ?? .null,
            "events": .array(s.9), "original_driver": s.10?.snapshot() ?? .null,
            "clock": .string("swift-dispatch-uptime-ns"), "model_execution": .bool(false)])
    }
}

private final class ParentCPUBackend: MLXBackendDriver, Sendable {
    let original: MLXTransportDriver, control: ParentCompositionCPUControl
    init(original: MLXTransportDriver, control: ParentCompositionCPUControl) { self.original = original; self.control = control }
    var identity: MLXSessionIdentity { original.identity }
    var serviceObservation: BRSServiceTrace { original.serviceObservation }
    func withHealthyAdmission<T>(_ body: () throws -> T) throws -> T { try original.withHealthyAdmission(body) }
    func withHealthyHostAction<T>(_ body: () throws -> T) throws -> T { try original.withHealthyHostAction(body) }
    func checkHealthy() throws { try original.checkHealthy() }
    func evaluate(_ request: MLXEvalRequest) async throws -> MLXHostReceipt {
        try await control.beforeEvaluate(request); return try await original.evaluate(request)
    }
    func release(_ request: MLXReturnRequest) async throws -> MLXReturnACK { try await original.release(request) }
    func snapshot() -> JSONValue { original.snapshot() }
    func shutdown() async throws -> MLXClosedEvidence {
        try await control.beforeShutdown()
        let evidence = try await original.shutdown()
        try control.afterShutdown(evidence)
        return evidence
    }
}
