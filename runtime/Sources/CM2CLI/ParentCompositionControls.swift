import Foundation
import Dispatch
import Synchronization
import CryptoKit
import RuntimeCore

private let parentCPUTokens: [UInt64] = [2127, 19010, 19287, 13098, 1724, 7344, 1626, 72]
private func pcField(_ value: JSONValue, _ key: String) -> JSONValue {
    guard case .object(let fields) = value else { return .null }; return fields[key] ?? .null
}
private func pcOne(_ rows: [JSONValue], _ name: String) throws -> JSONValue {
    try hostRequire(rows.count == 1, name + " exact population"); return rows[0]
}
private func pcEvents(_ rows: [JSONValue], _ name: String, key: String = "name") -> [JSONValue] {
    rows.filter { pcField($0, key) == .string(name) }
}
private func pcDirectory(_ url: URL) throws {
    try hostRequire(!FileManager.default.fileExists(atPath: url.path), "exclusive fixture directory " + url.path)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
}
private func pcSHA(_ bytes: Data) -> String { SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined() }
private func pcCaptured(_ root: ParentCompositionRoot, _ control: ParentCompositionCPUControl) -> JSONValue {
    .object(["capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)), "clock": .string("swift-dispatch-uptime-ns"),
        "composition": root.snapshot(), "native_control": control.snapshot()])
}
private func pcPoll(_ name: String, seconds: Double, deadline: HostDeadline, ready: @Sendable () -> Bool) async throws {
    let start = DispatchTime.now().uptimeNanoseconds
    let budget = min(seconds, max(0, deadline.remaining - 2))
    while Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000_000 < budget {
        if ready() { return }; try await Task.sleep(for: .milliseconds(2))
    }
    throw hostFault("src1b-observation-deadline", "actual observation unavailable: " + name)
}
private struct ParentCPUChecks {
    var rows: [JSONValue] = []
    mutating func equal(_ name: String, _ actual: JSONValue, _ expected: JSONValue) throws {
        let pass = actual == expected
        rows.append(.object(["check": .string(name), "actual": actual, "expected": expected, "passed": .bool(pass)]))
        try hostRequire(pass, name)
    }
    mutating func truth(_ name: String, _ value: Bool) throws { try equal(name, .bool(value), .bool(true)) }
    mutating func before(_ name: String, _ first: JSONValue, _ last: JSONValue) throws {
        guard let a = UInt64(try first.field("sequence").text()), let b = UInt64(try last.field("sequence").text()) else {
            throw hostFault("src1b-sequence", "actual Core event sequences")
        }
        try truth(name, a < b)
    }
}

/// Retains the original host caller of executeParent. It never substitutes for
/// the Core main/observer/watchdog or registry joins. Its start gate opens only
/// after its actual Task has been stored in this already-retained root.
private final class ParentCPUCaller: HostRetainedRoot, Sendable {
    private struct State {
        var task: Task<Void, Never>?, result: Result<ExecutionReport, any Error>?
        var bodyDone = false, joined = false
    }
    private let state = Mutex(State())
    func start(_ operation: @escaping @Sendable () async throws -> ExecutionReport) {
        let gate = HostStartGate()
        let task = Task.detached { [self] in
            await gate.wait()
            let result: Result<ExecutionReport, any Error>
            do { result = .success(try await operation()) } catch { result = .failure(error) }
            state.withLock { $0.result = result; $0.bodyDone = true }
        }
        state.withLock { precondition($0.task == nil); $0.task = task }; gate.open()
    }
    func joinAfterBodyDone() async throws -> ExecutionReport {
        let found = state.withLock { ($0.bodyDone, $0.task, $0.result, $0.joined) }
        try hostRequire(found.0 && found.1 != nil && found.2 != nil, "parent execute caller body must finish before observational join")
        if found.3 { return try found.2!.get() }
        await found.1!.value; state.withLock { $0.joined = true }
        return try found.2!.get()
    }
    func snapshot() -> JSONValue {
        state.withLock { .object(["body_done": .bool($0.bodyDone), "actual_joined": .bool($0.joined),
            "original_handle_retained": .bool($0.task != nil), "role": .string("native-caller-of-executeParentForTesting")]) }
    }
}

