import Foundation
import Dispatch
import Synchronization
import CryptoKit
import RuntimeCore

// Native, finite real-file controls. These do not replace B-C01...B-C10 and
// deliberately pass a literal tool argument after consuming a real B reply.
private let ntTokens: [UInt64] = [2127, 19010, 19287, 13098, 1724, 7344, 1626, 72]
private let ntVocabBytes = 11_422_654
private let ntVocabSHA = "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
private func ntField(_ value: JSONValue, _ key: String) -> JSONValue {
    guard case .object(let members) = value else { return .null }
    return members[key] ?? .null
}
private func ntSHA(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
private func ntOne(_ values: [JSONValue], _ name: String) throws -> JSONValue {
    try hostRequire(values.count == 1, name + " exact population"); return values[0]
}
private func ntEvents(_ values: [JSONValue], _ name: String, key: String = "name") -> [JSONValue] {
    values.filter { ntField($0, key) == .string(name) }
}
private func ntDirectory(_ url: URL) throws {
    try hostRequire(!FileManager.default.fileExists(atPath: url.path), "exclusive native boundary directory " + url.path)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
}
private func ntUnsigned(_ value: JSONValue, _ name: String) throws -> UInt64 {
    guard let result = UInt64(try value.text()) else { throw hostFault("src1b-native-clock", name) }
    return result
}
private struct NativeToolChecks {
    var rows: [JSONValue] = []
    mutating func equal(_ name: String, _ actual: JSONValue, _ expected: JSONValue) throws {
        let passed = actual == expected
        rows.append(.object(["check": .string(name), "actual": actual, "expected": expected, "passed": .bool(passed)]))
        try hostRequire(passed, name)
    }
    mutating func truth(_ name: String, _ actual: Bool) throws { try equal(name, .bool(actual), .bool(true)) }
    mutating func before(_ name: String, _ first: JSONValue, _ second: JSONValue) throws {
        let a = try ntUnsigned(first.field("sequence"), name), b = try ntUnsigned(second.field("sequence"), name)
        try truth(name, a < b)
    }
}
private func ntCode(_ f: NativeToolBoundaryCase) -> String? {
    switch f {
    case .t01, .t02: return nil
    case .t03: return "ToolAddedTokenUnsupported"
    case .t04: return "ToolTokenMissing"
    case .t05: return "ToolArgumentOutOfRange"
    case .t06: return "ToolOpenFailure"
    case .t07: return "ToolReadFailure"
    case .t08: return "ToolSchemaFailure"
    case .t09, .t10: return "ToolDuplicateMapping"
    case .t11: return "ToolAuthorityMismatch"
    case .t12: return "ToolCloseUnknown"
    case .t13, .t14: return "ToolBudgetExceeded"
    }
}
private func ntExit(_ f: NativeToolBoundaryCase) -> Int { ntCode(f) == nil ? 0 : (f == .t11 || f == .t12 ? 2 : 1) }

/// This caller is retained before its original Task can run. It is distinct
/// from Core's main/observer/watchdog and never stands in for their joins.
private final class NativeToolCaller: HostRetainedRoot, Sendable {
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
    var bodyDone: Bool { state.withLock { $0.bodyDone } }
    func join() async throws -> ExecutionReport {
        let s = state.withLock { ($0.bodyDone, $0.task, $0.result, $0.joined) }
        try hostRequire(s.0 && s.1 != nil && s.2 != nil, "native caller body finished before original Task.value")
        if !s.3 { await s.1!.value; state.withLock { $0.joined = true } }
        return try s.2!.get()
    }
    func snapshot() -> JSONValue {
        state.withLock { .object(["body_done": .bool($0.bodyDone), "actual_joined": .bool($0.joined),
            "original_handle_retained": .bool($0.task != nil), "role": .string("native-tool-caller-of-executeParentForTesting")]) }
    }
}
private func ntWaitCaller(_ caller: NativeToolCaller, deadline: HostDeadline) async throws {
    while deadline.remaining > 2 {
        if caller.bodyDone { return }
        try await Task.sleep(for: .milliseconds(2))
    }
    throw hostFault("src1b-native-observation-deadline", "original native parent caller not observed returned")
}

private func ntSource(_ name: String, domain: String, manifest: JSONValue, directory: URL,
                      checks: inout NativeToolChecks) throws -> (Data, JSONValue) {
    let row = try ntOne(manifest.field("sources").elements().filter { ntField($0, "name") == .string(name) }, name)
    try checks.equal(name + " filename", try row.field("file"), .string(name + ".json"))
    try checks.equal(name + " domain", try row.field("domain"), .string(domain))
    let bytes = try readHostSource(directory.appendingPathComponent(name + ".json"))
    try checks.equal(name + " byte count", try row.field("bytes"), .integer(bytes.count))
    try checks.equal(name + " exact source identity", try row.field("sha256"), .string(ntSHA(bytes)))
    return (bytes, row)
}
private func ntCommands(_ value: JSONValue, named name: String) -> [JSONValue] {
    switch value {
    case .object(let fields):
        let here: [JSONValue] = fields["command"] == .string(name) ? [value] : []
        return here + fields.values.flatMap { ntCommands($0, named: name) }
    case .array(let values): return values.flatMap { ntCommands($0, named: name) }
    default: return []
    }
}

private struct NativeToolFile {
    let capability: VocabFileCapability, path: URL, record: JSONValue
    let actualBytes: Int?, actualSHA: String?
}
private func ntFile(_ f: NativeToolBoundaryCase, directory: URL, vocabularyPath: URL,
                    checks: inout NativeToolChecks) throws -> NativeToolFile {
    let path: URL, capability: VocabFileCapability
    var bytes: Data?, absence = false, isDirectory = false
    switch f {
    case .t06:
        path = directory.appendingPathComponent("absent-tokenizer.json"); absence = true
        try checks.truth("fixed open path absent", !FileManager.default.fileExists(atPath: path.path))
        capability = try .nativeFixture(path: path, expectedBytes: nil, expectedSHA256: nil, purpose: .openFailure)
    case .t07:
        path = directory.appendingPathComponent("read-directory"); isDirectory = true
        try ntDirectory(path)
        capability = try .nativeFixture(path: path, expectedBytes: 0, expectedSHA256: ntSHA(Data()), purpose: .readFailure)
    case .t08, .t09, .t10, .t11, .t12, .t13:
        path = directory.appendingPathComponent("native-tokenizer.json")
        let purpose: VocabNativeFixture
        switch f {
        case .t08: bytes = Data("{".utf8); purpose = .schemaFailure
        case .t09: bytes = Data(#"{"model":{"vocab":{"a":0,"b":0}},"added_tokens":[]}"#.utf8); purpose = .duplicateMapping
        case .t10: bytes = Data(#"{"model":{"vocab":{"a":0,"\u0061":1}},"added_tokens":[]}"#.utf8); purpose = .duplicateMapping
        case .t11: bytes = Data(#"{"model":{"vocab":{"!":0}},"added_tokens":[]}"#.utf8); purpose = .authorityMismatch
        case .t12: bytes = Data(#"{"model":{"vocab":{"!":0}},"added_tokens":[]}"#.utf8); purpose = .closeUnknown
        default: bytes = Data(repeating: 0x20, count: 12_582_913); purpose = .byteLimit
        }
        try bytes!.write(to: path, options: .withoutOverwriting)
        capability = try .nativeFixture(path: path, expectedBytes: bytes!.count,
            expectedSHA256: f == .t11 ? String(repeating: "0", count: 64) : ntSHA(bytes!), purpose: purpose)
    default:
        path = vocabularyPath; bytes = try Data(contentsOf: path)
        try checks.equal("production authority size before run", .integer(bytes!.count), .integer(ntVocabBytes))
        try checks.equal("production authority SHA before run", .string(ntSHA(bytes!)), .string(ntVocabSHA))
        capability = try VocabFileCapability(productionPath: path)
    }
    let count = bytes?.count, hash = bytes.map(ntSHA)
    var fields: [String: JSONValue] = [:]
    fields["schema"] = .string("src1b-native-tool-file-v1"); fields["case_id"] = .string(f.rawValue)
    fields["capability"] = capability.diagnostic; fields["actual_path"] = .string(path.path)
    fields["actual_bytes"] = count.map(JSONValue.integer) ?? .null; fields["actual_sha256"] = hash.map(JSONValue.string) ?? .null
    fields["nonexistent"] = .bool(absence); fields["directory"] = .bool(isDirectory)
    fields["fixed_small_bytes_base64"] = bytes.flatMap { $0.count <= 1024 ? .string($0.base64EncodedString()) : nil } ?? .null
    fields["byte_limit_fixture"] = .bool(f == .t13)
    fields["synthetic_close_evidence_loss"] = .bool(f == .t12)
    fields["physical_close_failure_claimed"] = .bool(false)
    let record = JSONValue.object(fields)
    try saveHostJSON(record, to: directory.appendingPathComponent("file-capability.json"))
    return NativeToolFile(capability: capability, path: path, record: record, actualBytes: count, actualSHA: hash)
}

/// Full-file independent oracle for the *pinned production authority*. This
/// uses Foundation, not the production byte parser or any receipt as input.
/// It makes no claim about Foundation's duplicate-key behavior on other files.
private func ntReference(_ path: URL, token: UInt64, checks: inout NativeToolChecks) throws -> JSONValue {
    let raw = try Data(contentsOf: path)
    try checks.equal("independent full authority bytes", .integer(raw.count), .integer(ntVocabBytes))
    try checks.equal("independent full authority SHA", .string(ntSHA(raw)), .string(ntVocabSHA))
    guard let top = try JSONSerialization.jsonObject(with: raw) as? [String: Any],
          let model = top["model"] as? [String: Any], let vocab = model["vocab"] as? [String: Any],
          let added = top["added_tokens"] as? [[String: Any]] else {
        throw hostFault("src1b-native-reference", "fixed full tokenizer schema")
    }
    var ordinary: [UInt64: Data] = [:], symbols = Set<Data>(), addedIDs = Set<UInt64>()
    for (symbol, number) in vocab {
        guard let n = number as? NSNumber, let id = UInt64(n.stringValue), n.stringValue == String(id), id <= 151642 else {
            throw hostFault("src1b-native-reference", "fixed ordinary integer domain")
        }
        let bytes = Data(symbol.utf8)
        try hostRequire(ordinary.updateValue(bytes, forKey: id) == nil && symbols.insert(bytes).inserted, "independent unique full ordinary mapping")
    }
    for entry in added {
        guard let n = entry["id"] as? NSNumber, let id = UInt64(n.stringValue), n.stringValue == String(id),
              (151643...151668).contains(id), let symbol = entry["content"] as? String else {
            throw hostFault("src1b-native-reference", "fixed added integer domain")
        }
        try hostRequire(addedIDs.insert(id).inserted && symbols.insert(Data(symbol.utf8)).inserted, "independent unique full added mapping")
    }
    try checks.equal("full ordinary population", .integer(ordinary.count), .integer(151643))
    try checks.equal("full added population", .integer(addedIDs.count), .integer(26))
    var result: [String: JSONValue] = ["reference": .string("independent-Foundation-full-pinned-authority-UTF8"),
        "authority_bytes": .integer(raw.count), "authority_sha256": .string(ntSHA(raw)),
        "argument_token": .string(String(token)), "ordinary_population": .integer(ordinary.count), "added_population": .integer(addedIDs.count)]
    if let symbol = ordinary[token] {
        let digest = Array(SHA256.hash(data: symbol))
        let words: [UInt64] = stride(from: 0, to: 32, by: 8).map { index in
            digest[index..<(index + 8)].reduce(UInt64(0)) { ($0 << 8) | UInt64($1) }
        }
        func word(_ n: UInt64) -> JSONValue { .object(["kind": .string("Word"), "value": .string(String(n))]) }
        func pair(_ a: JSONValue, _ b: JSONValue) -> JSONValue { .object(["kind": .string("Pair"), "left": a, "right": b]) }
        result["classification"] = .string("ordinary")
        result["symbol_utf8_base64"] = .string(symbol.base64EncodedString()); result["symbol_bytes"] = .integer(symbol.count)
        result["symbol_sha256"] = .string(ntSHA(symbol)); result["digest_words_decimal"] = .array(words.map { .string(String($0)) })
        result["tool_info"] = pair(.object(["kind": .string("Nat"), "value": .string(String(symbol.count))]),
            pair(word(words[0]), pair(word(words[1]), pair(word(words[2]), word(words[3])))))
    } else {
        result["classification"] = .string(token >= 151936 ? "out-of-range" : addedIDs.contains(token) ? "added" : "missing")
        result["tool_info"] = .null
    }
    return .object(result)
}

private func ntToolChecks(_ f: NativeToolBoundaryCase, control: JSONValue, file: NativeToolFile,
                          response: JSONValue, parentLedger: JSONValue, events: [JSONValue],
                          deadline: HostDeadline, reference: inout JSONValue, checks: inout NativeToolChecks) throws {
    let invocation = try control.field("tool_invocation"), root = try control.field("tool_root")
    let outcome = try control.field("tool_outcome"), receipt = try outcome.field("receipt"), result = try outcome.field("result")
    let id = try invocation.field("tool_call_id").text(), io = try receipt.field("io"), rows = try root.field("events").elements()
    try checks.equal("forward exactly one original invocation", try control.field("perform_calls"), .integer(1))
    try checks.equal("forward returned", try control.field("returned"), .bool(true))
    try checks.equal("forward issues empty", try control.field("issues"), .array([]))
    try checks.equal("fixed literal argument", try invocation.field("token"), .unsigned(f.token))
    try checks.equal("receipt literal argument", try receipt.field("argument_token"), .string(String(f.token)))
    try checks.equal("receipt original call", try receipt.field("tool_call"), .string(id))
    try checks.equal("receipt original service", try receipt.field("service_id"), try invocation.field("service_id"))
    try checks.equal("receipt original caller", try receipt.field("caller"), try invocation.field("caller"))
    try checks.equal("receipt original site", try receipt.field("site"), try invocation.field("site"))
    try checks.equal("original file capability", try invocation.field("file"), file.capability.diagnostic)
    for value in [try receipt.field("response_evidence"), try invocation.field("response_evidence")] {
        try checks.equal("same original consumed response evidence", value, try response.field("evidence"))
    }
    for value in [try root.field("receipt"), try control.field("first_receipt"), try control.field("first_receipt_after_reentry")] {
        try checks.equal("first immutable receipt", value, receipt)
    }
    try checks.equal("Core retained original tool outcome", try parentLedger.field("host_composition").field("tool_outcomes").field(id), outcome)
    try checks.equal("Core retained original tool root", try parentLedger.field("host_composition").field("tool_roots").field(id), root)
    try checks.equal("original tool return event", try ntOne(ntEvents(events, "tool.original-return"), "tool return").field("details"), outcome)
    let expectedKind = ntExit(f) == 0 ? "ok" : ntExit(f) == 2 ? "infrastructure" : "source-failure"
    try checks.equal("precise tool disposition", try result.field("kind"), .string(expectedKind))
    try checks.equal("receipt disposition", try receipt.field("outcome"), .string(expectedKind))
    if let code = ntCode(f) {
        try checks.equal("precise tool code", try result.field("code"), .string(code))
        try checks.equal("precise receipt code", try receipt.field("failure").field("code"), .string(code))
    } else { try checks.equal("success has no failure", try receipt.field("failure"), .null) }
    let clock = try receipt.field("clock")
    let started = try ntUnsigned(clock.field("started_ns"), "tool start"), finished = try ntUnsigned(clock.field("finished_ns"), "tool finish")
    let last = try ntUnsigned(receipt.field("last_gate_ns"), "tool final gate")
    try checks.equal("tool clock domain", try clock.field("domain"), .string("swift-dispatch-uptime-ns"))
    try checks.equal("original 2s work budget", try receipt.field("work_budget_ns"), .string("2000000000"))
    try checks.truth("both real gates ordered", started <= finished && finished <= last)
    try checks.equal("actual clock arithmetic", try clock.field("elapsed_ns"), .string(String(finished - started)))
    try checks.equal("both actual budget gates", try receipt.field("within_work_budget"), .bool(finished - started < 2_000_000_000 && last - started < 2_000_000_000))
    if ntExit(f) == 0 { try checks.equal("success meets both gates", try receipt.field("within_work_budget"), .bool(true)) }
    var previous = started
    for row in rows {
        let stamp = try ntUnsigned(row.field("capture_ns"), "original IO event")
        try checks.truth("original IO clocks ordered", previous <= stamp && stamp <= last); previous = stamp
    }
    for name in ["tool.open-call", "tool.receipt-sealed", "tool.finished"] {
        try checks.equal(name + " exactly once", .integer(ntEvents(rows, name, key: "event").count), .integer(1))
    }
    let noncreated = f == .t06, unknown = f == .t12
    try checks.equal("actual open returned", try io.field("open_returned"), .bool(true))
    try checks.equal("actual opened versus noncreation", try io.field("opened"), .bool(!noncreated))
    try checks.equal("noncreated is not closed", try io.field("noncreated"), .bool(noncreated))
    try checks.equal("actual close call population", .integer(ntEvents(rows, "tool.close-call", key: "event").count), .integer(noncreated ? 0 : 1))
    try checks.equal("close attempted", try io.field("close_attempted"), .bool(!noncreated))
    try checks.equal("trusted closed observation", try io.field("closed"), .bool(!noncreated && !unknown))
    try checks.equal("original handle retention", try root.field("actual_handle_retained"), .bool(unknown))
    try checks.equal("actual tool IO obligation", try parentLedger.field("Omega").field("ToolIO:" + id), .string(unknown ? "Unresolved" : "Safe"))
    try checks.equal("ToolIO original root retained iff unresolved", .bool(try parentLedger.field("roots").elements().contains(.string("ToolIO:" + id))), .bool(unknown))
    try checks.equal("ToolIO safe event population", .integer(ntEvents(events, "tool.io-safe").count), .integer(unknown ? 0 : 1))
    try checks.equal("complete read versus actual failure", try io.field("read_completed"), .bool(![NativeToolBoundaryCase.t06, .t07, .t13, .t14].contains(f)))
    let authority = try receipt.field("authority")
    try checks.equal("actual authority path", try authority.field("path"), .string(file.path.path))
    try checks.equal("resolved path observed", try authority.field("resolved_path"), .string(file.path.resolvingSymlinksInPath().path))
    try checks.equal("authority independent of close acceptance", try authority.field("matched"), .bool(![NativeToolBoundaryCase.t06, .t07, .t11, .t13, .t14].contains(f)))
    if noncreated {
        try checks.equal("no invented descriptor", try io.field("handle_identity"), .null)
        try checks.equal("open failure has no bytes", try authority.field("bytes"), .null)
        try checks.equal("open failure has no hash", try authority.field("sha256"), .null)
    } else {
        try checks.truth("original descriptor identity exists", try io.field("handle_identity") != .null)
        let expectedCount = f == .t07 || f == .t14 ? 0 : file.actualBytes!
        let expectedSHA = f == .t07 || f == .t14 ? ntSHA(Data()) : file.actualSHA!
        try checks.equal("actual bytes read", try authority.field("bytes"), .integer(expectedCount))
        try checks.equal("actual bytes SHA", try authority.field("sha256"), .string(expectedSHA))
    }
    if f == .t06 || f == .t07 {
        let error = try receipt.field("original_io_error"), underlying = ntField(error, "underlying")
        let errno = f == .t06 ? 2 : 21
        let posix = [error, underlying].contains { ntField($0, "domain") == .string("NSPOSIXErrorDomain") && ntField($0, "code") == .integer(errno) }
        try checks.truth("actual original POSIX open/read cause", posix)
        try checks.truth("original NSError domain retained", !(try error.field("domain").text()).isEmpty)
        try checks.equal("not synthetic IO failure", try error.field("native_evidence_delivery_fault"), .bool(false))
    }
    if unknown {
        let marker = try receipt.field("native_close_observation")
        try checks.equal("actual original close returned", try marker.field("actual_close_returned"), .bool(true))
        try checks.equal("close evidence delivery boundary", try marker.field("boundary"), .string("after-original-close-before-evidence-acceptance"))
        try checks.equal("no physical close error claim", try marker.field("physical_close_failure_claimed"), .bool(false))
        try checks.equal("synthetic acceptance failure explicitly named", try receipt.field("original_close_error").field("native_evidence_delivery_fault"), .bool(true))
        try checks.equal("native close return once", .integer(ntEvents(rows, "tool.native-close-returned", key: "event").count), .integer(1))
        try checks.equal("close unknown once", .integer(ntEvents(rows, "tool.close-unknown", key: "event").count), .integer(1))
        try checks.equal("no accepted closed event", .integer(ntEvents(rows, "tool.closed", key: "event").count), .integer(0))
        let returned = try ntOne(ntEvents(rows, "tool.native-close-returned", key: "event"), "actual close return")
        try checks.equal("actual return marker matches retained event", try returned.field("detail"), marker)
        let call = try ntOne(ntEvents(rows, "tool.close-call", key: "event"), "original close call")
        let unknownEvent = try ntOne(ntEvents(rows, "tool.close-unknown", key: "event"), "unknown evidence acceptance")
        let actualReturn = try ntUnsigned(marker.field("capture_ns"), "actual close return")
        try checks.truth("actual close return precedes evidence rejection", ntUnsigned(call.field("capture_ns"), "close call") <= actualReturn && actualReturn <= ntUnsigned(unknownEvent.field("capture_ns"), "close evidence rejection"))
    } else {
        try checks.equal("no native close marker", try receipt.field("native_close_observation"), .null)
        try checks.equal("no close failure", try receipt.field("original_close_error"), .null)
    }
    if f == .t11 { try checks.equal("authority mismatch exact original cause", try receipt.field("original_io_error"), .string("vocab file authority mismatch")) }
    if f == .t13 { try checks.equal("byte limit distinct original cause", try receipt.field("original_io_error"), .string("tool file byte limit")) }
    if f == .t14 { try checks.equal("elapsed limit distinct original cause", try receipt.field("original_io_error"), .string("tool IO budget exceeded")) }
    if f == .t01 {
        let reentry = try control.field("reentry")
        for flag in ["enabled", "read_threw", "read_exact_guard", "read_root_unchanged", "finish_rejected", "finish_root_unchanged", "finish_receipt_unchanged", "original_outcome_unchanged", "passed"] {
            try checks.equal("T01 reentry " + flag, try reentry.field(flag), .bool(true))
        }
        try checks.equal("T01 actual read guard type", try reentry.field("read_error").field("type"), .string("RuntimeCore.RuntimeInvariant"))
        try checks.equal("T01 actual read guard", try reentry.field("read_error").field("description"), .string("tool read called twice"))
        for key in ["first_root_snapshot", "after_read_snapshot", "after_finish_snapshot"] { try checks.equal("T01 entire original root unchanged " + key, try reentry.field(key), root) }
        try checks.equal("T01 second receipt remains first", try reentry.field("finish_outcome").field("receipt"), receipt)
        try checks.equal("T01 second outcome stays outside Core", try reentry.field("second_outcome_submitted_to_core"), .bool(false))
        try checks.equal("T01 exact repeated finish outcome", try reentry.field("finish_outcome").field("result"),
            .object(["kind": .string("infrastructure"), "code": .string("ToolInfrastructure"), "detail": .string("tool finish repeated")]))
    } else { try checks.equal("no unintended reentry", try control.field("reentry").field("enabled"), .bool(false)) }
    if [.t01, .t02, .t03, .t04, .t05, .t14].contains(f) {
        reference = try ntReference(file.path, token: f.token, checks: &checks)
        let classification = f == .t03 ? "added" : f == .t04 ? "missing" : f == .t05 ? "out-of-range" : "ordinary"
        try checks.equal("independent full-file domain", try reference.field("classification"), .string(classification))
        if ntExit(f) == 0 {
            for key in ["symbol_utf8_base64", "symbol_bytes", "symbol_sha256", "digest_words_decimal", "argument_token"] {
                try checks.equal("independent symbol " + key, try receipt.field(key), try reference.field(key))
            }
            try checks.equal("independent complete ToolInfo", try result.field("value"), try reference.field("tool_info"))
        }
    }
    if f == .t14 {
        let work = try control.field("cpu_budget_work")
        let input = Data((0..<65_536).map { UInt8($0 & 255) }), seed = Data("SRC1B-T14-original-tool-budget-cpu-work-v1".utf8)
        try checks.equal("fixed CPU hash algorithm", try work.field("algorithm"), .string("previous=SHA256(fixed_input_bytes||previous); initial_previous=seed"))
        try checks.equal("actual CPU clock domain", try work.field("clock"), .string("swift-dispatch-uptime-ns"))
        try checks.equal("fixed CPU input bytes", try work.field("input_base64"), .string(input.base64EncodedString()))
        try checks.equal("fixed CPU input count", try work.field("input_bytes"), .integer(input.count))
        try checks.equal("fixed CPU input hash", try work.field("input_sha256"), .string(ntSHA(input)))
        try checks.equal("fixed CPU seed", try work.field("seed_base64"), .string(seed.base64EncodedString()))
        let a = try ntUnsigned(work.field("started_ns"), "native CPU start"), b = try ntUnsigned(work.field("finished_ns"), "native CPU finish")
        let iterations = try ntUnsigned(work.field("iterations"), "native CPU iterations")
        try checks.truth("real CPU prefix exceeds original budget", iterations > 0 && started <= a && a <= b && b - a >= 2_000_000_000)
        try checks.equal("real CPU elapsed arithmetic", try work.field("elapsed_ns"), .string(String(b - a)))
        try checks.equal("native minimum exactly 2s", try work.field("minimum_ns"), .string("2000000000"))
        try checks.equal("original final gate sees expiry", try receipt.field("within_work_budget"), .bool(false))
        let opened = try ntOne(ntEvents(rows, "tool.open-call", key: "event"), "original open after CPU work")
        try checks.truth("CPU prefix precedes real original open", b <= ntUnsigned(opened.field("capture_ns"), "open after CPU"))
        for key in ["elapsed_at_least_minimum", "additional_native_cpu_work"] { try checks.equal(key, try work.field(key), .bool(true)) }
        for key in ["io_latency_claimed", "sleep_used", "extra_task_created"] { try checks.equal(key, try work.field(key), .bool(false)) }
        var previous = seed
        for index in 0..<iterations {
            if index % 1024 == 0 { try hostRequire(deadline.remaining > 2, "independent CPU digest oracle within outer remaining budget") }
            var hasher = SHA256(); hasher.update(data: input); hasher.update(data: previous); previous = Data(hasher.finalize())
        }
        try checks.equal("independent CPU work digest", try work.field("final_digest_hex"), .string(previous.map { String(format: "%02x", $0) }.joined()))
    } else { try checks.equal("no additional CPU work", try control.field("cpu_budget_work"), .null) }
}

private func ntCompositionChecks(_ f: NativeToolBoundaryCase, report: ExecutionReport, final: JSONValue,
                                 control: JSONValue, file: NativeToolFile, deadline: HostDeadline,
                                 reference: inout JSONValue, checks: inout NativeToolChecks) throws {
    try checks.equal("original parent exit", .integer(Int(report.exitCode)), .integer(ntExit(f)))
    try checks.equal("immutable original parent primary", try final.field("primary_report"), report.json)
    let trial = try ntOne(report.json.field("trials").elements(), "parent primary trial")
    let events = try trial.field("events").elements(), ledger = try trial.field("ledger")
    let registry = try final.field("registry"), host = try registry.field("host_root"), response = try registry.field("response")
    let service = try registry.field("invocation"), parentLate = try final.field("parent_late_closure")
    let child = try host.field("primary_report"), childLate = try host.field("late_runtime_observation")
    let childTrial = try ntOne(child.field("trials").elements(), "child primary trial"), childLedger = try childTrial.field("ledger")
    let childEvents = try childTrial.field("events").elements(), lateLedger = try parentLate.field("late_ledger_observation")
    try checks.equal("original child success", try host.field("runtime").field("primary_exit_code"), .integer(0))
    try checks.equal("child primary immutable after actual join", try childLate.field("report"), child)
    try checks.equal("parent primary immutable after actual join", try parentLate.field("report"), report.json)
    try checks.equal("parent host domain", try ledger.field("host_composition").field("execution_domain"), .string("hostParent"))
    try checks.equal("parent has no direct backend", try ledger.field("backend_session"), .null)
    try checks.equal("child leaf domain", try childLedger.field("host_composition").field("execution_domain"), .string("mlxLeaf"))
    try checks.equal("original full B8", try service.field("tokens"), .array(ntTokens.map(JSONValue.unsigned)))
    let requested = ntEvents(childEvents, "mlx.eval_requested"), receipts = ntEvents(childEvents, "mlx.receipt_accepted")
    let acks = ntEvents(childEvents, "backend-return.ack_observed")
    for (name, list) in [("actual EVAL", requested), ("actual receipt", receipts), ("actual ACK", acks)] { try checks.equal(name + " population", .integer(list.count), .integer(2)) }
    try checks.equal("B request full tokens", try requested[1].field("details").field("tokens"), .array(ntTokens.map(JSONValue.unsigned)))
    let bReceipt = try receipts[1].field("details")
    try checks.equal("actual CPU B token", try bReceipt.field("worker_frame").field("token_id"), .unsigned(64288))
    let aTokens = try requested[0].field("details").field("tokens").elements().map { value -> UInt64 in
        guard let n = UInt64(try value.integerToken()) else { throw hostFault("src1b-native-token", "actual A Word") }; return n
    }
    try checks.equal("actual A population", .integer(aTokens.count), .integer(32))
    try checks.truth("actual A bounded tokens", aTokens.allSatisfy { $0 < 151936 })
    try checks.equal("actual A CPU sum", try receipts[0].field("details").field("worker_frame").field("token_id"), .unsigned(aTokens.reduce(0, +) % 151936))
    for index in 0..<2 {
        let ack = try acks[index].field("details"), receipt = try receipts[index].field("details")
        try checks.equal("ACK original receipt \(index)", try ack.field("request").field("eval_receipt"), receipt)
        let layers = try ack.field("worker_frame").field("layers").elements(), before = try receipt.field("worker_frame").field("layers").elements()
        try checks.equal("ACK 28 layers \(index)", .integer(layers.count), .integer(28))
        try checks.equal("receipt 28 layers \(index)", .integer(before.count), .integer(28))
        for layer in 0..<28 {
            try checks.equal("ACK index \(index)/\(layer)", try layers[layer].field("layer"), .integer(layer))
            try checks.equal("ACK actual before \(index)/\(layer)", try layers[layer].field("before"), before[layer])
            try checks.equal("ACK actual pages \(index)/\(layer)", try layers[layer].field("released_pages"), try before[layer].field("owned_pages"))
        }
    }
    try checks.equal("two backend returns safe", .integer(ntEvents(childEvents, "backend-return.safe").count), .integer(2))
    let childMain = try ntOne(Array(childLedger.field("J").members().values).filter { ntField($0, "role") == .string("main") }, "child main")
    try checks.equal("original child Unit publication", try childMain.field("published"), .object(["status": .string("ok"), "value": .object(["kind": .string("Unit")])]))
    try checks.equal("actual response delivered", try response.field("phase"), .string("success-delivered"))
    try checks.equal("actual source consumed response", try response.field("parent_consumed"), .bool(true))
    let responseEvidence = try response.field("evidence")
    try checks.equal("same actual response token", try responseEvidence.field("token"), .unsigned(64288))
    try checks.equal("same actual response receipt", try responseEvidence.field("receipt"), bReceipt)
    try checks.equal("same actual child publication", try responseEvidence.field("publication"), try childMain.field("publication_identity"))
    try checks.equal("same actual child Unit outcome", try responseEvidence.field("source_outcome"), try childMain.field("published"))
    try checks.equal("same actual child response intent", try responseEvidence.field("intent"), try childLedger.field("external_response_intent"))
    try checks.equal("same actual child ledger", try responseEvidence.field("child_ledger"), try childLedger.field("host_composition").field("ledger_id"))
    try checks.equal("same actual service identity", try responseEvidence.field("service_id"), try service.field("service_id"))
    try checks.equal("requester delivered original token", try host.field("demand").field("delivered"), .object(["status": .string("success"), "token": .unsigned(64288)]))
    let responseRows = try response.field("events").elements()
    for name in ["service.response-commit", "service.response-delivered", "service.parent.consumed"] { try checks.equal(name + " once", .integer(ntEvents(responseRows, name, key: "event").count), .integer(1)) }
    try checks.equal("one real parent wait path", .integer(ntEvents(responseRows, "service.parent.cached", key: "event").count + ntEvents(responseRows, "service.parent.registered", key: "event").count), .integer(1))
    let restored = try ntOne(ntEvents(events, "service.wait-restored"), "original holder restore").field("details").field("kont")
    try checks.equal("holder actually returned", try restored.field("owner"), .string("returned"))
    try checks.equal("holder not held", try restored.field("held"), .bool(false))
    try checks.equal("complete nine-field holder", .array(try restored.field("saved").members().keys.sorted().map(JSONValue.string)),
        .array(["environment", "name_env", "caller", "frames", "suspended", "pending", "evaluations", "next_pending", "next_evaluation"].sorted().map(JSONValue.string)))
    let received = try ntOne(ntEvents(events, "service.source-received"), "actual source consume")
    let registered = try ntOne(ntEvents(events, "tool.registered"), "original tool registration")
    try checks.before("real source consumption precedes literal tool", received, registered)
    let invocation = try control.field("tool_invocation")
    try checks.equal("same original tool registration", try registered.field("details"), invocation)
    try checks.equal("original parent caller", try invocation.field("caller"), try service.field("parent_caller"))
    try checks.equal("original parent scope", try invocation.field("scope").field("scope"), try service.field("parent_scope"))
    try ntToolChecks(f, control: control, file: file, response: response, parentLedger: lateLedger,
        events: events, deadline: deadline, reference: &reference, checks: &checks)
    for (name, closure) in [("parent", parentLate), ("child", childLate)] {
        try checks.equal(name + " actual original handles joined", try closure.field("all_original_handles_joined"), .bool(true))
        try checks.equal(name + " admitted original startup", try closure.field("startup_admitted"), .bool(true))
        try checks.equal(name + " creation settled", try closure.field("creation_settled"), .bool(true))
        for role in ["main", "observer", "watchdog"] {
            try checks.equal(name + " " + role + " created", try closure.field("original_handle_population").field(role + "_created"), .bool(true))
            try checks.equal(name + " " + role + " joined", try closure.field("original_handle_population").field(role + "_joined"), .bool(true))
        }
        for (job, row) in try closure.field("late_ledger_observation").field("J").members() {
            try checks.equal(name + " source Task.value " + job, try row.field("actual_task_exit_observed"), .bool(true))
        }
    }
    try checks.equal("child resources actually closed", try childLate.field("resource_closed"), .bool(true))
    try checks.equal("parent resources distinguish Unknown", try parentLate.field("resource_closed"), .bool(f != .t12))
    try checks.equal("registry observer actual join", try final.field("registry_final_closure").field("observer_joined"), .bool(true))
    try checks.equal("registry work/timer actual join", try final.field("registry_final_closure").field("all_created_handles_joined"), .bool(true))
    try checks.equal("child registry closes independently of parent ToolIO", try final.field("registry_final_closure").field("resource_closed"), .bool(true))
    try checks.equal("original requester actual join", try host.field("requester_observation").field("actual_joined"), .bool(true))
    let closure = try registry.field("closure"), completion = try closure.field("completion")
    for key in ["work_joined", "timer_joined", "resource_closed"] { try checks.equal("service closure " + key, try closure.field(key), .bool(true)) }
    try checks.equal("no startup substitute", try completion.field("startup_abort"), .null)
    try checks.equal("valid completion", try completion.field("valid"), .bool(true))
    try checks.equal("no host failures", try completion.field("failures"), .array([]))
    let resources = try completion.field("resources").elements()
    for row in resources {
        if try row.field("created").boolean() {
            try checks.equal("actual created resource closed", try row.field("closed"), .bool(true))
            try checks.truth("actual created resource joined if applicable", try row.field("joined") != .bool(false))
        }
    }
    for (kind, count) in [("context", 1), ("permit-router", 1), ("return-service", 1), ("worker-process", 1), ("worker-stdin", 1), ("worker-stdout", 1), ("worker-stderr", 1), ("pipe-end", 6)] {
        try checks.equal("real resource population " + kind, .integer(resources.filter { ntField($0, "kind") == .string(kind) }.count), .integer(count))
    }
    try checks.truth("actual archive/output files present", resources.contains { ntField($0, "kind") == .string("output-file") })
    let backend = try childLate.field("backend_evidence")
    try checks.equal("completion uses original backend evidence", try completion.field("backend"), backend)
    for flag in ["reaped", "stdout_joined", "stderr_joined", "stdin_closed", "permit_joined"] { try checks.equal("actual worker " + flag, try backend.field(flag), .bool(true)) }
    try checks.equal("actual worker exit0", try backend.field("actual_exit"), .integer(0))
    let worker = try ntOne(host.field("context").field("workers").elements(), "original CPU worker")
    try checks.equal("actual original worker shutdown ACK", try worker.field("shutdown_ack"), .bool(true))
    try checks.equal("two original worker eval claims", try worker.field("eval_claims"), .integer(2))
    try checks.equal("two original worker releases", try worker.field("completed_releases"), .integer(2))
    for flag in ["process_root", "stdout_root", "stderr_root", "stdin_root"] { try checks.equal("worker closed " + flag, try worker.field(flag), .bool(false)) }
    for flag in ["stdout_eof", "stderr_eof", "permit_forwarded"] { try checks.equal("worker original " + flag, try worker.field(flag), .bool(true)) }
    let main = try ntOne(Array(lateLedger.field("J").members().values).filter { ntField($0, "role") == .string("main") }, "late parent main")
    let scopeID = try service.field("parent_scope").text(), scope = try lateLedger.field("S").field(scopeID)
    if ntExit(f) == 2 {
        try checks.equal("infrastructure leaves parent unpublished", try main.field("published"), .null)
        try checks.equal("no normal source finalization", .integer(ntEvents(events, "scope.finalize").count), .integer(0))
        try checks.truth("infrastructure never marks source scope joined", try scope.field("state") != .string("Joined"))
        try checks.equal("original infrastructure report", try trial.field("outcome").field("status"), .string("infrastructure-error"))
        let expected = f == .t11 ? "ToolAuthorityMismatch; original root retained" : "ToolCloseUnknown"
        try checks.equal("precise original infrastructure report cause", try trial.field("outcome").field("message"), .string(expected))
        try checks.equal("late HostClose is separate", try lateLedger.field("host_composition").field("service_closure"), .null)
        try checks.equal("late independent child closure retained", try lateLedger.field("host_composition").field("late_service_closure"), closure)
    } else {
        let closed = try ntOne(ntEvents(events, "service.host-close-safe"), "HostClose safe")
        let finalized = try ntOne(ntEvents(events, "scope.finalize"), "parent scope finalize")
        let published = try ntOne(ntEvents(events, "result.published"), "parent result publication")
        try checks.before("HostClose before source scope finalization", closed, finalized)
        try checks.before("source scope before actual publication", finalized, published)
        try checks.before("original tool return before HostClose", try ntOne(ntEvents(events, "tool.original-return"), "original tool return"), closed)
        try checks.equal("actual source scope joined", try scope.field("state"), .string("Joined"))
        try checks.equal("main publication equals original reported outcome", try main.field("published"), try trial.field("outcome"))
        if ntExit(f) == 0 {
            try checks.equal("parent actual result retains B token and independent ToolInfo", try main.field("published"),
                .object(["status": .string("ok"), "value": .object(["kind": .string("Pair"),
                    "left": .object(["kind": .string("Word"), "value": .string("64288")]), "right": try reference.field("tool_info")])]))
        } else {
            try checks.equal("actual source fault publication", try main.field("published").field("status"), .string("fault"))
            let faults = try ledger.field("faults").members()
            try checks.equal("one precise original source tool fault", .integer(faults.values.filter { ntField($0, "code") == .string(ntCode(f)!) }.count), .integer(1))
        }
    }
}

func runParentNativeToolControls(boundaryCase f: NativeToolBoundaryCase, sourcesDirectory: URL,
        evidenceDirectory: URL, inputsPath: String, session: String, vocabularyPath: URL,
        deadline: HostDeadline, evidence: HostEvidence, frontendHost: CPUFrontendHost,
        entryRoots: ParentEntryRoots) async throws -> HostWorkResult {
    try ntDirectory(evidenceDirectory)
    var checks = NativeToolChecks(), firstError: (any Error)?, failure: JSONValue = .null, cleanup: [JSONValue] = []
    var root: ParentCompositionRoot?, native: ParentNativeToolControl?, caller: NativeToolCaller?
    var parent: ExecutionReport?, final: JSONValue = .null, reference: JSONValue = .null, fileRecord: JSONValue = .null
    do {
        let manifestBytes = try readHostSource(sourcesDirectory.appendingPathComponent("manifest.json"))
        try manifestBytes.write(to: evidenceDirectory.appendingPathComponent("source-manifest.json"), options: .withoutOverwriting)
        let manifest = try ExactJSON.decode(manifestBytes)
        try checks.equal("native source manifest", try manifest.field("schema"), .string("src1b-parent-native-tool-sources-v1"))
        try checks.equal("fixed 15 source population", .integer(try manifest.field("sources").elements().count), .integer(15))
        try checks.equal("fixed 14 case population", .integer(try manifest.field("cases").elements().count), .integer(14))
        let caseNames = (1...14).map { String(format: "B-T%02d", $0) }
        try checks.equal("fixed ordered native case identities", .array(try manifest.field("cases").elements().map { try $0.field("case_id") }), .array(caseNames.map(JSONValue.string)))
        try checks.equal("fixed ordered source identities", .array(try manifest.field("sources").elements().map { try $0.field("name") }),
            .array((caseNames.map { "parent-" + $0 } + ["child-unit_after_b"]).map(JSONValue.string)))
        let selected = try ntOne(manifest.field("cases").elements().filter { ntField($0, "case_id") == .string(f.rawValue) }, "selected native case")
        let parentName = "parent-" + f.rawValue, childName = "child-unit_after_b"
        try checks.equal("selected fixed parent", try selected.field("parent"), .string(parentName))
        try checks.equal("selected fixed child", try selected.field("child"), .string(childName))
        try checks.equal("selected fixed literal", try selected.field("tool_argument_decimal"), .string(String(f.token)))
        try checks.equal("selected genuine CPU success worker", try selected.field("worker_scenario"), .string("success"))
        try checks.equal("selected fixed parent exit", try selected.field("expected_parent_exit"), .integer(ntExit(f)))
        try checks.equal("selected precise tool code", try selected.field("expected_tool_code"), ntCode(f).map(JSONValue.string) ?? .null)
        try checks.equal("literal does not claim response dataflow", try selected.field("claims_response_token_as_tool_argument"), .bool(false))
        try saveHostJSON(selected, to: evidenceDirectory.appendingPathComponent("source-selection.json"))
        let (parentBytes, parentSource) = try ntSource(parentName, domain: "hostParent", manifest: manifest, directory: sourcesDirectory, checks: &checks)
        let (childBytes, childSource) = try ntSource(childName, domain: "mlxLeaf", manifest: manifest, directory: sourcesDirectory, checks: &checks)
        let source = try ExactJSON.decode(parentBytes), tool = try ntOne(ntCommands(source, named: "call_tool"), "checked source tool")
        try checks.equal("actual source tool literal", try tool.field("argument"), .object(["value": .string("word"), "literal": .unsigned(f.token)]))
        _ = try ntOne(ntCommands(source, named: "call_service"), "original source service")
        let parentDir = evidenceDirectory.appendingPathComponent("parent-compiler"), childDir = evidenceDirectory.appendingPathComponent("child-compiler")
        try ntDirectory(parentDir); try ntDirectory(childDir)
        let parentCompiler = CompilerBridge(evidenceDirectory: parentDir, cpuHost: frontendHost)
        let parentProgram = try await parentCompiler.checkParentSource(parentBytes)
        evidence.put("parent_compiler", parentCompiler.provenance)
        let childCompiler = CompilerBridge(evidenceDirectory: childDir, cpuHost: frontendHost)
        let childProgram = try await childCompiler.checkClosedSource(childBytes)
        evidence.put("child_compiler", childCompiler.provenance)
        for (name, program, record, provenance) in [("parent", parentProgram, parentSource, parentCompiler.provenance), ("child", childProgram, childSource, childCompiler.provenance)] {
            try checks.equal(name + " actual frontend exit", try provenance.field("exit_code"), .integer(0))
            try checks.equal(name + " actual frontend termination", try provenance.field("termination_observed"), .bool(true))
            try checks.equal(name + " actual frontend IO closed", try provenance.field("streams_closed"), .bool(true))
            try checks.equal(name + " actual frontend unchanged", try provenance.field("inputs_unchanged"), .bool(true))
            try checks.equal(name + " checked source byte identity", .string(program.sourceBytesSHA256), try record.field("sha256"))
            try checks.equal(name + " checked canonical identity", .string(program.canonicalSourceSHA256), try record.field("canonical_sha256"))
        }
        let file = try ntFile(f, directory: evidenceDirectory, vocabularyPath: vocabularyPath, checks: &checks); fileRecord = file.record
        let budget = try deadline.coreBudget()
        let leaf = try ChildLeafDescriptor(program: childProgram, name: "B", session: session,
            outputDirectory: evidenceDirectory.appendingPathComponent("child-host"), watchdogSeconds: budget)
        let control = ParentNativeToolControl(case: f); native = control; evidence.retain("parent_native_tool_control", control)
        let composition = try ParentCompositionRoot(program: parentProgram, leaf: leaf, inputsPath: inputsPath,
            worker: .nativeCPU(scenario: "success"), vocabulary: file.capability,
            parentWatchdogSeconds: budget, hostBudgetSeconds: 50, nativeTool: control)
        root = composition; try entryRoots.install(composition); evidence.retain("parent_composition", composition)
        let work = NativeToolCaller(); caller = work; evidence.retain("parent_execute_caller", work)
        work.start { try await composition.runForTesting() }
        try await ntWaitCaller(work, deadline: deadline)
        let original = try await work.join(); parent = original
        try saveHostJSON(original.json, to: evidenceDirectory.appendingPathComponent("parent-primary.json"))
        final = try await composition.joinOriginalHandles()
        try saveHostJSON(final, to: evidenceDirectory.appendingPathComponent("original-closures.json"))
        try saveHostJSON(final.field("registry").field("host_root").field("primary_report"), to: evidenceDirectory.appendingPathComponent("child-primary.json"))
        try saveHostJSON(control.snapshot(), to: evidenceDirectory.appendingPathComponent("native-tool-observation.json"))
        try ntCompositionChecks(f, report: original, final: final, control: control.snapshot(), file: file,
            deadline: deadline, reference: &reference, checks: &checks)
        if let count = file.actualBytes, let hash = file.actualSHA {
            let after = try Data(contentsOf: file.path)
            try checks.equal("original file bytes unchanged", .integer(after.count), .integer(count))
            try checks.equal("original file SHA unchanged", .string(ntSHA(after)), .string(hash))
        } else if f == .t06 { try checks.truth("failed open path remains absent", !FileManager.default.fileExists(atPath: file.path.path)) }
        try checks.equal("original caller Task.value joined", try work.snapshot().field("actual_joined"), .bool(true))
        try checks.truth("oracle completes inside outer remaining budget", deadline.remaining > 0)
    } catch {
        firstError = error
        failure = .object(["type": .string(String(reflecting: type(of: error))), "description": .string(String(describing: error))])
        var prefix: [String: JSONValue] = [:]
        prefix["first_failure"] = failure; prefix["checks"] = .array(checks.rows)
        prefix["parent_primary"] = parent?.json ?? .null; prefix["final"] = final
        prefix["composition"] = root?.snapshot() ?? .null; prefix["native_control"] = native?.snapshot() ?? .null
        prefix["caller"] = caller?.snapshot() ?? .null; prefix["file"] = fileRecord; prefix["host_evidence"] = evidence.snapshot()
        prefix["independent_tool_reference"] = reference
        do { try saveHostJSON(.object(prefix), to: evidenceDirectory.appendingPathComponent("first-failure-before-cleanup.json")) }
        catch { cleanup.append(.string("save first failure prefix: " + String(describing: error))) }
        if final == .null, let root {
            root.requestAbort(.runtimeFailure)
            if let caller {
                do {
                    try await ntWaitCaller(caller, deadline: deadline)
                    let original = try await caller.join(); if parent == nil { parent = original }
                    final = try await root.joinOriginalHandles()
                } catch { cleanup.append(.string("original cleanup observation: " + String(describing: error))) }
            }
        }
    }
    var fields: [String: JSONValue] = [:]
    fields["trial_id"] = .string(f.rawValue); fields["case_id"] = .string(f.rawValue)
    fields["passed"] = .bool(firstError == nil && cleanup.isEmpty)
    fields["first_failure"] = failure; fields["cleanup_errors"] = .array(cleanup); fields["checks"] = .array(checks.rows)
    fields["parent_primary"] = parent?.json ?? .null; fields["original_parent_exit"] = parent.map { .integer(Int($0.exitCode)) } ?? .null
    fields["final"] = final; fields["native_control"] = native?.snapshot() ?? .null; fields["caller"] = caller?.snapshot() ?? .null
    fields["file"] = fileRecord; fields["independent_tool_reference"] = reference
    fields["model_execution"] = .bool(false); fields["performance_claimed"] = .bool(false)
    fields["physical_close_failure_claimed"] = .bool(false)
    fields["scope"] = .string("fixed real-file native boundary on original checked parent/child and CPU worker; literal argument, not B-token dataflow; independent actual review required")
    let trial = JSONValue.object(fields)
    do { try saveHostJSON(trial, to: evidenceDirectory.appendingPathComponent("trial.json")) }
    catch { if let firstError { throw firstError }; throw error }
    if let firstError { throw firstError }
    try hostRequire(cleanup.isEmpty, "native boundary original cleanup failed")
    return HostWorkResult(status: "passed", exitCode: 0, value: trial, trials: [trial])
}
