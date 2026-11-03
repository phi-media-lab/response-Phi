import Foundation
import Synchronization
import Dispatch
import CryptoKit
import Darwin

// JSON carries the original worker evidence without reducing it to pass/fail.
indirect enum JSON: Codable, Sendable, Equatable {
    case object([String: JSON]), array([JSON]), string(String), int(Int64), real(Double), bool(Bool), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Int64.self) { self = .int(v) }
        else if let v = try? c.decode(Double.self) { self = .real(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSON].self) { self = .array(v) }
        else { self = .object(try c.decode([String: JSON].self)) }
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
    subscript(_ key: String) -> JSON { if case .object(let o) = self { return o[key] ?? .null }; return .null }
    var string: String? { if case .string(let s) = self { return s }; return nil }
    var integer: Int? { if case .int(let n) = self { return Int(exactly: n) }; return nil }
    var bool: Bool? { if case .bool(let b) = self { return b }; return nil }
    var array: [JSON]? { if case .array(let a) = self { return a }; return nil }
    var object: [String: JSON]? { if case .object(let o) = self { return o }; return nil }
    static func n(_ n: Int) -> JSON { .int(Int64(n)) }
    static func strings(_ xs: [String]) -> JSON { .array(xs.map(JSON.string)) }
    func data() throws -> Data { let e = JSONEncoder(); e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]; return try e.encode(self) }
    static func parse(_ data: Data) throws -> JSON {
        var scanner = JSONFrameScanner(bytes: Array(data)); return try scanner.check()
    }
    func text() -> String { (try? String(decoding: data(), as: UTF8.self)) ?? "null" }
}

struct BridgeFault: Error, Sendable, Equatable, CustomStringConvertible {
    let id: String
    let code: String
    let detail: String
    init(_ code: String, _ detail: String, id: String = UUID().uuidString) { self.id = id; self.code = code; self.detail = detail }
    var description: String { "\(code)[\(id)]: \(detail)" }
    var json: JSON { .object(["id": .string(id), "code": .string(code), "detail": .string(detail)]) }
}
func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw BridgeFault("invariant", message) }
}
func digest(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }

// Timers never cancel a producer or manufacture a receipt. A timeout resolves
// only the waiting promise; Session separately quarantines its retained roots.
final class Promise<T: Sendable>: Sendable {
    private struct State { var claimed = false; var result: Result<T, BridgeFault>?; var continuations: [CheckedContinuation<T, any Error>] = [] }
    private let state = Mutex(State())
    @discardableResult func resolve(_ result: Result<T, BridgeFault>, beforeResume: (@Sendable () -> Void)? = nil) -> Bool {
        let won = state.withLock { s in
            guard !s.claimed else { return false }; s.claimed = true; return true
        }
        guard won else { return false }
        // Reservation is private: a new wait still registers until quarantine
        // has finished. Recursive fail.resolve loses without locking callbacks.
        beforeResume?()
        let cs = state.withLock { s in
            s.result = result; let cs = s.continuations; s.continuations.removeAll(); return cs
        }
        for c in cs { switch result { case .success(let v): c.resume(returning: v); case .failure(let e): c.resume(throwing: e) } }
        return true
    }
    func wait() async throws -> T {
        try await withCheckedThrowingContinuation { c in
            let result = state.withLock { s -> Result<T, BridgeFault>? in
                if let result = s.result { return result }; s.continuations.append(c); return nil
            }
            if let result { switch result { case .success(let v): c.resume(returning: v); case .failure(let e): c.resume(throwing: e) } }
        }
    }
    func deadline(_ seconds: Double, _ fault: BridgeFault, onTimeout: (@Sendable (BridgeFault) -> Void)? = nil) {
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { [self] in
            resolve(.failure(fault), beforeResume: { onTimeout?(fault) })
        }
    }
}
final class Signal: Sendable {
    private let promise = Promise<Bool>()
    func open() { promise.resolve(.success(true)) }
    func wait(seconds: Double = 30, label: String = "signal") async throws {
        promise.deadline(seconds, BridgeFault("deadline", label)); _ = try await promise.wait()
    }
}