private func pcSource(_ name: String, manifest: JSONValue, directory: URL, checks: inout ParentCPUChecks) throws -> Data {
    let row = try pcOne(manifest.field("sources").elements().filter { pcField($0, "name") == .string(name) }, name)
    try checks.equal(name + " exact filename", try row.field("file"), .string(name + ".json"))
    let bytes = try readHostSource(directory.appendingPathComponent(name + ".json"))
    try checks.equal(name + " original byte count", try row.field("bytes"), .integer(bytes.count))
    try checks.equal(name + " original byte identity", try row.field("sha256"), .string(pcSHA(bytes)))
    return bytes
}

/// Independent Foundation lookup of the same pinned real authority. It is a
/// finite numeric/UTF8 oracle for this file, not the production strict parser
/// and not a claim about duplicate handling on arbitrary JSON documents.
private func pcToolReference(_ path: URL, token: UInt64, receipt: JSONValue, checks: inout ParentCPUChecks) throws -> JSONValue {
    let raw = try Data(contentsOf: path)
    try checks.equal("independent tokenizer size", .integer(raw.count), .integer(11_422_654))
    try checks.equal("independent tokenizer SHA256", .string(pcSHA(raw)), .string("aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"))
    guard let object = try JSONSerialization.jsonObject(with: raw) as? [String: Any],
          let model = object["model"] as? [String: Any], let vocab = model["vocab"] as? [String: Any] else {
        throw hostFault("src1b-reference", "fixed real tokenizer vocab")
    }
    let symbols = vocab.compactMap { name, value -> Data? in
        guard let number = value as? NSNumber, number.uint64Value == token else { return nil }; return Data(name.utf8)
    }
    try checks.equal("independent token mapping population", .integer(symbols.count), .integer(1))
    let symbol = symbols[0], digest = Array(SHA256.hash(data: symbol))
    let words: [UInt64] = stride(from: 0, to: 32, by: 8).map { start in
        digest[start..<(start + 8)].reduce(UInt64(0)) { ($0 << 8) | UInt64($1) }
    }
    func word(_ value: UInt64) -> JSONValue { .object(["kind": .string("Word"), "value": .string(String(value))]) }
    func pair(_ a: JSONValue, _ b: JSONValue) -> JSONValue { .object(["kind": .string("Pair"), "left": a, "right": b]) }
    let info = pair(.object(["kind": .string("Nat"), "value": .string(String(symbol.count))]),
        pair(word(words[0]), pair(word(words[1]), pair(word(words[2]), word(words[3])))))
    let reference: JSONValue = .object(["symbol_utf8_base64": .string(symbol.base64EncodedString()), "symbol_bytes": .integer(symbol.count),
        "symbol_sha256": .string(pcSHA(symbol)), "digest_words_decimal": .array(words.map { .string(String($0)) }),
        "tool_info": info,
        "authority_sha256": .string(pcSHA(raw)), "argument_token": .string(String(token)), "reference": .string("Foundation-fixed-authority-UTF8")])
    for key in ["symbol_utf8_base64", "symbol_bytes", "symbol_sha256", "digest_words_decimal", "argument_token"] {
        try checks.equal("independent tool " + key, try receipt.field(key), try reference.field(key))
    }
    return reference
}

