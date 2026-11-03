import Foundation

/// Internal Task exit, not a source value. Only Tx may consume an abort result.
enum RuntimeTaskExit:Sendable {
    case published(PublishedOutcome)
    // Only an unpublished infrastructure failure uses this branch in this finite backend profile.
    case source(Outcome)
    case registrationAborted(registration:String,task:String)
    var diagnostic:JSONValue {
        switch self {
        case .published(let p):return .object(["kind":.string("published"),"publication":.string(p.id),"job":.string(p.job),"task":.string(p.task),"outcome":p.outcome.json])
        case .source(let outcome):return .object(["kind":.string("unpublished"),"outcome":outcome.json])
        case .registrationAborted(let registration,let task):return .object(["kind":.string("registration-aborted"),"registration":.string(registration),"task":.string(task)])
        }
    }
}
struct ProducerLaunch:Sendable {
    let machine:Machine;let context:ProducerContext;let arguments:PrefillArguments;let site:String
}
struct OrdinaryLaunch:Sendable {
    let machine:Machine;let context:OrdinaryContext
}
enum RegistrationDecision:Sendable {
    case start(ProducerLaunch)
    case ordinary(OrdinaryLaunch)
    case abort(registration:String,task:String)
}

/// One consumer, one decision; consumed
/// payload is erased rather than retained as gate history. No source can call it.
final class StartAbortGate:@unchecked Sendable {
    private let lock=NSLock()
    private var resolved=false,consumed=false,waitCalled=false
    private var pending:RegistrationDecision?
    private var waiter:CheckedContinuation<RegistrationDecision,Error>?
    func take()async throws->RegistrationDecision {
        try await withCheckedThrowingContinuation {continuation in
            lock.lock()
            guard !waitCalled else {lock.unlock();continuation.resume(throwing:RuntimeInvariant("registration gate consumed twice"));return}
            waitCalled=true
            if let decision=pending {
                pending=nil;consumed=true;lock.unlock();continuation.resume(returning:decision)
            } else {waiter=continuation;lock.unlock()}
        }
    }
    func resolve(_ decision:RegistrationDecision)throws {
        lock.lock()
        guard !resolved else {lock.unlock();throw RuntimeInvariant("registration gate resolved twice")}
        resolved=true
        if let continuation=waiter {
            waiter=nil;consumed=true;lock.unlock();continuation.resume(returning:decision)
        } else {pending=decision;lock.unlock()}
    }
    var closed:Bool {lock.lock();defer{lock.unlock()};return !resolved}
    var description:JSONValue {
        lock.lock();defer{lock.unlock()}
        return .object(["resolved":.bool(resolved),"consumed":.bool(consumed),
                        "payload_retained":.bool(pending != nil),"waiter_retained":.bool(waiter != nil)])
    }
}

/// Thrown out of the synchronous admission closure when a prepared (gated, not
/// yet committed) registration fails its commit validation. The async caller
/// performs the actual abort+join; nothing is marked before that join.
struct PreparedRegistrationFailure:Error,Sendable {
    let registration:String;let underlying:any Error
}
struct PreparedProducerEnvelope:Sendable {
    let environment:Environment;let arguments:PrefillArguments;let site:String
}
struct PreparedOrdinaryEnvelope:Sendable {
    let environment:Environment;let site:String;let resultType:JSONValue;let latentEffect:JSONValue
}
struct RegistrationRecord:Sendable {
    let id:String;let taskID:String;let issuer:Caller;let target:ScopeHandle;let anchor:String;let site:String;let genBind:String
    var phase="prepared"
    var kind="producer"
    var task:Task<RuntimeTaskExit,Error>?
    var envelope:PreparedProducerEnvelope?
    var ordinaryEnvelope:PreparedOrdinaryEnvelope?
    var gate:StartAbortGate?
    var faultIDs:[String]=[]
    var abortEntered=false,terminal=false,joined=false,transferred=false
    var sourceJob:String?
    var gateHistory:JSONValue = .null
    var json:JSONValue {
        .object(["registration":.string(id),"task":.string(taskID),"issuer":issuer.json,
                 "target":RuntimeValue.scope(target).diagnostic,"anchor":.string(anchor),"site":.string(site),
                 "gen_bind":.string(genBind),"phase":.string(phase),"actual_task_retained":.bool(task != nil),
                 "envelope_retained":.bool(envelope != nil || ordinaryEnvelope != nil),"gate_retained":.bool(gate != nil),
                 "kind":.string(kind),
                 "gate":gate?.description ?? gateHistory,"fault_ids":.array(faultIDs.map(JSONValue.string)),
                 "abort_entered":.bool(abortEntered),"terminal_observed":.bool(terminal),"joined":.bool(joined),
                 "ownership_transferred":.bool(transferred),"source_job":sourceJob.map(JSONValue.string) ?? .null])
    }
}
