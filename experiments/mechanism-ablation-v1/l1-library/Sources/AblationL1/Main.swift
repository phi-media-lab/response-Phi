import Foundation
import Synchronization
import Dispatch
import Darwin

// MA1 L1 scenario program (the per-scenario code a user writes on top of the
// dynamic-ledger library). The library is Owner/Wait/Session/Support, ported
// from SMWO1. This file drives the fixed N cancellation scenario and the ten
// misuse variants M1..M10 from ../MISUSE-CATALOG.md. Every runtime rejection it
// observes comes from the library host or from the worker wire check; nothing
// here is a static/compile-time guarantee (that is level L2).

struct Options: Sendable {
    let python: String, worker: String, inputs: String, session: String, directory: URL
    let scenario: String, stubScenario: String, cpu: Bool
    init(_ arguments: [String]) throws {
        var values: [String: String] = [:]
        var i = 1
        while i < arguments.count {
            try require(i + 1 < arguments.count && arguments[i].hasPrefix("--") && values[arguments[i]] == nil, "CLI key/value/unique")
            values[arguments[i]] = arguments[i + 1]; i += 2
        }
        let required: Set<String> = ["--worker-python", "--worker-script", "--inputs", "--session", "--pgid", "--output-dir", "--scenario"]
        try require(required.isSubset(of: Set(values.keys)), "required CLI options")
        let allowed = required.union(["--stub-scenario"])
        try require(Set(values.keys).isSubset(of: allowed), "unknown CLI option")
        python = values["--worker-python"]!; worker = values["--worker-script"]!; inputs = values["--inputs"]!
        session = values["--session"]!; directory = URL(fileURLWithPath: values["--output-dir"]!)
        scenario = values["--scenario"]!; stubScenario = values["--stub-scenario"] ?? "success"
        cpu = (worker as NSString).lastPathComponent == "stub_worker.py"
        try require([python, worker, inputs, directory.path].allSatisfy { $0.hasPrefix("/") }, "absolute CLI paths")
        try require(values["--pgid"] == "0" && getpgrp() == getpid(), "Swift must lead wrapper-controlled process group")
        try require(!session.isEmpty, "session nonce")
        try require(!FileManager.default.fileExists(atPath: directory.appendingPathComponent("result.json").path), "new result destination")
    }
}

enum UIResult: Sendable {
    case receipt(HostReceipt), cancelled, failed(BridgeFault)
    var json: JSON { switch self {
    case .receipt(let receipt): .object(["kind": .string("value"), "receipt": receipt.raw])
    case .cancelled: .object(["kind": .string("cancelled")])
    case .failed(let fault): .object(["kind": .string("failure"), "fault": fault.json])
    } }
}
func uiTask(_ hub: ResultHub, _ probe: WaitProbe, _ lease: LeaseID, _ trace: Trace) -> Task<UIResult, Never> {
    Task.detached {
        do {
            let receipt = try await hub.waitCancellable(probe)
            trace.record("ui.successor", ["lease": lease.json, "wait_id": .string(probe.id), "outcome": .string("value")])
            return .receipt(receipt)
        } catch is CancellationError {
            trace.record("ui.successor", ["lease": lease.json, "wait_id": .string(probe.id), "outcome": .string("cancelled")])
            return .cancelled
        } catch {
            return .failed((error as? BridgeFault) ?? BridgeFault("ui", String(describing: error)))
        }
    }
}
final class UIRoot: Sendable {
    let task: Task<UIResult, Never>, lease: LeaseID, probe: WaitProbe, trace: Trace
    private let joined = Mutex(false)
    init(_ run: OriginalRun, _ probe: WaitProbe, _ trace: Trace) {
        self.task = uiTask(run.hub, probe, run.lease, trace); self.lease = run.lease; self.probe = probe; self.trace = trace
    }
    func cancel() { task.cancel() }
    func join() async -> UIResult {
        let result = await task.value; joined.withLock { $0 = true }
        trace.record("ui.original_task_join_returned", ["lease": lease.json, "wait_id": .string(probe.id), "result": result.json])
        return result
    }
}
func checkReuse(_ a: CompletedRun, _ b: CompletedRun) throws {
    let al = a.receipt.raw["layers"].array!, bl = b.receipt.raw["layers"].array!
    try require(a.lease.worker == b.lease.worker && b.lease.epoch == a.lease.epoch + 1, "same worker/new lease epoch")
    for i in 0..<28 {
        try require(al[i]["pool_id"] == bl[i]["pool_id"] && al[i]["cache_page_ids"] == bl[i]["cache_page_ids"], "same A logical pool page reused by B")
        try require(al[i]["owned_pages"].array![0]["allocation_epoch"].integer == a.lease.epoch && bl[i]["owned_pages"].array![0]["allocation_epoch"].integer == b.lease.epoch, "distinct exact allocation epochs")
    }
}

