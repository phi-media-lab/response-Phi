import Foundation
import Dispatch
import Synchronization
import Darwin

// These controls exercise the native mailbox protocol only. All source Caller,
// pool, publication and receipt identities below are explicitly synthetic. No
// checked source, Ledger, real worker, GPU or model is executed by this file.
fileprivate final class EBDNativeGate: Sendable {
    private struct State { var open = false; var waiter: CheckedContinuation<Void, Never>? }
    private let state = Mutex(State())
    func wait() async {
        await withCheckedContinuation { continuation in
            let ready = state.withLock { s -> Bool in
                if s.open { return true }
                precondition(s.waiter == nil); s.waiter = continuation; return false
            }
            if ready { continuation.resume() }
        }
    }
    func open() {
        let waiter = state.withLock { s -> CheckedContinuation<Void, Never>? in
            s.open = true; let found = s.waiter; s.waiter = nil; return found
        }
        waiter?.resume()
    }
}

/// A native-test Task root. Polling reads body_done; it never awaits .value as
/// an observation probe. The actual handle is joined only after that marker.
fileprivate final class EBDNativeTask<Value: Sendable>: Sendable {
    private struct State {
        var task: Task<Value, Error>?, prepared = false, bodyDone = false, joined = false
        var output: JSONValue = .null, failure: JSONValue = .null
    }
    private let state = Mutex(State())
    private let body: @Sendable () async throws -> Value
    private let render: @Sendable (Value) -> JSONValue
    init(body: @escaping @Sendable () async throws -> Value, render: @escaping @Sendable (Value) -> JSONValue) {
        self.body = body; self.render = render
    }
    func prepare() {
        state.withLock { s in
            precondition(!s.prepared); s.prepared = true
            s.task = Task<Value, Error>.detached(priority: .medium) {
                do {
                    let value = try await self.body()
                    self.state.withLock { s in s.output = self.render(value); s.bodyDone = true }
                    return value
                } catch {
                    self.state.withLock { s in s.failure = ebdNativeError(error); s.bodyDone = true }
                    throw error
                }
            }
        }
    }
    var handle: Task<Value, Error>? { state.withLock { $0.task } }
    func snapshot() -> JSONValue {
        state.withLock { s in .object(["prepared": .bool(s.prepared), "body_done": .bool(s.bodyDone),
            "actual_joined": .bool(s.joined), "retained_handle": .bool(s.task != nil),
            "output": s.output, "failure": s.failure]) }
    }
    func joinAfterBodyDone() async -> JSONValue {
        let ready = state.withLock { s in (s.bodyDone, s.task) }
        guard ready.0, let task = ready.1 else {
            return .object(["joined": .bool(false), "reason": .string("body-done-not-observed-or-no-handle")])
        }
        let result: JSONValue
        do { _ = try await task.value; result = .object(["joined": .bool(true), "task_threw": .bool(false)]) }
        catch { result = .object(["joined": .bool(true), "task_threw": .bool(true), "actual_error": ebdNativeError(error)]) }
        state.withLock { s in s.joined = true; s.task = nil }
        return result
    }
}

private func ebdNativeError(_ error: any Error) -> JSONValue {
    .object(["type": .string(String(reflecting: type(of: error))), "description": .string(String(describing: error))])
}

