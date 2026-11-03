import Foundation
import Synchronization

// This is a host await API. Swift retains the real caller stack; no PL Machine
// or affine holder is synthesized. The probe is observation, never authority.
final class WaitProbe: Sendable {
    let id = UUID().uuidString
    let label: String
    let entered = Signal(), attached = Signal(), returned = Signal()
    // Optional CPU-only fixture window; production callers leave nil.
    let beforeInstall: Signal?
    init(_ label: String, beforeInstall: Signal? = nil) { self.label = label; self.beforeInstall = beforeInstall }
}
enum WaitResolution: Sendable { case delivered(ProducerOutcome), cancelled }
fileprivate struct ResumeAction: Sendable {
    let continuation: CheckedContinuation<WaitResolution, Never>
    let resolution: WaitResolution
    let lease: LeaseID, probe: WaitProbe, trace: Trace
    func perform() {
        trace.record("wait.resume_begin", ["lease": lease.json, "wait_id": .string(probe.id)])
        continuation.resume(returning: resolution)
        trace.record("wait.resume_returned", ["lease": lease.json, "wait_id": .string(probe.id)])
    }
}
final class WaitCell: Sendable {
    private struct State {
        var winner: WaitResolution?
        var continuation: CheckedContinuation<WaitResolution, Never>?
        var installed = false, taken = false, attached = false
    }
    private let state = Mutex(State())
    let lease: LeaseID, probe: WaitProbe, trace: Trace
    init(_ lease: LeaseID, _ probe: WaitProbe, _ trace: Trace) {
        self.lease = lease; self.probe = probe; self.trace = trace
        trace.record("wait.cell_created", ["lease": lease.json, "wait_id": .string(probe.id)])
    }
    private func take(_ s: inout State) -> ResumeAction? {
        guard let winner = s.winner, let continuation = s.continuation else { return nil }
        precondition(s.installed && !s.taken)
        s.continuation = nil; s.taken = true
        trace.record("wait.continuation_taken", ["lease": lease.json, "wait_id": .string(probe.id)])
        return ResumeAction(continuation: continuation, resolution: winner, lease: lease, probe: probe, trace: trace)
    }
    // Hub holds its lock throughout installation/registry insertion. Neither
    // hub nor cell may perform a returned native action while holding a lock.
    fileprivate func install(_ c: CheckedContinuation<WaitResolution, Never>, cached: ProducerOutcome?) -> (Bool, ResumeAction?) {
        state.withLock { s in
            precondition(!s.installed); s.installed = true; s.continuation = c
            trace.record("wait.continuation_installed", ["lease": lease.json, "wait_id": .string(probe.id)])
            if s.winner != nil { return (false, take(&s)) }
            if let cached {
                s.winner = .delivered(cached)
                trace.record("wait.winner", ["lease": lease.json, "wait_id": .string(probe.id), "winner": .string("delivery"), "source": .string("cache")])
                return (false, take(&s))
            }
            s.attached = true
            trace.record("wait.attached", ["lease": lease.json, "wait_id": .string(probe.id)])
            return (true, nil)
        }
    }
    fileprivate func deliver(_ outcome: ProducerOutcome) -> ResumeAction? {
        state.withLock { s in
            precondition(s.installed && s.attached)
            guard s.winner == nil else { trace.record("wait.delivery_lost", ["lease": lease.json, "wait_id": .string(probe.id)]); return nil }
            s.winner = .delivered(outcome)
            trace.record("wait.winner", ["lease": lease.json, "wait_id": .string(probe.id), "winner": .string("delivery"), "source": .string("publish")])
            return take(&s)
        }
    }
    func cancel() {
        let action = state.withLock { s -> ResumeAction? in
            trace.record("wait.cancel_handler", ["lease": lease.json, "wait_id": .string(probe.id)])
            guard s.winner == nil else { trace.record("wait.cancel_lost", ["lease": lease.json, "wait_id": .string(probe.id)]); return nil }
            s.winner = .cancelled
            trace.record("wait.winner", ["lease": lease.json, "wait_id": .string(probe.id), "winner": .string("cancellation"), "source": .string("handler")])
            return take(&s)
        }
        action?.perform()
    }
}
final class ResultHub: Sendable {
    private struct State { var cached: ProducerOutcome?; var cells: [WaitCell] = [] }
    private let state = Mutex(State())
    let lease: LeaseID, trace: Trace
    init(_ lease: LeaseID, _ trace: Trace) { self.lease = lease; self.trace = trace }
    fileprivate func attach(_ cell: WaitCell, _ c: CheckedContinuation<WaitResolution, Never>) {
        let pair = state.withLock { s in
            let pair = cell.install(c, cached: s.cached)
            if pair.0 { s.cells.append(cell) }; return pair
        }
        if pair.0 { cell.probe.attached.open() }
        pair.1?.perform()
    }
    func publish(_ outcome: ProducerOutcome) {
        let actions = state.withLock { s -> [ResumeAction] in
            precondition(s.cached == nil && outcome.lease == lease)
            s.cached = outcome
            trace.record("producer.published", ["lease": lease.json, "outcome": outcome.json])
            let actions = s.cells.compactMap { $0.deliver(outcome) }
            let count = s.cells.count; s.cells.removeAll()
            trace.record("wait.registry_cleared", ["lease": lease.json, "entries": .n(count)])
            return actions
        }
        actions.forEach { $0.perform() }
    }
    func snapshot() -> JSON { state.withLock { .object(["published": .bool($0.cached != nil), "registry_entries": .n($0.cells.count)]) } }
    func waitCancellable(_ probe: WaitProbe) async throws -> HostReceipt {
        let cell = WaitCell(lease, probe, trace)
        probe.entered.open()
        let resolution = try await withTaskCancellationHandler {
            if let gate = probe.beforeInstall { try await gate.wait(seconds: 3, label: "CPU before-install fixture") }
            return await withCheckedContinuation { c in attach(cell, c) }
        } onCancel: { cell.cancel() }
        switch resolution {
        case .cancelled:
            trace.record("wait.returned", ["lease": lease.json, "wait_id": .string(probe.id), "outcome": .string("cancelled")])
            probe.returned.open(); throw CancellationError()
        case .delivered(let outcome):
            do {
                let receipt = try outcome.get()
                trace.record("wait.returned", ["lease": lease.json, "wait_id": .string(probe.id), "outcome": .string("value"), "receipt": receipt.raw])
                probe.returned.open(); return receipt
            } catch {
                trace.record("wait.returned", ["lease": lease.json, "wait_id": .string(probe.id), "outcome": .string("producer_failure"), "original_outcome": outcome.json])
                probe.returned.open(); throw error
            }
        }
    }
}
