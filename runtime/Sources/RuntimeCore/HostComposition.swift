import Foundation
import Dispatch
import Synchronization

package enum CheckedExecutionDomain: String, Sendable { case hostParent, mlxLeaf }
package enum ServiceAbortReason: String, Sendable { case parentStopped, hostDeadline, creationAborted, runtimeFailure }
package enum ServiceCreationPhase: String, Sendable {
    case validated, reserved, createdGated = "created-gated", committed
    case contextActivated = "context-activated", workerCreated = "worker-created"
    case requesterCreated = "requester-created", coreCreated = "core-created", started
}
/// Only the separate native CPU entry can install these ordering/fault hooks.
/// They run on the original parent task and grant no source or close authority.
package protocol ParentServiceCPUControl:Sendable {
    func beforeResponseWait(serviceID:String)async
    func afterResponseConsumed(serviceID:String)throws
}

package struct ChildLeafDescriptor: Sendable {
    package let program: CheckedProgram, name: String, session: String
    package let outputDirectory: URL, watchdogSeconds: Int
    package init(program: CheckedProgram, name: String, session: String,
                 outputDirectory: URL, watchdogSeconds: Int = 50) throws {
        try runtimeRequire(program.executionDomain == .mlxLeaf && name == "B", "fixed checked leaf service required")
        try runtimeRequire(UUID(uuidString:session) != nil && !session.isEmpty, "service session UUID")
        try runtimeRequire(outputDirectory.isFileURL && outputDirectory.path.hasPrefix("/") && (1...180).contains(watchdogSeconds), "service output/budget")
        try validateExternalDemandConfiguration(program,configured:true)
        self.program=program;self.name=name;self.session=session
        self.outputDirectory=outputDirectory;self.watchdogSeconds=watchdogSeconds
    }
    package var diagnostic:JSONValue { .object([
        "name":.string(name),"session":.string(session),"output_directory":.string(outputDirectory.path),
        "watchdog_seconds":.integer(watchdogSeconds),"profile":.string("src1-response-offer-owner1-v1"),
        "source_bytes_sha256":.string(program.sourceBytesSHA256),
        "canonical_source_sha256":.string(program.canonicalSourceSHA256),
        "compiler_response_sha256":.string(program.compilerResponseSHA256)]) }
}

package final class ServiceInvocation: Sendable {
    package let serviceID:String, session:String, tokens:[UInt64], requestBytes:Data, requestSHA256:String
    package let leaf:ChildLeafDescriptor
    let parentLedger:String, parentCaller:Caller, parentScope:String, sourceSite:String
    init(serviceID:String,parentLedger:String,caller:Caller,scope:ScopeHandle,site:String,
         tokens:[UInt64],leaf:ChildLeafDescriptor)throws {
        try runtimeRequire(tokens.count == 8 && tokens.allSatisfy{$0 < 151_936},"service source Tokens8 range")
        self.serviceID=serviceID;self.parentLedger=parentLedger;parentCaller=caller;parentScope=scope.id;sourceSite=site
        self.tokens=tokens;self.leaf=leaf;session=leaf.session
        let bytes=try JSONValue.object(["schema":.string("ebd1-request-v1"),"session":.string(leaf.session),
            "request_id":.string("B"),"port":.string("B"),"operation":.string("Prefill8"),
            "tokens":.array(tokens.map(JSONValue.unsigned))]).encoded()
        requestBytes=bytes;requestSHA256=digest(bytes)
    }
    package func validateStart(_ permit:ServiceStartPermit,response:ServiceResponsePort)throws {
        try runtimeRequire(permit.invocation === self && response.invocation === self,"foreign service start permit/response port")
    }
    package var diagnostic:JSONValue { .object([
        "service_id":.string(serviceID),"session":.string(session),"parent_ledger":.string(parentLedger),
        "parent_caller":parentCaller.json,"parent_scope":.string(parentScope),"source_site":.string(sourceSite),
        "tokens":.array(tokens.map(JSONValue.unsigned)),"request_bytes":.integer(requestBytes.count),
        "request_sha256":.string(requestSHA256),"leaf":leaf.diagnostic]) }
}

package final class ServiceStartPermit: Sendable {
    let invocation:ServiceInvocation
    package let originalWorkIdentity:String
    init(_ invocation:ServiceInvocation,originalWorkIdentity:String){self.invocation=invocation;self.originalWorkIdentity=originalWorkIdentity}
    package var serviceID:String {invocation.serviceID}
}