final class RunContext: Sendable {
    private struct State { var sessions: [WorkerSession] = []; var owners: [KVLoopOwner] = []; var uis: [UIRoot] = []; var cases: [JSON] = [] }
    private let state = Mutex(State())
    let options: Options, inputs: Inputs, trace: Trace, router: PermitRouter
    init(_ options: Options, _ inputs: Inputs) { self.options = options; self.inputs = inputs; trace = Trace(options.session); router = PermitRouter(trace) }
    func makeSession() -> WorkerSession {
        let s = WorkerSession(inputs: inputs, trace: trace, router: router, directory: options.directory,
                              python: options.python, script: options.worker, cpu: options.cpu, scenario: options.stubScenario)
        state.withLock { $0.sessions.append(s) }; return s
    }
    func makeOwner(_ s: WorkerSession) -> KVLoopOwner { let o = KVLoopOwner(s, trace); state.withLock { $0.owners.append(o) }; return o }
    func makeUI(_ run: OriginalRun, _ probe: WaitProbe) -> UIRoot { let u = UIRoot(run, probe, trace); state.withLock { $0.uis.append(u) }; return u }
    func quarantineAll(_ fault: BridgeFault) { state.withLock { $0.sessions }.forEach { $0.fail(fault) } }
    func activeSessions() -> [WorkerSession] { state.withLock { $0.sessions } }
    func snapshot() -> JSON {
        let v = state.withLock { ($0.sessions, $0.owners, $0.cases) }
        return .object(["sessions": .array(v.0.map { $0.snapshot() }), "owners": .array(v.1.map { $0.snapshot() }), "cases": .array(v.2)])
    }
}

// A misuse observation: what the unsafe step was, who rejected it (if anyone),
// the exact fault, and whether the system recovered or stayed unresolved.
struct MisuseError: Error { let value: JSON }
func rejection(_ code: String, _ layer: String, _ detail: String, final: JSON) -> JSON {
    .object(["rejected": .bool(true), "rejected_by_layer": .string(layer), "fault_code": .string(code), "detail": .string(detail), "final_state": final])
}

