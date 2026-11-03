import Foundation
import Synchronization
import Dispatch
import CryptoKit
import Darwin
import RuntimeCore

// WireJSON carries the original worker evidence without reducing it to pass/fail.
indirect enum WireJSON: Codable, Sendable, Equatable {
    case object([String: WireJSON]), array([WireJSON]), string(String), int(Int64), real(Double), bool(Bool), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Int64.self) { self = .int(v) }
        else if let v = try? c.decode(Double.self) { self = .real(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([WireJSON].self) { self = .array(v) }
        else { self = .object(try c.decode([String: WireJSON].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .int(let v): try c.encode(v)
        case .real(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    subscript(_ key: String) -> WireJSON { if case .object(let o) = self { return o[key] ?? .null }; return .null }
    var string: String? { if case .string(let s) = self { return s }; return nil }
    var integer: Int? { if case .int(let n) = self { return Int(exactly: n) }; return nil }
    var bool: Bool? { if case .bool(let b) = self { return b }; return nil }
    var array: [WireJSON]? { if case .array(let a) = self { return a }; return nil }
    var object: [String: WireJSON]? { if case .object(let o) = self { return o }; return nil }
    static func n(_ n: Int) -> WireJSON { .int(Int64(n)) }
    static func strings(_ xs: [String]) -> WireJSON { .array(xs.map(WireJSON.string)) }
    func data() throws -> Data { let e = JSONEncoder(); e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]; return try e.encode(self) }
    static func parse(_ data: Data) throws -> WireJSON {
        var scanner = JSONFrameScanner(bytes: Array(data)); return try scanner.check()
    }
    func text() -> String { (try? String(decoding: data(), as: UTF8.self)) ?? "null" }
    var core: JSONValue {
        switch self {
        case .object(let fields): .object(fields.mapValues(\.core))
        case .array(let values): .array(values.map(\.core))
        case .string(let value): .string(value)
        case .int(let value): .number(String(value))
        case .real(let value): .number(String(value))
        case .bool(let value): .bool(value)
        case .null: .null
        }
    }
    static func fromCore(_ value: JSONValue) throws -> WireJSON { try parse(value.encoded()) }
}

struct TransportFault: Error, Sendable, Equatable, CustomStringConvertible {
    let id: String
    let code: String
    let detail: String
    init(_ code: String, _ detail: String, id: String = UUID().uuidString) { self.id = id; self.code = code; self.detail = detail }
    var description: String { "\(code)[\(id)]: \(detail)" }
    var json: WireJSON { .object(["id": .string(id), "code": .string(code), "detail": .string(detail)]) }
}
func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TransportFault("invariant", message) }
}
func transportDigest(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }

// Timers never cancel a producer or manufacture a receipt. A timeout resolves
// only the waiting promise; Session separately quarantines its retained roots.
struct BRSObservedPromise: Sendable {
    let sink: BRSServiceTrace
    let key: JSONValue, origin: JSONValue
    let promiseID: String
    func record(_ event: String, wait: String, domain: String, stamp: BRSServiceStamp? = nil,
                result: String? = nil) {
        sink.record(event, domain: domain, stamp: stamp, origin: origin, key: key,
            details: .object(["promise_id": .string(promiseID), "wait_instance": .string(wait),
                "result": result.map(JSONValue.string) ?? .null,
                "execution_binding_reason": .string("transport-has-no-source-Caller"),
                "queue": .null, "queue_reason": .string("not-observed-at-Promise-boundary")]))
    }
}
final class TransportPromise<T: Sendable>: Sendable {
    private struct Waiting { let continuation: CheckedContinuation<T, any Error>; let observationID: String }
    private struct State { var claimed = false; var result: Result<T, TransportFault>?; var continuations: [Waiting] = [] }
    private let state = Mutex(State())
    private let observation: BRSObservedPromise?
    init(observation: BRSObservedPromise? = nil) { self.observation = observation }
    func reserve() -> Bool { state.withLock { s in
        guard !s.claimed else { return false }; s.claimed = true; return true
    } }
    func publishReserved(_ result: Result<T, TransportFault>) {
        let cs = state.withLock { s in
            precondition(s.claimed)
            guard case .none = s.result else { preconditionFailure("reserved promise published twice") }
            s.result = result; let cs = s.continuations; s.continuations.removeAll(); return cs
        }
        let domain = withUnsafeCurrentTask { $0 == nil ? "non_task" : "swift_task" }
        let label: String
        switch result { case .success: label = "success"; case .failure: label = "failure" }
        for waiting in cs {
            observation?.record("resume.pre", wait: waiting.observationID, domain: domain, result: label)
            switch result {
            case .success(let v): waiting.continuation.resume(returning: v)
            case .failure(let e): waiting.continuation.resume(throwing: e)
            }
            observation?.record("resume.post", wait: waiting.observationID, domain: domain, result: label)
        }
    }
    @discardableResult func resolve(_ result: Result<T, TransportFault>, beforeResume: (@Sendable () -> Void)? = nil) -> Bool {
        guard reserve() else { return false }
        // Reservation is private: a new wait still registers until quarantine
        // has finished. Recursive fail.resolve loses without locking callbacks.
        beforeResume?()
        publishReserved(result)
        return true
    }
    func wait() async throws -> T {
        let waitID = observation?.sink.nextWaitID() ?? "unobserved"
        do {
            let value: T = try await withCheckedThrowingContinuation { c in
                // Only the point stamp is captured inside the existing mutex.
                // Append occurs outside; concurrent delivery may append first.
                let captured = state.withLock { s -> (Result<T, TransportFault>?, BRSServiceStamp?) in
                    let stamp = observation == nil ? nil : BRSServiceStamp.capture(domain: "swift_task")
                    if let result = s.result { return (result, stamp) }
                    s.continuations.append(Waiting(continuation: c, observationID: waitID)); return (nil, stamp)
                }
                if let result = captured.0 {
                    let label: String
                    switch result { case .success: label = "success"; case .failure: label = "failure" }
                    observation?.record("wait.cached", wait: waitID, domain: "swift_task", stamp: captured.1, result: label)
                    observation?.record("cached.inline.pre", wait: waitID, domain: "swift_task", result: label)
                    switch result { case .success(let v): c.resume(returning: v); case .failure(let e): c.resume(throwing: e) }
                    observation?.record("cached.inline.post", wait: waitID, domain: "swift_task", result: label)
                } else {
                    observation?.record("wait.registered", wait: waitID, domain: "swift_task", stamp: captured.1)
                }
            }
            observation?.record("wait.return", wait: waitID, domain: "swift_task", result: "success")
            return value
        } catch {
            observation?.record("wait.return", wait: waitID, domain: "swift_task", result: "failure")
            throw error
        }
    }
    func deadline(_ seconds: Double, _ fault: TransportFault, onTimeout: (@Sendable (TransportFault) -> Void)? = nil) {
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { [self] in
            resolve(.failure(fault), beforeResume: { onTimeout?(fault) })
        }
    }
}

/// Small fields copied from the original wire command/frame. This is neither
/// another wire validator nor an authority to accept a reply.
func brsWireKey(_ value: WireJSON) -> JSONValue {
    let source = value["source_context"]
    return .object(["session": value["session"].core, "worker_id": value["worker_id"].core,
        "scope": source["scope"].core, "owner": source["owner"].core,
        "source_epoch": source["source_epoch"].core, "backend_return": source["backend_return"].core,
        "command": value["request_id"].core, "source_site": source["site"].core,
        "wire_seq": value["seq"].core, "wire_lease_epoch": value["lease_epoch"].core,
        "event_seq": value["event_seq"].core, "receipt_id": value["receipt_id"].core,
        "wire_kind": value["kind"].core])
}
func brsWireOrigin(_ value: WireJSON) -> JSONValue {
    let source = value["source_context"]
    return .object(Dictionary(uniqueKeysWithValues:
        ["job", "task", "command", "scope", "backend_return", "source_epoch", "owner", "site"]
            .map { ($0, source[$0].core) }))
}
func brsObservedCommand(_ value: WireJSON) -> Bool { ["EVAL", "RELEASE"].contains(value["kind"].string ?? "") }

// One stream writer preserves Swift event order. It is independent of all
// producer Tasks and of the worker stdout reader; flush() is a real queue fence.
final class TransportTrace: Sendable {
    private struct State { var seq = 0; var events: [WireJSON] = [] }
    private let state = Mutex(State())
    private let output = DispatchQueue(label: "cm1.swift-event-output")
    private let origin = DispatchTime.now().uptimeNanoseconds
    let session: String
    init(_ session: String) { self.session = session }
    func record(_ event: String, _ fields: [String: WireJSON] = [:]) {
        state.withLock { s in
            s.seq += 1
            var f = fields
            f["v"] = .n(1); if f["kind"] == nil { f["kind"] = .string(event) }; f["event"] = .string(event); f["session"] = .string(session); f["swift_seq"] = .n(s.seq)
            f["swift_ns"] = .int(Int64(DispatchTime.now().uptimeNanoseconds - origin))
            f["clock_origin"] = .string("Swift TransportTrace construction; monotonic uptimeNanoseconds")
            let value = WireJSON.object(f); s.events.append(value)
            if let data = try? value.data() { output.async { FileHandle.standardOutput.write(data + Data([10])) } }
        }
    }
    func snapshot() -> WireJSON { state.withLock { .array($0.events) } }
    func flush() async { await withCheckedContinuation { c in output.async { c.resume() } } }
    func save(_ url: URL, extra: [String: WireJSON]) throws {
        var all = extra; all["events"] = snapshot(); try WireJSON.object(all).data().write(to: url, options: .atomic)
    }
}

struct WireLease: Sendable, Equatable {
    let session: String, worker: String, request: String, task: String
    let epoch: Int
    var json: WireJSON { .object(["session": .string(session), "worker_id": .string(worker), "request": .string(request), "task_id": .string(task), "epoch": .n(epoch)]) }
}
struct WireReceipt: Sendable, Equatable {
    let lease: WireLease
    let raw: WireJSON
    var token: Int { raw["token_id"].integer! }
}
struct WireRequest: Sendable {
    let name: String, operation: String, task: String, purpose: String, digest: String
    let tokens: [WireJSON], sourceContext: WireJSON, nativeContext: WireJSON
    init(name: String, operation: String, task: String, purpose: String, tokens: [UInt64], sourceContext: WireJSON, nativeContext: WireJSON) throws {
        let count = operation == "Prefill32" ? 32 : operation == "Prefill8" ? 8 : 0
        try require(count > 0 && tokens.count == count && tokens.allSatisfy { $0 < 151936 }, "Prefill operation/token shape/range before submission")
        try require(!name.isEmpty && !task.isEmpty && (purpose == "checked-source" || purpose == "native-reference"), "request identity/domain")
        self.name = name; self.operation = operation; self.task = task; self.purpose = purpose
        self.tokens = tokens.map { .int(Int64($0)) }; self.sourceContext = sourceContext; self.nativeContext = nativeContext
        self.digest = try transportDigest(WireJSON.array(self.tokens).data())
    }
    var context: [String: WireJSON] { ["operation": .string(operation), "purpose": .string(purpose), "source_context": sourceContext, "native_context": nativeContext] }
}
struct TransportInputs: Sendable {
    static let python = "/Users/fbsh/tiny-llm/.venv/bin/python"
    static let a32 = [10711,5043,13037,21429,1682,2473,27011,17659,3184,12082,19196,2000,29909,16727,7135,1328,2916,14309,13802,2389,7986,3072,18156,14010,2036,27194,18628,4156,7415,20764,20659,19203]
    static let b7 = [2127,19010,19287,13098,1724,7344,1626]
    let path: String, sha256: String, raw: WireJSON
    init(_ path: String) throws {
        try require(path.hasPrefix("/"), "absolute input path")
        let data = try Data(contentsOf: URL(fileURLWithPath: path)); let raw = try WireJSON.parse(data)
        try require(raw["schema"].string == "cm1-inputs-v1" && raw["first_run_evaluations"].integer == 2 && raw["requests"] == .null, "CM1 input schema/fixed first-run metadata")
        try require(raw["python"].string == Self.python && raw["tiny_root"].string == "/Users/fbsh/tiny-llm", "fixed worker Python/reference paths")
        let snapshot = "173234aa840d113125e9f2271100ddbaf16c9620"
        try require(raw["model_snapshot"].string == snapshot && raw["model"].string == "/Users/fbsh/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B-MLX-4bit/snapshots/" + snapshot, "fixed cached model")
        try require(raw["layers"].integer == 28 && raw["page_size"].integer == 128 && raw["vocab_size"].integer == 151936, "fixed geometry")
        try require(raw["a32"] == .array(Self.a32.map(WireJSON.n)) && raw["b_prefix7"] == .array(Self.b7.map(WireJSON.n)), "frozen A32/B7 arrays")
        try require(raw["b_last_token_rule"].string == "this-session-actual-A-result", "B eighth token must be produced in this session")
        self.path = path; self.sha256 = transportDigest(data); self.raw = raw
    }
}
struct TransportDeadlines: Sendable {
    let ready: Double, command: Double, gate: Double
    static let gpu = TransportDeadlines(ready: 60, command: 30, gate: 30)
    // CPU-only pipe fault fixtures; these do not change GPU constants.
    static let cpu = TransportDeadlines(ready: 3, command: 1.5, gate: 3)
}

// JSONDecoder alone accepts repeated object keys and may coerce 1.0 to Int.
// The bounded frame parser keeps numeric spelling categories and rejects
// duplicate decoded keys; Foundation validates string escapes and primitives.
private struct JSONFrameScanner {
    let bytes: [UInt8]
    var index = 0
    mutating func whitespace() { while index < bytes.count && [9, 10, 13, 32].contains(bytes[index]) { index += 1 } }
    mutating func take(_ byte: UInt8) throws { whitespace(); try require(index < bytes.count && bytes[index] == byte, "WireJSON delimiter"); index += 1 }
    mutating func string() throws -> String {
        whitespace(); let begin = index
        try require(index < bytes.count && bytes[index] == 34, "WireJSON string"); index += 1
        while index < bytes.count {
            let byte = bytes[index]; index += 1
            if byte == 34 { return try JSONDecoder().decode(String.self, from: Data(bytes[begin..<index])) }
            if byte == 92 { try require(index < bytes.count, "WireJSON escape"); index += 1 }
            else { try require(byte >= 32, "WireJSON control character") }
        }
        throw TransportFault("malformed_frame", "unterminated WireJSON string")
    }
    mutating func value(_ depth: Int) throws -> WireJSON {
        whitespace(); try require(depth <= 64 && index < bytes.count, "WireJSON depth/value")
        switch bytes[index] {
        case 123:
            index += 1; whitespace(); var object: [String: WireJSON] = [:]
            if index < bytes.count && bytes[index] == 125 { index += 1; return .object(object) }
            while true {
                let key = try string(); try require(object[key] == nil, "duplicate WireJSON key")
                try take(58); object[key] = try value(depth + 1); whitespace()
                if index < bytes.count && bytes[index] == 125 { index += 1; return .object(object) }
                try take(44)
            }
        case 91:
            index += 1; whitespace(); var array: [WireJSON] = []
            if index < bytes.count && bytes[index] == 93 { index += 1; return .array(array) }
            while true {
                array.append(try value(depth + 1)); whitespace()
                if index < bytes.count && bytes[index] == 93 { index += 1; return .array(array) }
                try take(44)
            }
        case 34: return .string(try string())
        default:
            let begin = index
            while index < bytes.count && ![9, 10, 13, 32, 44, 93, 125].contains(bytes[index]) { index += 1 }
            try require(index > begin, "WireJSON primitive")
            let raw = Data(bytes[begin..<index])
            let decoded = try JSONDecoder().decode(WireJSON.self, from: raw)
            if bytes[begin] == 45 || (48...57).contains(bytes[begin]) {
                if raw.contains(46) || raw.contains(101) || raw.contains(69) { return .real(try JSONDecoder().decode(Double.self, from: raw)) }
                return .int(try JSONDecoder().decode(Int64.self, from: raw))
            }
            return decoded
        }
    }
    mutating func check() throws -> WireJSON { let result = try value(0); whitespace(); try require(index == bytes.count, "WireJSON trailing bytes"); return result }
}