private func pcCheckResult(_ f: ParentCPUFixture, parent: ExecutionReport, child: JSONValue,
                           final: JSONValue, control: JSONValue, held: JSONValue,
                           vocabularyPath: URL, checks: inout ParentCPUChecks) throws -> JSONValue {
    try checks.equal("original parent exit", .integer(Int(parent.exitCode)), .integer(f.parentExit))
    let parentTrial = try pcOne(parent.json.field("trials").elements(), "parent original trial")
    let parentLedger = try parentTrial.field("ledger"), events = try parentTrial.field("events").elements()
    let childTrial = try pcOne(child.field("trials").elements(), "child original trial")
    let childLedger = try childTrial.field("ledger"), childEvents = try childTrial.field("events").elements()
    let registry = try final.field("registry"), host = try registry.field("host_root"), response = try registry.field("response")
    let invocation = try registry.field("invocation"), scopeID = try invocation.field("parent_scope").text()
    let originalCaller = try invocation.field("parent_caller")
    try checks.equal("original child exit", try host.field("runtime").field("primary_exit_code"), .integer(f.childExit))
    try checks.equal("child primary preserved", try host.field("primary_report"), child)
    try checks.equal("parent primary preserved", try final.field("primary_report"), parent.json)
    try checks.equal("source complete B8 reaches invocation", try registry.field("invocation").field("tokens"), .array(parentCPUTokens.map(JSONValue.unsigned)))
    let requests = pcEvents(childEvents, "mlx.eval_requested"), receipts = pcEvents(childEvents, "mlx.receipt_accepted")
    try checks.equal("two actual EVAL requests", .integer(requests.count), .integer(2))
    try checks.equal("two actual adopted EVAL receipts", .integer(receipts.count), .integer(2))
    try checks.equal("B original source arguments", try requests[1].field("details").field("tokens"), .array(parentCPUTokens.map(JSONValue.unsigned)))
    try checks.equal("B actual protocol token", try receipts[1].field("details").field("worker_frame").field("token_id"), .unsigned(f.token))
    let aTokens = try requests[0].field("details").field("tokens").elements().map { value -> UInt64 in
        guard let word = UInt64(try value.integerToken()) else { throw hostFault("src1b-token", "A actual Word") }; return word
    }
    try checks.equal("A complete original token population", .integer(aTokens.count), .integer(32))
    try checks.equal("A original CPU sum rule", try receipts[0].field("details").field("worker_frame").field("token_id"), .unsigned(aTokens.reduce(0, +) % 151936))
    let acks = pcEvents(childEvents, "backend-return.ack_observed")
    try checks.equal("two actual original ACKs", .integer(acks.count), .integer(2))
    for index in 0..<2 {
        let ack = try acks[index].field("details"), receipt = try receipts[index].field("details")
        try checks.equal("ACK same complete receipt " + String(index), try ack.field("request").field("eval_receipt"), receipt)
        let layers = try ack.field("worker_frame").field("layers").elements()
        let before = try receipt.field("worker_frame").field("layers").elements()
        try checks.equal("ACK complete layer population " + String(index), .integer(layers.count), .integer(28))
        try checks.equal("receipt complete layer population " + String(index), .integer(before.count), .integer(28))
        for layer in 0..<28 {
            try checks.equal("ACK original layer index \(index)/\(layer)", try layers[layer].field("layer"), .integer(layer))
            try checks.equal("ACK original before snapshot \(index)/\(layer)", try layers[layer].field("before"), before[layer])
            try checks.equal("ACK original released pages \(index)/\(layer)", try layers[layer].field("released_pages"), try before[layer].field("owned_pages"))
        }
    }
    try checks.equal("two original backend returns safe", .integer(pcEvents(childEvents, "backend-return.safe").count), .integer(2))
    try checks.equal("child source has no fake backend", try childLedger.field("host_composition").field("execution_domain"), .string("mlxLeaf"))
    try checks.equal("parent has host execution domain", try parentLedger.field("host_composition").field("execution_domain"), .string("hostParent"))
    try checks.equal("parent has no direct backend session", try parentLedger.field("backend_session"), .null)
    let childMain = try pcOne(Array(childLedger.field("J").members().values).filter { pcField($0, "role") == .string("main") }, "original child main")
    if f.name == "B-C06" {
        try checks.equal("child source failure remains source failure", try childMain.field("published").field("status"), .string("fault"))
    } else {
        try checks.equal("child original source still returns Unit", try childMain.field("published"),
            .object(["status": .string("ok"), "value": .object(["kind": .string("Unit")])]))
    }
    try checks.equal("response terminal", try response.field("phase"), .string(f.hasResponse ? "success-delivered" : "failure"))
    try checks.equal("parent actual response consumption", try response.field("parent_consumed"), .bool(f.hasResponse))
    if f.hasResponse {
        try checks.equal("same response token", try response.field("evidence").field("token"), .unsigned(f.token))
        try checks.equal("successful original requester delivery", try host.field("demand").field("delivered"), .object(["status": .string("success"), "token": .unsigned(f.token)]))
    }
    let responseEvents = try response.field("events").elements()
    try checks.equal("one exact parent wait path", .integer(pcEvents(responseEvents, f.cached ? "service.parent.cached" : "service.parent.registered", key: "event").count), .integer(1))
    try checks.equal("original holder restored once", .integer(pcEvents(events, "service.wait-restored").count), .integer(1))
    let restored = try pcOne(pcEvents(events, "service.wait-restored"), "original holder restore").field("details").field("kont")
    try checks.equal("original holder no longer live", try restored.field("held"), .bool(false))
    try checks.equal("original holder right returned", try restored.field("owner"), .string("returned"))
    try checks.equal("complete original nine-field holder", .array(try restored.field("saved").members().keys.sorted().map(JSONValue.string)),
        .array(["environment", "name_env", "caller", "frames", "suspended", "pending", "evaluations", "next_pending", "next_evaluation"].sorted().map(JSONValue.string)))
    try checks.equal("source response return count", .integer(pcEvents(events, "service.source-received").count), .integer(f.hasResponse ? 1 : 0))
    try checks.equal("actual tool invocation count", .integer(pcEvents(events, "tool.registered").count), .integer(f.hasTool ? 1 : 0))
    var reference: JSONValue = .null
    if f.hasTool {
        let outcome = try control.field("tool_outcome"), receipt = try outcome.field("receipt")
        try checks.equal("tool actual response argument", try receipt.field("argument_token"), .string(String(f.token)))
        try checks.equal("tool same response evidence", try receipt.field("response_evidence"), try response.field("evidence"))
        try checks.equal("tool disposition", try outcome.field("result").field("kind"), .string(f.toolSucceeds ? "ok" : "source-failure"))
        try checks.equal("tool IO has no live handle", try control.field("tool_root").field("actual_handle_retained"), .bool(false))
        try checks.equal("file close versus noncreation", try receipt.field("io").field("closed"), .bool(f.name != "B-C05"))
        if f.name == "B-C05" {
            try checks.equal("real failed open creates no handle", try receipt.field("io").field("noncreated"), .bool(true))
        }
        if !f.toolSucceeds, let code = f.parentFaultCode {
            try checks.equal("original precise tool result", try outcome.field("result").field("code"), .string(code))
        }
        if f.toolSucceeds {
            reference = try pcToolReference(vocabularyPath, token: f.token, receipt: receipt, checks: &checks)
            try checks.equal("independent complete ToolInfo", try outcome.field("result").field("value"), try reference.field("tool_info"))
        }
        try checks.before("source consume precedes actual tool", try pcOne(pcEvents(events, "service.source-received"), "source receive"),
            try pcOne(pcEvents(events, "tool.registered"), "tool register"))
    }
    if let code = f.parentFaultCode {
        let faults = try parentLedger.field("faults").members()
        try checks.truth("original precise parent failure " + code, faults.values.contains { pcField($0, "code") == .string(code) })
    }
    let parentLate = try final.field("parent_late_closure"), childLate = try host.field("late_runtime_observation")
    for (name, closure) in [("parent", parentLate), ("child", childLate)] {
        try checks.equal(name + " actual original handles joined", try closure.field("all_original_handles_joined"), .bool(true))
        for kind in ["main", "observer", "watchdog"] {
            try checks.equal(name + " original " + kind + " existed", try closure.field("original_handle_population").field(kind + "_created"), .bool(true))
            try checks.equal(name + " original " + kind + " joined", try closure.field("original_handle_population").field(kind + "_joined"), .bool(true))
        }
        for (job, actual) in try closure.field("late_ledger_observation").field("J").members() {
            try checks.equal(name + " actual original source task " + job, try actual.field("actual_task_exit_observed"), .bool(true))
        }
    }
    try checks.equal("registry original observer joined", try final.field("registry_final_closure").field("observer_joined"), .bool(true))
    try checks.equal("registry original work and timer joined", try final.field("registry_final_closure").field("all_created_handles_joined"), .bool(true))
    try checks.equal("registry closed versus evidence gap", try final.field("registry_final_closure").field("resource_closed"), .bool(!f.closeFault))
    try checks.equal("original requester actually joined", try host.field("requester_observation").field("actual_joined"), .bool(true))
    try checks.equal("one original shutdown only", try control.field("shutdown_calls"), .integer(1))
    try checks.equal("original shutdown actually returned", try control.field("shutdown_returned"), .bool(true))
    let physicalClose = try control.field("original_shutdown_evidence")
    for flag in ["reaped", "stdout_joined", "stderr_joined", "stdin_closed", "permit_joined"] {
        try checks.equal("actual original backend " + flag, try physicalClose.field(flag), .bool(true))
    }
    try checks.equal("actual worker exit", try physicalClose.field("actual_exit"), .integer(0))
    if f.closeFault {
        try checks.equal("C08 actual fault boundary", try control.field("injected_close_failure").field("boundary"), .string("after-original-shutdown-return-before-Core-receive"))
        try checks.equal("C08 Core did not receive closure authority", try childLate.field("backend_evidence"), .null)
    }
    for gate in ["evaluation_gate", "parent_wait_gate", "shutdown_gate"] {
        try checks.equal(gate + " continuation discharged", try control.field(gate).field("waiter_registered"), .bool(false))
    }
    let lateLedger = try parentLate.field("late_ledger_observation")
    let mains = try lateLedger.field("J").members().values.filter { pcField($0, "role") == .string("main") }
    let main = try pcOne(Array(mains), "late original parent main")
    for key in ["job", "task"] {
        try checks.equal("service belongs to original parent main " + key, try originalCaller.field(key), try main.field("caller").field(key))
    }
    if f.timesOut || f.closeFault || f.parentFault {
        try checks.equal("incomplete parent never gains a publication", try main.field("published"), .null)
        try checks.equal("no normal source scope finalization", .integer(pcEvents(events, "scope.finalize").count), .integer(0))
    } else {
        let closed = try pcOne(pcEvents(events, "service.host-close-safe"), "HostClose safe")
        let finalized = try pcOne(pcEvents(events, "scope.finalize").filter { pcField($0, "scope") == .string(scopeID) }, "parent service scope finalize")
        let published = try pcOne(pcEvents(events, "result.published"), "parent publication")
        try checks.equal("HostClose belongs to original service scope", try closed.field("scope"), .string(scopeID))
        for (label, event) in [("HostClose", closed), ("service scope finalize", finalized), ("publication", published)] {
            for key in ["job", "task"] {
                try checks.equal(label + " belongs to original parent " + key, try event.field(key), try originalCaller.field(key))
            }
        }
        try checks.equal("original service scope actually joined", try lateLedger.field("S").field(scopeID).field("state"), .string("Joined"))
        try checks.before("HostClose before parent scope exit", closed, finalized)
        try checks.before("parent scope exit before publication", finalized, published)
        if f.hasTool { try checks.before("actual tool return before scope discharges HostClose", try pcOne(pcEvents(events, "tool.original-return"), "original tool return"), closed) }
        if f.parentExit == 0 {
            let info = try control.field("tool_outcome").field("result").field("value")
            try checks.equal("parent actual Pair retains token and actual ToolInfo", try main.field("published"),
                .object(["status": .string("ok"), "value": .object(["kind": .string("Pair"),
                    "left": .object(["kind": .string("Word"), "value": .string(String(f.token))]), "right": info])]))
        }
    }
    if !f.cached && f.hasTool {
        let open = try pcOne(pcEvents(control.field("shutdown_gate").field("events").elements(), "gate.open", key: "event"), "close gate open")
        let returned = try pcOne(pcEvents(control.field("events").elements(), "native.original-tool-return", key: "event"), "actual tool return")
        guard let toolNS = UInt64(try returned.field("capture_ns").text()), let openNS = UInt64(try open.field("capture_ns").text()) else {
            throw hostFault("src1b-clock", "actual uptime stamps")
        }
        try checks.truth("actual tool completed before original close was released", toolNS <= openNS)
        try checks.equal("held close had no service closure", try held.field("composition").field("registry").field("closure"), .null)
        if !f.timesOut && !f.closeFault {
            guard let origin = UInt64(try parentTrial.field("config").field("host_uptime_origin_ns").text()) else {
                throw hostFault("src1b-clock", "Core original uptime origin")
            }
            for eventName in ["scope.finalize", "result.published"] {
                let selected = pcEvents(events, eventName).filter { eventName != "scope.finalize" || pcField($0, "scope") == .string(scopeID) }
                let event = try pcOne(selected, "parent service " + eventName)
                guard let relative = UInt64(try event.field("t_ns").text()) else { throw hostFault("src1b-clock", "Core actual relative stamp") }
                let stamp = origin.addingReportingOverflow(relative)
                try checks.truth("parent " + eventName + " cannot precede actual close-gate release", !stamp.overflow && openNS <= stamp.partialValue)
            }
        }
    }
    return reference
}