func runN(_ ctx: RunContext) async throws -> JSON {
    let trace = ctx.trace, session = ctx.makeSession()
    try await session.start()
    let owner = ctx.makeOwner(session)
    trace.record("a.eval_requested", ["request": .string(ctx.inputs.aSpec.name)])
    let a = try owner.begin(ctx.inputs.aSpec)
    let probe = WaitProbe("N-UI"), ui = ctx.makeUI(a, probe)
    try await probe.attached.wait(seconds: session.deadlines.gate, label: "N UI attached")
    a.start.open()
    _ = try await a.entered.wait()
    trace.record("a.eval_entered", ["request": .string(a.lease.request)])
    trace.record("cancel.requested", ["wait_id": .string(probe.id), "blocked_by": a.lease.json])
    ui.cancel()
    trace.record("b.requested_marker", ["note": .string("B will be submitted after A returns")])
    // While A is still leased, B cannot be admitted.
    do { _ = try owner.begin(try ctx.inputs.bSpec(aToken: 0)); throw BridgeFault("unsafe_admission", "B admitted while A leased") }
    catch let f as BridgeFault where f.code != "unsafe_admission" {
        trace.record("b.pre_return_admission_rejected", ["fault": f.json])
    }
    let uiResult = await ui.join()
    trace.record("wait.returned", ["wait_id": .string(probe.id), "result": uiResult.json])
    guard case .cancelled = uiResult else { throw BridgeFault("N_not_cancelled", uiResult.json.text()) }
    let doneA = try await owner.drain(a)
    trace.record("a.join_returned", ["lease": a.lease.json])
    trace.record("a.release_acked", ["lease": a.lease.json, "token": .n(doneA.receipt.token)])
    let bSpec = try ctx.inputs.bSpec(aToken: doneA.receipt.token)
    trace.record("b.requested", ["request": .string(bSpec.name), "eighth_token": .n(doneA.receipt.token)])
    let b = try owner.begin(bSpec)
    trace.record("b.admitted", ["lease": b.lease.json])
    b.start.open()
    let bReceipt = try await b.hub.waitCancellable(WaitProbe("B-consume"))
    trace.record("b.receipt_validated", ["lease": b.lease.json, "token": .n(bReceipt.token)])
    let doneB = try await owner.drain(b)
    try checkReuse(doneA, doneB); try owner.requireIdle()
    let ack = try await session.shutdown()
    trace.record("session.shutdown_acked", [:])
    try await ctx.router.finish()
    return .object(["scenario": .string("N"), "ui": uiResult.json, "a": doneA.json, "b": doneB.json, "shutdown_ack": ack, "root_state": ctx.snapshot()])
}

// Drive A to a validated, still-leased receipt, with an attached waiter.
func leasedAWithWaiter(_ ctx: RunContext, _ session: WorkerSession, _ owner: KVLoopOwner, cancelWaiter: Bool) async throws -> (OriginalRun, UIRoot) {
    let a = try owner.begin(ctx.inputs.aSpec)
    let probe = WaitProbe("misuse-UI"), ui = ctx.makeUI(a, probe)
    try await probe.attached.wait(seconds: session.deadlines.gate, label: "misuse UI attached")
    a.start.open(); _ = try await a.entered.wait()
    if cancelWaiter { ui.cancel(); _ = await ui.join() }
    return (a, ui)
}
func finishABShutdown(_ ctx: RunContext, _ session: WorkerSession, _ owner: KVLoopOwner, _ a: OriginalRun) async throws -> JSON {
    let doneA = try await owner.drain(a)
    let b = try owner.begin(try ctx.inputs.bSpec(aToken: doneA.receipt.token)); b.start.open()
    let doneB = try await owner.drain(b)
    try checkReuse(doneA, doneB); try owner.requireIdle()
    let ack = try await session.shutdown(); try await ctx.router.finish()
    return .object(["a": doneA.json, "b": doneB.json, "shutdown_ack": ack])
}

