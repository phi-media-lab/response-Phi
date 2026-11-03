import Foundation
import Dispatch

private struct OwnerRecord {
    let poolGroup:String
    var word:UInt64?,epoch:String?,ordinal=0,releases=0
}
private struct ScopeRecord {
    let handle:ScopeHandle;let caller:Caller;let parent:Caller
    var admission=true;var state="Open";var local:[String]=[];var runtimeRoots:[String]=[];var children:[String]=[];var joined:Set<String>=[]
    var prefillClaimed=false;var producerJob:String?;var backendReturn:String?
    var creationSite="/main/body"
}
private struct JobRecord {
    let caller:Caller;let exitCaller:Caller;let bodyRegion:String?;let boundary:String;let role:String
    var task:Task<RuntimeTaskExit,Error>?;var cancelObserved=false;var started=false;var retired=false
    var prepared:Outcome?;var outcome:Outcome?;var publication:PublishedOutcome?;var terminal=false;var joins:Set<String>=[];var children:[String]=[]
    var accesses:[String]=[];var frames:[ActiveFrame]=[]
    var actualBodyExit:JSONValue = .null
    var actualTaskExitObserved=false
    var resultType:JSONValue = .null,latentEffect:JSONValue = .null
}
private struct AccessRecord {
    let job:String;let owner:String;let epoch:String;let region:String;let mode:String
    var state="Dormant";var commands:[String]=[];var replies:Set<String>=[]
}
private struct CommandRecord {let access:String;let job:String;let kind:String;let caller:Caller;var completed=false;var consumed=false;var reply:JSONValue?}

/// Host input holds the same complete Machine continuation used by source waits,
/// but is not a publication/Job and grants no device or resource authority.
private struct HostInputWaitRecord {
    let id:String,port:String,owner:String,site:String;let caller:Caller
    var kont:WaitContinuation?
    var phase="waiting",returned=false,received=false,rejected=false
    var tokens:[UInt64]?,history:JSONValue = .null
    var json:JSONValue { .object([
        "id":.string(id),"port":.string(port),"owner":.string(owner),"site":.string(site),"caller":caller.json,
        "phase":.string(phase),"returned":.bool(returned),"received":.bool(received),"rejected":.bool(rejected),
        "tokens":tokens.map{.array($0.map(JSONValue.unsigned))} ?? .null,
        "kont":kont?.description ?? history]) }
}

/// A source offer is immutable data, not a publication, waiter or resource
/// discharge. Its receipt is the original fully adopted B receipt.
private struct SourceResponseIntent:Sendable {
    let id:String,port:String,owner:String,receive:String,site:String
    let token:UInt64,caller:Caller,externalRequest:ExternalDemandRequest
    let request:MLXEvalRequest,receipt:MLXHostReceipt
    var json:JSONValue {.object([
        "id":.string(id),"port":.string(port),"owner":.string(owner),"receive":.string(receive),
        "site":.string(site),"caller":caller.json,"token":.unsigned(token),
        "external_request":externalRequest.json,"request":request.json,"receipt":receipt.json])}
}

/// Expected response-contract failure is separate from the source publication
/// and from backend failure. It must not skip production backend shutdown.
private struct SourceResponseContractFailure:Error,Sendable,CustomStringConvertible {
    let publication:String,outcome:Outcome
    var description:String {"ResponseContractFailure: MissingResponseIntent"}
    var json:JSONValue {.object(["kind":.string("ResponseContractFailure"),
        "code":.string("MissingResponseIntent"),"publication":.string(publication),
        "source_outcome":outcome.json,"message":.string(description)])}
}

