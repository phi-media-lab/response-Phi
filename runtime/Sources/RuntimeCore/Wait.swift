import Foundation

/// A publication is the immutable value actually returned by its original Task.
/// Neither a diagnostic JSON rendering nor an owner epoch identifies this object.
final class PublishedOutcome:Sendable {
    let id:String;let job:String;let task:String;let outcome:Outcome
    init(id:String,job:String,task:String,outcome:Outcome) {
        self.id=id;self.job=job;self.task=task;self.outcome=outcome
    }
}

struct MachineEvaluation:Sendable {
    let id:Int;let site:String;let kind:String
    var values:[RuntimeValue]=[]
    var json:JSONValue {
        .object(["id":.string(String(id)),"site":.string(site),"kind":.string(kind),
                 "values":.array(values.map(\.diagnostic))])
    }
}
struct WaitMachineState:Sendable {
    let environment:Environment;let caller:Caller;let frames:[ActiveFrame]
    let suspended:[String:Environment];let pending:[Int:Environment]
    let evaluations:[MachineEvaluation];let nextPending:Int;let nextEvaluation:Int
    var json:JSONValue {
        .object(["environment":environment.description,"name_env":environment.names.json,"caller":caller.json,
                 "frames":.array(frames.map(\.json)),
                 "suspended":.object(suspended.mapValues { .object(["slots":$0.description,"names":$0.names.json]) }),
                 "pending":.object(Dictionary(uniqueKeysWithValues:pending.map { (String($0.key),JSONValue.object(["slots":$0.value.description,"names":$0.value.names.json])) })),
                 "evaluations":.array(evaluations.map(\.json)),
                 "next_pending":.string(String(nextPending)),"next_evaluation":.string(String(nextEvaluation))])
    }
}

/// There is one storage object and one restoration right. Machine's pending
/// reference and Ledger's W reference never contain independent environments.
final class WaitContinuation:@unchecked Sendable {
    private let lock=NSLock()
    private var state:WaitMachineState?
    private var owner="machine-pending"
    let caller:Caller;let saved:JSONValue
    init(_ state:WaitMachineState) {self.state=state;caller=state.caller;saved=state.json}
    func claim(_ wait:String)throws {
        lock.lock();defer{lock.unlock()}
        try runtimeRequire(owner == "machine-pending" && state != nil,"wait continuation already owned")
        owner=wait
    }
    func restore(_ wait:String,install:(WaitMachineState)throws->Void)throws {
        lock.lock();defer{lock.unlock()}
        guard owner == wait,let original=state else {throw RuntimeInvariant("wait continuation restored twice or by foreign owner")}
        try install(original)
        state=nil;owner="returned"
    }
    var description:JSONValue {
        lock.lock();defer{lock.unlock()}
        return .object(["owner":.string(owner),"held":.bool(state != nil),"saved":saved])
    }
}

struct WaitRecord:Sendable {
    let id:String;let caller:Caller;let target:String;let task:String;let site:String
    let operand:JSONValue;let operandType:JSONValue;let resultType:JSONValue
    var kont:WaitContinuation?
    var registration="created",choice="undecided"
    var resumeIssued=false,returned=false,callsite=false
    var publication:String?
    var history:JSONValue = .null
    var json:JSONValue {
        .object(["wait":.string(id),"caller":caller.json,"target":.string(target),"target_task":.string(task),"site":.string(site),
                 "mode":.string("terminal"),"handler_installed":.bool(false),"continuation_installed":.bool(true),
                 "registration":.string(registration),"choice":.string(choice),"resume_issued":.bool(resumeIssued),"returned":.bool(returned),
                 "task_value_callsite":.bool(callsite),"publication":publication.map(JSONValue.string) ?? .null,
                 "operand":operand,"operand_type":operandType,"result_type":resultType,
                 "kont":kont?.description ?? history])
    }
}

