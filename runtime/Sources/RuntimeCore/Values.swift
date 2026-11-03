import Foundation

package struct ExecutionReport: Sendable {
    package var json: JSONValue; package let exitCode: Int32
    package init(json:JSONValue,exitCode:Int32) { self.json=json;self.exitCode=exitCode }
}

enum Witness: Hashable, Sendable {
    case size(UInt64)
    case brand(sort:String,id:String)
    var json: JSONValue {
        switch self {
        case .size(let n): return .object(["sort":.string("Size"),"size":.string(String(n))])
        case .brand(let sort,let id): return .object(["sort":.string(sort),"id":.string(id)])
        }
    }
}
struct NameEnv: Sendable {
    var values:[NominalID:Witness] = [:]
    func witness(_ id: NominalID) throws -> Witness {
        guard let found=values[id] else { throw RuntimeInvariant("missing realized witness \(id.key)") }; return found
    }
    mutating func bind(_ id: NominalID,_ witness: Witness) throws {
        let sort:String
        switch witness { case .size:sort="Size"; case .brand(let s,_):sort=s }
        try runtimeRequire(sort == id.sort,"witness sort mismatch")
        values[id]=witness
    }
    var json: JSONValue { .array(values.keys.sorted { $0.key < $1.key }.map { .object(["static":.string($0.key),"actual":values[$0]!.json]) }) }
}
struct OwnerHandle: Sendable { let id:String }
struct ScopeHandle: Sendable, Equatable { let id:String; let owner:String?; let epoch:String? }
struct ReadHandle: Sendable { let access:String; let owner:String; let epoch:String; let job:String; let region:String }
struct Caller: Sendable, Equatable {
    let job:String; let task:String; let invocation:String; let region:String; let scopes:[String]
    var json:JSONValue { .object(["job":.string(job),"task":.string(task),"invocation":.string(invocation),"region":.string(region),"scopes":.array(scopes.map(JSONValue.string))]) }
}
struct RuntimeInvariant: Error, Sendable, CustomStringConvertible {
    let description:String
    init(_ message:String) { description=message }
}
func runtimeRequire(_ condition: @autoclosure () throws -> Bool,_ message:String) throws {
    if try !condition() { throw RuntimeInvariant(message) }
}
struct SourceFailure: Error, Sendable { let ids:[String] }
struct IncompleteStop: Error, Sendable {}