fileprivate struct EBDNativeFixture: Sendable {
    static let session = "11111111-1111-4111-8111-111111111111"
    static let tokens: [UInt64] = [1, 4, 9, 16, 25, 36, 49, 72]
    let worker = "native-only-worker", owner = OwnerHandle(id: "native-only-owner")
    let a = ScopeHandle(id: "native-A-scope", owner: "native-only-owner", epoch: "native-A-epoch")
    let main = Caller(job: "native-main-job", task: "native-main-task", invocation: "native-A-invocation",
                      region: "native-A-region", scopes: ["native-init", "native-A-scope"])
    let registered = Caller(job: "native-main-job", task: "native-main-task", invocation: "native-host-invocation",
                            region: "native-host-region", scopes: [])
    let receiver = Caller(job: "native-main-job", task: "native-main-task", invocation: "native-main-invocation",
                         region: "native-main-region", scopes: ["native-init"])
    let producer = Caller(job: "native-A-job", task: "native-A-task", invocation: "native-A-kernel-invocation",
                         region: "native-A-kernel-region", scopes: [])
    let cleanup = Caller(job: "native-A-job", task: "native-A-task", invocation: "native-A-cleanup-invocation",
                        region: "native-A-cleanup-region", scopes: ["native-A-scope"])
    let site = "/native-only/A-produce", receiveSite = "/native-only/receive-B"
    let publication = "native-A-publication", beta = "native-A-beta"
    let request: ExternalDemandRequest, aReturn: MLXReturnRequest, bRequest: MLXEvalRequest, bReceipt: MLXHostReceipt
    static var requestJSON: JSONValue { .object(["schema": .string("ebd1-request-v1"),
        "session": .string(session), "request_id": .string("B"), "port": .string("B"),
        "operation": .string("Prefill8"), "tokens": .array(tokens.map(JSONValue.unsigned))]) }
    init() throws {
        request = try ExternalDemandRequest(data: Self.requestJSON.encoded(), path: "native-memory:request",
                                          expectedSession: Self.session)
        let pools = (0..<28).map { MLXPoolIdentity(layer: $0, poolID: "native-pool-\($0)", objectID: "native-object-\($0)") }
        let identity = try MLXSessionIdentity(session: Self.session, worker: worker, pid: Int(getpid()), pgid: Int(getpgrp()),
            poolGroup: "native-only-pool-group", pools: pools,
            provenance: .object(["native_only": .bool(true), "real_worker_spawned": .bool(false),
                "receipt_validated_by_backend": .bool(false)]))
        let aq = try MLXEvalRequest(commandID: "native-A-command", operation: "Prefill32", tokens: Array(1...32).map(UInt64.init),
            owner: owner.id, sourceEpoch: a.epoch!, ownerOrdinal: 1, scope: a.id, returnID: beta,
            caller: producer, access: "native-A-write", site: site, session: identity)
        let ar = MLXHostReceipt(request: aq, raw: .object(["native_only": .bool(true), "token_id": .unsigned(72)]))
        aReturn = MLXReturnRequest(receipt: ar, returnID: beta, scope: a.id)
        let bp = Caller(job: "native-B-job", task: "native-B-task", invocation: "native-B-invocation",
                        region: "native-B-region", scopes: [])
        bRequest = try MLXEvalRequest(commandID: "native-B-command", operation: "Prefill8", tokens: Self.tokens,
            owner: owner.id, sourceEpoch: "native-B-epoch", ownerOrdinal: 2, scope: "native-B-scope", returnID: "native-B-beta",
            caller: bp, access: "native-B-write", site: "/native-only/B-produce", session: identity)
        bReceipt = MLXHostReceipt(request: bRequest, raw: .object(["native_only": .bool(true), "token_id": .unsigned(271)]))
    }
}

fileprivate final class EBDNativeCaseRoot: Sendable {
    let name: String, fixture: EBDNativeFixture, control: ExternalDemandControl
    let mainGate: EBDNativeGate
    let mainTask: EBDNativeTask<RuntimeTaskExit>
    let receiverTask: EBDNativeTask<[UInt64]>
    init(name: String, fixture: EBDNativeFixture) {
        self.name = name; self.fixture = fixture
        let control = ExternalDemandControl(request: fixture.request, policy: .raiseMain)
        self.control = control
        let gate = EBDNativeGate(); mainGate = gate
        mainTask = EBDNativeTask(body: {
            await gate.wait()
            return .source(.ok(.unit))
        }, render: { $0.diagnostic })
        receiverTask = EBDNativeTask(body: {
            try await control.receive(port: "B", owner: fixture.owner, caller: fixture.receiver, site: fixture.receiveSite)
        }, render: { .array($0.map(JSONValue.unsigned)) })
    }
    func snapshot() -> JSONValue { .object(["case": .string(name), "native_only": .bool(true),
        "source_chain_executed": .bool(false), "control": control.snapshot(complete: false),
        "synthetic_main_task": mainTask.snapshot(), "native_receiver_task": receiverTask.snapshot()]) }
}

/// Caller retains this before invoking the function and through final output.
/// A polling timeout leaves actual handles and cells here; it is never a join.
package final class ExternalDemandNativeRoots: Sendable {
    private let roots = Mutex<[EBDNativeCaseRoot]>([])
    package init() {}
    fileprivate func install(_ root: EBDNativeCaseRoot) { roots.withLock { $0.append(root) } }
    package func snapshot() -> JSONValue {
        let copy = roots.withLock { $0 }
        return .object(["schema": .string("ebd1-native-roots-v1"), "native_only": .bool(true),
                        "cases": .array(copy.map { $0.snapshot() })])
    }
}

