import Foundation
import Synchronization
import Dispatch

/// Synchronous decision, asynchronous wait; opening this gate cannot execute
/// source work until all original handles and the HostClose edge are retained.
final class ServiceGate:Sendable {
    private struct State {var decision:Bool?,waiter:CheckedContinuation<Bool,Never>?,claimed=false}
    private let state=Mutex(State())
    func resolve(_ value:Bool){let w=state.withLock{s->CheckedContinuation<Bool,Never>? in guard s.decision == nil else{return nil};s.decision=value;let w=s.waiter;s.waiter=nil;return w};w?.resume(returning:value)}
    func wait()async->Bool {await withCheckedContinuation{c in let v=state.withLock{s->Bool? in if s.claimed{return false};s.claimed=true;if let v=s.decision{return v};s.waiter=c;return nil};if let v{c.resume(returning:v)}}}
}
private struct ServiceWorkExit:Sendable {let completion:ServiceHostCompletion?,timerJoined:Bool,abortedBeforeStart:Bool}
struct ServicePreparationRejected:Error,Sendable {let original:JSONValue}
final class ServiceEntry:Sendable {
    let originalWorkIdentity=UUID().uuidString
    let invocation:ServiceInvocation,port:ServiceResponsePort,host:any PreparedServiceHost,gate=ServiceGate(),timerGate=ServiceGate()
    init(_ invocation:ServiceInvocation,host:any PreparedServiceHost){self.invocation=invocation;self.host=host;port=ServiceResponsePort(invocation)}
}
struct ServiceClosureObservation:Sendable {
    let entry:ServiceEntry,workJoined:Bool,timerJoined:Bool,resourceClosed:Bool,completion:ServiceHostCompletion?,failure:JSONValue
    var diagnostic:JSONValue {.object(["service_id":.string(entry.invocation.serviceID),"work_joined":.bool(workJoined),"timer_joined":.bool(timerJoined),
        "resource_closed":.bool(resourceClosed),"completion":completion?.diagnostic ?? .null,"failure":failure,"response":entry.port.diagnostic])}
}
package struct RegistryClosureObservation:Sendable {
    package let observerJoined:Bool,allCreatedHandlesJoined:Bool,resourceClosed:Bool
    let evidence:JSONValue
    package var diagnostic:JSONValue {.object(["observer_joined":.bool(observerJoined),"all_created_handles_joined":.bool(allCreatedHandlesJoined),"resource_closed":.bool(resourceClosed),"evidence":evidence])}
}

