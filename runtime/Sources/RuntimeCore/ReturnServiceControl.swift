import Foundation
import Dispatch
import Synchronization

package enum ReturnServicePolicy: String, Sendable { case unchanged; case raiseMain = "raise-main" }

/// Trusted host experiment control. No method discharges a source or backend
/// obligation. The only source-task action is one request on its existing handle.
package final class ReturnServiceControl: Sendable {
    private struct Stamp: Sendable {
        let ns: UInt64, priority: Int?, base: Int?
        static func capture() -> Self {
            let ns = DispatchTime.now().uptimeNanoseconds
            return withUnsafeCurrentTask { task in
                Self(ns: ns, priority: task.map { Int($0.priority.rawValue) },
                     base: task.map { Int($0.basePriority.rawValue) })
            }
        }
    }
    private struct Progress: Sendable {
        let id: Int
        var registered = false, started = false, bodyDone = false, joined = false
        var segments = 0, iterations = 0
        var value: UInt32
        var disposition = "parked"
        var json: JSONValue { .object([
            "id": .integer(id), "wait_registered": .bool(registered), "started": .bool(started),
            "completed_segments": .integer(segments), "iterations": .integer(iterations),
            "value": .unsigned(UInt64(value)), "body_done": .bool(bodyDone), "joined": .bool(joined),
            "disposition": .string(disposition)]) }
    }
    private struct Edge: Sendable {
        let worker: String, scope: ScopeHandle, kernel: Caller, cleanup: Caller, exit: Caller
        let site: String, publication: String
        var json: JSONValue { .object([
            "worker_id": .string(worker), "scope": RuntimeValue.scope(scope).diagnostic,
            "kernel_caller": kernel.json, "cleanup_caller": cleanup.json, "exit_caller": exit.json,
            "site": .string(site), "publication": .string(publication)]) }
    }
    private struct Target: Sendable {
        let edge: Edge
        var ownJoinCaller: Caller?, mainCaller: Caller?, registeredMain: Caller?
        var request: MLXReturnRequest?
        var ownJoined = false, policySeen = false, ownerReleased = false
        var json: JSONValue { .object([
            "producer": edge.json, "ownjoin_caller": ownJoinCaller?.json ?? .null,
            "main_caller": mainCaller?.json ?? .null, "registered_main_caller": registeredMain?.json ?? .null,
            "return_request": request?.json ?? .null, "ownjoin_observed": .bool(ownJoined),
            "policy_seen": .bool(policySeen), "owner_release_observed": .bool(ownerReleased)]) }
    }
    /// All row fields remain typed until snapshot; progress mutation, capture,
    /// and append share one mutex. No JSON construction or IO in these hooks.
    private struct Row: Sendable {
        let event: String, stamp: Stamp
        var competitor: Int?, segment: Int?, input: UInt32?, output: UInt32?, iterations: Int?
        var apiCalled: Bool?, requestedPriority: Int?, reason: String?
        var progress: [Progress]?
        var json: JSONValue { .object([
            "event": .string(event), "capture_ns": .string(String(stamp.ns)),
            "current_priority": stamp.priority.map(JSONValue.integer) ?? .null,
            "base_priority": stamp.base.map(JSONValue.integer) ?? .null,
            "priority_null_reason": stamp.priority == nil ? .string("no-current-swift-task") : .null,
            "competitor": competitor.map(JSONValue.integer) ?? .null,
            "segment": segment.map(JSONValue.integer) ?? .null,
            "input": input.map { .unsigned(UInt64($0)) } ?? .null,
            "output": output.map { .unsigned(UInt64($0)) } ?? .null,
            "iterations": iterations.map(JSONValue.integer) ?? .null,
            "api_called": apiCalled.map(JSONValue.bool) ?? .null,
            "requested_priority": requestedPriority.map(JSONValue.integer) ?? .null,
            "reason": reason.map(JSONValue.string) ?? .null,
            "progress": progress.map { .array($0.map(\.json)) } ?? .null]) }
    }
    private struct State {
        var prepared = false, gate = "parked", finishStarted = false, closed = false
        var rows: [Row] = [], attempted = 0, dropped = 0, issues: [String] = []
        var progress: [Progress] = []
        var handles: [Int: Task<Void, Never>] = [:]
        var gateWaiters: [Int: CheckedContinuation<Bool, Never>] = [:]
        var doneWaiters: [CheckedContinuation<Void, Never>] = []
        var closeWaiters: [CheckedContinuation<Void, Never>] = []
        var readyWaiters: [CheckedContinuation<Bool, Never>] = []
        var target: Target?, lateEdge: Edge?
        var raiseCalls = 0
    }
    private let state = Mutex(State())
    package let session: String, policy: ReturnServicePolicy, competitionTasks: Int
    private let role: String
    private let origin = DispatchTime.now().uptimeNanoseconds
    private let capacity = 1024
    package init(session: String, policy: ReturnServicePolicy, competitionTasks: Int, role: String = "production") {
        precondition(competitionTasks == 0 || competitionTasks == 8)
        self.session = session; self.policy = policy; self.competitionTasks = competitionTasks; self.role = role
    }
    private func append(_ s: inout State, _ row: Row) {
        s.attempted += 1
        if s.rows.count < capacity { s.rows.append(row) } else { s.dropped += 1 }
    }
    private func issue(_ s: inout State, _ message: String) {
        if s.issues.count < 32 { s.issues.append(message) }
        append(&s, Row(event: "control.invalid", stamp: .capture(), reason: message))
    }
    package func prepare() {
        state.withLock { s in
            guard !s.prepared else { issue(&s, "duplicate prepare"); return }
            s.prepared = true; s.rows.reserveCapacity(capacity)
            s.progress = (0..<competitionTasks).map { Progress(id: $0, value: UInt32($0 + 1)) }
            append(&s, Row(event: "control.prepared", stamp: .capture(), progress: s.progress))
            // The mutex keeps registration from racing the saved original handles.
            for id in 0..<competitionTasks {
                s.handles[id] = Task.detached(priority: .medium) { await self.runCompetitor(id) }
                append(&s, Row(event: "competitor.created", stamp: .capture(), competitor: id))
            }
        }
    }
    private func awaitGate(_ id: Int) async -> Bool {
        await withCheckedContinuation { continuation in
            let action = state.withLock { s -> (Bool?, [CheckedContinuation<Bool, Never>]) in
                if s.gate == "parked" {
                    s.gateWaiters[id] = continuation; s.progress[id].registered = true
                    append(&s, Row(event: "competitor.wait.registered", stamp: .capture(), competitor: id))
                    let ready = s.gateWaiters.count == competitionTasks ? s.readyWaiters : []
                    if !ready.isEmpty { s.readyWaiters.removeAll() }
                    return (nil, ready)
                }
                append(&s, Row(event: "competitor.wait.cached", stamp: .capture(), competitor: id, reason: s.gate))
                return (s.gate == "run", [])
            }
            for waiter in action.1 { waiter.resume(returning: true) }
            if let run = action.0 { continuation.resume(returning: run) }
        }
    }
    @inline(never) private static func segment(_ input: UInt32) -> (UInt32, Int) {
        var value = input, iterations = 0
        for _ in 0..<250_000 { value = value &* 1_664_525 &+ 1_013_904_223; iterations += 1 }
        return (value, iterations)
    }
    private func runCompetitor(_ id: Int) async {
        let run = await awaitGate(id)
        var value = UInt32(id + 1)
        if run {
            state.withLock { s in
                s.progress[id].started = true; s.progress[id].disposition = "run"
                append(&s, Row(event: "competitor.started", stamp: .capture(), competitor: id))
            }
            for index in 1...32 {
                let input = value
                state.withLock { s in append(&s, Row(event: "competitor.segment.begin", stamp: .capture(),
                    competitor: id, segment: index, input: input)) }
                let computed = Self.segment(input); value = computed.0
                state.withLock { s in
                    s.progress[id].segments = index; s.progress[id].iterations += computed.1; s.progress[id].value = value
                    append(&s, Row(event: "competitor.segment.end", stamp: .capture(), competitor: id,
                        segment: index, input: input, output: value, iterations: computed.1))
                }
                await Task.yield()
            }
        }
        let done = state.withLock { s -> [CheckedContinuation<Void, Never>] in
            s.progress[id].bodyDone = true; s.progress[id].disposition = run ? "run" : "abort"
            append(&s, Row(event: "competitor.body_done", stamp: .capture(), competitor: id,
                output: value, iterations: s.progress[id].iterations, reason: s.progress[id].disposition))
            guard s.progress.allSatisfy(\.bodyDone) else { return [] }
            let result = s.doneWaiters; s.doneWaiters.removeAll(); return result
        }
        for waiter in done { waiter.resume() }
    }
    /// Called at the existing successful producer's return edge, synchronously.
    /// A later producer cannot re-target this session or re-open an aborted gate.
    @discardableResult func producerEdge(worker: String, scope: ScopeHandle, kernel: Caller,
        cleanup: Caller, exit: Caller, site: String, publication: String) -> Bool {
        let edge = Edge(worker: worker, scope: scope, kernel: kernel, cleanup: cleanup, exit: exit,
                        site: site, publication: publication)
        let action = state.withLock { s -> (Bool, [CheckedContinuation<Bool, Never>]) in
            if s.gate == "abort" {
                s.lateEdge = edge
                append(&s, Row(event: "producer.ignored", stamp: .capture(), reason: "sealed-abort"))
                return (false, [])
            }
            guard s.target == nil, s.gate == "parked", !s.finishStarted else { return (false, []) }
            // Fix the first candidate even if its diagnostic binding fails.
            // A later producer must never replace an invalid first target.
            s.target = Target(edge: edge)
            guard s.prepared, scope.owner != nil, scope.epoch != nil,
                  kernel.job == cleanup.job, kernel.task == cleanup.task,
                  kernel == exit, cleanup.scopes == [scope.id] else {
                issue(&s, "invalid first producer identity"); return (false, [])
            }
            append(&s, Row(event: "producer.edge", stamp: .capture(), progress: s.progress))
            s.gate = "run"
            append(&s, Row(event: "gate.run", stamp: .capture(), progress: s.progress))
            let waiters = s.gateWaiters.sorted { $0.key < $1.key }.map(\.value); s.gateWaiters.removeAll()
            return (true, waiters)
        }
        for waiter in action.1 { waiter.resume(returning: true) }
        return action.0
    }
    func ownJoinReturned(worker: String, scope: ScopeHandle, producer: Caller,
                         main: Caller, publication: String) {
        state.withLock { s in
            guard let t = s.target, t.edge.worker == worker, t.edge.scope == scope,
                  t.edge.exit.job == producer.job else { return }
            guard t.edge.exit == producer, t.edge.publication == publication, !t.ownJoined else {
                issue(&s, "target original ownjoin identity or uniqueness"); return
            }
            s.target!.ownJoined = true; s.target!.ownJoinCaller = main
            append(&s, Row(event: "ownjoin.return", stamp: .capture(), progress: s.progress))
        }
    }
    func applyPolicy(worker: String, scope: ScopeHandle, main: Caller, registeredMain: Caller?,
                     mainRole: String?, task: Task<RuntimeTaskExit, Error>?, request: MLXReturnRequest) {
        let accepted = state.withLock { s -> Bool in
            guard let t = s.target, t.edge.worker == worker, t.edge.scope == scope else { return false }
            let q = request.receipt.request
            guard !t.policySeen, t.ownJoined, t.ownJoinCaller == main, task != nil,
                  mainRole == "main", registeredMain?.job == main.job, registeredMain?.task == main.task,
                  registeredMain?.scopes.isEmpty == true, main.scopes.last == scope.id,
                  q.session.session == session, q.session.worker == worker,
                  q.job == t.edge.kernel.job, q.task == t.edge.kernel.task,
                  q.invocation == t.edge.kernel.invocation, q.region == t.edge.kernel.region,
                  q.site == t.edge.site, q.scope == scope.id, q.owner == scope.owner,
                  q.sourceEpoch == scope.epoch, request.scope == scope.id, request.returnID == q.returnID else {
                issue(&s, "target main/original request/ownjoin binding"); return false
            }
            s.target!.policySeen = true; s.target!.mainCaller = main
            s.target!.registeredMain = registeredMain; s.target!.request = request
            append(&s, Row(event: "policy.frontier", stamp: .capture(), progress: s.progress))
            append(&s, Row(event: "policy.requested", stamp: .capture(), apiCalled: false,
                requestedPriority: Int(TaskPriority.high.rawValue), reason: policy.rawValue))
            return true
        }
        guard accepted else { return }
        var called = false
        if policy == .raiseMain {
            if #available(macOS 26.0, *) { task!.escalatePriority(to: .high); called = true }
            else { state.withLock { issue(&$0, "priority API unavailable") } }
        }
        state.withLock { s in
            if called { s.raiseCalls += 1 }
            append(&s, Row(event: "policy.returned", stamp: .capture(), apiCalled: called,
                requestedPriority: Int(TaskPriority.high.rawValue), reason: policy.rawValue, progress: s.progress))
        }
    }
    func ownerReleased(worker: String, scope: ScopeHandle) {
        state.withLock { s in
            guard let t = s.target, t.edge.worker == worker, t.edge.scope == scope else { return }
            if !t.policySeen || t.ownerReleased { issue(&s, "target Owner release without unique policy") }
            s.target!.ownerReleased = true
            append(&s, Row(event: "owner.release", stamp: .capture(), progress: s.progress))
        }
    }
    /// Deadline callers use only this synchronous operation. Already-running
    /// bounded arithmetic is never cancelled and no original source root is cleared.
    @discardableResult package func abortIfParked(reason: String) -> Bool {
        let action = state.withLock { s -> (Bool, [CheckedContinuation<Bool, Never>], [CheckedContinuation<Bool, Never>]) in
            guard s.gate == "parked" else { return (false, [], []) }
            s.gate = "abort"
            append(&s, Row(event: "gate.abort", stamp: .capture(), reason: reason, progress: s.progress))
            let waiters = s.gateWaiters.sorted { $0.key < $1.key }.map(\.value); s.gateWaiters.removeAll()
            let ready = s.readyWaiters; s.readyWaiters.removeAll()
            return (true, waiters, ready)
        }
        for waiter in action.1 { waiter.resume(returning: false) }
        for waiter in action.2 { waiter.resume(returning: false) }
        return action.0
    }
    private func awaitDone() async {
        await withCheckedContinuation { continuation in
            let done = state.withLock { s in
                if s.progress.allSatisfy(\.bodyDone) { return true }
                s.doneWaiters.append(continuation); return false
            }
            if done { continuation.resume() }
        }
    }
    /// Called by original host work before publishing its result; its original
    /// timer remains live. A deadline path never calls this async join operation.
    package func finish(reason: String) async {
        abortIfParked(reason: reason)
        let first = state.withLock { s in
            if s.finishStarted { return false }; s.finishStarted = true; return true
        }
        if !first {
            await withCheckedContinuation { continuation in
                let closed = state.withLock { s in
                    if s.closed { return true }; s.closeWaiters.append(continuation); return false
                }
                if closed { continuation.resume() }
            }
            return
        }
        await awaitDone()
        let handles = state.withLock { s in
            append(&s, Row(event: "control.join.begin", stamp: .capture(), progress: s.progress))
            return s.handles.sorted { $0.key < $1.key }
        }
        for (id, handle) in handles {
            await handle.value
            state.withLock { s in
                s.progress[id].joined = true; s.handles.removeValue(forKey: id)
                append(&s, Row(event: "competitor.joined", stamp: .capture(), competitor: id))
            }
        }
        let waiters = state.withLock { s in
            s.closed = true
            append(&s, Row(event: "control.closed", stamp: .capture(), progress: s.progress))
            let result = s.closeWaiters; s.closeWaiters.removeAll(); return result
        }
        for waiter in waiters { waiter.resume() }
    }
    /// Native abort microcontrol alone uses readiness, never production service.
    fileprivate func awaitAllRegisteredForAbortControl() async -> Bool {
        precondition(role == "abort-control" && competitionTasks == 8)
        return await withCheckedContinuation { continuation in
            let ready = state.withLock { s -> Bool? in
                guard s.gate == "parked" else { return false }
                if s.gateWaiters.count == competitionTasks { return true }
                s.readyWaiters.append(continuation); return nil
            }
            if let ready { continuation.resume(returning: ready) }
        }
    }
    package func snapshot(complete: Bool) -> JSONValue {
        let copy = state.withLock { s in
            (s.rows, s.attempted, s.dropped, s.issues, s.progress, s.handles.keys.sorted(),
             s.gate, s.closed, s.target, s.lateEdge, s.raiseCalls, s.prepared,
             s.gateWaiters.keys.sorted(), s.readyWaiters.count, s.doneWaiters.count, s.closeWaiters.count)
        }
        let rows = copy.0.enumerated().map { index, row -> JSONValue in
            guard case .object(var fields) = row.json else { return .null }
            fields["append_seq"] = .integer(index + 1); return .object(fields)
        }
        let allDone = copy.4.allSatisfy(\.bodyDone), allJoined = copy.4.allSatisfy(\.joined) && copy.5.isEmpty
        return .object([
            "schema": .string("rsp1-return-service-v1"), "session": .string(session),
            "process_pid": .integer(Int(ProcessInfo.processInfo.processIdentifier)), "role": .string(role),
            "control_id": .string(session + "/" + role), "policy": .string(policy.rawValue),
            "competition_tasks": .integer(competitionTasks), "clock": .object([
                "domain": .string("swift-dispatch-uptime-ns"), "origin_ns": .string(String(origin))]),
            "parameters": .object(["segments": .integer(32), "iterations_per_segment": .integer(250_000),
                "multiplier": .integer(1_664_525), "increment": .integer(1_013_904_223),
                "expected_run_iterations": .integer(competitionTasks * 32 * 250_000)]),
            "capacity": .integer(capacity), "attempted": .integer(copy.1), "retained": .integer(rows.count),
            "dropped": .integer(copy.2), "issues": .array(copy.3.map(JSONValue.string)),
            "gate_disposition": .string(copy.6), "prepared": .bool(copy.11), "closed": .bool(copy.7),
            "all_body_done": .bool(allDone), "all_handles_joined": .bool(allJoined),
            "retained_handles": .array(copy.5.map(JSONValue.integer)), "raise_calls": .integer(copy.10),
            "retained_waiters": .object(["gate": .array(copy.12.map(JSONValue.integer)),
                "ready": .integer(copy.13), "done": .integer(copy.14), "close": .integer(copy.15)]),
            "target": copy.8?.json ?? .null, "late_producer": copy.9?.json ?? .null,
            "competitors": .array(copy.4.map(\.json)), "events": .array(rows),
            "collection_complete": .bool(complete),
            "measurement_valid": .bool(complete && copy.7 && allDone && allJoined && copy.2 == 0 && copy.3.isEmpty &&
                copy.12.isEmpty && copy.13 == 0 && copy.14 == 0 && copy.15 == 0),
            "validation_scope": .string("local collection/closure only; independent audit binds original source, full request, exact arithmetic, task priority and coverage; no native Task identity or saturation claim")])
    }
}