func runMisuse(_ ctx: RunContext, _ id: String) async throws -> JSON {
    let trace = ctx.trace, session = ctx.makeSession()
    try await session.start()
    let owner = ctx.makeOwner(session)
    switch id {
    case "M1":
        // release before join: consume A's receipt via hub, then release directly.
        let a = try owner.begin(ctx.inputs.aSpec); a.start.open()
        let receipt = try await a.hub.waitCancellable(WaitProbe("M1-consume"))
        let ack = try await session.release(receipt)          // worker ACKs: host never checked join
        trace.record("misuse.release_before_join_succeeded", ["ack": ack])
        var second = JSON.null
        do { _ = try await owner.drain(a); throw BridgeFault("unsafe", "double release admitted") }
        catch let f as BridgeFault where f.code != "unsafe" { second = f.json }
        let obs = JSON.object(["gap": .string("release-before-join not prevented by L1 host; worker ACKed"),
                               "second_release": second, "rejected_by_layer": .string("host-session-phase (second release)")])
        throw MisuseError(value: rejection("none-then-host", "silent-then-host", "first release silent; double caught by host phase", final: .object(["misuse": obs, "roots": ctx.snapshot()])))
    case "M2":
        let (a, _) = try await leasedAWithWaiter(ctx, session, owner, cancelWaiter: false)
        do { _ = try owner.begin(try ctx.inputs.bSpec(aToken: 0)); throw BridgeFault("unsafe", "B before drain admitted") }
        catch let f as BridgeFault where f.code != "unsafe" {
            trace.record("misuse.b_before_drain_rejected", ["fault": f.json])
            let recovered = try await finishABShutdown(ctx, session, owner, a)
            return .object(["scenario": .string("M2"), "rejected": rejection(f.code, "host-owner/session", f.detail, final: ctx.snapshot()), "recovered": recovered])
        }
    case "M3":
        let a = try owner.begin(ctx.inputs.aSpec); a.start.open()
        let doneA = try await owner.drain(a)
        do { _ = try await owner.drain(a); throw BridgeFault("unsafe", "double drain admitted") }
        catch let f as BridgeFault where f.code != "unsafe" {
            trace.record("misuse.double_drain_rejected", ["fault": f.json])
            let b = try owner.begin(try ctx.inputs.bSpec(aToken: doneA.receipt.token)); b.start.open()
            let doneB = try await owner.drain(b); try checkReuse(doneA, doneB); try owner.requireIdle()
            let ack = try await session.shutdown(); try await ctx.router.finish()
            return .object(["scenario": .string("M3"), "rejected": rejection(f.code, "host-owner", f.detail, final: ctx.snapshot()), "recovered": .object(["a": doneA.json, "b": doneB.json, "shutdown_ack": ack])])
        }
    case "M4":
        let (a, _) = try await leasedAWithWaiter(ctx, session, owner, cancelWaiter: true)
        do { _ = try owner.begin(try ctx.inputs.bSpec(aToken: 0)); throw BridgeFault("unsafe", "B after cancel before drain admitted") }
        catch let f as BridgeFault where f.code != "unsafe" {
            trace.record("misuse.cancel_as_completion_rejected", ["fault": f.json])
            let recovered = try await finishABShutdown(ctx, session, owner, a)
            return .object(["scenario": .string("M4"), "rejected": rejection(f.code, "host-owner/session", f.detail, final: ctx.snapshot()), "recovered": recovered])
        }
    case "M5":
        let (_, _) = try await leasedAWithWaiter(ctx, session, owner, cancelWaiter: true)
        do { _ = try await session.shutdown(); throw BridgeFault("unsafe", "shutdown with A leased admitted") }
        catch let f as BridgeFault where f.code != "unsafe" {
            throw MisuseError(value: rejection(f.code, "host-session-phase", "shutdown refused while A leased/unreturned", final: ctx.snapshot()))
        }
    case "M6", "M7":
        // Stub injects a bad/absent ACK; A up to RELEASE then the host rejects.
        let a = try owner.begin(ctx.inputs.aSpec); a.start.open()
        do { _ = try await owner.drain(a); throw BridgeFault("unsafe", "bad/absent ACK admitted") }
        catch let f as BridgeFault where f.code != "unsafe" {
            throw MisuseError(value: rejection(f.code, "host", "ACK identity/worker-exit rejected; KV stays unresolved, B not admitted", final: ctx.snapshot()))
        }
    case "M8":
        let a = try owner.begin(ctx.inputs.aSpec); a.start.open()
        let doneA = try await owner.drain(a)
        do {
            _ = try await session.eval(doneA.lease, spec: ctx.inputs.aSpec, entered: Promise<JSON>())
            throw BridgeFault("unsafe", "stale lease re-eval admitted")
        } catch let f as BridgeFault where f.code != "unsafe" {
            throw MisuseError(value: rejection(f.code, "worker-wire", "stale lease/epoch rejected by worker monotonic check, not by L1 host", final: ctx.snapshot()))
        }
    case "M9":
        let owner2 = ctx.makeOwner(session)
        let a = try owner.begin(ctx.inputs.aSpec); a.start.open()
        do { _ = try owner2.begin(try ctx.inputs.bSpec(aToken: 0)); throw BridgeFault("unsafe", "second owner on one session admitted") }
        catch let f as BridgeFault where f.code != "unsafe" {
            trace.record("misuse.second_owner_rejected", ["fault": f.json])
            let doneA = try await owner.drain(a)
            let b = try owner.begin(try ctx.inputs.bSpec(aToken: doneA.receipt.token)); b.start.open()
            let doneB = try await owner.drain(b); try checkReuse(doneA, doneB); try owner.requireIdle()
            let ack = try await session.shutdown(); try await ctx.router.finish()
            return .object(["scenario": .string("M9"), "rejected": rejection(f.code, "host-session-single-lease", f.detail, final: ctx.snapshot()),
                            "note": .string("session serializes to one lease; owner2 rejected without poisoning, owner1 recovered (differs from catalog sticky prediction)"),
                            "recovered": .object(["a": doneA.json, "b": doneB.json, "shutdown_ack": ack])])
        }
    default: throw BridgeFault("scenario", "unknown misuse \(id)")
    }
    throw BridgeFault("scenario", "misuse \(id) did not reach a rejection")
}