/// Fixed one-leaf registry. Its observer follows work -> runtime/IO/requester
/// closure. Neither that chain nor the timer waits on a parent consumer.
package final class ServiceRegistryRoot:Sendable {
    private struct State {
        var parentLedger:String?,started=false,sealed=false,aborted:ServiceAbortReason?,reserved=false,committed=false,reservationInProgress=false
        var entry:ServiceEntry?,work:Task<ServiceWorkExit,Never>?,timer:Task<Void,Never>?,observer:Task<Void,Never>?
        var workWaiter:CheckedContinuation<Task<ServiceWorkExit,Never>?,Never>?,sealWaiter:CheckedContinuation<Void,Never>?
        var closure:ServiceClosureObservation?,closureWaiters:[CheckedContinuation<ServiceClosureObservation,Never>]=[]
        var observerJoined=false,events:[JSONValue]=[],preparationFailure:JSONValue = .null
    }
    let leaf:ChildLeafDescriptor
    private let factory:any ServiceHostFactory,hostBudgetSeconds:Int,state=Mutex(State())
    package init(leaf:ChildLeafDescriptor,factory:any ServiceHostFactory,hostBudgetSeconds:Int=50)throws {
        try runtimeRequire((1...180).contains(hostBudgetSeconds),"registry host budget")
        self.leaf=leaf;self.factory=factory;self.hostBudgetSeconds=hostBudgetSeconds
    }
    private func row(_ name:String,_ detail:JSONValue = .null)->JSONValue {.object(["event":.string(name),"capture_ns":.string(String(DispatchTime.now().uptimeNanoseconds)),"details":detail])}
    func install(parentLedger:String)throws {
        let gate=ServiceGate()
        try state.withLock{s in
            try runtimeRequire(!s.started && !s.sealed,"registry reused/previously sealed")
            s.started=true;s.parentLedger=parentLedger
            s.observer=Task {guard await gate.wait() else{return};await self.observeLeaf()}
            s.events.append(row("registry.observer-installed"))
        }
        gate.resolve(true)
    }
    func reserve(parentLedger:String,caller:Caller,scope:ScopeHandle,site:String,tokens:[UInt64])throws->ServiceEntry {
        try state.withLock{s in try runtimeRequire(s.started && s.parentLedger == parentLedger && !s.reserved && !s.sealed && s.aborted == nil,"service reserve authority/once");s.reserved=true;s.reservationInProgress=true;s.events.append(row("service.reserved"))}
        let invocation:ServiceInvocation
        let host:any PreparedServiceHost
        do {
            invocation=try ServiceInvocation(serviceID:UUID().uuidString,parentLedger:parentLedger,caller:caller,scope:scope,site:site,tokens:tokens,leaf:leaf)
            state.withLock{$0.events.append(row("service.invocation-created",invocation.diagnostic))}
            host=try factory.prepare(invocation)
        }
        catch {
            let failure:JSONValue = .object(["original":backendDiagnostic(error),"created_resources":.integer(0),"boundary":.string("storage-only-factory-prepare")])
            let waiter=state.withLock{s->CheckedContinuation<Task<ServiceWorkExit,Never>?,Never>? in
                s.reservationInProgress=false;s.preparationFailure=failure;s.events.append(row("service.prepare-failed",failure))
                guard s.sealed else{return nil};let w=s.workWaiter;s.workWaiter=nil;return w}
            waiter?.resume(returning:nil)
            throw ServicePreparationRejected(original:failure)
        }
        let entry=ServiceEntry(invocation,host:host)
        var wake:CheckedContinuation<Task<ServiceWorkExit,Never>?,Never>?
        let work=try state.withLock{s->Task<ServiceWorkExit,Never> in
            try runtimeRequire(s.entry == nil,"duplicate service prepared root")
            s.entry=entry
            let timer=Task<Void,Never> {
                guard await entry.timerGate.wait() else{return}
                do {try await Task.sleep(for:.seconds(self.hostBudgetSeconds))} catch{return}
                self.requestAbort(.hostDeadline)
            }
            s.timer=timer
            let work=Task<ServiceWorkExit,Never> {
                let started=await entry.gate.wait()
                let completion:ServiceHostCompletion?
                if started {completion=await host.run(ServiceStartPermit(invocation,originalWorkIdentity:entry.originalWorkIdentity),response:entry.port)}
                else {completion=nil}
                // This is the original work suffix, not an observer waiting on
                // its own timer. The timer never awaits this work task.
                timer.cancel();entry.timerGate.resolve(false);await timer.value
                self.recordBodyDone(invocation.serviceID)
                return ServiceWorkExit(completion:completion,timerJoined:true,abortedBeforeStart:!started)
            }
            s.work=work;s.reservationInProgress=false;wake=s.workWaiter;s.workWaiter=nil;s.events.append(row("service.created-gated",.object(["invocation":invocation.diagnostic,"original_work_identity":.string(entry.originalWorkIdentity)])));return work
        }
        wake?.resume(returning:work)
        if state.withLock({$0.aborted != nil || $0.sealed}) {entry.gate.resolve(false);entry.timerGate.resolve(false);host.requestAbort(.creationAborted)}
        return entry
    }
    func commit(_ entry:ServiceEntry)throws {
        try state.withLock{s in
            try runtimeRequire(s.entry === entry && s.work != nil && s.timer != nil && !s.committed && s.aborted == nil && !s.sealed,"service commit after close/foreign")
            s.committed=true;s.events.append(row("service.committed",entry.invocation.diagnostic))
        }
        entry.timerGate.resolve(true);entry.gate.resolve(true)
    }
    private func recordBodyDone(_ id:String){state.withLock{$0.events.append(row("service.work-body-done",.string(id)))}}
    private func nextWork()async->Task<ServiceWorkExit,Never>? {
        await withCheckedContinuation{c in let ready=state.withLock{s->(Bool,Task<ServiceWorkExit,Never>?) in
            if let work=s.work{return(true,work)};if s.sealed && !s.reservationInProgress{return(true,nil)};s.workWaiter=c;return(false,nil)}
            if ready.0 {c.resume(returning:ready.1)}}
    }
    private func awaitSeal()async {await withCheckedContinuation{c in let done=state.withLock{s->Bool in if s.sealed{return true};s.sealWaiter=c;return false};if done{c.resume()}}}
    private func validateStartup(_ startup:ServiceStartupAbortObservation,completion:ServiceHostCompletion,entry:ServiceEntry)throws {
        let raw=startup.originalEvidence,resources=completion.resources
        try runtimeRequire(startup.resources.map(\.diagnostic) == resources.map(\.diagnostic),"startup has different final population")
        try runtimeRequire(try raw.field("schema").text() == "src1b-host-composition-v1" && raw.field("invocation") == entry.invocation.diagnostic &&
            raw.field("phase").text() == startup.stage.rawValue,"startup original host identity/phase")
        let runtime=try raw.field("runtime")
        if let observed=completion.runtime {
            try runtimeRequire(observed.executionDomain == .mlxLeaf && observed.serviceID == entry.invocation.serviceID &&
                !observed.startupAdmitted && observed.creationSettled && observed.allOriginalHandlesJoined,"startup Core was admitted/unsettled/unjoined")
            if let seal=entry.port.runtimeSeal {try runtimeRequire(observed.seal === seal,"startup foreign original Core seal")}
            else {
                try runtimeRequire(try !observed.originalHandlePopulation.field("main_created").boolean() &&
                    !observed.originalHandlePopulation.field("observer_created").boolean() && !observed.originalHandlePopulation.field("watchdog_created").boolean(),"unbound Core created actual handles")
            }
            try runtimeRequire(try runtime.field("root_id").text() == observed.seal.id && runtime.field("is_installed").boolean() &&
                !runtime.field("startup_admitted").boolean() && runtime.field("creation_settled").boolean() && !runtime.field("original_main_retained").boolean() &&
                !runtime.field("original_observer_retained").boolean() && !runtime.field("original_watchdog_retained").boolean(),"startup original joined Core population")
        } else {
            try runtimeRequire(entry.port.runtimeSeal == nil,"startup omits installed Core observation")
            if runtime != .null {try runtimeRequire(try !runtime.field("is_installed").boolean() && !runtime.field("original_main_retained").boolean() &&
                !runtime.field("original_observer_retained").boolean() && !runtime.field("original_watchdog_retained").boolean(),"startup omits created Core")}
        }
        let requesterCreated=try raw.field("requester_actually_created").boolean()
        try runtimeRequire(requesterCreated == (completion.requester != nil),"startup original requester population")
        if let requester=completion.requester {
            try runtimeRequire(requester.actualJoined && requester.control.serviceResponsePort === entry.port,"startup requester actual join")
        }
        var used:Set<String>=[]
        func requireResource(_ kind:HostResourceKind,_ id:String,_ original:JSONValue)throws->HostResourceObservation {
            let key=kind.rawValue+":"+id
            let matches=resources.filter{$0.kind == kind && $0.id == id}
            try runtimeRequire(matches.count == 1 && !used.contains(key),"startup resource missing/duplicate "+key)
            let found=matches[0]
            try runtimeRequire(found.complete && found.originalEvidence == original,"startup resource not closed/original "+key)
            used.insert(key);return found
        }
        func handle(_ row:JSONValue,kind:HostResourceKind)throws {
            let id=try row.field("handle_id").text(),item=try requireResource(kind,id,row)
            try runtimeRequire(try item.created && row.field("created").boolean() && row.field("close_attempted").boolean() &&
                row.field("closed").boolean() && row.field("fault") == .null && item.closed && item.joined == nil,"startup original fd not closed")
        }
        let context=try raw.field("context")
        if context != .null {
            try runtimeRequire(try context.field("session").text() == entry.invocation.session,"startup context session")
            let population=try context.field("host_resource_population").elements()
            try runtimeRequire(population.count <= 1,"fixed service created multiple workers")
            let workerRows=try context.field("workers").elements()
            try runtimeRequire(workerRows.count == population.count,"startup worker population omitted")
            for worker in population {
                let wid=try worker.field("worker_id").text()
                // Retain the original failure history even when later real fences/close succeed.
                // Array shape is required; an earlier failed observation is not a live resource.
                _ = try worker.field("fence_failures").elements()
                try runtimeRequire(try worker.field("eval_claims").integerToken() == "0" && worker.field("actual_host_resources_closed").boolean() &&
                    !worker.field("route_registered").boolean(),"startup no-EVAL/actual close evidence")
                let corresponding=try workerRows.filter{try $0.field("worker_id").text() == wid}
                try runtimeRequire(corresponding.count == 1 && (try corresponding[0].field("eval_claims").integerToken()) == "0","startup live worker has EVAL")
                let process=try requireResource(.workerProcess,wid+"/process",worker)
                let created=try worker.field("process_created").boolean()
                try runtimeRequire(process.created == created && process.started == created && (!created || process.joined == true),"startup process population/reap")
                for (kind,name) in [(HostResourceKind.workerStdin,"stdin"),(.workerStdout,"stdout"),(.workerStderr,"stderr")] {
                    let item=try requireResource(kind,wid+"/"+name+"-queue",worker)
                    let started=try worker.field(name+"_started").boolean()
                    try runtimeRequire(item.created && item.started == started && (started ? item.joined == true:item.joined == nil),"startup original queue fence")
                }
                let pipes=try worker.field("pipe_handles").elements()
                let roles=try pipes.map{try $0.field("role").text()}
                try runtimeRequire(Set(roles) == Set(["stdin-read","stdin-write","stdout-read","stdout-write","stderr-read","stderr-write"]) && pipes.count == 6,"startup exact six pipe ends")
                for pipe in pipes {try handle(pipe,kind:.pipeEnd)}
                for file in try worker.field("output_files").elements() {try handle(file,kind:.outputFile)}
            }
            let control=try context.field("control")
            let router=try requireResource(.permitRouter,entry.invocation.session+"/permit-router",control)
            let reader=try control.field("reader_started").boolean()
            try runtimeRequire(router.created && router.started == reader && (reader ? router.joined == true:router.joined == nil),"startup original permit reader")
            let returned=resources.filter{$0.kind == .returnService}
            try runtimeRequire(returned.count == 1,"startup missing return control root")
            let returnItem=try requireResource(.returnService,entry.invocation.session+"/return-service",returned[0].originalEvidence)
            try runtimeRequire(try returnItem.created && returnItem.originalEvidence.field("closed").boolean(),"startup return control incomplete")
            _=try requireResource(.context,entry.invocation.session+"/context",context)
        }
        for file in try raw.field("files").elements() {
            let item=try requireResource(.outputFile,file.field("file_id").text(),file)
            let created=try file.field("handle_created").boolean()
            try runtimeRequire(try item.created == created && file.field("open_returned").boolean() &&
                (!created || (file.field("close_attempted").boolean() && file.field("closed").boolean())),"startup original archive fd")
        }
        try runtimeRequire(used == Set(resources.map{$0.kind.rawValue+":"+$0.id}),"startup extra/unmapped actual resource")
    }
    private func observeLeaf()async {
        if let work=await nextWork() {
            let actual=await work.value
            guard let entry=state.withLock({$0.entry}) else{return}
            var closed=false,failure:JSONValue = .null
            if actual.abortedBeforeStart {closed=true}
            else if let c=actual.completion,c.valid {
                let resourcesOkay=c.resources.allSatisfy(\.complete)
                let domainsOkay=c.failures.allSatisfy{["creation","runtime","response","transport","host","io"].contains($0.domain)}
                if let runtime=c.runtime,let requester=c.requester,let backend=c.backend {
                    let kinds=c.resources.map(\.kind)
                    let required:[HostResourceKind]=[.context,.permitRouter,.returnService,.workerProcess,.workerStdin,.workerStdout,.workerStderr]
                    let population=required.allSatisfy{k in kinds.filter{$0 == k}.count == 1} && kinds.filter{$0 == .pipeEnd}.count == 6 && kinds.filter{$0 == .outputFile}.count >= 2
                    closed=runtime.executionDomain == .mlxLeaf && runtime.seal === entry.port.runtimeSeal && runtime.serviceID == entry.invocation.serviceID && runtime.allOriginalHandlesJoined && runtime.resourceClosed &&
                        requester.serviceID == entry.invocation.serviceID && requester.actualJoined && requester.control.serviceResponsePort === entry.port &&
                        runtime.backendEvidence?.json == backend.json && backend.complete && resourcesOkay && domainsOkay && population
                } else if let startup=c.startupAbort,c.backend == nil {
                    let beforeAdmission=c.runtime.map{!$0.startupAdmitted && $0.allOriginalHandlesJoined} ?? (entry.port.runtimeSeal == nil)
                    let requesterOkay=c.requester.map{$0.serviceID == entry.invocation.serviceID && $0.actualJoined && $0.control.serviceResponsePort === entry.port} ?? true
                    closed=beforeAdmission && requesterOkay && resourcesOkay && domainsOkay
                    do {try validateStartup(startup,completion:c,entry:entry)} catch {closed=false;failure=backendDiagnostic(error)}
                }
                if !closed && failure == .null {failure = .string("service completion does not close original population")}
            } else {failure = .string("service host incomplete or missing completion")}
            let closure=ServiceClosureObservation(entry:entry,workJoined:true,timerJoined:actual.timerJoined,resourceClosed:closed,
                completion:actual.completion,failure:failure)
            let waits=state.withLock{s->[CheckedContinuation<ServiceClosureObservation,Never>] in
                s.closure=closure;s.work=nil;s.timer=nil;s.events.append(row("service.work-joined",closure.diagnostic));let w=s.closureWaiters;s.closureWaiters=[];return w}
            if closed,let startup=actual.completion?.startupAbort {
                _=entry.port.abort(.init(code:"ServiceCreationRejected",original:startup.diagnostic))
            }
            if !closed {_=entry.port.abort(.init(code:"ServiceHostIncomplete",original:closure.diagnostic))}
            for w in waits {w.resume(returning:closure)}
        }
        await awaitSeal()
        state.withLock{$0.events.append(row("registry.observer-returning"))}
    }
    func awaitClosure(_ entry:ServiceEntry)async->ServiceClosureObservation {
        await withCheckedContinuation{c in let ready=state.withLock{s->ServiceClosureObservation? in
            if let value=s.closure{return value};s.closureWaiters.append(c);return nil};if let ready{c.resume(returning:ready)}}
    }
    package func requestAbort(_ reason:ServiceAbortReason){
        let entry=state.withLock{s->ServiceEntry? in if s.aborted == nil{s.aborted=reason;s.events.append(row("registry.abort",.string(reason.rawValue)))};return s.entry}
        if let entry {
            _=entry.port.abort(.init(code:"ServiceAborted",original:.string(reason.rawValue)))
            entry.host.requestAbort(reason)
            let committed=state.withLock{$0.committed}
            if !committed {entry.gate.resolve(false);entry.timerGate.resolve(false)}
        }
    }
    func requestAbortIfOwned(parentLedger:String,reason:ServiceAbortReason){
        if state.withLock({$0.parentLedger == parentLedger}) {requestAbort(reason)}
    }
    package func sealAndJoin()async->RegistryClosureObservation {
        let values=state.withLock{s->(CheckedContinuation<Task<ServiceWorkExit,Never>?,Never>?,CheckedContinuation<Void,Never>?,Task<Void,Never>?) in
            s.sealed=true;s.events.append(row("registry.sealed"))
            let canFinish=s.work == nil && !s.reservationInProgress
            let result=(canFinish ? s.workWaiter:nil,s.sealWaiter,s.observer)
            if canFinish{s.workWaiter=nil};s.sealWaiter=nil;return result}
        values.0?.resume(returning:nil);values.1?.resume()
        let uncommitted=state.withLock{s->ServiceEntry? in s.committed ? nil:s.entry}
        if let uncommitted {uncommitted.gate.resolve(false);uncommitted.timerGate.resolve(false);uncommitted.host.requestAbort(.creationAborted)}
        if let observer=values.2 {await observer.value;state.withLock{s in s.observerJoined=true;s.observer=nil;s.events.append(row("registry.observer-joined"))}}
        let flags=state.withLock{s in
            let nestedJoined=s.closure?.completion.map{c in c.valid && (c.runtime?.allOriginalHandlesJoined ?? (c.startupAbort != nil)) &&
                (c.requester?.actualJoined ?? (c.startupAbort != nil))} ?? (s.entry == nil || s.closure?.completion == nil && s.closure?.resourceClosed == true)
            return(s.observerJoined,s.work == nil && s.timer == nil && nestedJoined,s.entry == nil ? true:s.closure?.resourceClosed == true)
        }
        return RegistryClosureObservation(observerJoined:flags.0,allCreatedHandlesJoined:flags.1,resourceClosed:flags.2,evidence:snapshot())
    }
    package func snapshot()->JSONValue {state.withLock{s in .object(["schema":.string("src1b-service-registry-v1"),"parent_ledger":s.parentLedger.map(JSONValue.string) ?? .null,
        "started":.bool(s.started),"sealed":.bool(s.sealed),"reserved":.bool(s.reserved),"reservation_in_progress":.bool(s.reservationInProgress),"committed":.bool(s.committed),"aborted":s.aborted.map{.string($0.rawValue)} ?? .null,
        "actual_work_retained":.bool(s.work != nil),"actual_timer_retained":.bool(s.timer != nil),"actual_observer_retained":.bool(s.observer != nil),"observer_joined":.bool(s.observerJoined),
        "invocation":s.entry?.invocation.diagnostic ?? .null,"host_root":s.entry?.host.snapshot() ?? .null,"response":s.entry?.port.diagnostic ?? .null,
        "closure":s.closure?.diagnostic ?? .null,"preparation_failure":s.preparationFailure,"events":.array(s.events)])}}
}