final class ChildResponseEvidence: Sendable {
    let invocation:ServiceInvocation, publication:PublishedOutcome, receipt:MLXHostReceipt
    let intent:JSONValue, boundary:String, token:UInt64, childLedger:String
    init(invocation:ServiceInvocation,publication:PublishedOutcome,receipt:MLXHostReceipt,
         intent:JSONValue,boundary:String,childLedger:String)throws {
        self.invocation=invocation;self.publication=publication;self.receipt=receipt
        self.intent=intent;self.boundary=boundary;self.childLedger=childLedger;token=try receipt.token
    }
    var diagnostic:JSONValue {.object(["service_id":.string(invocation.serviceID),"child_ledger":.string(childLedger),
        "publication":.string(publication.id),"source_outcome":publication.outcome.json,
        "receipt":receipt.json,"intent":intent,"safe_boundary":.string(boundary),"token":.unsigned(token)])}
}
struct ServiceResponseFailure:Error,Sendable {
    let code:String, original:JSONValue
    var originalIDs:[String]=[]
    var json:JSONValue {.object(["code":.string(code),"original":original,"original_ids":.array(originalIDs.map(JSONValue.string))])}
}

/// One winner. The requester observes Reserved, the parent observes Delivered.
/// No callback executes while this mutex is held and no cell awaits its consumer.
package final class ServiceResponsePort:Sendable {
    private enum Winner { case pending, reserved(ChildResponseEvidence), delivered(ChildResponseEvidence), failed(ServiceResponseFailure) }
    private typealias Reply=Result<ChildResponseEvidence,ServiceResponseFailure>
    private struct State {
        var winner:Winner = .pending
        var requesterWaiter:CheckedContinuation<Reply,Never>?,parentWaiter:CheckedContinuation<Reply,Never>?
        var requesterClaimed=false,parentClaimed=false,parentConsumed=false
        var demandControlID:String?,runtimeSeal:RuntimeExecutionSeal?,childLedger:String?,runtimeAdmitted=false
        var rows:[JSONValue]=[],lateFailures:[JSONValue]=[]
    }
    let invocation:ServiceInvocation
    private let state=Mutex(State())
    init(_ invocation:ServiceInvocation){self.invocation=invocation}
    var pending:Bool {state.withLock {s in if case .pending=s.winner{return true};return false}}
    var noSuccessfulReservation:Bool {state.withLock{s in switch s.winner{case .pending,.failed:return true;case .reserved,.delivered:return false}}}
    var runtimeSeal:RuntimeExecutionSeal? {state.withLock{$0.runtimeSeal}}
    func claimDemandControl(_ id:String)throws {try state.withLock{s in try runtimeRequire(s.demandControlID == nil,"service demand control created twice");s.demandControlID=id}}
    func bindRuntime(controlID:String,root:RuntimeExecutionRoot,ledger:String)throws {
        try state.withLock{s in
            try runtimeRequire(s.demandControlID == controlID && s.runtimeSeal == nil,"foreign/duplicate child runtime seal")
            guard case .pending=s.winner else {throw RuntimeInvariant("child runtime binding after response failure")}
            s.runtimeSeal=root.seal;s.childLedger=ledger;s.rows.append(row("service.runtime-bound",.object(["root":.string(root.id),"child_ledger":.string(ledger)])))
        }
    }
    func admitRuntime(controlID:String,root:RuntimeExecutionRoot)throws {
        try state.withLock{s in
            try runtimeRequire(s.demandControlID == controlID && s.runtimeSeal === root.seal && !s.runtimeAdmitted,"foreign/duplicate service runtime admission")
            guard case .pending=s.winner else {throw RuntimeInvariant("service abort won before runtime admission")}
            try root.admitStartup();s.runtimeAdmitted=true;s.rows.append(row("service.runtime-admitted",.string(root.id)))
        }
    }
    package var serviceID:String {invocation.serviceID}
    private func row(_ event:String,_ detail:JSONValue = .null)->JSONValue {
        .object(["event":.string(event),"capture_ns":.string(String(DispatchTime.now().uptimeNanoseconds)),"details":detail])
    }
    func reserve(_ evidence:ChildResponseEvidence)throws {
        var waiter:CheckedContinuation<Reply,Never>?
        try state.withLock {s in
            try runtimeRequire(evidence.invocation === invocation,"foreign service commit evidence")
            try runtimeRequire(s.runtimeSeal != nil && s.runtimeAdmitted && s.childLedger == evidence.childLedger,"service evidence from foreign/unadmitted child ledger")
            guard case .pending=s.winner else {throw RuntimeInvariant("service response already decided")}
            s.winner = .reserved(evidence);waiter=s.requesterWaiter;s.requesterWaiter=nil
            s.rows.append(row("service.response-commit",evidence.diagnostic))
        }
        waiter?.resume(returning:.success(evidence))
    }
    @discardableResult func abort(_ failure:ServiceResponseFailure)->Bool {
        var request:CheckedContinuation<Reply,Never>?,parent:CheckedContinuation<Reply,Never>?
        let won=state.withLock {s in
            guard case .pending=s.winner else {s.lateFailures.append(failure.json);return false}
            s.winner = .failed(failure);request=s.requesterWaiter;parent=s.parentWaiter
            s.requesterWaiter=nil;s.parentWaiter=nil;s.rows.append(row("service.response-failed",failure.json));return true
        }
        request?.resume(returning:.failure(failure));parent?.resume(returning:.failure(failure));return won
    }
    func awaitRequester()async->Result<ChildResponseEvidence,ServiceResponseFailure> {
        await withCheckedContinuation {c in
            let immediate:Reply?=state.withLock {s in
                guard !s.requesterClaimed else {return .failure(.init(code:"ServiceRepeatedRequester",original:.null))}
                s.requesterClaimed=true
                switch s.winner {
                case .pending:s.requesterWaiter=c;s.rows.append(row("service.requester.registered"));return nil
                case .reserved(let e),.delivered(let e):return .success(e)
                case .failed(let f):return .failure(f)
                }
            }
            if let immediate {c.resume(returning:immediate)}
        }
    }
    func delivered(_ evidence:ChildResponseEvidence)throws {
        var waiter:CheckedContinuation<Reply,Never>?
        try state.withLock {s in
            guard case .reserved(let original)=s.winner,original === evidence else {throw RuntimeInvariant("foreign or duplicate actual requester delivery")}
            s.winner = .delivered(evidence);waiter=s.parentWaiter;s.parentWaiter=nil
            s.rows.append(row("service.response-delivered",evidence.diagnostic))
        }
        waiter?.resume(returning:.success(evidence))
    }
    func awaitParent()async->Result<ChildResponseEvidence,ServiceResponseFailure> {
        await withCheckedContinuation {c in
            let immediate:Reply?=state.withLock {s in
                guard !s.parentClaimed else {return .failure(.init(code:"ServiceRepeatedConsume",original:.null))}
                s.parentClaimed=true
                switch s.winner {
                case .pending,.reserved:s.parentWaiter=c;s.rows.append(row("service.parent.registered"));return nil
                case .delivered(let e):s.rows.append(row("service.parent.cached"));return .success(e)
                case .failed(let f):return .failure(f)
                }
            }
            if let immediate {c.resume(returning:immediate)}
        }
    }
    func consumed(_ evidence:ChildResponseEvidence)throws {
        try state.withLock {s in
            guard case .delivered(let original)=s.winner,original === evidence,s.parentClaimed,!s.parentConsumed else {throw RuntimeInvariant("service parent response consume identity")}
            s.parentConsumed=true;s.rows.append(row("service.parent.consumed",evidence.diagnostic))
        }
    }
    package var diagnostic:JSONValue {state.withLock {s in
        let phase:String,evidence:JSONValue
        switch s.winner {
        case .pending:phase="pending";evidence = .null
        case .reserved(let e):phase="success-reserved";evidence=e.diagnostic
        case .delivered(let e):phase="success-delivered";evidence=e.diagnostic
        case .failed(let f):phase="failure";evidence=f.json
        }
        return .object(["service_id":.string(serviceID),"phase":.string(phase),"evidence":evidence,
            "demand_control_id":s.demandControlID.map(JSONValue.string) ?? .null,"runtime_root_id":s.runtimeSeal.map{.string($0.id)} ?? .null,"runtime_admitted":.bool(s.runtimeAdmitted),"child_ledger":s.childLedger.map(JSONValue.string) ?? .null,
            "requester_claimed":.bool(s.requesterClaimed),"parent_claimed":.bool(s.parentClaimed),"parent_consumed":.bool(s.parentConsumed),
            "requester_waiter":.bool(s.requesterWaiter != nil),"parent_waiter":.bool(s.parentWaiter != nil),
            "events":.array(s.rows),"late_failures":.array(s.lateFailures)])
    }}
}