/// Serial source authority and obligations; transport retains its own IO roots.
actor Ledger {
    nonisolated let serviceObservation: BRSServiceTrace
    nonisolated let serviceSession: String
    nonisolated let serviceWorker: String
    nonisolated let returnService: ReturnServiceControl?
    nonisolated let externalDemand: ExternalDemandControl?
    private let start=DispatchTime.now().uptimeNanoseconds
    private var nextID:UInt64=0
    private var owners:[String:OwnerRecord]=[:],scopes:[String:ScopeRecord]=[:]
    private var jobs:[String:JobRecord]=[:],accesses:[String:AccessRecord]=[:]
    private var commands:[String:CommandRecord]=[:],regions:[String:JSONValue]=[:]
    private var obligations:[String:String]=[:],edges:[String:Set<String>]=[:]
    private var roots:Set<String>=[],faults:[String:JSONValue]=[:]
    private var sourceRoots:[String:Machine]=[:],registrations:[String:RegistrationRecord]=[:]
    private var events:[JSONValue]=[];private var finalState="active"
    private let program:CheckedProgram;private let backend:(any MLXBackendDriver)?;private let watchdogSeconds:Int
    private let nativeOwnsBackend:Bool
    nonisolated let ledgerID=UUID().uuidString
    private let parentHost:ParentHostEnvironment?
    private let parentCPU:(any ParentServiceCPUControl)?
    private var serviceCreationAttempted=false
    private var rejectedServiceWaits:[JSONValue]=[]
    private var serviceEntry:ServiceEntry?,serviceConsumed:ChildResponseEvidence?
    private var serviceWait:WaitContinuation?,serviceWaitHistory:JSONValue = .null
    private var serviceWaitID:String?,serviceWaitCaller:Caller?,serviceWaitSite:String?
    private var serviceWaitReturned=false,serviceWaitReceived=false,serviceWaitRejected=false,serviceWaitStopped=false
    private var serviceClosure:ServiceClosureObservation?
    private var lateServiceClosure:ServiceClosureObservation?
    private var toolRoots:[String:ToolReadRoot]=[:],toolOutcomes:[String:ToolCallOutcome]=[:]
    private var lateBackendEvidence:MLXClosedEvidence?,lateBackendValidated=false
    private let externalResultDelivery:ExternalResultDeliveryPolicy
    private let forkPolicy:MLXForkTestPolicy,deterministic:ForkDeterministicBinding?
    private let phaseControl:(any MLXForkCPUPhaseControl)?
    private let producerHeld=ForkMilestone(),producerOpen=ForkMilestone(),waiterAttached=ForkMilestone(),ordinaryStartOpen=ForkMilestone()
    private var policyProducer:String?,policyWaiter:String?,policyWaiterScope:String?,policyWait:String?
    private var mlxCommands:[String:MLXCommandRecord]=[:],backendReturns:[String:BackendReturnRecord]=[:]
    private var lastWireSequence:UInt64=0,lastWireLease:UInt64=0
    private var pageEpochs:[String:UInt64]=[:]
    private var taskExitObservations:[String:JSONValue]=[:]
    private var waits:[String:WaitRecord]=[:],waitTaskObservations:[String:JSONValue]=[:]
    private var hostInputWaits:[String:HostInputWaitRecord]=[:]
    private var externalABound=false,externalInputClaimed=false,externalBRegistered=false
    private var externalBRequest:MLXEvalRequest?,externalBReceipt:MLXHostReceipt?
    private var externalResponseIntent:SourceResponseIntent?
    private var terminalEvidence:[String:[JSONValue]]=[:]
    private var publicationWaits=PublicationWaitProtocol()
    private var pendingPublicationResumes:[String:PublicationResume]=[:]
    private var failedPublicationInstalls:[String:CheckedContinuation<PublicationWake,Never>]=[:]
    private var drainSourceSnapshots:[String:JSONValue]=[:]
    private var unpublishedSourceSnapshots:[String:JSONValue]=[:]
    private var hostMainJoined=false,backendClosed=false
    private var hostFinishStarted=false
    private var sourceJoinSafePublication:PublishedOutcome?
    private var sourceJoinSafeBoundarySequence:String?
    private var externalResponseAttempted=false
    private var externalResponseState="not-attempted"
    private var externalResponsePublication:String?
    private var externalResponseFailure:JSONValue = .null
    private var hostJoinObserver:Task<Void,Never>?
    private var mainStartGate:RuntimeGate?
    private var hostObserverBodyReturning=false,hostObserverJoined=false
    private var lastReport:(status:String,outcome:Outcome,exit:Int32)?
    private var backendCloseEvidence:JSONValue = .null,backendFailure:JSONValue = .null
    init(program:CheckedProgram,backend:(any MLXBackendDriver)?,watchdogSeconds:Int,nativeOwnsBackend:Bool,
         forkPolicy:MLXForkTestPolicy = .none,deterministic:ForkDeterministicBinding? = nil,phaseControl:(any MLXForkCPUPhaseControl)? = nil,returnService:ReturnServiceControl? = nil,externalDemand:ExternalDemandControl? = nil,
         externalResultDelivery:ExternalResultDeliveryPolicy = .afterBackendClose,parentHost:ParentHostEnvironment? = nil,parentCPU:(any ParentServiceCPUControl)? = nil) {
        self.program=program;self.backend=backend;self.watchdogSeconds=watchdogSeconds;self.nativeOwnsBackend=nativeOwnsBackend
        self.parentHost=parentHost;self.parentCPU=parentCPU
        self.serviceObservation=backend?.serviceObservation ?? BRSServiceTrace(session:"host-parent",role:"host-parent")
        self.serviceSession=backend?.identity.session ?? "host-parent";self.serviceWorker=backend?.identity.worker ?? "none"
        self.forkPolicy=forkPolicy;self.deterministic=deterministic;self.phaseControl=phaseControl;self.returnService=returnService
        self.externalDemand=externalDemand
        self.externalResultDelivery=externalResultDelivery
        if let backend {roots.insert("BackendSession:"+backend.identity.session)}
        if parentHost != nil {roots.insert("ServiceRegistryRoot")}
    }
    private func requireBackend()throws->any MLXBackendDriver {
        guard let backend else {throw RuntimeInvariant("hostParent has no MLX backend authority")};return backend
    }
    // Synchronous leaf observation; no source IDs or protocol transitions.
    private func observeService(_ event:String,caller:Caller,scope:ScopeHandle,site:String,
                                stamp:BRSServiceStamp? = nil,details:JSONValue = .null) {
        let beta=scopes[scope.id]?.backendReturn,record=beta.flatMap{backendReturns[$0]}
        var key=brsOperation(session:serviceSession,worker:serviceWorker,scope:scope.id,owner:scope.owner,
            epoch:scope.epoch,beta:beta,command:record?.command,site:site)
        if case .object(var fields)=key,let receipt=record?.receipt {
            let frame=record?.ack?.raw ?? receipt.raw
            fields["wire_seq"]=frame.brsField("seq");fields["wire_lease_epoch"]=frame.brsField("lease_epoch")
            fields["event_seq"]=frame.brsField("event_seq");fields["receipt_id"]=frame.brsField("receipt_id")
            key = .object(fields)
        }
        serviceObservation.record(event,stamp:stamp,binding:caller.json,
            origin:record?.receipt.map{$0.request.serviceOrigin} ?? .null,key:key,details:details)
    }
    func installHostObserver(_ observer:Task<Void,Never>)throws {
        try runtimeRequire(hostJoinObserver == nil && !hostObserverBodyReturning && !hostObserverJoined,"duplicate host observer")
        hostJoinObserver=observer;roots.insert("HostMainJoinObserver")
        emit("host.observer-installed")
    }
    func hostObserverReturning() {
        hostObserverBodyReturning=true;emit("host.observer-returning")
    }
    /// The runtime calls only after actual await observer.value. On watchdog it
    /// deliberately does not call this method or remove the real helper root.
    func didJoinHostObserver()throws->ExecutionReport {
        guard hostJoinObserver != nil,hostObserverBodyReturning,!hostObserverJoined,let saved=lastReport else {throw RuntimeInvariant("host observer join without actual return/report")}
        hostObserverJoined=true;hostJoinObserver=nil;roots.remove("HostMainJoinObserver")
        emit("host.observer-joined")
        return report(status:saved.status,outcome:saved.outcome,exit:saved.exit)
    }
    func fresh(_ prefix:String)->String { nextID += 1;return prefix+":"+String(nextID) }
    func attachSource(_ machine:Machine,job:String)throws {
        try runtimeRequire(sourceRoots[job] == nil,"source root installed twice")
        sourceRoots[job]=machine;roots.insert("Source:"+job)
    }
    func live() throws { if finalState != "active" { throw IncompleteStop() } }
    func emit(_ name:String,site:String?=nil,caller:Caller?=nil,scope:ScopeHandle?=nil,details:JSONValue = .object([:])) {
        events.append(.object(["sequence":.string(String(events.count)),"t_ns":.string(String(DispatchTime.now().uptimeNanoseconds-start)),
            "name":.string(name),"site":site.map(JSONValue.string) ?? .null,"activation":caller.map { .string($0.invocation) } ?? .null,
            "job":caller.map { .string($0.job) } ?? .null,"task":caller.map { .string($0.task) } ?? .null,
            "scope":scope.map { .string($0.id) } ?? .null,"owner":scope?.owner.map(JSONValue.string) ?? .null,
            "epoch":scope?.epoch.map(JSONValue.string) ?? .null,"details":details]))
    }
    private func edge(_ from:String,_ to:String) { edges[from,default:[]].insert(to) }
    private func region(_ id:String,kind:String,parent:String?) { regions[id] = .object(["kind":.string(kind),"parent":parent.map(JSONValue.string) ?? .null,"state":.string("Live")]) }
    private func endRegion(_ id:String) {
        if let value=regions[id],case .object(var row)=value {row["state"] = .string("Ended");regions[id] = .object(row)}
    }
    private func actualCaller(_ caller:Caller)throws {
        guard let job=jobs[caller.job],job.caller.task == caller.task,!job.retired,
              let r=regions[caller.region],case .object(let fields)=r,fields["state"] == .string("Live") else {
            throw RuntimeInvariant("inactive or foreign actual caller")
        }
    }
    private func reported(_ ids:[String],channel:JSONValue) {
        for id in ids {
            guard let value=faults[id],case .object(var row)=value,let prior=row["reportedVia"],case .array(var channels)=prior else {continue}
            if !channels.contains(channel) {channels.append(channel)}
            row["reportedVia"] = .array(channels);faults[id] = .object(row)
        }
    }
    func rethrown(_ ids:[String],caller:Caller,site:String) {
        reported(ids,channel:.object(["kind":.string("rethrow"),"invocation":.string(caller.invocation),"site":.string(site)]))
        emit("fault.rethrown",site:site,caller:caller,details:.object(["ids":.array(ids.map(JSONValue.string))]))
    }
    func fault(_ code:String,caller:Caller,site:String,details:JSONValue = .null,causeKeys:[String]=[])->SourceFailure {
        let ordinal=faults.values.filter {value in
            guard case .object(let row)=value else{return false};return row["invocation"] == .string(caller.invocation)
        }.count
        let id=fresh("fault");faults[id] = .object(["code":.string(code),"invocation":.string(caller.invocation),"site":.string(site),
            "obligation":.null,"localOrdinal":.string(String(ordinal)),"payload":.object(["code":.string(code),"data":details == .null ? RuntimeValue.unit.diagnostic:details]),
            "causes":.array(causeKeys.map(JSONValue.string)),"details":details,"reportedVia":.array([])])
        emit("fault.created",site:site,caller:caller,details:.object(["id":.string(id),"code":.string(code)]));return SourceFailure(ids:[id])
    }
    func markHandled(_ ids:[String],caller:Caller,site:String) { emit("fault.handled",site:site,caller:caller,details:.object(["ids":.array(ids.map(JSONValue.string))])) }
    func push(_ frame:ActiveFrame) throws {
        try live();guard jobs[frame.caller.job] != nil else { throw RuntimeInvariant("frame without job") }
        try runtimeRequire(!jobs.values.contains { $0.frames.contains { $0.id == frame.id } },"duplicate frame")
        jobs[frame.caller.job]!.frames.append(frame)
        emit("frame.push",site:frame.site,caller:frame.caller,details:frame.json)
    }
    func pop(_ frame:ActiveFrame) throws {
        try live();try runtimeRequire(jobs[frame.caller.job]?.frames.last?.id == frame.id,"non-LIFO or duplicate frame pop")
        jobs[frame.caller.job]!.frames.removeLast();emit("frame.pop",site:frame.site,caller:frame.caller,details:frame.json)
    }
    func startMain(root:RuntimeExecutionRoot? = nil) async throws -> (String,Task<RuntimeTaskExit,Error>) {
        try live();let p=fresh("job"),t=fresh("task"),iM=fresh("invocation"),i0=fresh("invocation")
        let rh=fresh("region"),rm=fresh("region"),s=fresh("scope"),rs=fresh("region")
        let hostCaller=Caller(job:p,task:t,invocation:iM,region:rm,scopes:[])
        let caller=Caller(job:p,task:t,invocation:i0,region:rs,scopes:[s])
        let handle=ScopeHandle(id:s,owner:nil,epoch:nil);let gate=RuntimeGate()
        var env=Environment();let actual:[Witness]=[.brand(sort:"Job",id:p),.brand(sort:"Task",id:t),.brand(sort:"Invocation",id:iM),.brand(sort:"Invocation",id:i0),.brand(sort:"Region",id:rh),.brand(sort:"Region",id:rm),.brand(sort:"Scope",id:s),.brand(sort:"Region",id:rs)]
        for (n,w) in actual.enumerated() {
            let sorts=["Job","Task","Invocation","Invocation","Region","Region","Scope","Region"]
            try env.names.bind(NominalID(site:"/main/body",role:"init-private",index:n,sort:sorts[n]),w)
        }
        let initial=env,program=self.program
        let finish=ActiveFrame(id:fresh("frame"),kind:"JobFinish",site:"/main/body",retained:[],caller:caller)
        let exit=ActiveFrame(id:fresh("frame"),kind:"ScopeExit",site:"/main/body",retained:[],caller:caller)
        let machine=Machine(program:program,ledger:self,environment:initial,caller:caller)
        machine.installInitialFrames([finish,exit])
        let task=Task<RuntimeTaskExit,Error> {
            await gate.wait()
            return await machine.runMain(scope:handle,hostCaller:hostCaller)
        }
        mainStartGate=gate;try root?.installMain(task)
        jobs[p]=JobRecord(caller:hostCaller,exitCaller:hostCaller,bodyRegion:nil,boundary:"HostRoot",role:"main",task:task)
        jobs[p]!.frames=[finish,exit];try attachSource(machine,job:p)
        jobs[p]!.children=[s];scopes[s]=ScopeRecord(handle:handle,caller:caller,parent:hostCaller)
        region(rh,kind:"host",parent:nil);region(rm,kind:"main",parent:rh);region(rs,kind:"scope",parent:rm)
        obligations["Job:"+p]="Active";roots.formUnion(["Task:"+t,"Job:"+p,"Scope:"+s]);edge("Job:"+p,"Scope:"+s)
        emit("compiler.accepted",details:.object(["source":.string(program.sourceHash)]))
        emit("init",site:"/main/body",caller:caller,scope:handle,details:.object(["boundary":.string("HostRoot"),"name_env":initial.names.json,"main_in_local_roots":.bool(false)]))
        emit("frame.push",site:"/main/body",caller:caller,details:finish.json);emit("frame.push",site:"/main/body",caller:caller,details:exit.json)
        return (p,task)
    }
    func openMainGate()async {if let gate=mainStartGate {await gate.release();mainStartGate=nil}}
    func started(_ caller:Caller) throws { try live();try runtimeRequire(jobs[caller.job]?.started == false,"duplicate task start");jobs[caller.job]!.started=true;emit("task.started",caller:caller) }
    func launchProducer(scope h:ScopeHandle,arguments:PrefillArguments,environment:Environment,site:String,parent:Caller,genBind:ActiveFrame) async throws -> ProducerContext {
        let backend=try requireBackend()
        try live();try actualCaller(parent)
        // Source rejection precedes the driver's transient idle-phase guard.
        // This actor does not suspend between these reads and the first claim;
        // a permanently consumed epoch therefore cannot become a backend fault
        // merely because its original EVAL is still pending.
        guard let s=scopes[h.id],s.handle == h,s.admission,s.state == "Open",let owner=h.owner,let epoch=h.epoch else { throw fault("RegistrationClosed",caller:parent,site:site) }
        if s.prefillClaimed { throw fault("PrefillAlreadyClaimed",caller:parent,site:site) }
        if let externalDemand {
            try runtimeRequire(jobs[parent.job]?.role == "main" && jobs[parent.job]?.caller.task == parent.task,
                               "external demand producer issuer is not original main")
            if arguments.operation == "Prefill32" {
                try runtimeRequire(!externalABound && owners[owner]?.ordinal == 1 && s.backendReturn != nil,
                                   "external A target is not first actual occupied Owner epoch")
                try externalDemand.bindA(worker:serviceWorker,scope:h,main:s.caller,producerSite:site,backendReturn:s.backendReturn!)
                externalABound=true
            } else {
                guard let input=hostInputWaits.values.first(where:{$0.received && !$0.rejected}) else {
                    throw RuntimeInvariant("external B registration before actual source receive")
                }
                try runtimeRequire(arguments.operation == "Prefill8" && !externalBRegistered && owners[owner]?.ordinal == 2 &&
                    input.owner == owner && input.caller.job == parent.job && input.caller.task == parent.task && input.tokens == arguments.tokens,
                    "external B source arguments/owner/original main mismatch")
                externalBRegistered=true
                emit("external.b-source-arguments",site:site,caller:parent,scope:h,details:.object([
                    "receive":.string(input.id),"receive_site":.string(input.site),"tokens":.array(arguments.tokens.map(JSONValue.unsigned))]))
            }
        }
        do { return try backend.withHealthyAdmission {
            scopes[h.id]!.prefillClaimed=true
            emit("prefill.claimed",site:site,caller:parent,scope:h,details:.object(["operation":.string(arguments.operation),"tokens":.array(arguments.tokens.map(JSONValue.unsigned))]))
            try runtimeRequire(owners[owner]?.epoch == epoch,"registration epoch mismatch")
            try runtimeRequire(genBind.kind == "GenBind" && genBind.caller == parent && jobs[parent.job]?.frames.last?.id == genBind.id,
                               "registration lost unique issuer GenBind")
            let anchor:String
            // A cleanup caller may carry its registration scope on the stack but
            // executes in rC as a registered Job, not that scope's body/caller.
            if let current=parent.scopes.last,scopes[current]?.caller == parent {
                anchor="Scope:"+current
            } else {anchor="Job:"+parent.job}
            let reg=fresh("registration"),t=fresh("task"),gate=StartAbortGate()
            registrations[reg]=RegistrationRecord(id:reg,taskID:t,issuer:parent,target:h,anchor:anchor,site:site,genBind:genBind.id,
                envelope:PreparedProducerEnvelope(environment:environment,arguments:arguments,site:site),gate:gate)
            obligations["Registration:"+reg]="Active";edge(anchor,"Registration:"+reg)
            if let beta=s.backendReturn {edge("BackendReturn:"+beta,"Registration:"+reg)}
            if anchor.hasPrefix("Scope:") {scopes[String(anchor.dropFirst(6))]!.runtimeRoots.append(reg)}
            roots.formUnion(["Registration:"+reg,"PreparedEnvelope:"+reg])
            emit("registration.prepare",site:site,caller:parent,scope:h,
                 details:.object(["registration":.string(reg),"anchor":.string(anchor),"gen_bind":.string(genBind.id)]))
            // The closure has no captured source environment or callable Machine.
            // Only a later Start payload can transfer the committed source envelope.
            let task=Task<RuntimeTaskExit,Error> {
                switch try await gate.take() {
                case .start(let launch):
                    try await self.registrationStartObserved(reg,task:t,job:launch.context.kernelCaller.job,gate:gate.description)
                    return await launch.machine.runProducer(context:launch.context,arguments:launch.arguments,site:launch.site)
                case .ordinary:throw RuntimeInvariant("producer gate received ordinary payload")
                case .abort(let identity,let taskID):
                    try await self.registrationAbortEntered(reg,task:t,decisionRegistration:identity,decisionTask:taskID,gate:gate.description)
                    return .registrationAborted(registration:identity,task:taskID)
                }
            }
            registrations[reg]!.task=task;roots.insert("PreparedTask:"+t)
            emit("registration.task_prepared",site:site,caller:parent,scope:h,
                 details:.object(["registration":.string(reg),"task":.string(t)]))
            // All throwing commit guards are before the first source-record write.
            // No catch wraps this commit or translates postcommit failure to rollback.
            // Every throwing guard, including the fixture policy selection, runs
            // before commit. A failure here must not leave the still-closed real
            // Task behind its gate: the synchronous admission closure cannot
            // await, so launchProducer below performs the actual abort+join.
            let policySelected:Bool
            do {try validateRegistrationCommit(reg);policySelected=try producerPolicySelected(site:site)} catch {
                throw PreparedRegistrationFailure(registration:reg,underlying:error)
            }
            let context=commitPreparedRegistration(reg)
            bindProducerPolicy(context,site:site,selected:policySelected)
            let launch=ProducerLaunch(machine:sourceRoots[context.kernelCaller.job]!,context:context,arguments:arguments,site:site)
            try gate.resolve(.start(launch))
            return context
        } } catch let failure as PreparedRegistrationFailure {
            // Actual rollback: resolve the gate with .abort, await the very Task
            // placed in Tx, mark the registration Safe, then rethrow the commit error.
            _=try await abortPreparedRegistration(failure.registration,underlying:failure.underlying)
            throw failure.underlying
        }
    }
    private func validateRegistrationCommit(_ reg:String)throws {
        try live();guard let tx=registrations[reg],let scope=scopes[tx.target.id] else {throw RuntimeInvariant("missing prepared registration")}
        try actualCaller(tx.issuer)
        try runtimeRequire(tx.phase == "prepared" && tx.task != nil && tx.envelope != nil && tx.gate?.closed == true &&
                           obligations["Registration:"+reg] == "Active" && jobs[tx.issuer.job]?.frames.last?.id == tx.genBind,
                           "registration not commit-ready")
        try runtimeRequire(scope.handle == tx.target && scope.admission && scope.state == "Open" && scope.producerJob == nil &&
                           tx.target.owner != nil && owners[tx.target.owner!]?.epoch == tx.target.epoch,
                           "registration commit target changed")
    }
    /// Actor-atomic, nonthrowing transfer after validateRegistrationCommit; no
    /// await or recoverable error may turn a partial commit into precommit abort.
    private func commitPreparedRegistration(_ reg:String)->ProducerContext {
        let tx=registrations[reg]!,h=tx.target,owner=h.owner!,epoch=h.epoch!,parent=tx.issuer,site=tx.site
        let envelope=tx.envelope!,task=tx.task!,t=tx.taskID
        let p=fresh("job"),i=fresh("invocation"),rp=fresh("region"),aw=fresh("access"),ac=fresh("access"),rc=fresh("region")
        let caller=Caller(job:p,task:t,invocation:i,region:rc,scopes:[h.id])
        let read=ReadHandle(access:ac,owner:owner,epoch:epoch,job:p,region:rc)
        let names:[Witness]=[.brand(sort:"Job",id:p),.brand(sort:"Task",id:t),.brand(sort:"Invocation",id:i),.brand(sort:"Region",id:rp),.brand(sort:"Access",id:aw),.brand(sort:"Access",id:ac),.brand(sort:"Region",id:rc)]
        let exitCaller=Caller(job:p,task:t,invocation:i,region:rp,scopes:[])
        let context=ProducerContext(caller:caller,kernelCaller:exitCaller,scope:h,write:aw,read:read,names:names)
        let frame=ActiveFrame(id:fresh("frame"),kind:"KernelFinish",site:site,retained:[],caller:exitCaller)
        let machine=Machine(program:program,ledger:self,environment:envelope.environment,caller:exitCaller)
        machine.installInitialFrames([frame])
        jobs[p]=JobRecord(caller:exitCaller,exitCaller:exitCaller,bodyRegion:rc,boundary:h.id,role:"producer",task:task)
        jobs[p]!.frames=[frame];sourceRoots[p]=machine;roots.insert("Source:"+p)
        jobs[p]!.accesses=[aw,ac];scopes[h.id]!.local.append(p)
        scopes[h.id]!.producerJob=p
        accesses[aw]=AccessRecord(job:p,owner:owner,epoch:epoch,region:rp,mode:"Write",state:"Live")
        accesses[ac]=AccessRecord(job:p,owner:owner,epoch:epoch,region:rc,mode:"Read")
        region(rp,kind:"producer",parent:scopes[h.id]!.caller.region);regions[rc] = .object(["kind":.string("cleanup"),"parent":.string(rp),"state":.string("Dormant")])
        obligations["Registration:"+reg]="Safe";obligations["Job:"+p]="Active";obligations["Access:"+aw]="Active";obligations["Access:"+ac]="Active"
        edge("Scope:"+h.id,"Job:"+p);edge("Job:"+p,"Access:"+aw);edge("Job:"+p,"Access:"+ac)
        if let beta=scopes[h.id]!.backendReturn {edge("BackendReturn:"+beta,"Job:"+p)}
        roots.formUnion(["Task:"+t,"Job:"+p,"Access:"+aw,"Access:"+ac])
        registrations[reg]!.phase="committed";registrations[reg]!.transferred=true;registrations[reg]!.sourceJob=p
        registrations[reg]!.task=nil;registrations[reg]!.envelope=nil;registrations[reg]!.gate=nil
        roots.subtract(["Registration:"+reg,"PreparedTask:"+t,"PreparedEnvelope:"+reg])
        emit("registration.commit",site:site,caller:parent,scope:h,details:.object(["registration":.string(reg),"job":.string(p),"task":.string(t),"write":.string(aw),"read":.string(ac)]))
        emit("frame.push",site:site,caller:exitCaller,details:frame.json)
        return context
    }

    /// No MLX idle-phase requirement: ordinary code may register while the
    /// producer is evaluating or owns KV. Source gate and health commit are
    /// synchronous; every optional native wait occurs in a later method.
    func launchOrdinary(scope h:ScopeHandle,environment:Environment,site:String,parent:Caller,genBind:ActiveFrame) throws -> OrdinaryContext {
        let backend=try requireBackend()
        try live();try actualCaller(parent)
        guard let target=scopes[h.id],target.handle == h,target.admission,target.state == "Open" else {
            throw fault("RegistrationClosed",caller:parent,site:site)
        }
        return try backend.withHealthyHostAction {
            try validateOrdinaryCaptures(environment,target:h)
            try runtimeRequire(genBind.kind == "GenBind" && genBind.caller == parent && jobs[parent.job]?.frames.last?.id == genBind.id,"ordinary lost issuer GenBind")
            try runtimeRequire(scopes[h.id]?.admission == true && scopes[h.id]?.state == "Open","ordinary target changed during synchronous commit")
            let annotation=try program.annotation(site+"/job/body")
            let sharedType=try program.annotation(site).binder("generated",0).type
            let resultType=try program.table.field(program.table.field(sharedType,"fields"),"payload"),latentEffect=annotation.effect
            try runtimeRequire(resultType != .null && latentEffect != .null,"ordinary body lacks complete type/effect")
            let ordinarySelected=try ordinaryPolicySelected(site:site,scope:h)
            let anchor:String
            if let current=parent.scopes.last,scopes[current]?.caller == parent {anchor="Scope:"+current}
            else {anchor="Job:"+parent.job}
            let reg=fresh("registration"),t=fresh("task"),gate=StartAbortGate()
            registrations[reg]=RegistrationRecord(id:reg,taskID:t,issuer:parent,target:h,anchor:anchor,site:site,genBind:genBind.id,
                ordinaryEnvelope:PreparedOrdinaryEnvelope(environment:environment,site:site,resultType:resultType,latentEffect:latentEffect),gate:gate)
            registrations[reg]!.kind="ordinary"
            obligations["Registration:"+reg]="Active";edge(anchor,"Registration:"+reg)
            if let beta=target.backendReturn {edge("BackendReturn:"+beta,"Registration:"+reg)}
            if anchor.hasPrefix("Scope:") {scopes[String(anchor.dropFirst(6))]!.runtimeRoots.append(reg)}
            roots.formUnion(["Registration:"+reg,"PreparedEnvelope:"+reg])
            emit("registration.prepare",site:site,caller:parent,scope:h,details:registrations[reg]!.json)
            let task=Task<RuntimeTaskExit,Error> {
                switch try await gate.take() {
                case .ordinary(let launch):
                    try await self.registrationStartObserved(reg,task:t,job:launch.context.caller.job,gate:gate.description)
                    try await self.beforeOrdinaryStart(launch.context)
                    return await launch.machine.runOrdinary(context:launch.context)
                case .abort(let identity,let taskID):
                    try await self.registrationAbortEntered(reg,task:t,decisionRegistration:identity,decisionTask:taskID,gate:gate.description)
                    return .registrationAborted(registration:identity,task:taskID)
                case .start:throw RuntimeInvariant("ordinary gate received producer payload")
                }
            }
            registrations[reg]!.task=task;roots.insert("PreparedTask:"+t)
            emit("registration.task_prepared",site:site,caller:parent,scope:h,details:registrations[reg]!.json)
            // All fallible guards preceded creation. No suspension can close
            // the target between the validated source gate and this commit.
            let p=fresh("job"),i=fresh("invocation"),r=fresh("region")
            let caller=Caller(job:p,task:t,invocation:i,region:r,scopes:[h.id])
            let exit=Caller(job:p,task:t,invocation:i,region:r,scopes:[])
            let names:[Witness]=[.brand(sort:"Job",id:p),.brand(sort:"Task",id:t),.brand(sort:"Invocation",id:i),.brand(sort:"Region",id:r)]
            let context=OrdinaryContext(caller:caller,exitCaller:exit,scope:h,names:names,site:site)
            let finish=ActiveFrame(id:fresh("frame"),kind:"JobFinish",site:site,retained:[],caller:caller)
            let machine=Machine(program:program,ledger:self,environment:environment,caller:caller)
            machine.installInitialFrames([finish])
            jobs[p]=JobRecord(caller:caller,exitCaller:exit,bodyRegion:r,boundary:h.id,role:"ordinary",task:task)
            jobs[p]!.resultType=resultType;jobs[p]!.latentEffect=latentEffect;jobs[p]!.frames=[finish]
            sourceRoots[p]=machine;roots.formUnion(["Source:"+p,"Task:"+t,"Job:"+p])
            region(r,kind:"ordinary",parent:target.caller.region);scopes[h.id]!.local.append(p)
            obligations["Job:"+p]="Active";edge("Scope:"+h.id,"Job:"+p)
            if let beta=target.backendReturn {edge("BackendReturn:"+beta,"Job:"+p)}
            registrations[reg]!.phase="committed";registrations[reg]!.transferred=true;registrations[reg]!.sourceJob=p
            registrations[reg]!.task=nil;registrations[reg]!.ordinaryEnvelope=nil;registrations[reg]!.gate=nil
            obligations["Registration:"+reg]="Safe";roots.subtract(["Registration:"+reg,"PreparedTask:"+t,"PreparedEnvelope:"+reg])
            emit("registration.commit",site:site,caller:parent,scope:h,details:.object(["kind":.string("ordinary"),"registration":.string(reg),"job":.string(p),"task":.string(t),"caller":caller.json,"result_type":resultType,"latent_effect":latentEffect]))
            emit("frame.push",site:site,caller:caller,details:finish.json)
            bindOrdinaryPolicy(context,selected:ordinarySelected)
            try gate.resolve(.ordinary(OrdinaryLaunch(machine:machine,context:context)))
            return context
        }
    }

    private func validateOrdinaryCaptures(_ environment:Environment,target:ScopeHandle)throws {
        guard let targetRecord=scopes[target.id],targetRecord.handle == target else {throw RuntimeInvariant("ordinary capture target identity")}
        if let owner=target.owner {try runtimeRequire(owners[owner]?.epoch == target.epoch,"ordinary target old epoch")}
        var visited=Set<ObjectIdentifier>()
        func knownWitness(_ witness:Witness)throws {
            guard case .brand(let sort,let id)=witness else {return}
            let known:Bool
            switch sort {
            case "Owner":known=owners[id] != nil
            case "Scope":known=scopes[id] != nil
            case "Epoch":known=scopes.values.contains{$0.handle.epoch == id}
            case "Job":known=jobs[id] != nil
            case "Task":known=jobs.values.contains{$0.caller.task == id}
            case "Invocation":known=jobs.values.contains{$0.caller.invocation == id} || scopes.values.contains{$0.caller.invocation == id}
            case "Region":known=regions[id] != nil
            case "Access":known=accesses[id] != nil
            default:known=false
            }
            try runtimeRequire(known,"ordinary capture unknown nominal witness "+sort)
        }
        func regionCovers(_ outer:String)throws {
            try runtimeRequire(regions[outer]?.field("state").text() == "Live","ordinary capture ended live support")
            var cursor:String?=targetRecord.caller.region
            while let r=cursor {
                if r == outer {return}
                guard case .object(let row)?=regions[r],case .string(let next)?=row["parent"] else {break}
                cursor=next
            }
            throw RuntimeInvariant("ordinary capture lifetime does not cover registration scope")
        }
        func walk(_ value:RuntimeValue)throws {
            switch value {
            case .read:throw RuntimeInvariant("ordinary capture contains Read")
            case .pair(let a,let b):try walk(a);try walk(b)
            case .sum(_,let payload):try walk(payload)
            case .package(let package):
                if visited.insert(ObjectIdentifier(package)).inserted {for witness in package.witnesses {try knownWitness(witness)};try walk(package.payload)}
            case .closure(let closure):
                if visited.insert(ObjectIdentifier(closure)).inserted {
                    let captured=try closure.copyCapturesForRegistration()
                    for witness in captured.names.values.values {try knownWitness(witness)}
                    for slot in captured.slots.values {try runtimeRequire(slot.copyable,"ordinary closure affine capture");try walk(slot.value)}
                }
            case .scope(let handle):
                guard let record=scopes[handle.id],record.handle == handle else {throw RuntimeInvariant("ordinary capture forged Scope")}
                if let owner=handle.owner {try runtimeRequire(owners[owner]?.epoch == handle.epoch,"ordinary capture stale owned Scope")}
                try regionCovers(record.caller.region) // admission may already be closed
            case .owner(let owner):try runtimeRequire(owners[owner.id] != nil,"ordinary capture foreign Owner")
            case .shared(let job),.control(let job):try runtimeRequire(jobs[job] != nil,"ordinary capture unknown Job")
            case .failure(let ids):try runtimeRequire(!ids.isEmpty && ids.allSatisfy{faults[$0] != nil},"ordinary capture foreign Failure")
            case .unit,.bool,.nat,.word,.errorPayload:break
            }
        }
        for witness in environment.names.values.values {try knownWitness(witness)}
        for slot in environment.slots.values {try runtimeRequire(slot.copyable,"ordinary affine source capture");try walk(slot.value)}
    }

    private func generatedName(_ site:String)throws->String {
        guard let node=program.nodes[site] else {throw RuntimeInvariant("native policy missing checked site")}
        return try node.field("result_name").text()
    }
    /// Throwing half of the fixture policy binding; runs before commit so that
    /// no throw can follow a committed registration whose Task is not yet started.
    private func producerPolicySelected(site:String)throws->Bool {
        let name=try generatedName(site)
        let selected = deterministic?.producerSite == site || (forkPolicy == .ownedCoexist && name == "AH")
        if selected {try runtimeRequire(policyProducer == nil,"native producer identity rebound")}
        return selected
    }
    private func bindProducerPolicy(_ context:ProducerContext,site:String,selected:Bool) {
        if selected {
            policyProducer=context.kernelCaller.job
            emit("native.producer-bound",site:site,caller:context.kernelCaller,scope:context.scope,details:policyDescription)
        }
    }
    private func ordinaryPolicySelected(site:String,scope:ScopeHandle)throws->Bool {
        let name=try generatedName(site)
        let selected = deterministic?.forkSite == site ||
            ((forkPolicy == .earlyCancel || forkPolicy == .closedTarget) && name == "WH")
        if selected {
            try runtimeRequire(policyWaiter == nil,"native waiter identity rebound")
            if let deterministic {
                try runtimeRequire(scope.owner == nil && scopes[scope.id]?.creationSite == deterministic.scopeSite,"D waiter registered in foreign scope activation")
            }
        }
        return selected
    }
    private func bindOrdinaryPolicy(_ context:OrdinaryContext,selected:Bool) {
        if selected {
            policyWaiter=context.caller.job;policyWaiterScope=context.scope.id
            emit("native.waiter-bound",site:context.site,caller:context.caller,scope:context.scope,details:policyDescription)
        }
    }
    func producerParentReady(_ context:ProducerContext,site:String,parent:Caller)async throws {
        if forkPolicy == .attachedCancellation && policyProducer == context.kernelCaller.job {
            await producerHeld.wait();try live();try actualCaller(parent)
            emit("native.producer-parent-received-held",site:site,caller:parent,details:policyDescription)
        } else if forkPolicy == .ownedCoexist && policyProducer == context.kernelCaller.job {
            guard let phaseControl else {throw RuntimeInvariant("C1 requires actual CPU pipe phase control")}
            let evidence=try await phaseControl.waitForEvaluationEntered(job:context.kernelCaller.job)
            try live();try actualCaller(parent);emit("native.eval-enter-observed",site:site,caller:parent,details:evidence)
        }
    }
    func ordinaryParentReady(_ context:OrdinaryContext,parent:Caller)async throws {
        let name=try generatedName(context.site)
        if forkPolicy == .attachedCancellation && policyWaiter == context.caller.job {
            await waiterAttached.wait();try live();try actualCaller(parent)
            emit("native.fork-parent-received-attached",site:context.site,caller:parent,scope:context.scope,details:policyDescription)
        } else if forkPolicy == .ownedCoexist && name == "evalH" {
            guard let phaseControl,let producer=policyProducer else {throw RuntimeInvariant("C1 continue lacks actual producer")}
            let evidence=try await phaseControl.continueEvaluation(job:producer)
            try live();try actualCaller(parent);emit("native.eval-continued-after-fork",site:context.site,caller:parent,scope:context.scope,details:evidence)
        }
    }
    private func beforeOrdinaryStart(_ context:OrdinaryContext)async throws {
        if policyWaiter == context.caller.job && (forkPolicy == .earlyCancel || forkPolicy == .closedTarget) {
            emit("native.ordinary-start-held",site:context.site,caller:context.caller,scope:context.scope,details:policyDescription)
            await ordinaryStartOpen.wait();try live()
            if forkPolicy == .earlyCancel {try runtimeRequire(publicationWaits.isCancelled(context.caller.job),"native start before actual source cancellation")}
            if forkPolicy == .closedTarget {try runtimeRequire(scopes[context.scope.id]?.state == "Draining","native start before actual target close")}
            emit("native.ordinary-start-released",site:context.site,caller:context.caller,scope:context.scope,details:policyDescription)
        }
    }
    private func publicationAttachedForPolicy(_ row:PublicationWaitRecord)throws {
        guard forkPolicy == .attachedCancellation,policyWaiter == row.caller.job else {return}
        try runtimeRequire(row.site == deterministic?.waitSite && row.target == policyProducer && row.registration == "attached" &&
            row.handlerInstalled && row.handlerActive && row.continuationInstalled && row.continuation != nil &&
            row.choice.label == "undecided" && row.kont != nil && jobs[row.target]?.publication == nil,
            "D waiter did not actually attach pending with full holder")
        try runtimeRequire(policyWait == nil,"D waiter attached twice")
        policyWait=row.id
        emit("native.waiter-attached",site:row.site,caller:row.caller,details:.object(["cell":row.json,"state":snapshot()]))
        waiterAttached.release()
    }
    private var policyDescription:JSONValue {
        .object(["policy":.string(forkPolicy.rawValue),"producer":policyProducer.map(JSONValue.string) ?? .null,
            "waiter":policyWaiter.map(JSONValue.string) ?? .null,"waiter_scope":policyWaiterScope.map(JSONValue.string) ?? .null,
            "wait":policyWait.map(JSONValue.string) ?? .null,"producer_held":producerHeld.json,"producer_open":producerOpen.json,
            "waiter_attached":waiterAttached.json,"ordinary_start":ordinaryStartOpen.json])
    }
    private func registrationStartObserved(_ reg:String,task:String,job:String,gate:JSONValue)throws {
        try live();guard let tx=registrations[reg] else {throw RuntimeInvariant("start without registration")}
        try runtimeRequire(tx.phase == "committed" && tx.taskID == task && tx.sourceJob == job && tx.transferred &&
                           tx.task == nil && tx.envelope == nil && tx.ordinaryEnvelope == nil && tx.gate == nil && jobs[job]?.task != nil,
                           "start transfer identity mismatch")
        try runtimeRequire(gate == .object(["resolved":.bool(true),"consumed":.bool(true),"payload_retained":.bool(false),"waiter_retained":.bool(false)]),
                           "start gate retained callable payload")
        registrations[reg]!.gateHistory=gate
        emit("registration.start_observed",site:tx.site,caller:jobs[job]!.exitCaller,scope:tx.target,
             details:.object(["registration":.string(reg),"task":.string(task)]))
    }
    private func registrationAbortEntered(_ reg:String,task:String,decisionRegistration:String,decisionTask:String,gate:JSONValue)throws {
        try live();guard let tx=registrations[reg] else {throw RuntimeInvariant("abort without registration")}
        try runtimeRequire(reg == decisionRegistration && task == decisionTask && tx.taskID == task && tx.phase == "aborting" &&
                           tx.task != nil && tx.envelope != nil && tx.sourceJob == nil && !tx.transferred && !tx.abortEntered,
                           "abort-only identity or phase mismatch")
        try runtimeRequire(gate == .object(["resolved":.bool(true),"consumed":.bool(true),"payload_retained":.bool(false),"waiter_retained":.bool(false)]),
                           "abort gate retained payload")
        registrations[reg]!.abortEntered=true;registrations[reg]!.gateHistory=gate
        emit("registration.abort_entered",site:tx.site,scope:tx.target,
             details:.object(["registration":.string(reg),"task":.string(task),"source_code_entered":.bool(false)]))
    }
    private func abortPreparedRegistration(_ reg:String,underlying:any Error)async throws->ProducerContext {
        guard let tx=registrations[reg],tx.phase == "prepared",let task=tx.task,let gate=tx.gate else {
            throw RuntimeInvariant("rollback outside prepared phase")
        }
        let faultIDs=(underlying as? SourceFailure)?.ids ?? []
        registrations[reg]!.faultIDs=faultIDs;registrations[reg]!.phase="aborting"
        do {
            try gate.resolve(.abort(registration:reg,task:tx.taskID))
            emit("registration.abort_requested",site:tx.site,caller:tx.issuer,scope:tx.target,
                 details:.object(["registration":.string(reg),"task":.string(tx.taskID)]))
            let exit=try await task.value // the very Task originally placed in Tx
            try live()
            guard case .registrationAborted(let actualReg,let actualTask)=exit else {throw RuntimeInvariant("abort returned source outcome")}
            try runtimeRequire(actualReg == reg && actualTask == tx.taskID && registrations[reg]?.abortEntered == true &&
                               registrations[reg]?.phase == "aborting" && obligations["Registration:"+reg] == "Active",
                               "abort terminal identity or obligation mismatch")
            registrations[reg]!.terminal=true
            emit("registration.task_terminal_observed",site:tx.site,scope:tx.target,
                 details:.object(["registration":.string(reg),"task":.string(tx.taskID)]))
            registrations[reg]!.joined=true
            emit("registration.join",site:tx.site,caller:tx.issuer,scope:tx.target,
                 details:.object(["registration":.string(reg),"task":.string(tx.taskID),"channel":.string(tx.anchor)]))
            obligations["Registration:"+reg]="Safe"
            registrations[reg]!.phase="aborted-joined";registrations[reg]!.task=nil
            registrations[reg]!.envelope=nil;registrations[reg]!.gate=nil
            roots.subtract(["Registration:"+reg,"PreparedTask:"+tx.taskID,"PreparedEnvelope:"+reg])
            emit("registration.abort",site:tx.site,caller:tx.issuer,scope:tx.target,
                 details:.object(["registration":.string(reg),"task":.string(tx.taskID),"fault_ids":.array(faultIDs.map(JSONValue.string)),
                                  "message":.string(String(describing:underlying))]))
        } catch {
            registrations[reg]!.phase="unresolved";obligations["Registration:"+reg]="Unresolved"
            emit("registration.unresolved",site:tx.site,caller:tx.issuer,scope:tx.target,
                 details:.object(["registration":.string(reg),"message":.string(String(describing:error))]))
            throw error // no Safe/cleanup/GenBind return for uncertain rollback
        }
        throw underlying // original commit failure, only after actual join
    }
    private func validAccess(_ id:String,job:String,mode:String)throws {
        guard let a=accesses[id] else { throw RuntimeInvariant("unknown access") }
        try runtimeRequire(a.job == job && a.mode == mode && a.state == "Live","stale/sealed/foreign access")
        try runtimeRequire(owners[a.owner]?.epoch == a.epoch,"old epoch admission")
        try runtimeRequire(jobs[job]?.retired == false,"submission after RETIRE")
    }
    private func issue(_ access:String,kind:String,caller:Caller,site:String)->String {
        let id=fresh("command");commands[id]=CommandRecord(access:access,job:caller.job,kind:kind,caller:caller)
        accesses[access]!.commands.append(id);obligations["Command:"+id]="Active";edge("Access:"+access,"Command:"+id);roots.insert("Command:"+id)
        emit("command.issue",site:site,caller:caller,details:.object(["command":.string(id),"access":.string(access),"kind":.string(kind)]));return id
    }
    private func complete(_ id:String,caller:Caller,site:String) {
        commands[id]!.completed=true;obligations["Command:"+id]="Safe";roots.remove("Command:"+id)
        emit("command.completed",site:site,caller:caller,details:.object(["command":.string(id),"evidence":.string("synchronous-cpu-return")]))
    }
    private func seal(_ access:String,caller:Caller,site:String)throws {
        guard let a=accesses[access] else { throw RuntimeInvariant("seal missing access") }
        try runtimeRequire(a.state == "Live" || a.state == "Dormant","duplicate seal")
        try runtimeRequire(a.commands.allSatisfy { commands[$0]?.completed == true },"access completion unknown")
        try runtimeRequire(a.replies.isEmpty,"unconsumed LOAD reply")
        accesses[access]!.state="Sealed";emit("access.sealed",site:site,caller:caller,details:.object(["access":.string(access)]))
        accesses[access]!.state="Revoked";emit("access.revoked",site:site,caller:caller,details:.object(["access":.string(access)]))
        obligations["Access:"+access]="Safe";roots.remove("Access:"+access);emit("access.safe",site:site,caller:caller,details:.object(["access":.string(access)]))
    }
    func load(_ read:ReadHandle,index:UInt64,caller:Caller,site:String)throws->UInt64 {
        try live();try actualCaller(caller);if index >= 1 { throw fault("ReadBounds",caller:caller,site:site,details:.string(String(index))) }
        guard read.job == caller.job else {throw fault("ReadJobMismatch",caller:caller,site:site)}
        guard let a=accesses[read.access],a.job == caller.job,a.mode == "Read",a.owner == read.owner,a.epoch == read.epoch,a.region == read.region else {throw fault("ReadDescriptorMismatch",caller:caller,site:site)}
        guard a.state == "Live",jobs[caller.job]?.retired == false else {throw fault("AccessRevoked",caller:caller,site:site)}
        guard owners[a.owner]?.epoch == a.epoch else {throw fault("ReadEpochMismatch",caller:caller,site:site)}
        // Caller may be a synchronous nested scope: require actual region ancestry.
        var r:String?=caller.region;var covered=false
        while let current=r {
            if current == read.region { covered=true;break }
            if let value=regions[current],case .object(let fields)=value,let pv=fields["parent"],case .string(let parent)=pv { r=parent } else { r=nil }
        }
        guard covered else {throw fault("ReadLifetimeMismatch",caller:caller,site:site)}
        let id=issue(read.access,kind:"LOAD",caller:caller,site:site)
        guard let value=owners[read.owner]?.word else {throw RuntimeInvariant("LOAD before accepted initialized token cell")}
        commands[id]!.reply=RuntimeValue.word(value).diagnostic;complete(id,caller:caller,site:site)
        accesses[read.access]!.replies.insert(id);emit("load.reply",site:site,caller:caller,details:.object(["command":.string(id),"index":.string(String(index)),"word":.string(String(value))]))
        try runtimeRequire(accesses[read.access]!.replies.remove(id) != nil,"reply consumed twice");commands[id]!.consumed=true
        emit("load.consumed",site:site,caller:caller,details:.object(["command":.string(id)]));return value
    }
    func finishAccesses(_ caller:Caller,site:String)throws {
        try live();for id in jobs[caller.job]!.accesses where accesses[id]!.state != "Revoked" { try seal(id,caller:caller,site:site) }
    }
    func publishedExit(_ p:String)throws->PublishedOutcome {
        try live()
        guard let job=jobs[p],job.retired,let publication=job.publication,publication.job == p,
              publication.task == job.caller.task else {throw RuntimeInvariant("missing actual published Task exit")}
        if case .infrastructure=publication.outcome {throw RuntimeInvariant("infrastructure is not a Shared outcome")}
        return publication
    }
    /// Called by the original Task after source retirement, before publication.
    func beforePublication(_ p:String,site:String)async throws {
        try live()
        guard let job=jobs[p],job.retired,job.prepared != nil,job.publication == nil,job.outcome == nil,
              sourceRoots[p] == nil,job.frames.isEmpty,job.children.allSatisfy({scopes[$0]?.state == "Joined"}) else {
            throw RuntimeInvariant("prepublication without legal RuntimeExit")
        }
        try runtimeRequire(!job.terminal && regions[job.exitCaller.region]?.field("state").text() == "Live","prepublication lacks live job region")
        emit("task.runtime-exit",site:site,caller:job.exitCaller,details:.object(["role":.string(job.role),"job_region":regions[job.exitCaller.region] ?? .null,"prepared":job.prepared?.json ?? .null]))
        if forkPolicy == .attachedCancellation && policyProducer == p {
            guard case .ok?=job.prepared else {throw RuntimeInvariant("D producer did not prepare successful source result")}
            roots.insert("NativeGate:producer:"+p)
            emit("native.producer-prepublication-held",site:site,caller:job.exitCaller,details:snapshot())
            producerHeld.release()
            await producerOpen.wait()
            try live()
            try runtimeRequire(policyWaiterScope.flatMap{scopes[$0]?.state} == "Joined","D producer opened before waiter scope finalize")
            roots.remove("NativeGate:producer:"+p)
            emit("native.producer-prepublication-released",site:site,caller:job.exitCaller,details:policyDescription)
        }
    }
    func publish(_ p:String,site:String)throws->(PublishedOutcome,[String]) {
        try live()
        guard let job=jobs[p],job.retired,let outcome=job.prepared,job.publication == nil,job.outcome == nil,
              sourceRoots[p] == nil,job.frames.isEmpty,!job.terminal else {throw RuntimeInvariant("PUBLISH outside publishReady")}
        try runtimeRequire(regions[job.exitCaller.region]?.field("state").text() == "Live","PUBLISH lacks live RuntimeExit region")
        let publication=PublishedOutcome(id:fresh("publication"),job:p,task:job.caller.task,outcome:outcome)
        let candidates=try publicationWaits.publish(publication)
        jobs[p]!.outcome=outcome;jobs[p]!.publication=publication
        emit("result.published",site:site,caller:job.exitCaller,details:.object(["publication":.string(publication.id),"outcome":outcome.json,
            "attached_candidates":.array(candidates.map(JSONValue.string))]))
        return (publication,candidates)
    }
    func claimPublication(_ w:String,publication:PublishedOutcome,site:String)throws {
        try live()
        try runtimeRequire(jobs[publication.job]?.publication === publication,"claim does not use original publication")
        let action=try publicationWaits.claimDelivery(w,publication:publication)
        let row=try publicationWaits.record(w)
        emit("wait.publication-claim",site:row.site,caller:row.caller,details:row.json)
        if let action {try performPublicationResume(action)}
    }
    func retirePublicationRegistry(_ publication:PublishedOutcome,site:String)throws {
        try live();try publicationWaits.retireRegistry(publication)
        emit("wait.registry-retired",site:site,caller:jobs[publication.job]!.exitCaller,
            details:.object(["target":.string(publication.job),"publication":.string(publication.id),"protocol":publicationWaits.json]))
    }
    private func performPublicationResume(_ action:PublicationResume)throws {
        pendingPublicationResumes[action.wake.wait]=action
        try live()
        try publicationWaits.resumeBegin(action.wake)
        let row=try publicationWaits.record(action.wake.wait)
        emit("wait.resume_begin",site:row.site,caller:row.caller,details:row.json)
        // No mutable protocol/holder borrow or lock spans this actual resume.
        // This existing producer/canceller/waiter Task performs the action.
        try action.perform()
        try publicationWaits.resumeReturned(action.wake)
        pendingPublicationResumes.removeValue(forKey:action.wake.wait)
        emit("wait.resume_returned",site:row.site,caller:row.caller,details:.object(["wait":.string(row.id)]))
    }
    func createPublicationWait(target:String,caller:Caller,site:String,operand:JSONValue,operandType:JSONValue,resultType:JSONValue,
                               kont:WaitContinuation,machine:Machine)throws->String {
        try live();try actualCaller(caller)
        guard let targetJob=jobs[target],sourceRoots[caller.job] === machine else {throw RuntimeInvariant("AWAIT? target/source mismatch")}
        try runtimeRequire(kont.caller == caller && (targetJob.task != nil || targetJob.terminal),"AWAIT? missing original target Task")
        let w=fresh("wait")
        try publicationWaits.create(id:w,caller:caller,target:target,task:targetJob.caller.task,site:site,
            operand:operand,operandType:operandType,resultType:resultType,kont:kont)
        roots.insert("Wait:"+w)
        emit("wait.created",site:site,caller:caller,details:try publicationWaits.record(w).json)
        return w
    }
    /// This actor call is separate from WAIT-NEW, leaving the early-cancel
    /// window. Handler installation and its bit read/claim never suspend.
    func waitPublication(_ w:String)async throws->PublicationWake {
        try live();let original=try publicationWaits.record(w);try actualCaller(original.caller)
        try publicationWaits.installHandler(w)
        emit("wait.handler-installed",site:original.site,caller:original.caller,details:try publicationWaits.record(w).json)
        return await withCheckedContinuation { continuation in
            do {
                let action=try publicationWaits.installContinuation(w,continuation)
                if let action {pendingPublicationResumes[w]=action}
                let row=try publicationWaits.record(w)
                emit("wait."+row.registration,site:row.site,caller:row.caller,details:row.json)
                try publicationAttachedForPolicy(row)
                if let action {try performPublicationResume(action)}
            } catch {
                // An invariant/Unknown is not a source wake. Keep the real
                // holder/native continuation; the original watchdog reports it.
                if publicationWaits.cells[w]?.continuationInstalled != true {failedPublicationInstalls[w]=continuation}
                markUnresolved(original.caller,message:String(describing:error))
            }
        }
    }
    func returnPublicationWait(_ wake:PublicationWake,caller:Caller,machine:Machine)throws->Outcome {
        try live();try actualCaller(caller)
        try runtimeRequire(sourceRoots[caller.job] === machine,"AWAIT? restore to foreign Machine")
        let row=try publicationWaits.record(wake.wait)
        if let publication=row.choice.publication {
            try runtimeRequire(jobs[row.target]?.publication === publication && publication.job == row.target && publication.task == row.task,"AWAIT? foreign publication")
        }
        let outcome=try publicationWaits.restore(wake,caller:caller) {holder,state in try machine.restoreWait(holder,state:state)}
        roots.remove("Wait:"+row.id)
        if case .failure(let ids)=outcome {reported(ids,channel:.object(["kind":.string("await_cancel"),"wait":.string(row.id),"caller":.string(caller.job),"site":.string(row.site)]))}
        emit("wait.returned",site:row.site,caller:caller,details:try publicationWaits.record(row.id).json)
        return outcome
    }
    func publicationReturnObserved(_ w:String,caller:Caller,outcome:Outcome)throws {
        try live();try actualCaller(caller);try publicationWaits.observedReturn(w,caller:caller)
        let row=try publicationWaits.record(w)
        emit("wait.caller-return-observed",site:row.site,caller:caller,details:.object(["wait":.string(w),"outcome":outcome.json,"record":row.json]))
    }
    func producerEpilogue(_ p:String,site:String)async throws->PublishedOutcome {
        let publication=try publishedExit(p)
        emit("task.exiting",site:site,caller:jobs[p]!.exitCaller,details:.object(["publication":.string(publication.id)]))
        return publication
    }
    private func observeTerminal(_ publication:PublishedOutcome,channel:String,site:String)throws {
        try live()
        guard let job=jobs[publication.job],job.retired,job.publication === publication,
              publication.task == job.caller.task else {throw RuntimeInvariant("Task return differs from original publication")}
        let row:JSONValue = .object(["channel":.string(channel),"task":.string(publication.task),
                                     "publication":.string(publication.id),"kind":.string("actual-Task.value-returned")])
        terminalEvidence[publication.job,default:[]].append(row)
        jobs[publication.job]!.terminal=true
        endRegion(job.exitCaller.region)
        emit("task.terminal_observed",site:site,caller:job.caller,details:row)
    }
    func awaitTerminal(target:String,caller:Caller,site:String,operand:JSONValue,operandType:JSONValue,resultType:JSONValue,
                       kont:WaitContinuation,machine:Machine)async throws->Outcome {
        try live();try actualCaller(caller)
        guard let targetJob=jobs[target],sourceRoots[caller.job] === machine else {
            throw RuntimeInvariant("AWAIT target/source root mismatch")
        }
        try runtimeRequire(kont.caller == caller && (targetJob.task != nil || targetJob.terminal),"AWAIT missing actual Task")
        let w=fresh("wait")
        try kont.claim(w)
        waits[w]=WaitRecord(id:w,caller:caller,target:target,task:targetJob.caller.task,site:site,
                            operand:operand,operandType:operandType,resultType:resultType,kont:kont)
        roots.insert("Wait:"+w)
        emit("wait.created",site:site,caller:caller,details:waits[w]!.json)
        let publication:PublishedOutcome
        if targetJob.terminal {
            guard let stored=targetJob.publication,!(terminalEvidence[target] ?? []).isEmpty else {
                throw RuntimeInvariant("cached AWAIT lacks actual terminal evidence")
            }
            publication=stored;waits[w]!.registration="cached"
            emit("wait.cached",site:site,caller:caller,details:.object(["wait":.string(w),"target":.string(target),"publication":.string(stored.id)]))
        } else {
            guard let task=targetJob.task else {throw RuntimeInvariant("attached AWAIT lost original Task")}
            waits[w]!.registration="attached";waits[w]!.callsite=true
            emit("wait.attached",site:site,caller:caller,details:.object(["wait":.string(w),"target":.string(target)]))
            emit("wait.task-value-callsite",site:site,caller:caller,
                 details:.object(["wait":.string(w),"target":.string(target),"task":.string(targetJob.caller.task)]))
            // No intervening await: parent source cancel can re-enter this
            // actor only after this original caller invokes actual Task.value.
            do {
                let exit=try await task.value
                jobs[target]!.actualTaskExitObserved=true
                waitTaskObservations[w] = .object(["kind":.string("returned"),"target":.string(target),"task":.string(targetJob.caller.task),
                                                  "actual_exit":exit.diagnostic])
                guard case .published(let actual)=exit else {throw RuntimeInvariant("AWAIT received unpublished or registration-only Task exit")}
                try runtimeRequire(actual.job == target && actual.task == targetJob.caller.task && jobs[target]?.publication === actual,
                                   "AWAIT observed foreign Task publication")
                try observeTerminal(actual,channel:"Wait:"+w,site:site)
                publication=actual
            } catch {
                if waitTaskObservations[w] == nil {
                    jobs[target]!.actualTaskExitObserved=true
                    waitTaskObservations[w] = .object(["kind":.string("threw-or-invalid-exit"),"target":.string(target),
                                                       "message":.string(String(describing:error))])
                }
                throw error // W/kont/Task/source roots survive; no source Failure is invented.
            }
        }
        try live()
        try runtimeRequire(publication.job == target && jobs[target]?.publication === publication && jobs[target]?.terminal == true,
                           "AWAIT delivered foreign or nonterminal publication")
        if case .infrastructure=publication.outcome {throw RuntimeInvariant("AWAIT infrastructure publication")}
        try runtimeRequire(waits[w]?.choice == "undecided" && waits[w]?.resumeIssued == false && waits[w]?.returned == false,
                           "AWAIT duplicate delivery")
        waits[w]!.choice="delivered";waits[w]!.publication=publication.id;waits[w]!.resumeIssued=true
        emit("wait.delivered",site:site,caller:caller,details:.object(["wait":.string(w),"publication":.string(publication.id),"outcome":publication.outcome.json]))
        emit("wait.resume-issued",site:site,caller:caller,details:.object(["wait":.string(w)]))
        // The caller Task is suspended in this actor method. Restore all of its
        // actual state before releasing the sole return token; no await here.
        try kont.restore(w) {try machine.restoreWait(kont,state:$0)}
        waits[w]!.history=kont.description;waits[w]!.kont=nil;waits[w]!.returned=true;roots.remove("Wait:"+w)
        if case .failure(let ids)=publication.outcome {
            reported(ids,channel:.object(["kind":.string("await"),"wait":.string(w),"caller":.string(caller.job),"site":.string(site)]))
        }
        emit("wait.returned",site:site,caller:caller,details:.object(["wait":.string(w),"publication":.string(publication.id)]))
        return publication.outcome
    }
    func requestCancel(_ p:String,caller:Caller,site:String)throws {
        try live();try actualCaller(caller);guard let job=jobs[p] else { throw RuntimeInvariant("unknown Control") }
        let request=fresh("cancel-request")
        emit("cancel.requested",site:site,caller:caller,details:.object(["request":.string(request),"target":.string(p),"control":RuntimeValue.control(p).diagnostic]))
        let (previous,invoked,actions)=try publicationWaits.requestCancel(caller:p,request:request)
        for action in actions {pendingPublicationResumes[action.wake.wait]=action}
        emit("cancel.accepted",site:site,caller:caller,details:.object(["request":.string(request),"target":.string(p),"previous":.bool(previous),"cancel_bit":.bool(true),"terminal":.bool(job.terminal),"handlers":.array(invoked.map(JSONValue.string)),"swift_task_cancel_forwarded":.bool(false)]))
        for w in invoked {emit("wait.cancel-handler",site:site,caller:caller,details:.object(["request":.string(request),"record":try publicationWaits.record(w).json]))}
        for action in actions {try performPublicationResume(action)}
        if forkPolicy == .earlyCancel && policyWaiter == p {
            emit("native.ordinary-start-opened-by-source-cancel",site:site,caller:caller,details:.object(["waiter":.string(p),"request":.string(request)]))
            ordinaryStartOpen.release()
        }
    }
    func observeCancel(_ caller:Caller,site:String)throws {
        try live();try actualCaller(caller)
        if publicationWaits.isCancelled(caller.job) && jobs[caller.job]?.cancelObserved == false {
            jobs[caller.job]!.cancelObserved=true
            emit("a.cancel_observed",site:site,caller:caller,details:.object(["policy":.string("finish-registered-kernel-and-cleanup")]))
        }
    }
    private func beginDrain(_ h:ScopeHandle,body:Outcome,site:String)throws->ScopeRecord {
        try live();guard let s=scopes[h.id] else { throw RuntimeInvariant("unknown scope exit") }
        try runtimeRequire(s.handle == h,"scope exit handle mismatch")
        try runtimeRequire(s.state == "Open","scope drained twice")
        scopes[h.id]!.admission=false;scopes[h.id]!.state="Draining"
        // The caller Machine is suspended in this same drain invocation. The
        // snapshot is immutable diagnostics, never a second restoration source.
        drainSourceSnapshots[h.id]=sourceRoots[s.caller.job]?.drainStateDescription()
        emit("body.exit",site:site,caller:s.caller,scope:h,details:body.json);emit("scope.drain",site:site,caller:s.caller,scope:h)
        if forkPolicy == .closedTarget && policyWaiterScope == h.id {
            emit("native.ordinary-start-opened-by-close",site:site,caller:s.caller,scope:h,details:policyDescription)
            ordinaryStartOpen.release()
        }
        return s
    }
    private func requireSafe(_ root:String,seen:Set<String>)throws {
        if !seen.isEmpty && root.hasPrefix("Scope:") {try runtimeRequire(scopes[String(root.dropFirst(6))]?.state == "Joined","unjoined required Scope")}
        if seen.contains(root) { return };var seen=seen;seen.insert(root)
        if let state=obligations[root] { try runtimeRequire(state == "Safe","Unresolved requirement "+root) }
        for child in edges[root,default:[]] { try requireSafe(child,seen:seen) }
    }
    func retire(_ caller:Caller,outcome:Outcome,site:String)throws {
        try live();guard let job=jobs[caller.job] else { throw RuntimeInvariant("retire unknown job") }
        try runtimeRequire(caller == job.exitCaller && caller.scopes.isEmpty,"RETIRE caller is not exact RuntimeExit")
        try runtimeRequire(!job.retired && !job.terminal && job.prepared == nil && job.outcome == nil && job.publication == nil &&
            regions[caller.region]?.field("state").text() == "Live" && sourceRoots[caller.job]?.retirementReady() == true && job.frames.isEmpty,"RETIRE with recoverable source control/nonlive job")
        if case .infrastructure=outcome {throw RuntimeInvariant("RETIRE cannot publish infrastructure as source outcome")}
        if case .failure(let ids)=outcome {try runtimeRequire(!ids.isEmpty && Set(ids).count == ids.count && ids.allSatisfy{faults[$0] != nil},"RETIRE Failure missing original fault identities")}
        try runtimeRequire(registrations.values.filter{$0.anchor == "Job:"+caller.job}.allSatisfy {
            obligations["Registration:"+$0.id] == "Safe"
        },"RETIRE with unresolved direct Registration")
        try runtimeRequire(job.children.allSatisfy { scopes[$0]?.state == "Joined" },"RETIRE with unjoined child")
        try runtimeRequire(job.accesses.allSatisfy { accesses[$0]?.state == "Revoked" },"RETIRE with usable access")
        if job.role == "producer" {emit("a.submissions_stopped",site:site,caller:caller,details:.object(["retirement_preconditions_checked":.bool(true)]))}
        if let bodyRegion=job.bodyRegion,bodyRegion != job.exitCaller.region {endRegion(bodyRegion)}
        jobs[caller.job]!.retired=true
        sourceRoots.removeValue(forKey:caller.job);roots.remove("Source:"+caller.job)
        emit("source.retired",site:site,caller:caller,details:.object(["source_environment_cleared":.bool(true)]))
        jobs[caller.job]!.prepared=outcome
        emit("result.prepared",site:site,caller:caller,details:outcome.json)
        emit("task.publish-ready",site:site,caller:caller)
    }
    func hostTaskAwaitFailed(job:String,message:String,actualExit:RuntimeTaskExit?=nil)->ExecutionReport? {
        jobs[job]?.actualTaskExitObserved=true
        taskExitObservations[job] = .object(["channel":.string("HostRoot"),"kind":.string(actualExit == nil ? "threw":"returned-invalid"),
                                            "actual_exit":actualExit?.diagnostic ?? .null,"error":.string(message)])
        if obligations["Job:"+job] == "Active" {obligations["Job:"+job]="Unresolved"}
        emit("runtime.unresolved",caller:jobs[job]?.caller,details:.object(["message":.string(message)]))
        return infrastructure(message) // original actual Task and all roots retained
    }

    func receiveExternal(port:String,owner:OwnerHandle,caller:Caller,site:String,
                         kont:WaitContinuation,machine:Machine)async throws->(String,RuntimeValue) {
        try live();try actualCaller(caller)
        try runtimeRequire(sourceRoots[caller.job] === machine && kont.caller == caller,"host input foreign source holder")
        let id=fresh("host-input")
        try kont.claim(id)
        hostInputWaits[id]=HostInputWaitRecord(id:id,port:port,owner:owner.id,site:site,caller:caller,kont:kont)
        roots.insert("HostInput:"+id)
        emit("external.receive-enter",site:site,caller:caller,details:hostInputWaits[id]!.json)
        do {
            guard port == "B" else {throw fault("HostInputPort",caller:caller,site:site)}
            guard let job=jobs[caller.job],job.role == "main",job.caller.task == caller.task else {
                throw fault("HostInputCaller",caller:caller,site:site)
            }
            guard let record=owners[owner.id] else {throw RuntimeInvariant("host input foreign Owner")}
            if record.epoch != nil {throw fault("OwnerBusy",caller:caller,site:site)}
            if externalInputClaimed {throw fault("HostInputAlreadyClaimed",caller:caller,site:site)}
            guard let externalDemand else {throw fault("HostInputUnavailable",caller:caller,site:site)}
            externalInputClaimed=true
            // A mailbox suspension never enters A's Scope/BackendReturn requires.
            // The control retains the payload waiter; this actor retains the one
            // source continuation. No Shared or replacement task is constructed.
            let tokens=try await externalDemand.receive(port:port,owner:owner,caller:caller,site:site)
            try live();try actualCaller(caller)
            try runtimeRequire(tokens.count == 8 && tokens.allSatisfy{$0 < 151936},"invalid complete host payload")
            try runtimeRequire(owners[owner.id]?.epoch == nil && hostInputWaits[id]?.kont === kont,
                               "host input source/Owner changed during wait")
            hostInputWaits[id]!.tokens=tokens;hostInputWaits[id]!.phase="delivered"
            try restoreHostInput(id,holder:kont,machine:machine)
            var value=RuntimeValue.word(tokens[7])
            for token in tokens.dropLast().reversed() {value = .pair(.word(token),value)}
            return (id,value)
        } catch let failure as SourceFailure {
            // A recoverable source rejection returns the complete original frame.
            // Infrastructure/Unknown errors deliberately keep its sole holder.
            hostInputWaits[id]!.rejected=true;hostInputWaits[id]!.phase="rejected"
            try restoreHostInput(id,holder:kont,machine:machine)
            throw failure
        }
    }
    private func restoreHostInput(_ id:String,holder:WaitContinuation,machine:Machine)throws {
        guard let record=hostInputWaits[id],record.kont === holder,!record.returned else {
            throw RuntimeInvariant("host input restoration identity/once")
        }
        try holder.restore(id) { state in try machine.restoreWait(holder,state:state) }
        hostInputWaits[id]!.history=holder.description;hostInputWaits[id]!.kont=nil
        hostInputWaits[id]!.returned=true
        // Successful receive is not complete until the original source caller
        // observes its return. Rejections have no payload to mark received.
        if record.rejected {roots.remove("HostInput:"+id)}
        emit("external.receive-restored",site:record.site,caller:record.caller,details:hostInputWaits[id]!.json)
    }
    func externalReceiveReturned(_ id:String,caller:Caller,tokens:[UInt64])throws {
        try live();try actualCaller(caller)
        guard let record=hostInputWaits[id],record.caller == caller,record.returned,!record.received,
              !record.rejected,record.kont == nil,record.tokens == tokens else {
            throw RuntimeInvariant("host input actual receive identity/once")
        }
        hostInputWaits[id]!.received=true;hostInputWaits[id]!.phase="received"
        roots.remove("HostInput:"+id)
        emit("external.receive-returned",site:record.site,caller:caller,details:hostInputWaits[id]!.json)
    }

    private var serviceWaitDescription:JSONValue {.object(["id":serviceWaitID.map(JSONValue.string) ?? .null,
        "caller":serviceWaitCaller?.json ?? .null,"site":serviceWaitSite.map(JSONValue.string) ?? .null,
        "returned":.bool(serviceWaitReturned),"received":.bool(serviceWaitReceived),"rejected":.bool(serviceWaitRejected),"stopped":.bool(serviceWaitStopped),
        "kont":serviceWait?.description ?? serviceWaitHistory,"invocation":serviceEntry?.invocation.diagnostic ?? .null])}
    private func restoreServiceWait(_ holder:WaitContinuation,machine:Machine)throws {
        guard let id=serviceWaitID,serviceWait === holder,!serviceWaitReturned else {throw RuntimeInvariant("service continuation identity/once")}
        try holder.restore(id){try machine.restoreWait(holder,state:$0)}
        serviceWaitHistory=holder.description;serviceWait=nil;serviceWaitReturned=true
        if serviceWaitRejected || serviceWaitStopped {roots.remove("ServiceWait:"+id)}
        emit("service.wait-restored",site:serviceWaitSite,caller:serviceWaitCaller,details:serviceWaitDescription)
    }
    func callService(name:String,scope:ScopeHandle,tokens:RuntimeValue,caller:Caller,site:String,
                     kont:WaitContinuation,machine:Machine)async throws->RuntimeValue {
        try live();try actualCaller(caller)
        try runtimeRequire(sourceRoots[caller.job] === machine && kont.caller == caller,"service foreign source continuation")
        // Even an ordinary source rejection returns precisely this same frame.
        let id=fresh("service-wait");try kont.claim(id)
        let priorWait=serviceWaitDescription
        if serviceWaitID != nil {
            // The first consumed response remains immutable on a caught repeat.
            try kont.restore(id){try machine.restoreWait(kont,state:$0)}
            throw fault("ServiceRepeatedCall",caller:caller,site:site,details:priorWait)
        }
        serviceWaitID=id;serviceWait=kont;serviceWaitCaller=caller;serviceWaitSite=site
        roots.insert("ServiceWait:"+id);emit("service.wait-enter",site:site,caller:caller,details:serviceWaitDescription)
        do {
            guard let parentHost,program.executionDomain == .hostParent,name == "B",
                  let job=jobs[caller.job],job.role == "main",job.caller.task == caller.task else {throw fault("ServiceInvalidCaller",caller:caller,site:site)}
            guard let current=scopes[scope.id],current.handle == scope,scope.owner == nil,scope.epoch == nil,
                  current.caller == caller,caller.scopes.last == scope.id,current.state == "Open",current.admission else {throw fault("ServiceInvalidScope",caller:caller,site:site)}
            let arguments=try prefillArguments(operation:"Prefill8",value:tokens,caller:caller,site:site)
            serviceCreationAttempted=true
            let entry:ServiceEntry
            do {entry=try parentHost.registry.reserve(parentLedger:ledgerID,caller:caller,scope:scope,site:site,tokens:arguments.tokens)}
            catch let rejected as ServicePreparationRejected {throw fault("ServiceCreationRejected",caller:caller,site:site,details:rejected.original)}
            serviceEntry=entry
            let obligation="HostClose:"+entry.invocation.serviceID
            obligations[obligation]="Active";edge("Scope:"+scope.id,obligation);roots.insert(obligation)
            scopes[scope.id]!.runtimeRoots.append(obligation)
            emit("service.host-close-registered",site:site,caller:caller,scope:scope,details:entry.invocation.diagnostic)
            try live();try runtimeRequire(scopes[scope.id]?.admission == true,"service close before commit")
            try parentHost.registry.commit(entry)
            emit("service.committed",site:site,caller:caller,scope:scope,details:entry.invocation.diagnostic)
            await parentCPU?.beforeResponseWait(serviceID:entry.invocation.serviceID)
            try live();try actualCaller(caller)
            let response=await entry.port.awaitParent()
            try live();try actualCaller(caller)
            switch response {
            case .success(let evidence):
                try runtimeRequire(evidence.invocation === entry.invocation && evidence.receipt.request.tokens == arguments.tokens &&
                    evidence.receipt.request.session.session == entry.invocation.session,"service delivered evidence identity")
                serviceConsumed=evidence
                try restoreServiceWait(kont,machine:machine)
                return .word(evidence.token)
            case .failure(let failure):
                if ["ServiceChildSourceFailure","ServiceResponseContractFailure","ServiceCreationRejected"].contains(failure.code) {
                    throw fault(failure.code,caller:caller,site:site,details:failure.json,causeKeys:failure.originalIDs.map{"service:"+entry.invocation.serviceID+":"+$0})
                }
                throw RuntimeInvariant("service response infrastructure: "+failure.code)
            }
        } catch let failure as SourceFailure {
            serviceWaitRejected=true;try restoreServiceWait(kont,machine:machine)
            if !serviceCreationAttempted {
                rejectedServiceWaits.append(serviceWaitDescription);serviceWaitID=nil;serviceWaitCaller=nil;serviceWaitSite=nil
                serviceWaitHistory = .null;serviceWaitReturned=false;serviceWaitReceived=false;serviceWaitRejected=false
            }
            throw failure
        } catch {
            parentHost?.registry.requestAbortIfOwned(parentLedger:ledgerID,reason:.runtimeFailure)
            let original=backendDiagnostic(error)
            // Restore the original nine-field frame even when stop won while
            // this Task was waiting. This permits only its inactive exit: no
            // response consume, source-received event or successor is run.
            if serviceWait === kont && !serviceWaitReturned {
                serviceWaitStopped=true
                do {try restoreServiceWait(kont,machine:machine)}
                catch {emit("service.wait-restore-failed",site:site,caller:caller,details:.object(["original_error":original,"restore_error":backendDiagnostic(error),"wait":serviceWaitDescription]))}
            }
            emit("service.wait-unresolved",site:site,caller:caller,details:.object(["error":original,"wait":serviceWaitDescription]));throw error
        }
    }
    func serviceReceiveReturned(caller:Caller,token:UInt64)throws {
        try live();try actualCaller(caller)
        guard let entry=serviceEntry,let evidence=serviceConsumed,serviceWaitCaller == caller,
              serviceWaitReturned,!serviceWaitReceived,!serviceWaitRejected,serviceWait == nil,evidence.token == token else {throw RuntimeInvariant("service actual source return identity/once")}
        try entry.port.consumed(evidence);serviceWaitReceived=true
        if let id=serviceWaitID {roots.remove("ServiceWait:"+id)}
        emit("service.source-received",site:serviceWaitSite,caller:caller,details:.object(["wait":serviceWaitDescription,"evidence":evidence.diagnostic]))
        do {try parentCPU?.afterResponseConsumed(serviceID:entry.invocation.serviceID)}
        catch {emit("native.parent-infrastructure-after-consume",caller:caller,details:backendDiagnostic(error));throw RuntimeInvariant("native parent post-consume infrastructure: "+String(describing:error))}
    }
    func prepareTool(name:String,argument:UInt64,caller:Caller,site:String)throws->(ToolInvocation,ToolReadRoot,any VocabSymbolTool) {
        try live();try actualCaller(caller)
        guard let parentHost,program.executionDomain == .hostParent,name == "tokenizer.vocab_symbol",
              jobs[caller.job]?.role == "main",jobs[caller.job]?.caller.task == caller.task,
              let sid=caller.scopes.last,let scope=scopes[sid],scope.caller == caller,scope.handle.owner == nil,
              scope.state == "Open",scope.admission else {throw fault("ToolInvalidCallerOrScope",caller:caller,site:site)}
        guard serviceWaitReceived,let evidence=serviceConsumed,evidence.invocation.parentScope == sid,
              evidence.invocation.parentCaller.job == caller.job,evidence.invocation.parentCaller.task == caller.task else {throw fault("ToolResponseUnavailable",caller:caller,site:site)}
        guard toolRoots.isEmpty else {throw RuntimeInvariant("fixed tool operation repeated")}
        let id=fresh("tool-call"),invocation=ToolInvocation(id:id,argument:argument,response:evidence,caller:caller,scope:scope.handle,site:site,file:parentHost.file)
        let root=ToolReadRoot(invocation);toolRoots[id]=root
        obligations["ToolIO:"+id]="Active";edge("Scope:"+sid,"ToolIO:"+id);roots.insert("ToolIO:"+id);scopes[sid]!.runtimeRoots.append("ToolIO:"+id)
        emit("tool.registered",site:site,caller:caller,scope:scope.handle,details:invocation.diagnostic)
        return(invocation,root,parentHost.tool)
    }
    func completeTool(_ outcome:ToolCallOutcome,invocation:ToolInvocation,root:ToolReadRoot,caller:Caller,site:String)throws->RuntimeValue {
        try runtimeRequire(outcome.root === root && root.invocation === invocation && toolRoots[invocation.toolCallID] === root &&
            invocation.caller == caller && invocation.site == site && toolOutcomes[invocation.toolCallID] == nil,"foreign/repeated tool outcome")
        toolOutcomes[invocation.toolCallID]=outcome
        emit("tool.original-return",site:site,caller:caller,scope:invocation.scope,details:outcome.diagnostic)
        // A late actual close is retained above. It cannot revive the stopped
        // source or restore a continuation after the watchdog has won.
        try live();try actualCaller(caller)
        guard root.resourceSafe else {obligations["ToolIO:"+invocation.toolCallID]="Unresolved";throw RuntimeInvariant("ToolCloseUnknown")}
        obligations["ToolIO:"+invocation.toolCallID]="Safe";roots.remove("ToolIO:"+invocation.toolCallID)
        emit("tool.io-safe",site:site,caller:caller,scope:invocation.scope,details:root.snapshot())
        switch outcome.disposition {
        case .value(let value):return value
        case .source(let code,let detail):throw fault(code.rawValue,caller:caller,site:site,details:.object(["tool_call":.string(invocation.toolCallID),"original":detail,"receipt":outcome.receipt]))
        case .infrastructure(let code,_):throw RuntimeInvariant(code.rawValue+"; original root retained")
        }
    }
    private func drainHostClose(_ scope:ScopeHandle,site:String)async throws {
        guard let entry=serviceEntry,entry.invocation.parentScope == scope.id,let parentHost else{return}
        emit("service.host-close-wait",site:site,caller:scopes[scope.id]?.caller,scope:scope,details:entry.invocation.diagnostic)
        let closure=await parentHost.registry.awaitClosure(entry)
        try runtimeRequire(closure.entry === entry,"foreign HostClose observation")
        serviceClosure=closure
        emit("service.host-close-observed",site:site,caller:scopes[scope.id]?.caller,scope:scope,details:closure.diagnostic)
        try live()
        guard closure.workJoined && closure.timerJoined && closure.resourceClosed else {
            obligations["HostClose:"+entry.invocation.serviceID]="Unresolved";throw RuntimeInvariant("HostClose incomplete")
        }
        let infrastructure=closure.completion?.startupAbort == nil && (closure.completion?.failures.contains{$0.domain != "response"} ?? false)
        obligations["HostClose:"+entry.invocation.serviceID]="Safe";roots.remove("HostClose:"+entry.invocation.serviceID)
        emit("service.host-close-safe",site:site,caller:scopes[scope.id]?.caller,scope:scope,details:closure.diagnostic)
        if infrastructure {throw RuntimeInvariant("service host failure after actual resource close")}
    }

    func closureObservation()async->(closed:Bool,sourceTasksObserved:Bool,backend:MLXClosedEvidence?,json:JSONValue) {
        if let parentHost,let entry=serviceEntry,serviceClosure == nil {
            // Infrastructure can skip the source's normal drain. The retained
            // registry still observes the actual leaf work, without waiting on
            // this parent. This late fact changes neither Scope nor publication.
            let actual=await parentHost.registry.awaitClosure(entry)
            if actual.entry === entry {lateServiceClosure=actual;emit("service.late-close-observed",details:actual.diagnostic)}
        }
        let toolClosed=toolRoots.values.allSatisfy(\.resourceSafe)
        let closed=parentHost == nil ? lateBackendValidated && toolClosed : toolClosed && (serviceEntry == nil || (serviceClosure ?? lateServiceClosure)?.resourceClosed == true)
        let sourceTasksObserved=jobs.values.allSatisfy{$0.actualTaskExitObserved} && registrations.values.allSatisfy{$0.task == nil || $0.joined}
        return(closed,sourceTasksObserved,lateBackendValidated ? lateBackendEvidence:nil,snapshot())
    }

    /// There is no await, callback or reply in this operation. A source may
    /// offer inside its RUN while the Scope/BackendReturn are still Active.
    /// Every rejection leaves the first valid intent unchanged and is catchable.
    func offerExternalResponse(port:String,owner:OwnerHandle,token:UInt64,caller:Caller,site:String)throws {
        let backend=try requireBackend()
        try live();try actualCaller(caller)
        guard port == "B" else {throw fault("HostResponsePort",caller:caller,site:site)}
        guard let main=jobs[caller.job],main.role == "main",main.caller.task == caller.task else {
            throw fault("HostResponseCaller",caller:caller,site:site)
        }
        guard let externalDemand else {throw fault("HostResponseUnavailable",caller:caller,site:site)}
        let receives=hostInputWaits.values.filter{$0.received && !$0.rejected && $0.caller.job == caller.job && $0.caller.task == caller.task}
        guard receives.count == 1,let input=receives.first,input.returned,input.kont == nil,
              input.port == port,input.tokens == externalDemand.request.tokens else {
            throw fault("HostResponseRequest",caller:caller,site:site)
        }
        guard let request=externalBRequest,let receipt=externalBReceipt,
              mlxCommands[request.commandID]?.phase == "accepted-success",
              mlxCommands[request.commandID]?.receipt == receipt else {
            throw fault("HostResponseReceipt",caller:caller,site:site)
        }
        guard owners[owner.id] != nil,owner.id == input.owner,owner.id == request.owner else {
            throw fault("HostResponseOwner",caller:caller,site:site)
        }
        try runtimeRequire(request.session == backend.identity && receipt.request == request &&
            request.session.session == externalDemand.request.session &&
            request.operation == externalDemand.request.operation && request.tokens == externalDemand.request.tokens,
            "source response adopted request/receipt identity")
        guard try receipt.token == token else {throw fault("HostResponseToken",caller:caller,site:site)}
        guard externalResponseIntent == nil else {throw fault("HostResponseAlreadyOffered",caller:caller,site:site)}
        let intent=SourceResponseIntent(id:fresh("response-intent"),port:port,owner:owner.id,receive:input.id,
            site:site,token:token,caller:caller,externalRequest:externalDemand.request,request:request,receipt:receipt)
        externalResponseIntent=intent
        emit("external.response-offered",site:site,caller:caller,details:.object([
            "intent":intent.json,"state":snapshot()]))
    }

    func allocate(size:UInt64,caller:Caller,site:String)throws->(OwnerHandle,[Witness]) {
        let backend=try requireBackend()
        try live();try actualCaller(caller)
        if size == 0 {throw fault("AllocationSizeZero",caller:caller,site:site)}
        try runtimeRequire(size == 1,"size outside CM1 executable profile")
        if !owners.isEmpty {throw fault("BackendOwnerBusy",caller:caller,site:site)}
        return try backend.withHealthyAdmission {
            let id=fresh("owner")
            owners[id]=OwnerRecord(poolGroup:backend.identity.poolGroup);roots.insert("Owner:"+id)
            emit("allocate",site:site,caller:caller,details:.object(["owner":.string(id),"size":.string("1"),"allocation_kind":.string("MLXPrefillOwner1"),"backend_contract":cm1BackendContract,"footprint":backend.identity.json]))
            return (OwnerHandle(id:id),[.size(1),.brand(sort:"Owner",id:id)])
        }
    }
    func enterScope(owner:OwnerHandle?,parent:Caller,site:String,retained:[SlotID])throws->ScopeContext {
        try live();try actualCaller(parent)
        if let owner {
            guard let current=owners[owner.id] else {throw RuntimeInvariant("unknown Owner")}
            if current.epoch != nil {throw fault("OwnerBusy",caller:parent,site:site)}
        }
        let ownerBackend=owner == nil ? nil:try requireBackend()
        var acquireStamp:BRSServiceStamp?
        func commit()->ScopeContext {
            let epoch=owner.map{_ in fresh("epoch")}
            if let owner {owners[owner.id]!.epoch=epoch;owners[owner.id]!.ordinal += 1;owners[owner.id]!.word=nil}
            let sid=fresh("scope"),r=fresh("region"),i=fresh("invocation")
            let h=ScopeHandle(id:sid,owner:owner?.id,epoch:epoch)
            let c=Caller(job:parent.job,task:parent.task,invocation:i,region:r,scopes:parent.scopes+[sid])
            let frame=ActiveFrame(id:fresh("frame"),kind:"ScopeExit",site:site,retained:retained,caller:parent)
            scopes[sid]=ScopeRecord(handle:h,caller:c,parent:parent);region(r,kind:"scope",parent:parent.region)
            scopes[sid]!.creationSite=site
            roots.insert("Scope:"+sid)
            if let p=parent.scopes.last,scopes[p]?.caller == parent {
                scopes[p]!.children.append(sid);edge("Scope:"+p,"Scope:"+sid)
            } else {
                jobs[parent.job]!.children.append(sid);edge("Job:"+parent.job,"Scope:"+sid)
            }
            jobs[parent.job]!.frames.append(frame)
            if let owner,let epoch {
                let beta=fresh("backend-return")
                backendReturns[beta]=BackendReturnRecord(id:beta,owner:owner.id,epoch:epoch,scope:sid,poolGroup:ownerBackend!.identity.poolGroup)
                scopes[sid]!.backendReturn=beta;obligations["BackendReturn:"+beta]="Active"
                edge("Scope:"+sid,"BackendReturn:"+beta);roots.insert("BackendReturn:"+beta)
                acquireStamp = .capture(domain:"swift_task")
                emit("backend-return.issued",site:site,caller:c,scope:h,details:backendReturns[beta]!.json)
            }
            emit("frame.push",site:site,caller:parent,details:frame.json)
            emit(owner == nil ? "scope.enter":"run.acquire",site:site,caller:c,scope:h,details:.object(["parent":parent.json,"ordinal":owner.map{.integer(owners[$0.id]!.ordinal)} ?? .null,"backend_return_contract":owner == nil ? .null:cm1BackendContract]))
            let names:[Witness] = owner == nil ? [.brand(sort:"Scope",id:sid),.brand(sort:"Region",id:r),.brand(sort:"Invocation",id:i)] : [.brand(sort:"Epoch",id:epoch!),.brand(sort:"Scope",id:sid),.brand(sort:"Region",id:r),.brand(sort:"Invocation",id:i)]
            return ScopeContext(handle:h,caller:c,names:names,frame:frame)
        }
        if owner != nil {
            let context=try ownerBackend!.withHealthyAdmission {commit()}
            observeService("owner.acquire",caller:context.caller,scope:context.handle,site:site,
                stamp:acquireStamp,details:.object(["parent":parent.json,"operation_kind":.string("RUN-enter")]))
            return context
        }
        return commit()
    }
    func prefillArguments(operation:String,value:RuntimeValue,caller:Caller,site:String)throws->PrefillArguments {
        try live();try actualCaller(caller)
        let count:Int
        switch operation {case "Prefill32":count=32;case "Prefill8":count=8;default:throw RuntimeInvariant("unregistered kernel")}
        var current=value,tokens:[UInt64]=[]
        for _ in 1..<count {
            guard case .pair(.word(let token),let tail)=current else {throw RuntimeInvariant("kernel argument is not the checked right-nested Word Pair")}
            tokens.append(token);current=tail
        }
        guard case .word(let last)=current else {throw RuntimeInvariant("kernel final argument is not Word")};tokens.append(last)
        if let index=tokens.firstIndex(where:{$0 >= 151936}) {
            throw fault("PrefillTokenRange",caller:caller,site:site,details:.object(["index":.integer(index),"token":.unsigned(tokens[index]),"proof":.string("parent-validation-before-registration-and-send")]))
        }
        return PrefillArguments(operation:operation,tokens:tokens)
    }
    func evaluatePrefill(_ context:ProducerContext,arguments:PrefillArguments,site:String)async throws {
        let backend=try requireBackend()
        try live();let caller=context.kernelCaller
        try actualCaller(caller);try validAccess(context.write,job:caller.job,mode:"Write")
        guard let s=scopes[context.scope.id],let beta=s.backendReturn,let owner=context.scope.owner,let epoch=context.scope.epoch else {throw RuntimeInvariant("prefill has no backend return")}
        try runtimeRequire(s.prefillClaimed && s.producerJob == caller.job && s.local.contains(caller.job) && accesses[context.write]?.commands.isEmpty == true &&
            jobs[caller.job]?.frames.count == 1 && jobs[caller.job]?.frames.last?.kind == "KernelFinish" &&
            backendReturns[beta]?.phase == "unoccupied" && obligations["BackendReturn:"+beta] == "Active","prefill admission/control mismatch")
        let command=issue(context.write,kind:arguments.operation+".mlx-host-eval",caller:caller,site:site)
        let request=try MLXEvalRequest(commandID:command,operation:arguments.operation,tokens:arguments.tokens,owner:owner,sourceEpoch:epoch,ownerOrdinal:owners[owner]!.ordinal,scope:s.handle.id,returnID:beta,caller:caller,access:context.write,site:site,session:backend.identity)
        mlxCommands[command]=MLXCommandRecord(request:request);roots.insert("MLXCommand:"+command);edge("Command:"+command,"MLXCommand:"+command)
        backendReturns[beta]!.phase="eval-pending";backendReturns[beta]!.command=command
        if arguments.operation == "Prefill8",let externalDemand {
            try runtimeRequire(externalBRegistered && externalBRequest == nil,"external B eval registration/once")
            try externalDemand.observeBRequest(request);externalBRequest=request
        }
        emit("mlx.eval_requested",site:site,caller:caller,scope:context.scope,details:request.json)
        do {
            let receipt=try await backend.evaluate(request)
            // Raw successful evidence remains recorded even if a watchdog has
            // already won. Such late evidence cannot authorize release.
            mlxCommands[command]!.receipt=receipt
            emit("mlx.receipt_observed",site:site,caller:caller,scope:context.scope,details:receipt.json)
            try live();try actualCaller(caller);try validAccess(context.write,job:caller.job,mode:"Write")
            try runtimeRequire(mlxCommands[command]?.phase == "issued" && backendReturns[beta]?.phase == "eval-pending" && obligations["BackendReturn:"+beta] == "Active","receipt adoption phase")
            let history=try validateMLXReceipt(receipt,expected:request,lastSequence:lastWireSequence,lastLease:lastWireLease,pageHistory:pageEpochs)
            let token=try receipt.token,sequence=try receipt.evalSequence,lease=try receipt.wireLease
            // No await or throw from here through the corresponding Safe writes.
            pageEpochs=history;lastWireSequence=sequence;lastWireLease=lease
            mlxCommands[command]!.phase="accepted-success"
            backendReturns[beta]!.receipt=receipt;backendReturns[beta]!.phase="occupied"
            owners[owner]!.word=token;commands[command]!.completed=true;obligations["Command:"+command]="Safe"
            roots.remove("Command:"+command);roots.remove("MLXCommand:"+command)
            emit("mlx.receipt_accepted",site:site,caller:caller,scope:context.scope,details:receipt.json)
            emit("command.completed",site:site,caller:caller,details:.object(["command":.string(command),"evidence":.string("host-mx-eval-and-token-item")]))
            if arguments.operation == "Prefill8",let externalDemand {
                try runtimeRequire(externalBRequest?.commandID == request.commandID && externalBReceipt == nil,
                                   "external B receipt registration/once")
                try externalDemand.observeBReceipt(receipt);externalBReceipt=receipt
            }
        } catch {
            let failure=backendDiagnostic(error);backendFailure=failure
            mlxCommands[command]!.failure=failure
            if obligations["Command:"+command] == "Active" {obligations["Command:"+command]="Unresolved";mlxCommands[command]!.phase="unresolved"}
            if obligations["BackendReturn:"+beta] == "Active" {obligations["BackendReturn:"+beta]="Unresolved";backendReturns[beta]!.phase="unknown";backendReturns[beta]!.failure=failure}
            emit("mlx.incomplete",site:site,caller:caller,scope:context.scope,details:failure)
            throw error
        }
    }
    func finishWrite(_ context:ProducerContext,site:String)throws->ActiveFrame {
        try live()
        guard let ids=accesses[context.write]?.commands,ids.count == 1,let record=mlxCommands[ids[0]] else {throw RuntimeInvariant("Prefill missing actual eval Command")}
        try runtimeRequire(record.phase == "accepted-success" && record.receipt != nil && obligations["Command:"+ids[0]] == "Safe" && owners[context.read.owner]?.word != nil,"Prefill lacks adopted initialized token")
        guard let old=jobs[context.caller.job]?.frames.last,old.kind == "KernelFinish",jobs[context.caller.job]!.frames.count == 1 else {throw RuntimeInvariant("missing unique kernel continuation")}
        try seal(context.write,caller:context.kernelCaller,site:site)
        emit("write.submissions_stopped",site:site,caller:context.kernelCaller,scope:context.scope,details:.object(["access":.string(context.write)]))
        accesses[context.read.access]!.state="Live";regions[context.read.region] = .object(["kind":.string("cleanup"),"state":.string("Live"),"parent":.string(accesses[context.write]!.region)])
        let frame=ActiveFrame(id:old.id,kind:"JobFinish",site:site,retained:[],caller:context.caller);jobs[context.caller.job]!.frames=[frame]
        emit("frame.transition",site:site,caller:context.caller,details:.object(["id":.string(old.id),"before":.string("KernelFinish"),"after":.string("JobFinish")]))
        emit("cleanup.started",site:site,caller:context.caller,scope:context.scope);return frame
    }

    func drain(_ h:ScopeHandle,body:Outcome,site:String)async throws->Outcome {
        if h.owner != nil,let caller=scopes[h.id]?.caller {observeService("drain.enter",caller:caller,scope:h,site:site)}
        let s=try beginDrain(h,body:body,site:site)
        var failed:[String]=[]
        if case .failure(let ids)=body {failed += ids;reported(ids,channel:.object(["scope":.string(h.id),"kind":.string("body")]))}
        if case .infrastructure(let text)=body {throw RuntimeInvariant(text)}
        for p in s.local {
            guard let task=jobs[p]?.task else {throw RuntimeInvariant("registered job lost original Task")}
            emit("scope.join-callsite",site:site,caller:s.caller,scope:h,details:.object(["job":.string(p),"task":.string(jobs[p]!.caller.task)]))
            let publication:PublishedOutcome
            do {
                if h.owner != nil {observeService("ownjoin.call",caller:s.caller,scope:h,site:site,
                    details:.object(["joined_job":.string(p),"joined_task":.string(jobs[p]!.caller.task)]))}
                let actual=try await task.value
                if h.owner != nil {observeService("ownjoin.return",caller:s.caller,scope:h,site:site,
                    details:.object(["joined_job":.string(p),"joined_task":.string(jobs[p]!.caller.task),"result":.string("returned")]))}
                jobs[p]!.actualTaskExitObserved=true
                taskExitObservations[p] = .object(["channel":.string(h.id),"kind":.string("actual-Task.value-returned"),"actual_exit":actual.diagnostic])
                guard case .published(let value)=actual else {throw RuntimeInvariant("scope joined unpublished infrastructure exit")}
                try runtimeRequire(value.job == p && value.task == jobs[p]?.caller.task,"scope joined foreign original Task")
                try observeTerminal(value,channel:h.id,site:site);publication=value
            } catch {
                jobs[p]!.actualTaskExitObserved=true
                if taskExitObservations[p] == nil {taskExitObservations[p] = .object(["channel":.string(h.id),"kind":.string("actual-Task.value-threw"),"failure":backendDiagnostic(error)])}
                if obligations["Job:"+p] == "Active" {obligations["Job:"+p]="Unresolved"}
                throw error
            }
            try live();try runtimeRequire(jobs[p]?.retired == true && jobs[p]?.publication === publication,"join before source retirement")
            jobs[p]!.joins.insert(h.id);scopes[h.id]!.joined.insert(p);jobs[p]!.task=nil
            if obligations["Job:"+p] == "Active" {obligations["Job:"+p]="Safe"}
            if obligations["Job:"+p] == "Safe" {roots.remove("Job:"+p);roots.remove("Task:"+jobs[p]!.caller.task)}
            emit("scope.join",site:site,caller:s.caller,scope:h,details:.object(["job":.string(p),"publication":.string(publication.id),"outcome":publication.outcome.json]))
            // Additional sample is after actual Task.value, original publication
            // checks and the committed scope.join; it is not the earlier BRS tick.
            if h.owner != nil {returnService?.ownJoinReturned(worker:serviceWorker,scope:h,
                producer:jobs[p]!.exitCaller,main:s.caller,publication:publication.id)}
            if h.owner != nil {try externalDemand?.ownJoinReturned(worker:serviceWorker,scope:h,
                main:s.caller,producer:jobs[p]!.exitCaller,publication:publication.id)}
            if case .failure(let ids)=publication.outcome {failed += ids;reported(ids,channel:.object(["scope":.string(h.id),"kind":.string("local-job"),"job":.string(p)]))}
        }
        try await drainHostClose(h,site:site)
        if let beta=s.backendReturn {try await returnBackend(h,beta:beta,site:site)}
        try commitScopeExit(h,site:site)
        let unique=failed.reduce(into:[String]()){if !$0.contains($1){$0.append($1)}}
        return unique.isEmpty ? body:.failure(unique)
    }
    /// The only exclusion is this scope's own beta. Inherited Scope nodes must
    /// still be Joined; a real dependency cycle is never deleted to pass a guard.
    private func requireBaseSafe(_ h:ScopeHandle,beta:String)throws {
        guard let s=scopes[h.id] else {throw RuntimeInvariant("missing scope")}
        try runtimeRequire(s.handle == h && s.state == "Draining" && !s.admission && s.backendReturn == beta &&
            Set(s.local) == s.joined && s.children.allSatisfy{scopes[$0]?.state == "Joined"},"backend return before actual ownjoin/child finalize")
        for dependency in edges["Scope:"+h.id,default:[]] where dependency != "BackendReturn:"+beta {
            try requireSafe(dependency,seen:["Scope:"+h.id])
        }
        try runtimeRequire(owners[h.owner!]?.epoch == h.epoch && !accesses.values.contains{$0.owner == h.owner && $0.epoch == h.epoch && $0.state != "Revoked"},"backend return with live/foreign epoch access")
    }
    private func returnBackend(_ h:ScopeHandle,beta:String,site:String)async throws {
        let backend=try requireBackend()
        try live();try requireBaseSafe(h,beta:beta)
        guard let record=backendReturns[beta],record.scope == h.id,record.epoch == h.epoch,obligations["BackendReturn:"+beta] == "Active" else {throw RuntimeInvariant("return obligation not Active/current")}
        if record.phase == "unoccupied" {
            try runtimeRequire(record.command == nil && record.receipt == nil && !mlxCommands.values.contains{$0.request.sourceEpoch == record.epoch},"no-occupancy proof contradicted by issued eval")
            backendReturns[beta]!.phase="no-occupancy";obligations["BackendReturn:"+beta]="Safe";roots.remove("BackendReturn:"+beta)
            observeService("beta.safe",caller:scopes[h.id]!.caller,scope:h,site:site,details:.object(["no_occupancy":.bool(true)]))
            emit("backend-return.no-occupancy",site:site,caller:scopes[h.id]!.caller,scope:h,details:backendReturns[beta]!.json);return
        }
        guard record.phase == "occupied",let receipt=record.receipt else {throw RuntimeInvariant("return has no independently adopted receipt")}
        let request=MLXReturnRequest(receipt:receipt,returnID:beta,scope:h.id)
        let mainCaller=scopes[h.id]!.caller
        returnService?.applyPolicy(worker:serviceWorker,scope:h,main:mainCaller,
            registeredMain:jobs[mainCaller.job]?.caller,mainRole:jobs[mainCaller.job]?.role,
            task:jobs[mainCaller.job]?.task,request:request)
        do {
            try externalDemand?.applyFrontier(worker:serviceWorker,scope:h,main:mainCaller,
                registeredMain:jobs[mainCaller.job]?.caller,mainRole:jobs[mainCaller.job]?.role,
                task:jobs[mainCaller.job]?.task,request:request)
        } catch {
            // The frontier check failed before any RELEASE was sent, so the worker
            // still holds the pages. That is undischargeable here: mark the
            // obligation Unresolved (phase release-blocked, never "unknown") and the
            // scope Unresolved instead of leaving beta Active and the scope Draining.
            let failure=backendDiagnostic(error);backendFailure=failure
            if obligations["BackendReturn:"+beta] == "Active" {obligations["BackendReturn:"+beta]="Unresolved";backendReturns[beta]!.phase="release-blocked";backendReturns[beta]!.failure=failure}
            scopes[h.id]!.state="Unresolved"
            emit("backend-return.unresolved",site:site,caller:scopes[h.id]!.caller,scope:h,details:backendReturns[beta]!.json)
            throw error
        }
        backendReturns[beta]!.phase="release-sent"
        emit("backend-return.release_requested",site:site,caller:scopes[h.id]!.caller,scope:h,details:request.json)
        do {
            observeService("release.call",caller:scopes[h.id]!.caller,scope:h,site:site)
            let ack=try await backend.release(request)
            let returnStamp=BRSServiceStamp.capture(domain:"swift_task")
            backendReturns[beta]!.ack=ack
            observeService("release.return",caller:scopes[h.id]!.caller,scope:h,site:site,stamp:returnStamp)
            emit("backend-return.ack_observed",site:site,caller:scopes[h.id]!.caller,scope:h,details:ack.json)
            try live();try requireBaseSafe(h,beta:beta)
            try runtimeRequire(backendReturns[beta]?.phase == "release-sent" && obligations["BackendReturn:"+beta] == "Active","ACK adoption not once/current")
            try validateMLXReturn(ack,expected:request)
            let sequence=try backendInt(ack.raw.field("seq"))
            try runtimeRequire(sequence == lastWireSequence+1,"ACK wire sequence does not follow eval")
            lastWireSequence=sequence;backendReturns[beta]!.phase="acknowledged";obligations["BackendReturn:"+beta]="Safe";roots.remove("BackendReturn:"+beta)
            observeService("beta.safe",caller:scopes[h.id]!.caller,scope:h,site:site)
            emit("backend-return.safe",site:site,caller:scopes[h.id]!.caller,scope:h,details:backendReturns[beta]!.json)
        } catch {
            let failure=backendDiagnostic(error);backendFailure=failure
            if obligations["BackendReturn:"+beta] == "Active" {obligations["BackendReturn:"+beta]="Unresolved";backendReturns[beta]!.phase="unknown";backendReturns[beta]!.failure=failure}
            scopes[h.id]!.state="Unresolved"
            emit("backend-return.unresolved",site:site,caller:scopes[h.id]!.caller,scope:h,details:backendReturns[beta]!.json)
            throw error
        }
    }
    private func commitScopeExit(_ h:ScopeHandle,site:String)throws {
        try live();guard let s=scopes[h.id] else {throw RuntimeInvariant("unknown scope exit")}
        try runtimeRequire(s.handle == h && s.state == "Draining" && !s.admission && Set(s.local) == s.joined && s.children.allSatisfy{scopes[$0]?.state == "Joined"},"scope finalize before ownjoin")
        try requireSafe("Scope:"+h.id,seen:[])
        if let owner=h.owner,let beta=s.backendReturn {
            try requireBaseSafe(h,beta:beta)
            try runtimeRequire(obligations["BackendReturn:"+beta] == "Safe" && ["acknowledged","no-occupancy"].contains(backendReturns[beta]!.phase),"Owner release lacks return evidence")
            owners[owner]!.epoch=nil;owners[owner]!.releases += 1
            observeService("owner.release",caller:s.caller,scope:h,site:site)
            returnService?.ownerReleased(worker:serviceWorker,scope:h)
            try externalDemand?.ownerReleased(worker:serviceWorker,scope:h)
            emit("resource.released",site:site,caller:s.caller,scope:h,details:.object(["backend_return":.string(beta),"pool_group":.string(owners[owner]!.poolGroup),"before":.string("Active"),"after":.string("Idle"),"physical_free_claimed":.bool(false)]))
        }
        scopes[h.id]!.state="Joined";roots.remove("Scope:"+h.id);endRegion(s.caller.region)
        emit("scope.finalize",site:site,caller:s.caller,scope:h)
        if forkPolicy == .attachedCancellation && policyWaiterScope == h.id {
            guard let waiter=policyWaiter,let w=policyWait else {throw RuntimeInvariant("D finalize lacks original waiter")}
            let cell=try publicationWaits.record(w)
            try runtimeRequire(cell.caller.job == waiter && cell.returned && cell.received && cell.choice.label == "cancelled" &&
                s.joined.contains(waiter) && jobs[waiter]?.terminal == true && jobs[waiter]?.joins.contains(h.id) == true,
                "D finalize lacks actual cancelled caller/ownjoin")
            emit("native.waiter-scope-finalized-open-producer",site:site,caller:s.caller,scope:h,details:snapshot())
            producerOpen.release()
        }
    }

    func markUnresolved(_ caller:Caller,message:String) {
        if obligations["Job:"+caller.job] == "Active" {obligations["Job:"+caller.job]="Unresolved"}
        for a in jobs[caller.job]?.accesses ?? [] {
            if obligations["Access:"+a] == "Active" {obligations["Access:"+a]="Unresolved";accesses[a]!.state="Unknown"}
            for c in accesses[a]?.commands ?? [] where obligations["Command:"+c] == "Active" {obligations["Command:"+c]="Unresolved"}
        }
        emit("runtime.unresolved",caller:caller,details:.object(["message":.string(message)]))
    }
    func bodyReturningUnpublished(_ caller:Caller,error:any Error,sourceSnapshot:JSONValue) {
        let evidence=backendDiagnostic(error)
        unpublishedSourceSnapshots[caller.job]=sourceSnapshot
        jobs[caller.job]?.actualBodyExit = .object(["kind":.string("about-to-return-unpublished"),"failure":evidence])
        emit("task.unpublished-returning",caller:caller,details:evidence)
    }
    /// Check the independent response contract at the common source-safe
    /// boundary. Missing intent is recorded before either delivery policy's
    /// close, so a later close failure cannot hide it. Valid offers are not
    /// delivered here; their original policy-specific callsite remains below.
    private func checkExternalResponseContract(_ publication:PublishedOutcome,caller:Caller)throws->SourceResponseContractFailure? {
        guard let externalDemand,case .ok=publication.outcome,externalResponseIntent == nil else {return nil}
        try live()
        try runtimeRequire(sourceJoinSafePublication === publication && jobs[caller.job]?.publication === publication &&
            publication.job == caller.job && publication.task == caller.task && hostMainJoined,
            "response contract requires same actually joined safe main publication")
        try runtimeRequire(!externalResponseAttempted,"duplicate external response contract check")
        let failure=SourceResponseContractFailure(publication:publication.id,outcome:publication.outcome)
        externalResponseAttempted=true;externalResponsePublication=publication.id
        externalResponseState="contract-failed";externalResponseFailure=failure.json
        _=externalDemand.serviceResponsePort?.abort(.init(code:"ServiceResponseContractFailure",original:failure.json))
        externalDemand.abort(reason:"source-response-contract:MissingResponseIntent")
        emit("external.response-contract-failed",caller:caller,details:.object([
            "publication":.string(publication.id),"outcome":publication.outcome.json,
            "delivery_policy":.string(externalResultDelivery.rawValue),
            "endpoint":.string("source-joined-contract-check-before-backend-close"),
            "failure":externalResponseFailure,"backend_closed":.bool(backendClosed)]))
        return failure
    }
    private func completeExternalResponse(_ publication:PublishedOutcome,caller:Caller)throws {
        guard let externalDemand else {return}
        try live()
        try runtimeRequire(sourceJoinSafePublication === publication && jobs[caller.job]?.publication === publication &&
            publication.job == caller.job && publication.task == caller.task && hostMainJoined,
            "external response requires same actually joined safe main publication")
        try runtimeRequire(!externalResponseAttempted,"duplicate external source response attempt")
        externalResponseAttempted=true;externalResponsePublication=publication.id
        let endpoint=nativeOwnsBackend ? "native-source-joined" :
            (externalResultDelivery == .afterSourceJoin ? "source-joined-before-backend-close" : "source-joined-and-backend-closed")
        do {
            switch publication.outcome {
            case .ok:
                guard let intent=externalResponseIntent else {
                    throw RuntimeInvariant("response commit bypassed missing-intent contract check")
                }
                guard let receipt=externalBReceipt,let request=externalBRequest,
                      let input=hostInputWaits[intent.receive] else {
                    throw RuntimeInvariant("source response intent lost original B receipt/input")
                }
                try runtimeRequire(try intent.caller.job == caller.job && intent.caller.task == caller.task &&
                    intent.port == externalDemand.request.port && intent.externalRequest.json == externalDemand.request.json &&
                    intent.request == request && intent.receipt == receipt && receipt.request == request &&
                    intent.owner == input.owner && intent.owner == request.owner &&
                    input.received && input.returned && !input.rejected && input.kont == nil &&
                    input.caller.job == caller.job && input.caller.task == caller.task &&
                    input.tokens == request.tokens && intent.token == receipt.token,
                    "source response intent/receipt/main identity changed before commit")
                if let port=externalDemand.serviceResponsePort {
                    guard let boundary=sourceJoinSafeBoundarySequence else {throw RuntimeInvariant("service commit missing source-safe boundary")}
                    let evidence=try ChildResponseEvidence(invocation:port.invocation,publication:publication,receipt:receipt,intent:intent.json,boundary:boundary,childLedger:ledgerID)
                    try externalDemand.commitServiceResponse(evidence)
                } else {try externalDemand.reply(token:intent.token,caller:caller,publication:publication.id)}
                externalResponseState="success-committed"
                emit("external.source-reply",caller:caller,details:.object([
                    "publication":.string(publication.id),"token":.unsigned(intent.token),"request":request.json,
                    "response_intent":.string(intent.id),"source_outcome":publication.outcome.json,
                    "delivery_policy":.string(externalResultDelivery.rawValue),"endpoint":.string(endpoint),
                    "source_join_safe_boundary":sourceJoinSafeBoundarySequence.map(JSONValue.string) ?? .null,
                    "backend_closed":.bool(backendClosed),"host_final_state":.string(finalState)]))
            case .failure(let ids):
                _=externalDemand.serviceResponsePort?.abort(.init(code:"ServiceChildSourceFailure",original:.object(["publication":.string(publication.id),"outcome":publication.outcome.json]),originalIDs:ids))
                externalDemand.abort(reason:"original-source-failure")
                // abort is winner-first and may already be sealed by the
                // independent host deadline. This records our actual call,
                // not a fabricated second notification or client receipt.
                externalResponseState="source-failure-abort-requested"
                emit("external.source-failure",caller:caller,details:.object([
                    "publication":.string(publication.id),"outcome":publication.outcome.json,
                    "delivery_policy":.string(externalResultDelivery.rawValue),"endpoint":.string(endpoint),
                    "source_join_safe_boundary":sourceJoinSafeBoundarySequence.map(JSONValue.string) ?? .null,
                    "backend_closed":.bool(backendClosed),"host_final_state":.string(finalState)]))
            case .infrastructure:
                throw RuntimeInvariant("external response received infrastructure outcome as publication")
            }
        } catch {
            externalResponseState="attempt-failed";externalResponseFailure=backendDiagnostic(error)
            emit("external.source-delivery-failed",caller:caller,details:.object([
                "publication":.string(publication.id),"outcome":publication.outcome.json,
                "delivery_policy":.string(externalResultDelivery.rawValue),"endpoint":.string(endpoint),
                "failure":externalResponseFailure]))
            throw error
        }
    }
    func finishHost(job:String,publication:PublishedOutcome)async->ExecutionReport? {
        let outcome=publication.outcome
        jobs[job]?.actualTaskExitObserved=true
        if finalState != "active" {
            taskExitObservations[job] = .object(["channel":.string("HostRoot-late"),"kind":.string("actual-Task.value-returned"),"actual_exit":RuntimeTaskExit.published(publication).diagnostic])
            emit("host.late_observation",details:outcome.json);return nil
        }
        // The original observer is unique. Keep this synchronous once claim
        // across backend.shutdown's actor reentrancy; no second close/reply.
        guard !hostFinishStarted else {
            emit("host.finish-already-started",details:.object(["job":.string(job),"publication":.string(publication.id)]));return nil
        }
        hostFinishStarted=true
        taskExitObservations[job] = .object(["channel":.string("HostRoot"),"kind":.string("actual-Task.value-returned"),"actual_exit":RuntimeTaskExit.published(publication).diagnostic])
        jobs[job]?.actualTaskExitObserved=true
        do {
            try observeTerminal(publication,channel:"HostRoot",site:"/main/body")
            guard let j=jobs[job],j.publication === publication,publication.job == job else {throw RuntimeInvariant("host joined foreign main")}
            jobs[job]!.joins.insert("HostRoot");jobs[job]!.task=nil
            if obligations["Job:"+job] == "Active" {obligations["Job:"+job]="Safe"}
            if obligations["Job:"+job] == "Safe" {roots.remove("Task:"+j.caller.task);roots.remove("Job:"+job)}
            hostMainJoined=true;emit("host.join",caller:j.caller)
            try runtimeRequire(owners.values.allSatisfy{$0.epoch == nil} && scopes.values.allSatisfy{$0.state == "Joined"} && obligations.values.allSatisfy{$0 == "Safe"},"host has unresolved source obligations")
            try runtimeRequire(waits.values.allSatisfy{$0.returned && $0.kont == nil} && publicationWaits.cells.values.allSatisfy{$0.returned && $0.received && $0.kont == nil && !$0.handlerActive} && pendingPublicationResumes.isEmpty && failedPublicationInstalls.isEmpty,"host retains wait continuation/native action")
            try runtimeRequire(hostInputWaits.values.allSatisfy{$0.returned && ($0.received || $0.rejected) && $0.kont == nil},
                               "host retains external source input continuation")
            sourceJoinSafePublication=publication;sourceJoinSafeBoundarySequence=String(events.count)
            // Both policies take exactly this same snapshot at the same guard
            // boundary. snapshot stores only this event's identity, never the
            // event or its own snapshot recursively.
            emit("host.source-join-safe-boundary",caller:j.caller,details:.object([
                "publication":.string(publication.id),"outcome":outcome.json,
                "delivery_policy":.string(externalResultDelivery.rawValue),
                "host_final_state":.string(finalState),"roots":.array(roots.sorted().map(JSONValue.string)),
                "state":snapshot()]))
            if parentHost != nil {
                try runtimeRequire(backend == nil && serviceWait == nil && (serviceEntry == nil || serviceClosure?.resourceClosed == true),"parent final boundary retains HostClose/holder")
                finalState="completed";emit("host.parent-return",caller:j.caller,details:outcome.json)
                if case .failure(let ids)=outcome {reported(ids,channel:.object(["kind":.string("host-return")]))}
                let code:Int32;if case .failure=outcome {code=1}else{code=0}
                return report(status:"completed",outcome:outcome,exit:code)
            }
            let backend=try requireBackend()
            let responseContractFailure=try checkExternalResponseContract(publication,caller:j.caller)
            if nativeOwnsBackend {
                // The fixed native CPU entry owns this real session throughout
                // execute. Returning its source portion does not discharge the
                // independent process/IO roots or claim production HOST-RETURN.
                if let failure=responseContractFailure {
                    // Native fixtures retain their pre-existing close ownership.
                    // This diagnostic does not claim backend close or HOST-RETURN.
                    finalState="incomplete"
                    emit("native.response-contract-failed",caller:j.caller,details:failure.json)
                    return report(status:"incomplete",outcome:.infrastructure(failure.description),exit:2)
                }
                try completeExternalResponse(publication,caller:j.caller)
                finalState="native-source-joined"
                emit("native.source_joined",caller:j.caller,details:.object(["outcome":outcome.json,"backend_owner":.string("native-cpu-fixture"),"backend_roots":backend.snapshot()]))
                if case .failure(let ids)=outcome {reported(ids,channel:.object(["kind":.string("native-source-return")]))}
                let exit:Int32;if case .failure=outcome {exit=1}else{exit=0}
                return report(status:"native-source-joined",outcome:outcome,exit:exit)
            }
            if responseContractFailure == nil && externalResultDelivery == .afterSourceJoin {
                try completeExternalResponse(publication,caller:j.caller)
            }
            emit("host.backend-close-callsite",caller:j.caller)
            let evidence=try await backend.shutdown()
            backendCloseEvidence=evidence.json;lateBackendEvidence=evidence
            try validateMLXClosed(evidence,session:backend.identity,lastSequence:lastWireSequence)
            lateBackendValidated=true
            emit("host.backend-close-observed",caller:j.caller,details:evidence.json)
            try live()
            backendClosed=true;roots.remove("BackendSession:"+backend.identity.session)
            emit("host.backend-closed",caller:j.caller,details:evidence.json)
            if responseContractFailure == nil && externalResultDelivery == .afterBackendClose {
                try completeExternalResponse(publication,caller:j.caller)
            }
            if let failure=responseContractFailure {
                // Original main publication remains .ok (including .ok(Unit)).
                // Source safety and the real backend close have both completed;
                // only this independent response contract failed. The original
                // observer still returns and is actually joined by execute.
                finalState="incomplete"
                emit("host.response-contract-failed",caller:j.caller,details:failure.json)
                return report(status:"incomplete",outcome:.infrastructure(failure.description),exit:2)
            }
            finalState="completed";emit("host.return",caller:j.caller,details:outcome.json)
            if case .failure(let ids)=outcome {reported(ids,channel:.object(["kind":.string("host-return")]))}
            let exit:Int32;if case .failure=outcome {exit=1}else{exit=0}
            return report(status:"completed",outcome:outcome,exit:exit)
        } catch {backendFailure=backendDiagnostic(error);return infrastructure(String(describing:error))}
    }
    func timeout()->ExecutionReport? {
        guard finalState == "active" else{return nil};finalState="incomplete"
        externalDemand?.abort(reason:"runtime-watchdog")
        parentHost?.registry.requestAbortIfOwned(parentLedger:ledgerID,reason:.parentStopped)
        emit("watchdog.incomplete",details:.object(["seconds":.integer(watchdogSeconds),"release_authorized":.bool(false)]))
        return report(status:"incomplete",outcome:.infrastructure("runtime watchdog; original roots retained"),exit:124)
    }
    func infrastructure(_ message:String)->ExecutionReport? {
        guard finalState == "active" else{return nil};finalState="incomplete"
        externalDemand?.abort(reason:"runtime-infrastructure")
        parentHost?.registry.requestAbortIfOwned(parentLedger:ledgerID,reason:.runtimeFailure)
        emit("runtime.infrastructure",details:.object(["message":.string(message),"release_authorized":.bool(false)]))
        return report(status:"incomplete",outcome:.infrastructure(message),exit:2)
    }
    private func snapshot()->JSONValue {
        let directBackendApplicable = program.executionDomain != .hostParent
        var state:[String:JSONValue]=[:]
        if !directBackendApplicable {
            state["execution_domain"] = .string("hostParent")
            state["direct_backend_applicable"] = .bool(false)
        }
        state["O"] = .object(owners.mapValues{.object(["pool_group":.string($0.poolGroup),"source_epoch":$0.epoch.map(JSONValue.string) ?? .null,"ordinal":.integer($0.ordinal),"releases":.integer($0.releases),"word":$0.word.map(JSONValue.unsigned) ?? .null,"allocation_kind":.string("MLXPrefillOwner1")])})
        state["S"] = .object(scopes.mapValues{.object(["state":.string($0.state),"admission":.bool($0.admission),"caller":$0.caller.json,"parent":$0.parent.json,"localJobRoots":.array($0.local.map(JSONValue.string)),"localRuntimeRoots":.array($0.runtimeRoots.map(JSONValue.string)),"synchronousChildren":.array($0.children.map(JSONValue.string)),"joined":.array($0.joined.sorted().map(JSONValue.string)),"prefill_claimed":.bool($0.prefillClaimed),"producer_job":$0.producerJob.map(JSONValue.string) ?? .null,"backend_return":$0.backendReturn.map(JSONValue.string) ?? .null])})
        state["J"] = .object(jobs.mapValues {j -> JSONValue in
            var row:[String:JSONValue]=[:]
            row["caller"]=j.caller.json;row["boundary"] = .string(j.boundary);row["role"] = .string(j.role)
            row["result_type"]=j.resultType;row["latent_effect"]=j.latentEffect
            row["actualTaskRetained"] = .bool(j.task != nil);row["started"] = .bool(j.started)
            row["sourceRetired"] = .bool(j.retired);row["terminalObserved"] = .bool(j.terminal)
            row["actual_task_exit_observed"] = .bool(j.actualTaskExitObserved);row["actual_body_exit"]=j.actualBodyExit
            row["cancel"] = .bool(publicationWaits.isCancelled(j.caller.job))
            row["prepared"]=j.prepared?.json ?? .null;row["published"]=j.outcome?.json ?? .null
            row["publication_identity"]=j.publication.map{.string($0.id)} ?? .null
            row["runtime_exit_caller"]=j.retired && !j.terminal ? j.exitCaller.json:.null
            row["joins"] = .array(j.joins.sorted().map(JSONValue.string))
            row["synchronousChildren"] = .array(j.children.map(JSONValue.string));row["frames"] = .array(j.frames.map(\.json))
            return .object(row)
        })
        state["accesses"] = .object(accesses.mapValues{.object(["job":.string($0.job),"owner":.string($0.owner),"epoch":.string($0.epoch),"region":.string($0.region),"mode":.string($0.mode),"state":.string($0.state),"commands":.array($0.commands.map(JSONValue.string)),"pendingReplies":.array($0.replies.sorted().map(JSONValue.string))])})
        state["commands"] = .object(commands.mapValues{.object(["access":.string($0.access),"job":.string($0.job),"kind":.string($0.kind),"caller":$0.caller.json,"completed":.bool($0.completed),"consumed":.bool($0.consumed),"reply":$0.reply ?? .null])})
        state["Tx"] = .object(registrations.mapValues(\.json));state["mlx_commands"] = .object(mlxCommands.mapValues(\.json))
        state["backend_returns"] = .object(backendReturns.mapValues(\.json));state["backend_session"] = directBackendApplicable ? (backend?.snapshot() ?? .null):.null
        state["native_policy"] = policyDescription
        state["host_main_joined"] = .bool(hostMainJoined);state["backend_closed"] = directBackendApplicable ? .bool(backendClosed):.null
        state["backend_close_evidence"] = directBackendApplicable ? backendCloseEvidence:.null;state["backend_failure"] = directBackendApplicable ? backendFailure:.null
        var hostComposition:[String:JSONValue] = [:]
        hostComposition["ledger_id"] = .string(ledgerID)
        hostComposition["execution_domain"] = .string(program.executionDomain.rawValue)
        hostComposition["registry"] = parentHost?.registry.snapshot() ?? .null
        hostComposition["service_wait"] = serviceWait?.description ?? serviceWaitHistory
        hostComposition["rejected_service_waits"] = .array(rejectedServiceWaits)
        hostComposition["service_creation_attempted"] = .bool(serviceCreationAttempted)
        hostComposition["native_parent_control"] = .bool(parentCPU != nil)
        hostComposition["service_wait_returned"] = .bool(serviceWaitReturned)
        hostComposition["service_wait_received"] = .bool(serviceWaitReceived)
        hostComposition["service_wait_rejected"] = .bool(serviceWaitRejected)
        hostComposition["service_invocation"] = serviceEntry?.invocation.diagnostic ?? .null
        hostComposition["service_response_consumed"] = serviceConsumed?.diagnostic ?? .null
        hostComposition["service_closure"] = serviceClosure?.diagnostic ?? .null
        hostComposition["late_service_closure"] = lateServiceClosure?.diagnostic ?? .null
        hostComposition["tool_roots"] = .object(toolRoots.mapValues{$0.snapshot()})
        hostComposition["tool_outcomes"] = .object(toolOutcomes.mapValues(\.diagnostic))
        hostComposition["late_backend_validated"] = .bool(lateBackendValidated)
        hostComposition["late_backend_evidence"] = lateBackendEvidence?.json ?? .null
        state["host_composition"] = .object(hostComposition)
        state["external_response_intent"] = externalResponseIntent?.json ?? .null
        state["external_response_failure"] = externalResponseFailure
        state["external_result_delivery"] = .object([
            "policy":.string(externalResultDelivery.rawValue),"host_finish_started":.bool(hostFinishStarted),
            "source_join_safe_boundary":sourceJoinSafeBoundarySequence.map(JSONValue.string) ?? .null,
            "source_join_safe_publication":sourceJoinSafePublication.map{.string($0.id)} ?? .null,
            "response_attempted":.bool(externalResponseAttempted),
            "response_state":.string(externalDemand == nil ? "not-configured" : externalResponseState),
            "response_publication":externalResponsePublication.map(JSONValue.string) ?? .null,
            "response_failure":externalResponseFailure,
            "success_committed":.bool(externalResponseState == "success-committed"),
            "source_failure_abort_requested":.bool(externalResponseState == "source-failure-abort-requested")])
        state["backend_close_owner"] = directBackendApplicable ? .string(nativeOwnsBackend ? "native-cpu-fixture":"production-host"):.null
        state["host_join_observer"] = .object(["actual_handle_retained":.bool(hostJoinObserver != nil),"body_returning":.bool(hostObserverBodyReturning),"actual_joined":.bool(hostObserverJoined)])
        state["last_wire_sequence"] = .unsigned(lastWireSequence);state["last_wire_lease"] = .unsigned(lastWireLease);state["page_allocation_epochs"] = .object(pageEpochs.mapValues(JSONValue.unsigned))
        state["Lambda"] = .object(regions);state["Omega"] = .object(obligations.mapValues(JSONValue.string));state["requires"] = .object(edges.mapValues{.array($0.sorted().map(JSONValue.string))})
        state["roots"] = .array(roots.sorted().map(JSONValue.string));state["faults"] = .object(faults)
        state["waits"] = .object(waits.mapValues(\.json));state["publication_waits"] = publicationWaits.json
        state["host_input_waits"] = .object(hostInputWaits.mapValues(\.json))
        state["external_demand"] = externalDemand?.snapshot(complete:false) ?? .null
        state["task_exit_observations"] = .object(taskExitObservations);state["wait_task_observations"] = .object(waitTaskObservations)
        state["terminal_evidence"] = .object(terminalEvidence.mapValues(JSONValue.array));state["drain_source_snapshots"] = .object(drainSourceSnapshots)
        state["unpublished_source_snapshots"] = .object(unpublishedSourceSnapshots)
        state["source_wait_roots"] = .object(sourceRoots.mapValues(\.heldWaitDescription))
        state["pending_publication_resumes"] = .array(pendingPublicationResumes.keys.sorted().map(JSONValue.string));state["failed_publication_installs"] = .array(failedPublicationInstalls.keys.sorted().map(JSONValue.string))
        state["static_interface"] = .object(["source_sha256":.string(program.sourceHash),"entry_type":program.entryType,"entry_effect":program.entryEffect,"static_value_count":.integer(program.table.values.count),"profile":.string(program.executionDomain == .hostParent ? "src1b-parent-host-close-v1":"src1-response-offer-owner1-v1"),"backend_contract":directBackendApplicable ? cm1BackendContract:.null])
        return .object(state)
    }
    private func report(status:String,outcome:Outcome,exit:Int32)->ExecutionReport {
        lastReport=(status,outcome,exit)
        let directBackendApplicable = program.executionDomain != .hostParent
        var configFields:[String:JSONValue] = ["source_sha256":.string(program.sourceHash),"source_bytes_sha256":.string(program.rawHash),"sigma":.string(program.executionDomain == .hostParent ? "p8-src1b-parent-fixed-sigma-v1":"p8-src1-fixed-sigma-v1"),"watchdog_seconds":.integer(watchdogSeconds),"host_uptime_origin_ns":.string(String(start))]
        var checks:[String:JSONValue] = ["completed":.bool(status == "completed"),"native_source_joined":.bool(status == "native-source-joined"),"all_obligations_safe":.bool(obligations.values.allSatisfy{$0 == "Safe"}),"host_main_joined":.bool(hostMainJoined),"backend_closed":directBackendApplicable ? .bool(backendClosed):.null,"numerical_reference":.string("requires-independent-reference")]
        if !directBackendApplicable {
            configFields["execution_domain"] = .string("hostParent");configFields["direct_backend_applicable"] = .bool(false)
            checks["execution_domain"] = .string("hostParent");checks["direct_backend_applicable"] = .bool(false)
        }
        let config:JSONValue = .object(configFields)
        let trial:JSONValue = .object(["trial_id":.string("0"),"config":config,"events":.array(events),"metrics":.object([:]),"ledger":snapshot(),"outcome":outcome.json,"incomplete":.bool(status == "incomplete"),"checks":.object(checks)])
        var reportFields:[String:JSONValue] = ["schema_version":.integer(1),"experiment":.string("core-language-runtime-mlx-fork-cm2"),"provenance":backend?.identity.provenance ?? .object(["execution_domain":.string("hostParent"),"backend":.null]),"config":config,"trials":.array([trial]),"status":.string(status)]
        if !directBackendApplicable {
            reportFields["execution_domain"] = .string("hostParent");reportFields["direct_backend_applicable"] = .bool(false)
            reportFields["backend_session"] = .null;reportFields["backend_closed"] = .null
            reportFields["backend_close_evidence"] = .null;reportFields["backend_close_owner"] = .null
        }
        return ExecutionReport(json:.object(reportFields),exitCode:exit)
    }

}