@main struct AblationL1 {
    static func main() async {
        _ = signal(SIGPIPE, SIG_IGN)
        do {
            let options = try Options(CommandLine.arguments), inputs = try Inputs(options.inputs)
            let ctx = RunContext(options, inputs)
            try FileManager.default.createDirectory(at: options.directory, withIntermediateDirectories: true)
            ctx.router.start()
            ctx.trace.record("session.started", ["scenario": .string(options.scenario), "stub_scenario": .string(options.stubScenario),
                "swift_pid": .n(Int(getpid())), "pgid": .n(Int(getpgrp())), "cpu": .bool(options.cpu), "inputs_sha256": .string(inputs.sha256)])
            var document: [String: JSON]; let status: Int32
            do {
                let value: JSON
                if options.scenario == "N" { value = try await runN(ctx) }
                else if options.scenario.hasPrefix("misuse:") { value = try await runMisuse(ctx, String(options.scenario.dropFirst(7))) }
                else { throw BridgeFault("scenario", "unknown scenario \(options.scenario)") }
                document = ["status": .string("completed"), "result": value]; status = 0
            } catch let m as MisuseError {
                // Expected sticky misuse: rejection recorded, roots retained,
                // bounded OS cleanup of the stub worker, nonzero exit.
                ctx.quarantineAll(BridgeFault("misuse_sticky", options.scenario))
                document = ["status": .string("misuse-rejected-sticky"), "misuse": m.value]; status = 1
            } catch {
                document = ["status": .string("unresolved"), "fault": ((error as? BridgeFault) ?? BridgeFault("main", String(describing: error))).json]; status = 1
            }
            document["passed"] = .bool(status == 0)
            document["schema"] = .string("ma1-l1-result-v1"); document["session"] = .string(options.session)
            document["scenario"] = .string(options.scenario); document["level"] = .string("L1")
            document["inputs_sha256"] = .string(inputs.sha256); document["roots"] = ctx.snapshot()
            // Bounded cleanup for any still-live stub worker on a sticky exit.
            if status != 0 {
                for s in ctx.activeSessions() { await s.closeFailedCPUFixture() }
                document["cleanup"] = ctx.snapshot()
            }
            ctx.trace.record("session.result", ["status": document["status"]!, "passed": .bool(status == 0)])
            try ctx.trace.save(options.directory.appendingPathComponent("result.json"), extra: document)
            await ctx.trace.flush()
            exit(status)
        } catch {
            FileHandle.standardError.write(Data(("startup: " + String(describing: error) + "\n").utf8)); exit(2)
        }
    }
}