private struct EBDNativePoll {
    let evidence: JSONValue, hit: Bool
}
/// Fixed native-only coverage wait, never a measured service workload. Every
/// hit/timeout preserves its actual last snapshot and polling clock endpoints.
private func ebdNativePoll(_ label: String, snapshot: @Sendable () -> JSONValue,
                          predicate: @Sendable (JSONValue) -> Bool) async -> EBDNativePoll {
    let start = DispatchTime.now().uptimeNanoseconds
    var attempts = 0, last: JSONValue = .null, hit = false
    while true {
        last = snapshot(); attempts += 1
        if predicate(last) { hit = true; break }
        if DispatchTime.now().uptimeNanoseconds - start >= 1_000_000_000 { break }
        do { try await Task.sleep(for: .milliseconds(1)) } catch { break }
    }
    let end = DispatchTime.now().uptimeNanoseconds
    return EBDNativePoll(evidence: .object(["label": .string(label), "hit": .bool(hit),
        "attempts": .integer(attempts), "budget_ns": .string("1000000000"), "sleep_ns": .string("1000000"),
        "start_uptime_ns": .string(String(start)), "end_uptime_ns": .string(String(end)),
        "performance_measurement": .bool(false), "last_snapshot": last]), hit: hit)
}

private func ebdNativeSchemaControls() -> JSONValue {
    var rows: [JSONValue] = []
    do {
        let original = EBDNativeFixture.requestJSON
        let valid = try ExternalDemandRequest(data: original.encoded(), path: "native-memory:valid", expectedSession: EBDNativeFixture.session)
        rows.append(.object(["name": .string("valid-complete-B8"), "input": original,
            "accepted": .bool(true), "passed": .bool(valid.tokens == EBDNativeFixture.tokens && valid.raw == original)]))
        if rows.last?.brsField("passed") != .bool(true) { return .object(["rows": .array(rows), "passed": .bool(false)]) }
        let replacements: [(String, String, JSONValue)] = [
            ("wrong-schema", "schema", .string("other")),
            ("unknown-field", "extra", .bool(true)),
            ("wrong-session", "session", .string("22222222-2222-4222-8222-222222222222")),
            ("wrong-id", "request_id", .string("A")), ("wrong-port", "port", .string("C")),
            ("wrong-operation", "operation", .string("Prefill32")),
            ("short-payload", "tokens", .array(Array(EBDNativeFixture.tokens.dropLast()).map(JSONValue.unsigned))),
            ("long-payload", "tokens", .array((EBDNativeFixture.tokens + [1]).map(JSONValue.unsigned))),
            ("range", "tokens", .array(([151_936] + Array(EBDNativeFixture.tokens.dropFirst())).map(JSONValue.unsigned))),
            ("bool-token", "tokens", .array([.bool(true)] + EBDNativeFixture.tokens.dropFirst().map(JSONValue.unsigned)))]
        for (name, key, value) in replacements {
            var fields = try original.members(); fields[key] = value
            let mutated = JSONValue.object(fields), bytes = try mutated.encoded()
            do {
                _ = try ExternalDemandRequest(data: bytes, path: "native-memory:" + name, expectedSession: EBDNativeFixture.session)
                rows.append(.object(["name": .string(name), "input": mutated, "accepted": .bool(true), "passed": .bool(false)]))
                break
            } catch {
                let expectedType = (name == "bool-token" || name == "unknown-field") ? error is ExactJSONError : error is RuntimeInvariant
                rows.append(.object(["name": .string(name), "input": mutated, "accepted": .bool(false),
                    "actual_error": ebdNativeError(error), "passed": .bool(expectedType)]))
                if !expectedType { break }
            }
        }
        if rows.contains(where: { $0.brsField("passed") != .bool(true) }) {
            return .object(["rows": .array(rows), "passed": .bool(false)])
        }
        let duplicate = Data(("{\"schema\":\"ebd1-request-v1\",\"schema\":\"ebd1-request-v1\",\"session\":\"" +
            EBDNativeFixture.session + "\",\"request_id\":\"B\",\"port\":\"B\",\"operation\":\"Prefill8\",\"tokens\":[1,4,9,16,25,36,49,72]}").utf8)
        do {
            _ = try ExternalDemandRequest(data: duplicate, path: "native-memory:duplicate", expectedSession: EBDNativeFixture.session)
            rows.append(.object(["name": .string("duplicate-json-key"), "input_utf8": .string(String(decoding: duplicate, as: UTF8.self)),
                                 "accepted": .bool(true), "passed": .bool(false)]))
        } catch {
            rows.append(.object(["name": .string("duplicate-json-key"), "input_utf8": .string(String(decoding: duplicate, as: UTF8.self)),
                "accepted": .bool(false), "actual_error": ebdNativeError(error), "passed": .bool(error is ExactJSONError)]))
        }
    } catch { rows.append(.object(["name": .string("schema-infrastructure"), "passed": .bool(false), "actual_error": ebdNativeError(error)])) }
    return .object(["rows": .array(rows), "passed": .bool(rows.count == 12 && rows.allSatisfy { $0.brsField("passed") == .bool(true) })])
}

