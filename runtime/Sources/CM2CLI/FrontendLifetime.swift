import Foundation
import Dispatch
import Synchronization
import Darwin
import RuntimeCore

/// Only CPU fixtures use this host registry. Its mutex linearizes the entire
/// register/spawn/IO-registration prefix against permanent shutdown sealing.
/// It owns direct compiler children, not arbitrary descendants or source Jobs.
final class CPUFrontendHost: HostRetainedRoot, Sendable {
    private struct State {
        var sealed = false, cleanupStarted = false
        var children: [ChildControl] = []
        var waiters: [CheckedContinuation<JSONValue, Never>] = []
        var result: JSONValue?
    }
    private let state = Mutex(State())
    private let queue = DispatchQueue(label: "cm2.cpu-frontend-finalizer")
    private let deadline: HostDeadline
    init(deadline: HostDeadline) { self.deadline = deadline }

    /// IO registration is nonthrowing and starts the three original workers.
    /// No child can appear after seal, including between register and run().
    func launch(_ child: ChildControl, startIO: () -> Void) throws -> Double {
        try state.withLock { s in
            try hostRequire(!s.sealed, "CPU frontend host is sealed")
            let budget = min(60, deadline.remaining - 17)
            try hostRequire(budget > 0, "CPU frontend has no bounded child/cleanup budget")
            try hostRequire(s.children.allSatisfy { $0.processFinished && $0.ioFinished }, "previous CPU compiler child/IO still live")
            s.children.append(child)
            try child.launch()
            startIO()
            return min(60, max(0, deadline.remaining - 17))
        }
    }
    func snapshot() -> JSONValue {
        let observed = state.withLock { ($0.sealed, $0.cleanupStarted, $0.children) }
        let children = observed.2.map { $0.snapshot() }
        return .object(["kind": .string("cpu-frontend-host"), "sealed": .bool(observed.0),
            "cleanup_started": .bool(observed.1), "total_host_seconds": .integer(deadline.seconds),
            "cleanup_wait_budget_seconds": .integer(12),
            "all_children_reaped": .bool(observed.2.allSatisfy { $0.processFinished }),
            "all_io_joined": .bool(observed.2.allSatisfy { $0.ioFinished }), "children": .array(children)])
    }
    /// TERM 2s, KILL until 7s, original IO workers until 12s, using absolute
    /// shared cutoffs rather than multiplying waits by child count. These are
    /// wait budgets, not a real-time scheduling guarantee. No source work join.
    func stopAndJoin() async -> JSONValue {
        await withCheckedContinuation { continuation in
            let action = state.withLock { s -> (JSONValue?, [ChildControl]?) in
                s.sealed = true
                if let result = s.result { return (result, nil) }
                s.waiters.append(continuation)
                if s.cleanupStarted { return (nil, nil) }
                s.cleanupStarted = true
                return (nil, s.children)
            }
            if let value = action.0 { continuation.resume(returning: value); return }
            guard let children = action.1 else { return }
            queue.async { [self] in
                let began = DispatchTime.now()
                for child in children where !child.processFinished { child.stop("cpu-host-finalization") }
                for child in children { _ = child.waitForTermination(until: began + 2) }
                for child in children where !child.processFinished { child.killIfRunning() }
                for child in children { _ = child.waitForTermination(until: began + 7) }
                for child in children { _ = child.waitForIO(until: began + 12) }
                let result = snapshot()
                let waiters = state.withLock { s in
                    s.result = result; let found = s.waiters; s.waiters.removeAll(); return found
                }
                for waiter in waiters { waiter.resume(returning: result) }
            }
        }
    }
}

/// A shared, multi-observer terminal fence replaces the consumable semaphore.
/// Foundation remains the sole actual direct-child reaper. The original bridge
/// and CPU host may both observe these same terminal/IO groups safely.
final class ChildControl: @unchecked Sendable {
    private struct State {
        var launched = false, launchFailed = false, terminated = false
        var pid: Int32?, pgid: Int32?, exit: Int32?, terminationKind: String?
        var reason: String?, termRequested = false, killRequested = false
    }
    private let process: Process
    private let identity: JSONValue
    private let state = Mutex(State())
    private let terminal = DispatchGroup()
    private let streams: DispatchGroup
    private let streamsSnapshot: @Sendable () -> JSONValue
    init(_ process: Process, identity: JSONValue, streams: DispatchGroup, streamsSnapshot: @escaping @Sendable () -> JSONValue) {
        self.process = process; self.identity = identity; self.streams = streams; self.streamsSnapshot = streamsSnapshot
        process.terminationHandler = { [weak self] observed in self?.didTerminate(observed) }
    }
    func launch() throws {
        try state.withLock { s in
            try hostRequire(!s.launched && !s.launchFailed && s.reason == nil, "compiler launch after stop or duplicate launch")
            // An unadmitted child creates no terminal obligation. In particular,
            // a registry seal/budget refusal must not leave an unmatched retain.
            terminal.enter()
            do {
                try process.run()
                s.launched = true; s.pid = process.processIdentifier
                s.pgid = Darwin.getpgid(process.processIdentifier)
            } catch {
                s.launchFailed = true; terminal.leave(); throw error
            }
        }
    }
    private func didTerminate(_ observed: Process) {
        let changed = state.withLock { s -> Bool in
            guard !s.terminated && !s.launchFailed else { return false }
            s.terminated = true; s.exit = observed.terminationStatus
            s.terminationKind = observed.terminationReason == .exit ? "exit" : "signal"
            return true
        }
        if changed { terminal.leave() }
    }
    var reason: String? { state.withLock { $0.reason } }
    var processFinished: Bool { state.withLock { $0.launchFailed || $0.terminated } }
    var ioFinished: Bool { streams.wait(timeout: .now()) == .success }
    func waitForTermination(until: DispatchTime) -> Bool { terminal.wait(timeout: until) == .success }
    func waitForIO(until: DispatchTime) -> Bool { streams.wait(timeout: until) == .success }
    func stop(_ reason: String) {
        state.withLock { s in
            if s.reason == nil { s.reason = reason }
            if s.launched && !s.terminated && !s.termRequested && process.isRunning {
                s.termRequested = true; process.terminate()
            }
        }
    }
    func killIfRunning() {
        state.withLock { s in
            if s.reason == nil { s.reason = "frontend-kill" }
            if s.launched && !s.terminated && !s.killRequested && process.isRunning {
                s.killRequested = true; _ = Darwin.kill(process.processIdentifier, SIGKILL)
            }
        }
    }
    func snapshot() -> JSONValue {
        let s = state.withLock { $0 }
        return .object(["identity": identity, "launched": .bool(s.launched), "launch_failed": .bool(s.launchFailed),
            "pid": s.pid.map { .integer(Int($0)) } ?? .null, "observed_pgid": s.pgid.map { .integer(Int($0)) } ?? .null,
            "termination_observed": .bool(s.terminated), "actual_exit": s.exit.map { .integer(Int($0)) } ?? .null,
            "termination_kind": s.terminationKind.map(JSONValue.string) ?? .null,
            "process_root": .bool(s.launched && !s.terminated), "io_root": .bool(!ioFinished),
            "term_requested": .bool(s.termRequested), "kill_requested": .bool(s.killRequested),
            "stop_reason": s.reason.map(JSONValue.string) ?? .null, "streams": streamsSnapshot()])
    }
}
