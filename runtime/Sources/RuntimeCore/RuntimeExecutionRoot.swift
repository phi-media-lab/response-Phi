import Foundation
import Synchronization
import Dispatch

final class RuntimeExecutionSeal:Sendable {let id=UUID().uuidString}
final class RuntimeStartClaim:Sendable {let seal:RuntimeExecutionSeal;init(_ seal:RuntimeExecutionSeal){self.seal=seal}}
package struct RuntimeClosureObservation:Sendable {
    let seal:RuntimeExecutionSeal,serviceID:String?
    package let executionDomain:CheckedExecutionDomain,report:ExecutionReport
    package let allOriginalHandlesJoined:Bool,resourceClosed:Bool,backendEvidence:MLXClosedEvidence?
    package let startupAdmitted:Bool,originalHandlePopulation:JSONValue
    package let creationSettled:Bool
    let ledgerEvidence:JSONValue
    package var diagnostic:JSONValue {.object(["root_id":.string(seal.id),"service_id":serviceID.map(JSONValue.string) ?? .null,
        "execution_domain":.string(executionDomain.rawValue),"report":report.json,"report_exit_code":.integer(Int(report.exitCode)),
        "all_original_handles_joined":.bool(allOriginalHandlesJoined),"resource_closed":.bool(resourceClosed),
        "startup_admitted":.bool(startupAdmitted),"creation_settled":.bool(creationSettled),"original_handle_population":originalHandlePopulation,
        "backend_evidence":backendEvidence?.json ?? .null,"late_ledger_observation":ledgerEvidence])}
}
package struct RequesterJoinObservation:Sendable {
    let control:ExternalDemandControl
    package let serviceID:String,actualJoined:Bool
    let evidence:JSONValue
    package var diagnostic:JSONValue {.object(["service_id":.string(serviceID),"actual_joined":.bool(actualJoined),"original_control":evidence])}
}