// One stream writer preserves Swift event order. It is independent of all
// producer Tasks and of the worker stdout reader; flush() is a real queue fence.
final class Trace: Sendable {
    private struct State { var seq = 0; var events: [JSON] = [] }
    private let state = Mutex(State())
    private let output = DispatchQueue(label: "smwo1.swift-event-output")
    private let origin = DispatchTime.now().uptimeNanoseconds
    let session: String
    init(_ session: String) { self.session = session }
    func record(_ event: String, _ fields: [String: JSON] = [:]) {
        state.withLock { s in
            s.seq += 1
            var f = fields
            f["v"] = .n(1); if f["kind"] == nil { f["kind"] = .string(event) }; f["event"] = .string(event); f["session"] = .string(session); f["swift_seq"] = .n(s.seq)
            f["swift_ns"] = .int(Int64(DispatchTime.now().uptimeNanoseconds - origin))
            f["clock_origin"] = .string("Swift Trace construction; monotonic uptimeNanoseconds")
            let value = JSON.object(f); s.events.append(value)
            if let data = try? value.data() { output.async { FileHandle.standardOutput.write(data + Data([10])) } }
        }
    }
    func snapshot() -> JSON { state.withLock { .array($0.events) } }
    func flush() async { await withCheckedContinuation { c in output.async { c.resume() } } }
    func save(_ url: URL, extra: [String: JSON]) throws {
        var all = extra; all["events"] = snapshot(); try JSON.object(all).data().write(to: url, options: .atomic)
    }
}

struct LeaseID: Sendable, Equatable {
    let session: String, worker: String, request: String, task: String
    let epoch: Int
    var json: JSON { .object(["session": .string(session), "worker_id": .string(worker), "request": .string(request), "task_id": .string(task), "epoch": .n(epoch)]) }
}
struct HostReceipt: Sendable, Equatable {
    let lease: LeaseID
    let raw: JSON
    var token: Int { raw["token_id"].integer! }
}
enum ProducerOutcome: Sendable, Equatable {
    case success(HostReceipt)
    case failure(LeaseID, BridgeFault)
    var lease: LeaseID { switch self { case .success(let r): r.lease; case .failure(let l, _): l } }
    func get() throws -> HostReceipt { switch self { case .success(let r): return r; case .failure(_, let e): throw e } }
    var json: JSON { switch self {
    case .success(let r): .object(["kind": .string("success"), "lease": r.lease.json, "receipt": r.raw])
    case .failure(let l, let e): .object(["kind": .string("failure"), "lease": l.json, "fault": e.json])
    } }
}
struct Deadlines: Sendable {
    let ready: Double, command: Double, gate: Double
    static let gpu = Deadlines(ready: 60, command: 30, gate: 30)
    // CPU-only pipe fault fixtures; these do not change GPU constants.
    static let cpu = Deadlines(ready: 3, command: 1.5, gate: 3)
}

// JSONDecoder alone accepts repeated object keys and may coerce 1.0 to Int.
// The bounded frame parser keeps numeric spelling categories and rejects
// duplicate decoded keys; Foundation validates string escapes and primitives.
private struct JSONFrameScanner {
    let bytes: [UInt8]
    var index = 0
    mutating func whitespace() { while index < bytes.count && [9, 10, 13, 32].contains(bytes[index]) { index += 1 } }
    mutating func take(_ byte: UInt8) throws { whitespace(); try require(index < bytes.count && bytes[index] == byte, "JSON delimiter"); index += 1 }
    mutating func string() throws -> String {
        whitespace(); let begin = index
        try require(index < bytes.count && bytes[index] == 34, "JSON string"); index += 1
        while index < bytes.count {
            let byte = bytes[index]; index += 1
            if byte == 34 { return try JSONDecoder().decode(String.self, from: Data(bytes[begin..<index])) }
            if byte == 92 { try require(index < bytes.count, "JSON escape"); index += 1 }
            else { try require(byte >= 32, "JSON control character") }
        }
        throw BridgeFault("malformed_frame", "unterminated JSON string")
    }
    mutating func value(_ depth: Int) throws -> JSON {
        whitespace(); try require(depth <= 64 && index < bytes.count, "JSON depth/value")
        switch bytes[index] {
        case 123:
            index += 1; whitespace(); var object: [String: JSON] = [:]
            if index < bytes.count && bytes[index] == 125 { index += 1; return .object(object) }
            while true {
                let key = try string(); try require(object[key] == nil, "duplicate JSON key")
                try take(58); object[key] = try value(depth + 1); whitespace()
                if index < bytes.count && bytes[index] == 125 { index += 1; return .object(object) }
                try take(44)
            }
        case 91:
            index += 1; whitespace(); var array: [JSON] = []
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
            try require(index > begin, "JSON primitive")
            let raw = Data(bytes[begin..<index])
            let decoded = try JSONDecoder().decode(JSON.self, from: raw)
            if bytes[begin] == 45 || (48...57).contains(bytes[begin]) {
                if raw.contains(46) || raw.contains(101) || raw.contains(69) { return .real(try JSONDecoder().decode(Double.self, from: raw)) }
                return .int(try JSONDecoder().decode(Int64.self, from: raw))
            }
            return decoded
        }
    }
    mutating func check() throws -> JSON { let result = try value(0); whitespace(); try require(index == bytes.count, "JSON trailing bytes"); return result }
}