func runParentCompositionControls(fixture: String, sourcesDirectory: URL, evidenceDirectory: URL,
        inputsPath: String, session: String, vocabularyPath: URL, deadline: HostDeadline,
        evidence: HostEvidence, frontendHost: CPUFrontendHost, entryRoots: ParentEntryRoots) async throws -> HostWorkResult {
    let f = try ParentCPUFixture(fixture)
    try pcDirectory(evidenceDirectory)
    var checks = ParentCPUChecks(), failure: JSONValue = .null, cleanupErrors: [JSONValue] = []
    var held: JSONValue = .null, parentReport: ExecutionReport?, childReport: JSONValue = .null, final: JSONValue = .null, reference: JSONValue = .null
    var composition: ParentCompositionRoot?, control: ParentCompositionCPUControl?, caller: ParentCPUCaller?
    var callerStarted = false
    do {
        let manifestBytes = try readHostSource(sourcesDirectory.appendingPathComponent("manifest.json"))
        try manifestBytes.write(to: evidenceDirectory.appendingPathComponent("source-manifest.json"), options: .withoutOverwriting)
        let manifest = try ExactJSON.decode(manifestBytes)
        try checks.equal("source manifest schema", try manifest.field("schema"), .string("src1b-parent-cpu-source-generation-v1"))
        let parentBytes = try pcSource("parent", manifest: manifest, directory: sourcesDirectory, checks: &checks)
        let childBytes = try pcSource(f.child, manifest: manifest, directory: sourcesDirectory, checks: &checks)
        let selected = try pcOne(manifest.field("fixtures").elements().filter { pcField($0, "fixture") == .string(fixture) }, "selected fixture")
        try checks.equal("fixed selected child", try selected.field("child"), .string(f.child))
        try checks.equal("fixed worker token rule", try selected.field("worker_scenario"), .string(f.workerScenario))
        try saveHostJSON(selected, to: evidenceDirectory.appendingPathComponent("source-selection.json"))
        let parentCompilerDir = evidenceDirectory.appendingPathComponent("parent-compiler"), childCompilerDir = evidenceDirectory.appendingPathComponent("child-compiler")
        try pcDirectory(parentCompilerDir); try pcDirectory(childCompilerDir)
        let parentCompiler = CompilerBridge(evidenceDirectory: parentCompilerDir, cpuHost: frontendHost)
        let parentProgram = try await parentCompiler.checkParentSource(parentBytes)
        evidence.put("parent_compiler", parentCompiler.provenance)
        let childCompiler = CompilerBridge(evidenceDirectory: childCompilerDir, cpuHost: frontendHost)
        let childProgram = try await childCompiler.checkClosedSource(childBytes)
        evidence.put("child_compiler", childCompiler.provenance)
        let budget: Int
        if f.timesOut { budget = 3 } else { budget = try deadline.coreBudget() }
        let leaf = try ChildLeafDescriptor(program: childProgram, name: "B", session: session,
            outputDirectory: evidenceDirectory.appendingPathComponent("child-host"), watchdogSeconds: budget)
        let file: VocabFileCapability
        if f.name == "B-C05" {
            // This exclusive parent directory exists; its child path does not.
            // No synthetic read error or permission-bit assumption is used.
            let missing = evidenceDirectory.appendingPathComponent("absent-tokenizer.json")
            try hostRequire(!FileManager.default.fileExists(atPath: missing.path), "fixed open-failure path absent")
            file = try .nativeFixture(path: missing, expectedBytes: nil, expectedSHA256: nil, purpose: .openFailure)
        } else { file = try VocabFileCapability(productionPath: vocabularyPath) }
        let native = try ParentCompositionCPUControl(fixture: fixture); control = native
        evidence.retain("parent_cpu_control", native)
        let root = try ParentCompositionRoot(program: parentProgram, leaf: leaf, inputsPath: inputsPath,
            worker: .nativeCPU(scenario: f.workerScenario), vocabulary: file, parentWatchdogSeconds: budget,
            hostBudgetSeconds: 50, cpuControl: native)
        composition = root; try entryRoots.install(root); evidence.retain("parent_composition", root)
        let work = ParentCPUCaller(); caller = work; evidence.retain("parent_execute_caller", work)
        work.start { try await root.runForTesting(control: native) }; callerStarted = true
        if f.cached {
            try await pcPoll("cached response and independently closed child", seconds: 15, deadline: deadline) {
                pcField(pcField(root.registry.snapshot(), "closure"), "resource_closed") == .bool(true) &&
                pcField(pcField(native.snapshot(), "parent_wait_gate"), "waiter_registered") == .bool(true)
            }
            held = pcCaptured(root, native)
            try checks.equal("cached response actually delivered before parent wait", try root.registry.snapshot().field("response").field("phase"), .string("success-delivered"))
            try checks.equal("child close does not need parent consume", try root.registry.snapshot().field("response").field("parent_consumed"), .bool(false))
            try saveHostJSON(held, to: evidenceDirectory.appendingPathComponent("held.json"))
            native.releaseParentWait()
        } else {
            try await pcPoll("registered parent waiter and held original evaluate", seconds: 10, deadline: deadline) {
                pcField(pcField(root.registry.snapshot(), "response"), "parent_waiter") == .bool(true) &&
                pcField(pcField(native.snapshot(), "evaluation_gate"), "waiter_registered") == .bool(true)
            }
            try saveHostJSON(pcCaptured(root, native), to: evidenceDirectory.appendingPathComponent("registered-before-evaluate.json"))
            native.releaseEvaluation()
            try await pcPoll("held original shutdown and original tool/response observation", seconds: 10, deadline: deadline) {
                pcField(pcField(native.snapshot(), "shutdown_gate"), "waiter_registered") == .bool(true) &&
                (!f.hasTool || pcField(native.snapshot(), "tool_outcome") != .null)
            }
            held = pcCaptured(root, native)
            try saveHostJSON(held, to: evidenceDirectory.appendingPathComponent("held.json"))
            try checks.equal("actual shutdown still gated", try native.snapshot().field("shutdown_returned"), .bool(false))
            if !f.timesOut && !f.parentFault { native.releaseShutdown() }
        }
        try await pcPoll("original parent execute caller report", seconds: 15, deadline: deadline) { pcField(work.snapshot(), "body_done") == .bool(true) }
        let report = try await work.joinAfterBodyDone(); parentReport = report
        try saveHostJSON(report.json, to: evidenceDirectory.appendingPathComponent("parent-primary.json"))
        if f.timesOut {
            try await pcPoll("both original watchdog reports before gate opening", seconds: 6, deadline: deadline) {
                pcField(pcField(pcField(root.registry.snapshot(), "host_root"), "runtime"), "primary_exit_code") == .integer(124)
                && pcField(pcField(root.registry.snapshot(), "host_root"), "primary_report") != .null
            }
            let childHost = try root.registry.snapshot().field("host_root")
            childReport = try childHost.field("primary_report")
            try checks.equal("original parent watchdog won", .integer(Int(report.exitCode)), .integer(124))
            try checks.equal("original child watchdog won", try childHost.field("runtime").field("primary_exit_code"), .integer(124))
            try checks.truth("original child watchdog report saved", childReport != .null)
            try saveHostJSON(childReport, to: evidenceDirectory.appendingPathComponent("child-primary-before-gate.json"))
            try saveHostJSON(pcCaptured(root, native), to: evidenceDirectory.appendingPathComponent("two-reports-before-gate.json"))
            native.releaseShutdown()
        } else if f.parentFault { native.releaseShutdown() }
        final = try await root.joinOriginalHandles()
        if childReport == .null { childReport = try final.field("registry").field("host_root").field("primary_report") }
        try saveHostJSON(childReport, to: evidenceDirectory.appendingPathComponent("child-primary.json"))
        try saveHostJSON(final, to: evidenceDirectory.appendingPathComponent("original-closures.json"))
        reference = try pcCheckResult(f, parent: report, child: childReport, final: final, control: native.snapshot(), held: held,
            vocabularyPath: vocabularyPath, checks: &checks)
    } catch {
        failure = .object(["type": .string(String(reflecting: type(of: error))), "description": .string(String(describing: error))])
        let prefix: JSONValue = .object(["failure": failure, "composition": composition?.snapshot() ?? .null,
            "native_control": control?.snapshot() ?? .null, "caller": caller?.snapshot() ?? .null])
        do { try saveHostJSON(prefix, to: evidenceDirectory.appendingPathComponent("first-failure-before-cleanup.json")) }
        catch { cleanupErrors.append(.string("save original failure prefix: " + String(describing: error))) }
        composition?.requestAbort(.runtimeFailure); control?.releaseAll()
        if callerStarted, let caller, let composition {
            do {
                try await pcPoll("original failed parent caller after gates released", seconds: 8, deadline: deadline) { pcField(caller.snapshot(), "body_done") == .bool(true) }
                let original = try await caller.joinAfterBodyDone()
                if parentReport == nil { parentReport = original }
                final = try await composition.joinOriginalHandles()
            } catch { cleanupErrors.append(.string("original cleanup observation: " + String(describing: error))) }
        }
    }
    let complete = failure == .null && cleanupErrors.isEmpty
    let trial: JSONValue = .object(["trial_id": .string(fixture), "fixture": .string(fixture), "passed": .bool(complete),
        "first_failure": failure, "cleanup_errors": .array(cleanupErrors), "checks": .array(checks.rows),
        "parent_primary": parentReport?.json ?? .null, "child_primary": childReport, "held": held, "final": final,
        "native_control": control?.snapshot() ?? .null, "caller": caller?.snapshot() ?? .null, "tool_reference": reference,
        "model_execution": .bool(false), "performance_claimed": .bool(false),
        "scope": .string("fixed checked CPU composition; requires independent raw/source review")])
    try saveHostJSON(trial, to: evidenceDirectory.appendingPathComponent("trial.json"))
    return HostWorkResult(status: complete ? "passed" : "failed", exitCode: complete ? 0 : 1, value: trial, trials: [trial])
}