package protocol ServiceHostFactory:Sendable {
    func prepare(_ invocation:ServiceInvocation)throws -> any PreparedServiceHost
}
package protocol PreparedServiceHost:Sendable {
    func run(_ permit:ServiceStartPermit,response:ServiceResponsePort)async -> ServiceHostCompletion
    func requestAbort(_ reason:ServiceAbortReason)
    func snapshot()->JSONValue
}
package enum HostResourceKind:String,Sendable {
    case context,permitRouter="permit-router",returnService="return-service",workerProcess="worker-process"
    case workerStdin="worker-stdin",workerStdout="worker-stdout",workerStderr="worker-stderr",pipeEnd="pipe-end",outputFile="output-file"
}
package struct HostResourceObservation:Sendable {
    package let kind:HostResourceKind,id:String,created:Bool,started:Bool,closed:Bool,joined:Bool?,originalEvidence:JSONValue
    package init(kind:HostResourceKind,id:String,created:Bool,started:Bool,closed:Bool,joined:Bool?,originalEvidence:JSONValue)throws {
        try runtimeRequire(!id.isEmpty && (created || (!started && joined != true)),"host resource observation population")
        self.kind=kind;self.id=id;self.created=created;self.started=started;self.closed=closed;self.joined=joined;self.originalEvidence=originalEvidence
    }
    var complete:Bool {!created || (closed && joined != false)}
    package var diagnostic:JSONValue {.object(["kind":.string(kind.rawValue),"id":.string(id),"created":.bool(created),
        "started":.bool(started),"closed":.bool(closed),"joined":joined.map(JSONValue.bool) ?? .null,"original_evidence":originalEvidence])}
}
package struct HostServiceFailure:Sendable {
    package let domain:String,code:String,id:String,original:JSONValue
    package init(domain:String,code:String,id:String,original:JSONValue) {
        self.domain=domain;self.code=code;self.id=id;self.original=original
    }
    package var diagnostic:JSONValue {.object(["domain":.string(domain),"code":.string(code),"id":.string(id),"original":original])}
}
package struct ServiceStartupAbortObservation:Sendable {
    package let stage:ServiceCreationPhase,resources:[HostResourceObservation],cause:HostServiceFailure,originalEvidence:JSONValue
    package init(stage:ServiceCreationPhase,resources:[HostResourceObservation],cause:HostServiceFailure,originalEvidence:JSONValue)throws {
        try runtimeRequire(Set(resources.map{$0.kind.rawValue+":"+$0.id}).count == resources.count,"duplicate startup resource")
        self.stage=stage;self.resources=resources;self.cause=cause;self.originalEvidence=originalEvidence
    }
    package var diagnostic:JSONValue {.object(["stage":.string(stage.rawValue),"resources":.array(resources.map(\.diagnostic)),"cause":cause.diagnostic,"original_evidence":originalEvidence])}
}
package struct ServiceHostCompletion:Sendable {
    let valid:Bool,originalEvidence:JSONValue
    let runtime:RuntimeClosureObservation?,requester:RequesterJoinObservation?,backend:MLXClosedEvidence?
    let startupAbort:ServiceStartupAbortObservation?,resources:[HostResourceObservation],failures:[HostServiceFailure]
    package init(runtime:RuntimeClosureObservation?,requester:RequesterJoinObservation?,backend:MLXClosedEvidence?,
                 startupAbort:ServiceStartupAbortObservation?,resources:[HostResourceObservation],failures:[HostServiceFailure])throws {
        try runtimeRequire(backend == nil || startupAbort == nil,"normal close and startup abort cannot alias")
        try runtimeRequire(Set(resources.map{$0.kind.rawValue+":"+$0.id}).count == resources.count,"duplicate host resource")
        self.runtime=runtime;self.requester=requester;self.backend=backend;self.startupAbort=startupAbort;self.resources=resources;self.failures=failures
        valid=true;originalEvidence = .null
    }
    private init(runtime:RuntimeClosureObservation?,requester:RequesterJoinObservation?,backend:MLXClosedEvidence?,resources:[HostResourceObservation],failures:[HostServiceFailure],originalEvidence:JSONValue){
        self.runtime=runtime;self.requester=requester;self.backend=backend;startupAbort=nil;self.resources=resources;self.failures=failures;self.originalEvidence=originalEvidence;valid=false
    }
    package static func incomplete(runtime:RuntimeClosureObservation? = nil,requester:RequesterJoinObservation? = nil,backend:MLXClosedEvidence? = nil,
                                   resources:[HostResourceObservation],failures:[HostServiceFailure],originalEvidence:JSONValue)->Self {
        Self(runtime:runtime,requester:requester,backend:backend,resources:resources,failures:failures,originalEvidence:originalEvidence)
    }
    package var diagnostic:JSONValue {.object(["valid":.bool(valid),"incomplete":.bool(!valid),"original_evidence":originalEvidence,"runtime":runtime?.diagnostic ?? .null,"requester":requester?.diagnostic ?? .null,
        "backend":backend?.json ?? .null,"startup_abort":startupAbort?.diagnostic ?? .null,
        "resources":.array(resources.map(\.diagnostic)),"failures":.array(failures.map(\.diagnostic))])}
}
package struct ParentHostEnvironment:Sendable {
    let registry:ServiceRegistryRoot,tool:any VocabSymbolTool,file:VocabFileCapability
    package init(registry:ServiceRegistryRoot,tool:any VocabSymbolTool,file:VocabFileCapability)throws {
        self.registry=registry;self.tool=tool;self.file=file
    }
    package var diagnostic:JSONValue {.object(["registry":registry.snapshot(),"tool_file":file.diagnostic])}
}