private func ebdNativeRunCase(_ name: String, roots: ExternalDemandNativeRoots) async -> JSONValue {
    var observations: [String: JSONValue] = [:], checks: [String: JSONValue] = [:], polls: [JSONValue] = [], negatives: [JSONValue] = []
    var failure: JSONValue = .null
    let root: EBDNativeCaseRoot
    do { root = try EBDNativeCaseRoot(name: name, fixture: EBDNativeFixture()) }
    catch { return .object(["case": .string(name), "passed": .bool(false), "actual_error": ebdNativeError(error)]) }
    roots.install(root) // Before any real Task is created.
    let f = root.fixture, c = root.control
    func negative(_ label: String, _ body: () throws -> Void) throws {
        var actual: (any Error)?
        do { try body() } catch { actual = error }
        let rejected = actual.map { $0 is RuntimeInvariant } ?? false
        negatives.append(.object(["name": .string(label), "rejected": .bool(rejected),
            "actual_error": actual.map(ebdNativeError) ?? .null]))
        try runtimeRequire(rejected, "native negative did not reject with RuntimeInvariant: " + label)
    }
    do {
        try c.bindA(worker: f.worker, scope: f.a, main: f.main, producerSite: f.site, backendReturn: f.beta)
        if name == "abort-before-trigger" {
            c.prepare()
            let poll = await ebdNativePoll("actual-trigger-wait", snapshot: { c.snapshot(complete: false) },
                predicate: { $0.brsField("retained_waiters").brsField("trigger") == .bool(true) })
            polls.append(poll.evidence); try runtimeRequire(poll.hit, "native trigger registration timeout")
            observations["before_abort"] = c.snapshot(complete: false)
            c.abort(reason: "native-before-trigger")
            let opened = try c.producerEdge(worker: f.worker, scope: f.a, kernel: f.producer, cleanup: f.cleanup,
                                           exit: f.producer, site: f.site, publication: f.publication)
            checks["late_producer_ignored"] = .bool(!opened)
            observations["after_abort_and_late_edge"] = c.snapshot(complete: false)
        } else {
            root.mainTask.prepare()
            let late = name == "late-registered" || name == "no-prepare-payload-abort"
            if !late { c.prepare() }
            _ = try c.producerEdge(worker: f.worker, scope: f.a, kernel: f.producer, cleanup: f.cleanup,
                                  exit: f.producer, site: f.site, publication: f.publication)
            if !late {
                let poll = await ebdNativePoll("actual-enqueue", snapshot: { c.snapshot(complete: false) },
                    predicate: { $0.brsField("queued") == .bool(true) })
                polls.append(poll.evidence); try runtimeRequire(poll.hit, "native actual enqueue timeout")
            }
            if name == "guard-rejections" {
                try negative("wrong-ownjoin-publication") {
                    try c.ownJoinReturned(worker: f.worker, scope: f.a, main: f.main, producer: f.producer, publication: "foreign")
                }
            }
            try c.ownJoinReturned(worker: f.worker, scope: f.a, main: f.main, producer: f.producer, publication: f.publication)
            try c.applyFrontier(worker: f.worker, scope: f.a, main: f.main, registeredMain: f.registered,
                                mainRole: "main", task: root.mainTask.handle, request: f.aReturn)
            if name == "guard-rejections" {
                try negative("duplicate-frontier") {
                    try c.applyFrontier(worker: f.worker, scope: f.a, main: f.main, registeredMain: f.registered,
                                        mainRole: "main", task: root.mainTask.handle, request: f.aReturn)
                }
            }
            try c.ownerReleased(worker: f.worker, scope: f.a)
            observations["after_frontier_and_release"] = c.snapshot(complete: false)
            checks["one_frontier_policy"] = .bool(c.snapshot(complete: false).brsField("raise_calls") == .integer(late ? 0 : 1))
            if name == "guard-rejections" {
                do {
                    _ = try await c.receive(port: "B", owner: OwnerHandle(id: "foreign"), caller: f.receiver, site: f.receiveSite)
                    negatives.append(.object(["name": .string("wrong-owner"), "rejected": .bool(false)]))
                } catch { negatives.append(.object(["name": .string("wrong-owner"), "rejected": .bool(error is RuntimeInvariant), "actual_error": ebdNativeError(error)])) }
                try runtimeRequire(negatives.last?.brsField("rejected") == .bool(true), "native wrong-owner was not rejected")
            }
            root.receiverTask.prepare()
            if late {
                let poll = await ebdNativePoll("actual-payload-wait", snapshot: { c.snapshot(complete: false) },
                    predicate: { $0.brsField("retained_waiters").brsField("payload") == .bool(true) })
                polls.append(poll.evidence); try runtimeRequire(poll.hit, "native payload registration timeout")
                observations["before_enqueue_or_abort"] = c.snapshot(complete: false)
                if name == "no-prepare-payload-abort" { c.abort(reason: "native-no-prepare") }
                else { c.prepare() }
            }
            let received = await ebdNativePoll("native-receiver-body-done", snapshot: { root.receiverTask.snapshot() },
                predicate: { $0.brsField("body_done") == .bool(true) })
            polls.append(received.evidence); try runtimeRequire(received.hit, "native receiver body_done timeout")
            if name == "no-prepare-payload-abort" {
                checks["no_requester_or_enqueue"] = .bool(c.snapshot(complete: false).brsField("prepared") == .bool(false) &&
                    c.snapshot(complete: false).brsField("queued") == .bool(false))
                checks["payload_aborted"] = .bool(root.receiverTask.snapshot().brsField("failure") != .null)
            } else {
                checks["all_eight_payload_values"] = .bool(root.receiverTask.snapshot().brsField("output") == .array(f.request.tokens.map(JSONValue.unsigned)))
                if name == "guard-rejections" {
                    do {
                        _ = try await c.receive(port: "B", owner: f.owner, caller: f.receiver, site: f.receiveSite)
                        negatives.append(.object(["name": .string("duplicate-receive"), "rejected": .bool(false)]))
                    } catch { negatives.append(.object(["name": .string("duplicate-receive"), "rejected": .bool(error is RuntimeInvariant), "actual_error": ebdNativeError(error)])) }
                    try runtimeRequire(negatives.last?.brsField("rejected") == .bool(true), "native duplicate receive was not rejected")
                }
                try c.observeBRequest(f.bRequest); try c.observeBReceipt(f.bReceipt)
                if name == "guard-rejections" {
                    let foreign = Caller(job: "foreign-job", task: f.registered.task, invocation: f.registered.invocation,
                                         region: f.registered.region, scopes: [])
                    try negative("wrong-reply-caller") { try c.reply(token: 271, caller: foreign, publication: "native-main-publication") }
                    try negative("wrong-reply-token") { try c.reply(token: 272, caller: f.registered, publication: "native-main-publication") }
                }
                try c.reply(token: 271, caller: f.registered, publication: "native-main-publication")
                if name == "guard-rejections" {
                    try negative("duplicate-reply") { try c.reply(token: 271, caller: f.registered, publication: "native-main-publication") }
                }
                observations["after_reply"] = c.snapshot(complete: false)
                checks["late_enqueue_never_rechecks_policy"] = .bool(c.snapshot(complete: false).brsField("raise_calls") == .integer(late ? 0 : 1))
            }
        }
    } catch { failure = ebdNativeError(error); observations["first_failure_prefix"] = root.snapshot(); c.abort(reason: "native-case-failed") }

    // Every cleanup decision is conditional on actual body_done. A missed
    // bound retains the original objects in roots and makes this case fail.
    if c.snapshot(complete: false).brsField("prepared") == .bool(true) {
        let done = await ebdNativePoll("requester-body-done-before-join", snapshot: { c.snapshot(complete: false) },
            predicate: { $0.brsField("body_done") == .bool(true) })
        polls.append(done.evidence)
        if done.hit { await c.finish() } else {
            observations["requester_join_not_attempted"] = c.snapshot(complete: false)
            if failure == .null { failure = .string("requester body_done timeout; original handle retained") }
            c.abort(reason: "native-requester-body-timeout")
        }
    } else { await c.finish() }
    if root.receiverTask.snapshot().brsField("prepared") == .bool(true) {
        let done = await ebdNativePoll("receiver-body-done-before-join", snapshot: { root.receiverTask.snapshot() },
            predicate: { $0.brsField("body_done") == .bool(true) })
        polls.append(done.evidence)
        if done.hit { observations["receiver_actual_join"] = await root.receiverTask.joinAfterBodyDone() }
        else if failure == .null { failure = .string("native receiver body_done timeout; handle retained") }
    }
    if root.mainTask.snapshot().brsField("prepared") == .bool(true) {
        root.mainGate.open()
        let done = await ebdNativePoll("synthetic-main-body-done-before-join", snapshot: { root.mainTask.snapshot() },
            predicate: { $0.brsField("body_done") == .bool(true) })
        polls.append(done.evidence)
        if done.hit { observations["synthetic_main_actual_join"] = await root.mainTask.joinAfterBodyDone() }
        else if failure == .null { failure = .string("native main body_done timeout; handle retained") }
    }
    let final = c.snapshot(complete: failure == .null)
    checks["requester_join_if_created"] = .bool(final.brsField("prepared") == .bool(false) ||
        (final.brsField("body_done") == .bool(true) && final.brsField("actual_joined") == .bool(true) && final.brsField("retained_task") == .bool(false)))
    checks["no_continuation_waiters"] = .bool(final.brsField("retained_waiters") == .object([
        "trigger": .bool(false), "payload": .bool(false), "result": .bool(false), "done": .integer(0), "close": .integer(0)]))
    checks["native_roots_join_if_created"] = .bool(
        (root.receiverTask.snapshot().brsField("prepared") == .bool(false) || root.receiverTask.snapshot().brsField("actual_joined") == .bool(true)) &&
        (root.mainTask.snapshot().brsField("prepared") == .bool(false) || root.mainTask.snapshot().brsField("actual_joined") == .bool(true)))
    if name == "early-cached" || name == "late-registered" || name == "guard-rejections" {
        checks["reply_received_exactly"] = .bool(final.brsField("delivered") == .object(["status": .string("success"), "token": .unsigned(271)]))
    }
    if name == "abort-before-trigger" {
        checks["abort_zero_enqueue_consume"] = .bool(final.brsField("queued") == .bool(false) && final.brsField("consumed") == .bool(false))
        checks["abort_delivered"] = .bool(final.brsField("delivered").brsField("status") == .string("failure"))
    }
    if name == "guard-rejections" {
        checks["seven_actual_guards_rejected"] = .bool(negatives.count == 7 && negatives.allSatisfy { $0.brsField("rejected") == .bool(true) })
        checks["only_expected_control_issues"] = .bool((try? final.brsField("issues").elements().count) == 7)
    }
    else { checks["no_unexpected_control_issues"] = .bool(final.brsField("issues") == .array([])) }
    let passed = failure == .null && checks.values.allSatisfy { $0 == .bool(true) }
    return .object(["case": .string(name), "native_only": .bool(true), "passed": .bool(passed),
        "source_chain_executed": .bool(false), "worker_receipts_synthetic": .bool(true),
        "performance_measurement": .bool(false), "checks": .object(checks), "first_failure": failure,
        "polls": .array(polls), "negative_attempts": .array(negatives), "observations": .object(observations),
        "final_control": final, "actual_native_roots": root.snapshot()])
}

package func runExternalDemandNativeControls(_ roots: ExternalDemandNativeRoots) async -> JSONValue {
    let schema = ebdNativeSchemaControls()
    var cases: [JSONValue] = []
    if schema.brsField("passed") == .bool(true) {
        for name in ["early-cached", "late-registered", "guard-rejections", "abort-before-trigger", "no-prepare-payload-abort"] {
            let row = await ebdNativeRunCase(name, roots: roots); cases.append(row)
            if row.brsField("passed") != .bool(true) { break } // First failure, no replacement.
        }
    }
    return .object(["schema": .string("ebd1-native-controls-v1"), "native_only": .bool(true),
        "source_chain_executed": .bool(false), "real_worker_spawned": .bool(false), "performance_measurement": .bool(false),
        "passed": .bool(schema.brsField("passed") == .bool(true) && cases.count == 5 && cases.allSatisfy { $0.brsField("passed") == .bool(true) }),
        "schema_controls": schema, "cases": .array(cases), "retained_roots": roots.snapshot()])
}
