import Foundation
import Synchronization

final class PublicationGate: Sendable {
    let held = Signal(), release = Signal()
}
final class OriginalRun: Sendable {
    let lease: LeaseID, spec: RequestSpec, hub: ResultHub
    let task: Task<ProducerOutcome, Never>
    let start: Signal, entered: Promise<JSON>
    let gate: PublicationGate?
    init(lease: LeaseID, spec: RequestSpec, hub: ResultHub, task: Task<ProducerOutcome, Never>, start: Signal, entered: Promise<JSON>, gate: PublicationGate?) {
        self.lease = lease; self.spec = spec; self.hub = hub; self.task = task; self.start = start; self.entered = entered; self.gate = gate
    }
}
struct CompletedRun: Sendable {
    let lease: LeaseID, receipt: HostReceipt, ack: JSON
    var json: JSON { .object(["lease": lease.json, "receipt": receipt.raw, "release_ack": ack]) }
}

// One owner serializes logical KV leases. The live original Task, transport,
// full receipt and KV-release obligation are distinct roots. Task termination
// alone never clears this active slot.
final class KVLoopOwner: Sendable {
    private struct State {
        var active: OriginalRun?
        var phase = "idle", nextEpoch = 1
        var joined: ProducerOutcome?, releaseACK: JSON?
        var completed: [CompletedRun] = []
        var quarantine: BridgeFault?
    }
    private let state = Mutex(State())
    let session: WorkerSession, trace: Trace
    init(_ session: WorkerSession, _ trace: Trace) { self.session = session; self.trace = trace }
    func begin(_ spec: RequestSpec, gate: PublicationGate? = nil) throws -> OriginalRun {
        return try session.withAdmission { try state.withLock { s in
            try require(s.phase == "idle" && s.active == nil && s.quarantine == nil && spec.epoch == s.nextEpoch, "B admission requires prior complete join/receipt/release ACK")
            let lease = LeaseID(session: trace.session, worker: session.id, request: spec.name, task: UUID().uuidString, epoch: spec.epoch)
            let hub = ResultHub(lease, trace), start = Signal(), entered = Promise<JSON>()
            let task = Task.detached { [session, trace] () -> ProducerOutcome in
                let outcome: ProducerOutcome
                do {
                    try await start.wait(seconds: session.deadlines.gate, label: "producer start \(spec.name)")
                    trace.record("producer.eval_requested", ["lease": lease.json, "input": spec.json])
                    let receipt = try await session.eval(lease, spec: spec, entered: entered)
                    trace.record("producer.receipt_validated", ["lease": lease.json, "receipt": receipt.raw])
                    if let gate {
                        trace.record("producer.publication_held", ["lease": lease.json, "receipt": receipt.raw])
                        gate.held.open()
                        try await gate.release.wait(seconds: session.deadlines.gate, label: "publication gate \(spec.name)")
                    }
                    outcome = .success(receipt)
                } catch {
                    let fault = (error as? BridgeFault) ?? BridgeFault("producer", String(describing: error))
                    session.fail(fault); entered.resolve(.failure(fault)); outcome = .failure(lease, fault)
                }
                // The exact Task stored below publishes, then returns the same
                // immutable outcome. There is no receipt-observer producer.
                hub.publish(outcome)
                trace.record("producer.returning", ["lease": lease.json, "outcome": outcome.json])
                return outcome
            }
            let run = OriginalRun(lease: lease, spec: spec, hub: hub, task: task, start: start, entered: entered, gate: gate)
            s.active = run; s.phase = "leased"; s.joined = nil; s.releaseACK = nil
            trace.record("owner.admitted", ["lease": lease.json, "original_task_stored": .bool(true)])
            return run
        } }
    }
    func drain(_ run: OriginalRun) async throws -> CompletedRun {
        try state.withLock { s in
            try require(s.active === run && s.phase == "leased", "drain current original Task exactly once")
            s.phase = "joining"
        }
        trace.record("owner.original_join_begin", ["lease": run.lease.json])
        let outcome = await run.task.value
        trace.record("owner.original_join_returned", ["lease": run.lease.json, "outcome": outcome.json])
        state.withLock { $0.joined = outcome }
        do {
            try require(outcome.lease == run.lease, "original Task outcome lease")
            let receipt = try outcome.get()
            try session.checkHealthy()
            state.withLock { $0.phase = "releasing" }
            trace.record("owner.release_requested", ["lease": run.lease.json, "receipt": receipt.raw])
            let ack = try await session.release(receipt)
            try session.checkHealthy()
            let completed = CompletedRun(lease: run.lease, receipt: receipt, ack: ack)
            state.withLock { s in
                s.releaseACK = ack; s.completed.append(completed); s.active = nil; s.phase = "idle"; s.nextEpoch += 1
            }
            trace.record("owner.released", ["lease": run.lease.json, "release_ack": ack])
            return completed
        } catch {
            let fault = (error as? BridgeFault) ?? BridgeFault("owner", String(describing: error))
            session.fail(fault)
            state.withLock { $0.phase = "unresolved"; $0.quarantine = fault }
            trace.record("owner.unresolved", ["lease": run.lease.json, "fault": fault.json, "roots": snapshot()])
            throw fault
        }
    }
    func rejectAdmissionProbe(_ spec: RequestSpec) throws {
        do { _ = try begin(spec); throw BridgeFault("unsafe_admission", "probe unexpectedly admitted \(spec.name)") }
        catch let fault as BridgeFault {
            if fault.code == "unsafe_admission" { throw fault }
            trace.record("owner.admission_rejected", ["request_id": .string(spec.name), "fault": fault.json, "roots": snapshot()])
        }
    }
    func snapshot() -> JSON { state.withLock { s in .object([
        "phase": .string(s.phase), "next_epoch": .n(s.nextEpoch), "active_lease": s.active?.lease.json ?? .null,
        "original_task_root": .bool(s.active != nil), "original_task_joined": .bool(s.joined != nil),
        "joined_outcome": s.joined?.json ?? .null, "release_ack": s.releaseACK ?? .null,
        "kv_obligation": .bool(s.active != nil), "quarantine": s.quarantine?.json ?? .null,
        "completed": .array(s.completed.map(\.json)), "hub": s.active?.hub.snapshot() ?? .null
    ]) } }
    func requireIdle() throws {
        try session.checkHealthy()
        try state.withLock { s in try require(s.phase == "idle" && s.active == nil && s.quarantine == nil, "owner checkpoint idle") }
    }
}
