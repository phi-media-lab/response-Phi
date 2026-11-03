import Foundation
import Dispatch
import Synchronization
import CryptoKit
import Darwin
import RuntimeCore
import MLXTransport

enum HostMode: String, Sendable {
    case checked, deterministic, reference, cpu
    case externalCPU = "external-cpu"
    case joinedDeliveryCPU = "joined-delivery-cpu"
    case sourceResponseCPU = "source-response-cpu"
    var checksSource: Bool { self == .checked || self == .deterministic }
    var isCPU: Bool { self == .cpu || self == .externalCPU || self == .joinedDeliveryCPU || self == .sourceResponseCPU }
}

struct HostOptions: Sendable {
    let mode: HostMode, inputs: String, session: String
    let returnConversion: HRCReturnConversionMode
    let returnService: ReturnServicePolicy, competitionTasks: Int
    let externalRequest: ExternalDemandRequest?, demandPolicy: ExternalDemandPolicy?
    let externalResultDelivery: ExternalResultDeliveryPolicy
    let deliveryFixture: String?
    let responseFixture: String?
    let directory: URL, source: URL?, sourcesDirectory: URL?
    init(_ args: [String], mode: HostMode) throws {
        var fields: [String: String] = [:]
        let specific = mode.checksSource ? "--source" : mode.isCPU ? "--sources-dir" : nil
        var allowed: Set<String> = ["--inputs", "--session", "--pgid", "--output-dir", "--return-conversion", "--return-service", "--competition-tasks"]
        if let specific { allowed.insert(specific) }
        if mode == .joinedDeliveryCPU { allowed.insert("--delivery-fixture") }
        if mode == .sourceResponseCPU { allowed.insert("--response-fixture") }
        let required = allowed
        if mode == .checked { allowed.formUnion(["--external-request", "--demand-policy", "--external-result-delivery"]) }
        try hostRequire(args.count.isMultiple(of: 2), "arguments require key/value pairs")
        for i in stride(from: 0, to: args.count, by: 2) {
            try hostRequire(allowed.contains(args[i]) && fields[args[i]] == nil && !args[i + 1].isEmpty,
                            "unknown, duplicate or empty option")
            fields[args[i]] = args[i + 1]
        }
        let supplied = Set(fields.keys)
        try hostRequire(required.isSubset(of: supplied), "all fixed entry options are required")
        let demandEnabled = fields["--external-request"] != nil
        try hostRequire(demandEnabled == (fields["--demand-policy"] != nil), "external-request and demand-policy are a pair")
        guard let delivery = ExternalResultDeliveryPolicy(rawValue: fields["--external-result-delivery"] ?? "after-backend-close") else {
            throw hostFault("usage", "external-result-delivery must be after-backend-close or after-source-join")
        }
        try hostRequire(fields["--external-result-delivery"] == nil || demandEnabled,
                        "explicit result delivery requires an external request")
        if mode == .joinedDeliveryCPU {
            try hostRequire(["J1", "J2", "J3", "J4", "J5", "J6", "J7"].contains(fields["--delivery-fixture"]!),
                            "unknown fixed joined delivery CPU fixture")
        }
        self.externalResultDelivery = delivery
        self.deliveryFixture = fields["--delivery-fixture"]
        if mode == .sourceResponseCPU {
            try hostRequire((1...15).map({ String(format: "S%02d", $0) }).contains(fields["--response-fixture"]!),
                            "unknown fixed source response CPU fixture")
        }
        self.responseFixture = fields["--response-fixture"]
        guard let returnConversion = HRCReturnConversionMode(rawValue: fields["--return-conversion"]!) else {
            throw hostFault("usage", "return-conversion must be original or candidate")
        }
        guard let returnService = ReturnServicePolicy(rawValue: fields["--return-service"]!),
              ["0", "8"].contains(fields["--competition-tasks"]!),
              let competitionTasks = Int(fields["--competition-tasks"]!) else {
            throw hostFault("usage", "return-service must be unchanged or raise-main; competition-tasks must be 0 or 8")
        }
        try hostRequire(returnConversion == .original, "RSP1 fixes original return-conversion")
        try hostRequire(mode != .reference || (returnService == .unchanged && competitionTasks == 0),
                        "native reference permits only unchanged/0/original")
        try hostRequire(mode != .externalCPU || (returnService == .unchanged && competitionTasks == 0),
                        "external CPU fixes legacy unchanged/0; demand policies belong to its native fixtures")
        try hostRequire(mode != .joinedDeliveryCPU || (returnService == .unchanged && competitionTasks == 0),
                        "joined delivery CPU fixes unchanged/0/original")
        try hostRequire(mode != .sourceResponseCPU || (returnService == .unchanged && competitionTasks == 0),
                        "source response CPU fixes unchanged/0/original")
        guard #available(macOS 26.0, *) else { throw hostFault("unsupported-os", "RSP1 requires macOS 26 Task escalation API") }
        guard let pgid = Int32(fields["--pgid"]!), pgid >= 0,
              UUID(uuidString: fields["--session"]!) != nil else { throw hostFault("usage", "invalid PGID or session UUID") }
        let expected = pgid == 0 ? getpid() : pgid
        try hostRequire(getpgrp() == getpid() && getpgrp() == expected, "entry must be the wrapper's actual group leader")
        if demandEnabled {
            guard let policy = ExternalDemandPolicy(rawValue: fields["--demand-policy"]!) else {
                throw hostFault("usage", "demand-policy must be unchanged or raise-main")
            }
            try hostRequire(mode == .checked && returnService == .unchanged,
                            "EBD1 requires checked entry and unchanged legacy return-service")
            self.externalRequest = try readExternalDemandRequest(fields["--external-request"]!, session: fields["--session"]!)
            self.demandPolicy = policy
        } else { self.externalRequest = nil; self.demandPolicy = nil }
        self.mode = mode; self.inputs = fields["--inputs"]!; self.session = fields["--session"]!
        self.returnConversion = returnConversion; self.returnService = returnService; self.competitionTasks = competitionTasks
        self.directory = URL(fileURLWithPath: fields["--output-dir"]!).standardizedFileURL
        self.source = fields["--source"].map { URL(fileURLWithPath: $0).standardizedFileURL }
        self.sourcesDirectory = fields["--sources-dir"].map { URL(fileURLWithPath: $0).standardizedFileURL }
        try FileManager.default.createDirectory(at: directory.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard Darwin.mkdir(directory.path, 0o700) == 0 else { throw hostFault("output-create", "exclusive output directory failed, errno \(errno)") }
    }
}

func hostFault(_ code: String, _ message: String) -> MLXBackendFailure {
    MLXBackendFailure(faultID: UUID().uuidString, code: code,
        raw: .object(["origin": .string("trusted-host-entry"), "message": .string(message)]))
}
func hostRequire(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
    if try !condition() { throw hostFault("host-invariant", message) }
}
func saveHostJSON(_ value: JSONValue, to path: URL) throws {
    try (value.encoded() + Data([10])).write(to: path, options: .withoutOverwriting)
}
func hostFileIdentity(_ path: URL) throws -> JSONValue {
    let data = try Data(contentsOf: path)
    return .object(["path": .string(path.path), "bytes": .integer(data.count),
                    "sha256": .string(SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined())])
}
func readHostSource(_ path: URL) throws -> Data {
    let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
    try hostRequire(attributes[.type] as? FileAttributeType == .typeRegular, "source must be a regular file")
    let handle = try FileHandle(forReadingFrom: path)
    defer { try? handle.close() }
    var bytes = Data()
    while bytes.count <= CompilerBridge.sourceLimit {
        let request = min(65_536, CompilerBridge.sourceLimit + 1 - bytes.count)
        guard let chunk = try handle.read(upToCount: request), !chunk.isEmpty else { return bytes }
        bytes.append(chunk)
    }
    throw FrontendRejection(status: "execution-profile-not-supported",
        diagnostic: CompilerBridge.diagnostic("source-limit", "source exceeds 1 MiB"), exitCode: 3)
}

/// A trusted host start gate. It does not carry a source capability or a
/// second Machine continuation. The saved actual Task is installed before open.
final class HostStartGate: Sendable {
    private struct State { var open = false; var waiters: [CheckedContinuation<Void, Never>] = [] }
    private let state = Mutex(State())
    func wait() async {
        await withCheckedContinuation { continuation in
            let ready = state.withLock { s -> Bool in
                if s.open { return true }; s.waiters.append(continuation); return false
            }
            if ready { continuation.resume() }
        }
    }
    func open() {
        let waiters = state.withLock { s -> [CheckedContinuation<Void, Never>] in
            guard !s.open else { return [] }; s.open = true
            let found = s.waiters; s.waiters.removeAll(); return found
        }
        for waiter in waiters { waiter.resume() }
    }
}

struct HostWorkResult: Sendable {
    let status: String, exitCode: Int32, value: JSONValue
    let trials: [JSONValue]
    init(status: String, exitCode: Int32, value: JSONValue, trials: [JSONValue] = []) {
        self.status = status; self.exitCode = exitCode; self.value = value; self.trials = trials
    }
}
enum HostCompletion: Sendable {
    case work(Result<HostWorkResult, any Error>)
    case deadline(MLXBackendFailure)
}

/// Reserve the unique result before applying timeout quarantine, then expose
/// it to the original main waiter. A late work result cannot overwrite it.
final class HostResultCell: Sendable {
    private struct State {
        var claimed = false; var result: HostCompletion?
        var waiter: CheckedContinuation<HostCompletion, Never>?
    }
    private let state = Mutex(State())
    func reserve() -> Bool {
        state.withLock { s in if s.claimed { return false }; s.claimed = true; return true }
    }
    func publish(_ value: HostCompletion) {
        let waiter = state.withLock { s -> CheckedContinuation<HostCompletion, Never>? in
            precondition(s.claimed && s.result == nil)
            s.result = value; let found = s.waiter; s.waiter = nil; return found
        }
        waiter?.resume(returning: value)
    }
    func resolve(_ value: HostCompletion) { if reserve() { publish(value) } }
    func wait() async -> HostCompletion {
        await withCheckedContinuation { continuation in
            let value = state.withLock { s -> HostCompletion? in
                if let result = s.result { return result }
                precondition(s.waiter == nil); s.waiter = continuation; return nil
            }
            if let value { continuation.resume(returning: value) }
        }
    }
}

/// Diagnostic snapshots never replace the actual saved Task handles.
protocol HostRetainedRoot: AnyObject, Sendable { func snapshot() -> JSONValue }

final class HostEvidence: Sendable {
    private struct State {
        var values: [String: JSONValue] = [:]
        var work: Task<Void, Never>?, returned = false, joined = false
        var retained: [String: any HostRetainedRoot] = [:]
    }
    private let state = Mutex(State())
    func put(_ key: String, _ value: JSONValue) { state.withLock { $0.values[key] = value } }
    func retain(_ key: String, _ root: any HostRetainedRoot) {
        state.withLock { precondition($0.retained[key] == nil); $0.retained[key] = root }
    }
    func install(_ task: Task<Void, Never>) { state.withLock { precondition($0.work == nil); $0.work = task } }
    func returned() { state.withLock { $0.returned = true } }
    func join() async {
        let task = state.withLock { $0.work }
        precondition(task != nil)
        await task!.value
        state.withLock { $0.joined = true; $0.work = nil }
    }
    func snapshot() -> JSONValue {
        let observed = state.withLock { s in (s.values, s.returned, s.joined, s.work != nil, s.retained) }
        return .object(["observations": .object(observed.0),
            "top_level_work": .object(["about_to_return_observed": .bool(observed.1),
                "actual_task_joined": .bool(observed.2), "retained_handle": .bool(observed.3)]),
            "retained_roots": .object(observed.4.mapValues { $0.snapshot() })])
    }
}

struct HostDeadline: Sendable {
    let origin: UInt64, seconds: Int
    var remaining: Double {
        max(0, Double(seconds) - Double(DispatchTime.now().uptimeNanoseconds - origin) / 1_000_000_000)
    }
    func coreBudget() throws -> Int {
        let budget = min(180, Int(remaining.rounded(.down)) - 5)
        try hostRequire(budget > 0, "not enough declared total budget to start Core and save its report")
        return budget
    }
}