/// Real native-only tasks, same gate and finish implementation, no MLX/Core job.
package func runRSPAbortControl(_ control: ReturnServiceControl) async -> JSONValue {
    // The host has already retained this exact object before any Tasks exist.
    control.prepare()
    let allRegistered = await control.awaitAllRegisteredForAbortControl()
    let registered = control.snapshot(complete: false)
    let didAbort = control.abortIfParked(reason: "native-abort-control")
    await control.finish(reason: "native-abort-control-finish")
    let closed = control.snapshot(complete: true)
    let caller = Caller(job: "native-abort-job", task: "native-abort-task", invocation: "native-abort-invocation",
                        region: "native-abort-region", scopes: ["native-abort-scope"])
    let late = control.producerEdge(worker: "native-only-no-worker", scope: ScopeHandle(id: "native-abort-scope",
        owner: "native-only-no-owner", epoch: "native-only-no-epoch"), kernel: caller, cleanup: caller,
        exit: caller, site: "native-only-late-hook", publication: "native-only-no-publication")
    let after = control.snapshot(complete: true)
    let competitors: [JSONValue]
    if case .array(let values) = after.brsField("competitors") { competitors = values } else { competitors = [] }
    let noArithmetic = competitors.count == 8 && competitors.allSatisfy {
        $0.brsField("iterations") == .integer(0) && $0.brsField("completed_segments") == .integer(0)
    }
    let passed = allRegistered && didAbort && !late && noArithmetic && after.brsField("all_handles_joined") == .bool(true) &&
        after.brsField("all_body_done") == .bool(true) && after.brsField("target") == .null &&
        after.brsField("gate_disposition") == .string("abort") && after.brsField("issues") == .array([])
    return .object(["schema": .string("rsp1-native-abort-control-v1"), "passed": .bool(passed),
        "actual_all_registered_return": .bool(allRegistered), "actual_abort_return": .bool(didAbort), "actual_late_producer_return": .bool(late),
        "registered_snapshot": registered, "closed_snapshot": closed, "after_late_snapshot": after,
        "source_or_worker_executed": .bool(false),
        "late_input_role": .string("synthetic native-only hook argument; no PL/worker ownership or completion fact")])
}
