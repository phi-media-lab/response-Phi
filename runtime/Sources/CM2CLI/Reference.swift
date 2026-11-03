import Foundation
import Synchronization
import RuntimeCore
import MLXTransport

/// A native reference owner is deliberately outside the source-language
/// ledger. Its real saved Tasks and post-join release are separately recorded.
private final class ReferenceOwner: HostRetainedRoot {
    private struct Entry {
        let request: NativeReferenceRequest
        var task: Task<NativeReferenceReceipt, any Error>?
        var joined = false
        var receipt: JSONValue = .null, ack: JSONValue = .null, failure: JSONValue = .null
    }
    private struct State { var current: String?; var order: [String] = []; var entries: [String: Entry] = [:] }
    private let state = Mutex(State())
    private let driver: NativeReferenceDriver, context: TransportContext, host: HostEvidence
    init(driver: NativeReferenceDriver, context: TransportContext, host: HostEvidence) {
        self.driver = driver; self.context = context; self.host = host
    }
    func snapshot() -> JSONValue {
        state.withLock { s in .object(["kind": .string("native-reference-owner"),
            "current_task": s.current.map(JSONValue.string) ?? .null,
            "runs": .array(s.order.map { id in
                let e = s.entries[id]!
                return .object(["request": e.request.json, "original_task_joined": .bool(e.joined),
                    "retained_task_handle": .bool(e.task != nil), "receipt": e.receipt,
                    "release_ack": e.ack, "failure": e.failure])
            })]) }
    }
    private func saveSnapshot() { host.put("native_reference_owner", snapshot()) }
    func run(role: String, operation: String, tokens: [UInt64]) async throws -> NativeReferenceReceipt {
        try driver.checkHealthy()
        let request = NativeReferenceRequest(requestID: "ref-" + role, taskID: "native-ref-task:" + UUID().uuidString,
                                             operation: operation, tokens: tokens)
        let gate = HostStartGate(), context = context, driver = driver
        let task: Task<NativeReferenceReceipt, any Error> = try state.withLock { s in
            try hostRequire(s.current == nil && s.entries[request.taskID] == nil, "reference owner is busy or task repeated")
            let task = Task.detached {
                await gate.wait()
                try context.record("reference.producer.started", ["request": request.json])
                let receipt = try await driver.evaluateReference(request)
                try context.record("reference.producer.receipt", ["receipt": receipt.json])
                return receipt
            }
            s.current = request.taskID; s.order.append(request.taskID)
            s.entries[request.taskID] = Entry(request: request, task: task)
            return task
        }
        saveSnapshot()
        try context.record("reference.original_task_registered", ["request": request.json, "owner": snapshot()])
        gate.open()
        let receipt: NativeReferenceReceipt
        do {
            receipt = try await task.value
        } catch {
            state.withLock { s in
                s.entries[request.taskID]!.joined = true; s.entries[request.taskID]!.task = nil
                s.entries[request.taskID]!.failure = context.fault(error)
            }
            saveSnapshot()
            try? context.record("reference.original_task_join_failed", ["request": request.json, "fault": context.fault(error)])
            throw error
        }
        state.withLock { s in
            s.entries[request.taskID]!.joined = true; s.entries[request.taskID]!.task = nil
            s.entries[request.taskID]!.receipt = receipt.json
        }
        saveSnapshot()
        do {
            try hostRequire(receipt.request == request, "reference join returned a different original request")
            try context.record("reference.original_task_join_returned", ["request": request.json, "receipt": receipt.json])
            // This is the only release call and is after this same Task.value.
            let ack = try await driver.releaseReference(receipt)
            try hostRequire(try ack.field("receipt_id") == receipt.raw.field("receipt_id"), "reference ACK changed original receipt")
            state.withLock { s in
                s.entries[request.taskID]!.ack = ack
                s.current = nil
            }
            saveSnapshot()
            try context.record("reference.owner_released", ["request": request.json, "ack": ack])
            return receipt
        } catch {
            state.withLock { $0.entries[request.taskID]!.failure = context.fault(error) }
            saveSnapshot()
            try? context.record("reference.post_join_failed", ["request": request.json, "fault": context.fault(error)])
            throw error
        }
    }
}

func runReference(context: TransportContext, evidence: HostEvidence, directory: URL) async throws -> HostWorkResult {
    let driver = try await context.startReference()
    let owner = ReferenceOwner(driver: driver, context: context, host: evidence)
    evidence.retain("native_reference_owner", owner)
    let a = try await owner.run(role: "A", operation: "Prefill32", tokens: context.a32)
    // Use this session's observed A token, never an anticipated value or a
    // checked-source result supplied by the driver.
    let bTokens = context.bPrefix7 + [a.token]
    try context.record("reference.b_input_constructed", ["a_receipt": a.json,
        "tokens": .array(bTokens.map(JSONValue.unsigned))])
    let b = try await owner.run(role: "B", operation: "Prefill8", tokens: bTokens)
    let closed = try await driver.shutdown()
    try hostRequire(closed.exitCode == 0 && closed.reaped && closed.stdoutJoined && closed.stderrJoined &&
                    closed.stdinClosed && closed.permitJoined, "reference backend did not actually close")
    let result: JSONValue = .object(["kind": .string("isolated-native-reference"),
        "top_level_prefill_evaluations": .integer(2), "a": a.json, "b": b.json,
        "owner": owner.snapshot(), "backend_close": closed.json])
    evidence.put("reference_result", result)
    try saveHostJSON(result, to: directory.appendingPathComponent("reference.json"))
    return HostWorkResult(status: "completed", exitCode: 0, value: result)
}