/// Supplied by the host before any original runtime task is created. Reports
/// are immutable observations; a later actual join does not revise a timeout.
package final class RuntimeExecutionRoot:Sendable {
    let seal=RuntimeExecutionSeal()
    var id:String {seal.id}
    private struct State {
        var domain:CheckedExecutionDomain?,serviceID:String?,ledger:Ledger?
        var startClaim:RuntimeStartClaim?
        var creationSettled=false,creationWaiters:[CheckedContinuation<Void,Never>]=[]
        var main:Task<RuntimeTaskExit,Error>?,observer:Task<Void,Never>?,watchdog:Task<Void,Never>?
        var mainJoined=false,observerJoined=false,watchdogJoined=false
        var mainCreated=false,observerCreated=false,watchdogCreated=false,startupAdmitted=false
        var report:ExecutionReport?,reportWaiters:[CheckedContinuation<ExecutionReport,Never>]=[]
        var joinStarted=false,closure:RuntimeClosureObservation?,joinWaiters:[CheckedContinuation<RuntimeClosureObservation,Never>]=[]
        var events:[JSONValue]=[],joinErrors:[JSONValue]=[]
    }
    private let state=Mutex(State()),race=ReportRace()
    package init(){}
    package var isInstalled:Bool {state.withLock{$0.ledger != nil}}
    package var startupAdmitted:Bool {state.withLock{$0.startupAdmitted}}
    package var creationSettled:Bool {state.withLock{$0.creationSettled}}
    func creationEnded(){let waiters=state.withLock{s->[CheckedContinuation<Void,Never>] in
        guard !s.creationSettled else{return []};s.creationSettled=true;s.events.append(event("runtime.creation-settled"));let w=s.creationWaiters;s.creationWaiters=[];return w}
        for w in waiters{w.resume()}
    }
    private func awaitCreationSettled()async {await withCheckedContinuation{c in let done=state.withLock{s->Bool in if s.creationSettled{return true};s.creationWaiters.append(c);return false};if done{c.resume()}}}
    func claimStart()throws->RuntimeStartClaim {try state.withLock{s in
        try runtimeRequire(s.startClaim == nil && s.ledger == nil && s.report == nil,"RuntimeExecutionRoot start claimed twice")
        let claim=RuntimeStartClaim(seal);s.startClaim=claim;s.events.append(event("runtime.start-claimed"));return claim
    }}
    private func event(_ name:String)->JSONValue {.object(["event":.string(name),"capture_ns":.string(String(DispatchTime.now().uptimeNanoseconds))])}
    func install(ledger:Ledger,domain:CheckedExecutionDomain,serviceID:String?,claim:RuntimeStartClaim)throws {
        try state.withLock{s in try runtimeRequire(s.startClaim === claim && claim.seal === seal && s.ledger == nil && s.report == nil,"RuntimeExecutionRoot reused/foreign claim");s.ledger=ledger;s.domain=domain;s.serviceID=serviceID;s.events.append(event("runtime.root-installed"))}
    }
    func rejectPreflight(_ report:ExecutionReport,claim:RuntimeStartClaim,domain:CheckedExecutionDomain)async {
        let permitted=state.withLock{s in s.startClaim === claim && s.ledger == nil && s.report == nil}
        if permitted {
            state.withLock{if $0.domain == nil {$0.domain=domain}}
            // Same terminal channel as a running observer/watchdog, including
            // an awaitReport that started before this preflight rejection.
            creationEnded();_=await race.offer(report);publishReport(report,domain:domain)
        }
    }
    func installMain(_ task:Task<RuntimeTaskExit,Error>)throws {try state.withLock{s in try runtimeRequire(!s.mainCreated,"duplicate root main");s.main=task;s.mainCreated=true;s.events.append(event("runtime.main-installed"))}}
    func installObserver(_ task:Task<Void,Never>)throws {try state.withLock{s in try runtimeRequire(!s.observerCreated,"duplicate root observer");s.observer=task;s.observerCreated=true;s.events.append(event("runtime.observer-installed"))}}
    func installWatchdog(_ task:Task<Void,Never>)throws {try state.withLock{s in try runtimeRequire(!s.watchdogCreated,"duplicate root watchdog");s.watchdog=task;s.watchdogCreated=true;s.events.append(event("runtime.watchdog-installed"))}}
    func admitStartup()throws {try state.withLock{s in
        try runtimeRequire(!s.startupAdmitted && s.mainCreated && s.observerCreated && s.watchdogCreated && s.report == nil,"runtime startup admission before original handle population")
        s.startupAdmitted=true;s.events.append(event("runtime.startup-admitted"))
    }}
    func observedMain(){state.withLock{s in if !s.mainJoined{s.mainJoined=true;s.events.append(event("runtime.main-value-observed"))}}}
    func observedObserver(){state.withLock{s in if !s.observerJoined{s.observerJoined=true;s.events.append(event("runtime.observer-value-observed"))}}}
    func observedWatchdog(){state.withLock{s in if !s.watchdogJoined{s.watchdogJoined=true;s.events.append(event("runtime.watchdog-value-observed"))}}}
    func publishReport(_ report:ExecutionReport,domain:CheckedExecutionDomain? = nil){
        let waiters=state.withLock{s->[CheckedContinuation<ExecutionReport,Never>] in
            guard s.report == nil else {return []};if s.domain == nil{s.domain=domain};s.report=report;s.events.append(event("runtime.report-returned"));let w=s.reportWaiters;s.reportWaiters=[];return w}
        for w in waiters {w.resume(returning:report)}
    }
    func offerReport(_ report:ExecutionReport)async {_=await race.offer(report)}
    private let reportClaim=Mutex(false)
    package func awaitReport()async->ExecutionReport {
        if let saved=state.withLock({$0.report}) {return saved}
        let first=reportClaim.withLock{v->Bool in if v{return false};v=true;return true}
        if first {
            let original=await race.value()
            let handles=state.withLock{($0.watchdog,$0.observer,$0.ledger)}
            if let watchdog=handles.0 {watchdog.cancel();await watchdog.value;observedWatchdog()}
            var report=original
            if original.exitCode != 124,let observer=handles.1 {
                await observer.value;observedObserver()
                if let ledger=handles.2 {
                    do {report=try await ledger.didJoinHostObserver()}
                    catch {
                        let error=backendDiagnostic(error);state.withLock{$0.joinErrors.append(error)}
                        report=ExecutionReport(json:.object(["status":.string("incomplete"),"original_report":original.json,"observer_join_error":error]),exitCode:2)
                    }
                }
            }
            publishReport(report)
        }
        return await withCheckedContinuation{c in let ready=state.withLock{s->ExecutionReport? in if let r=s.report{return r};s.reportWaiters.append(c);return nil};if let ready {c.resume(returning:ready)}}
    }
    package func joinOriginalHandles()async->RuntimeClosureObservation {
        let first=state.withLock{s->Bool in if s.joinStarted{return false};s.joinStarted=true;s.events.append(event("runtime.late-join-begin"));return true}
        if !first {return await withCheckedContinuation{c in let ready=state.withLock{s->RuntimeClosureObservation? in if let x=s.closure{return x};s.joinWaiters.append(c);return nil};if let ready {c.resume(returning:ready)}}}
        let report=await awaitReport()
        await awaitCreationSettled()
        let handles=state.withLock{($0.main,$0.observer,$0.watchdog,$0.ledger,$0.domain,$0.serviceID,$0.observerJoined)}
        if let main=handles.0 {_ = try? await main.value;observedMain()}
        if let watchdog=handles.2 {watchdog.cancel();await watchdog.value;observedWatchdog()}
        if let observer=handles.1 {await observer.value;observedObserver()}
        if let ledger=handles.3,!handles.6,handles.1 != nil {
            do {_ = try await ledger.didJoinHostObserver()} catch {let error=backendDiagnostic(error);state.withLock{$0.joinErrors.append(error)}}
        }
        let observation=await handles.3?.closureObservation()
        let joined=state.withLock{($0.main == nil || $0.mainJoined) && ($0.observer == nil || $0.observerJoined) && ($0.watchdog == nil || $0.watchdogJoined)}
        let population=state.withLock{s in JSONValue.object(["main_created":.bool(s.mainCreated),"observer_created":.bool(s.observerCreated),"watchdog_created":.bool(s.watchdogCreated),
            "main_joined":.bool(s.mainJoined),"observer_joined":.bool(s.observerJoined),"watchdog_joined":.bool(s.watchdogJoined)])}
        let result=RuntimeClosureObservation(seal:seal,serviceID:handles.5,executionDomain:handles.4 ?? .mlxLeaf,
            report:report,allOriginalHandlesJoined:joined && (observation?.sourceTasksObserved ?? true),resourceClosed:(observation?.closed ?? false) && state.withLock{$0.joinErrors.isEmpty},
            backendEvidence:observation?.backend,startupAdmitted:startupAdmitted,originalHandlePopulation:population,creationSettled:creationSettled,
            ledgerEvidence:observation?.json ?? .object(["creation":.string("no-ledger")]))
        let waiters=state.withLock{s->[CheckedContinuation<RuntimeClosureObservation,Never>] in
            s.closure=result;s.events.append(event("runtime.late-join-returned"));let w=s.joinWaiters;s.joinWaiters=[]
            if joined {s.main=nil;s.observer=nil;s.watchdog=nil};return w}
        for w in waiters {w.resume(returning:result)}
        return result
    }
    package func snapshot()->JSONValue {state.withLock{s in .object(["root_id":.string(id),"start_claimed":.bool(s.startClaim != nil),"creation_settled":.bool(s.creationSettled),"is_installed":.bool(s.ledger != nil),"startup_admitted":.bool(s.startupAdmitted),
        "original_handle_population":.object(["main_created":.bool(s.mainCreated),"observer_created":.bool(s.observerCreated),"watchdog_created":.bool(s.watchdogCreated)]),"execution_domain":s.domain.map{.string($0.rawValue)} ?? .null,
        "service_id":s.serviceID.map(JSONValue.string) ?? .null,"original_main_retained":.bool(s.main != nil),"original_observer_retained":.bool(s.observer != nil),
        "original_watchdog_retained":.bool(s.watchdog != nil),"main_joined":.bool(s.mainJoined),"observer_joined":.bool(s.observerJoined),"watchdog_joined":.bool(s.watchdogJoined),
        "primary_report":s.report?.json ?? .null,"primary_exit_code":s.report.map{.integer(Int($0.exitCode))} ?? .null,"late_closure":s.closure?.diagnostic ?? .null,"join_errors":.array(s.joinErrors),"events":.array(s.events)])}}
}