indirect enum RuntimeValue: Sendable {
    case unit, bool(Bool), nat(UInt64), word(UInt64), errorPayload(String), failure([String])
    case pair(RuntimeValue,RuntimeValue), sum(Bool,RuntimeValue)
    case package(PackageValue), closure(ClosureValue)
    case owner(OwnerHandle), scope(ScopeHandle), read(ReadHandle), shared(String), control(String)
    /// Only immutable descriptive data reaches the event log, never source values.
    var diagnostic:JSONValue {
        switch self {
        case .unit:return .object(["kind":.string("Unit")])
        case .bool(let b):return .object(["kind":.string("Bool"),"value":.bool(b)])
        case .nat(let n):return .object(["kind":.string("Nat"),"value":.string(String(n))])
        case .word(let n):return .object(["kind":.string("Word"),"value":.string(String(n))])
        case .errorPayload(let c):return .object(["kind":.string("ErrorPayload"),"code":.string(c)])
        case .failure(let ids):return .object(["kind":.string("Failure"),"ids":.array(ids.map(JSONValue.string))])
        case .pair(let a,let b):return .object(["kind":.string("Pair"),"left":a.diagnostic,"right":b.diagnostic])
        case .sum(let left,let value):return .object(["kind":.string(left ? "Inl":"Inr"),"value":value.diagnostic])
        case .package(let p):return .object(["kind":.string("Package"),"identity":.string(p.id),"witnesses":.array(p.witnesses.map(\.json)),"payload":p.payload.diagnostic])
        case .closure(let c):return .object(["kind":.string("Closure"),"identity":.string(c.id),"site":.string(c.site),"mode":.string(c.mode)])
        case .owner(let o):return .object(["kind":.string("Owner"),"owner":.string(o.id)])
        case .scope(let s):return .object(["kind":.string("Scope"),"scope":.string(s.id),"owner":s.owner.map(JSONValue.string) ?? .null,"epoch":s.epoch.map(JSONValue.string) ?? .null])
        case .read(let r):return .object(["kind":.string("Read"),"access":.string(r.access),"owner":.string(r.owner),"epoch":.string(r.epoch),"job":.string(r.job),"region":.string(r.region)])
        case .shared(let p):return .object(["kind":.string("Shared"),"job":.string(p)])
        case .control(let p):return .object(["kind":.string("Control"),"job":.string(p)])
        }
    }
    var words:[UInt64]? {
        switch self {
        case .word(let n):return [n]
        case .pair(let a,let b):guard let x=a.words,let y=b.words else{return nil};return x+y
        default:return nil
        }
    }
}
final class PackageValue: Sendable {
    let id:String; let witnesses:[Witness]; let payload:RuntimeValue
    init(id:String,witnesses:[Witness],payload:RuntimeValue) { self.id=id;self.witnesses=witnesses;self.payload=payload }
}
struct Slot: Sendable { let type:JSONValue; let value:RuntimeValue; let copyable:Bool }
/// Machine-local mutable environment. It is never shared by two executing Tasks.
struct Environment: Sendable {
    var slots:[SlotID:Slot] = [:]
    var names=NameEnv()
    mutating func install(_ binding:Binding,_ value:RuntimeValue,copyable:Bool) throws {
        try runtimeRequire(slots[binding.id] == nil,"slot installed twice \(binding.id.key)")
        slots[binding.id]=Slot(type:binding.type,value:value,copyable:copyable)
    }
    mutating func take(_ id:SlotID,move:Bool) throws -> RuntimeValue {
        guard let slot=slots[id] else { throw RuntimeInvariant("unavailable source slot \(id.key)") }
        try runtimeRequire(move || slot.copyable,"affine slot copied")
        if move { slots.removeValue(forKey:id) }
        return slot.value
    }
    mutating func remove(_ id:SlotID) { slots.removeValue(forKey:id) }
    var affine:Set<SlotID> { Set(slots.filter { !$0.value.copyable }.keys) }
    mutating func retainFrame(_ plan:FramePlan) throws -> Environment {
        let ids=Set(plan.retained)
        try runtimeRequire(ids.isSubset(of:affine),"frame retains absent or Copy slot")
        var saved=Environment(slots:slots.filter { $0.value.copyable },names:names)
        for id in plan.retained { saved.slots[id]=slots.removeValue(forKey:id) }
        return saved
    }
    mutating func restore(_ saved:Environment) throws {
        for (id,slot) in saved.slots where !slot.copyable {
            try runtimeRequire(slots[id] == nil,"affine slot restored twice")
        }
        for (id,slot) in saved.slots { slots[id]=slot }
        names=saved.names
    }
    var description:JSONValue { .array(slots.keys.sorted { $0.key < $1.key }.map { .object(["binder":.string($0.key),"value":slots[$0]!.value.diagnostic]) }) }
}
/// Captures are immutable; an affine closure has a one-shot call gate.
final class ClosureValue: @unchecked Sendable {
    let id:String; let site:String; let mode:String
    private let lock=NSLock(); private var captured:Environment?
    init(id:String,site:String,mode:String,environment:Environment) { self.id=id;self.site=site;self.mode=mode;captured=environment }
    func enter() throws -> Environment {
        lock.lock();defer{lock.unlock()}
        guard let env=captured else { throw RuntimeInvariant("affine closure called twice") }
        if mode == "A" { captured=nil }
        return env
    }
    /// Only unrestricted immutable captures may be inspected for ordinary
    /// registration. This does not consume an affine closure or create authority.
    func copyCapturesForRegistration() throws -> Environment {
        lock.lock();defer{lock.unlock()}
        guard mode == "U",let env=captured,env.affine.isEmpty else {
            throw RuntimeInvariant("ordinary capture contains affine closure")
        }
        return env
    }
}
enum Outcome: Sendable {
    case ok(RuntimeValue), failure([String]), infrastructure(String)
    var json:JSONValue {
        switch self {
        case .ok(let v):return .object(["status":.string("ok"),"value":v.diagnostic])
        case .failure(let f):return .object(["status":.string("fault"),"faults":.array(f.map(JSONValue.string))])
        case .infrastructure(let e):return .object(["status":.string("infrastructure-error"),"message":.string(e)])
        }
    }
}
struct ActiveFrame: Sendable {
    let id:String; let kind:String; let site:String; let retained:[SlotID]; let caller:Caller
    var json:JSONValue { .object(["id":.string(id),"kind":.string(kind),"site":.string(site),"retained":.array(retained.map { .string($0.key) }),"caller":caller.json]) }
}

/// One-shot async gate ignores cancellation; cancellation never fabricates completion.
actor RuntimeGate {
    private var open=false
    private var waiters:[CheckedContinuation<Void,Never>] = []
    func wait() async { if open{return};await withCheckedContinuation { waiters.append($0) } }
    func release() { guard !open else{return};open=true;let pending=waiters;waiters.removeAll();for c in pending { c.resume() } }
}

/// A diagnostic watchdog wins independently of unstructured workers. No TaskGroup joins on timeout.
actor ReportRace {
    private var result:ExecutionReport?
    private var waiting:CheckedContinuation<ExecutionReport,Never>?
    func offer(_ report:ExecutionReport) -> Bool {
        guard result == nil else{return false};result=report;waiting?.resume(returning:report);waiting=nil;return true
    }
    func value() async -> ExecutionReport {
        if let result{return result}
        return await withCheckedContinuation { waiting=$0 }
    }
}
