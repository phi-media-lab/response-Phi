import Foundation
import RuntimeCore
import MLXTransport

// Native-only fixture orchestration. The stub uses the production pipe/worker
// protocol and each run executes a genuinely checked source in a new Ledger.
// No source/CLI parameter can select these scenarios or the fixed 3s watchdog.
private struct CPUControlFailure: Error { let message: String }
private func cpuField(_ value: JSONValue, _ key: String) -> JSONValue {
    guard case .object(let fields) = value else { return .null }; return fields[key] ?? .null
}
private func cpuInt(_ value: JSONValue) throws -> Int {
    guard let result = Int(try value.integerToken()) else { throw CPUControlFailure(message: "integer overflow") }
    return result
}
private func cpuDirectory(_ url: URL) throws {
    guard !FileManager.default.fileExists(atPath: url.path) else { throw CPUControlFailure(message: "evidence directory already exists: \(url.path)") }
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
}
private func cpuSave(_ value: JSONValue, _ url: URL) throws {
    try value.encoded().write(to: url, options: .withoutOverwriting)
}
private struct CPUChecks {
    var rows: [JSONValue] = []
    mutating func equal(_ name: String, _ observed: JSONValue, _ expected: JSONValue) throws {
        let ok = observed == expected
        rows.append(.object(["check": .string(name), "observed": observed, "expected": expected, "passed": .bool(ok)]))
        if !ok { throw CPUControlFailure(message: name) }
    }
    mutating func count(_ name: String, _ values: [JSONValue], _ expected: Int) throws {
        try equal(name, .integer(values.count), .integer(expected))
    }
    mutating func before(_ name: String, _ first: JSONValue, _ second: JSONValue) throws {
        guard let a = Int(try first.field("sequence").text()), let b = Int(try second.field("sequence").text()) else {
            throw CPUControlFailure(message: "missing actual event sequence: \(name)")
        }
        rows.append(.object(["check": .string(name), "first": first, "second": second, "passed": .bool(a < b)]))
        if a >= b { throw CPUControlFailure(message: name) }
    }
}
private func cpuEvents(_ events: [JSONValue], _ name: String) -> [JSONValue] {
    events.filter { cpuField($0, "name") == .string(name) }
}
private func cpuOne(_ events: [JSONValue], _ name: String, _ key: String? = nil, _ value: JSONValue = .null) throws -> JSONValue {
    let matches = cpuEvents(events, name).filter { key == nil || cpuField($0, key!) == value }
    guard matches.count == 1 else { throw CPUControlFailure(message: "expected one \(name), got \(matches.count)") }
    return matches[0]
}
private func cpuWord(_ n: UInt64) -> JSONValue { .object(["kind": .string("Word"), "value": .string(String(n))]) }
private func cpuPair(_ left: JSONValue, _ right: JSONValue) -> JSONValue { .object(["kind": .string("Pair"), "left": left, "right": right]) }

private struct CPUFixture: Sendable {
    let name: String, source: String, scenario: String, evals: Int, fault: String?
    var policy: MLXForkTestPolicy = .none
    var unknown: Bool { name == "C7" }
}
private let cpuSources = ["normal", "owned_coexist", "owned_ordinary_only", "cached_early_cancel", "ordinary_fault", "closed_target"]
private let cpuFixtures: [CPUFixture] = [
    .init(name:"C1",source:"owned_coexist",scenario:"cm2_eval_hold",evals:1,fault:"PrefillAlreadyClaimed",policy:.ownedCoexist),
    .init(name:"C2",source:"owned_ordinary_only",scenario:"success",evals:0,fault:nil),
    .init(name:"C3N",source:"normal",scenario:"success",evals:2,fault:nil),
    .init(name:"C3D",source:"normal",scenario:"success",evals:2,fault:nil,policy:.attachedCancellation),
    .init(name:"C4",source:"cached_early_cancel",scenario:"success",evals:0,fault:nil,policy:.earlyCancel),
    .init(name:"C5",source:"ordinary_fault",scenario:"success",evals:0,fault:"BodyRequestedFailure"),
    .init(name:"C6",source:"closed_target",scenario:"success",evals:0,fault:"RegistrationClosed",policy:.closedTarget),
    .init(name:"C7",source:"normal",scenario:"lost_release_ack",evals:1,fault:nil,policy:.attachedCancellation)
]

private func cpuReplacing(_ value: JSONValue, path: [String], replacement: JSONValue) throws -> JSONValue {
    if path.isEmpty { return replacement }
    if case .array(var array) = value {
        guard let index=Int(path[0]),array.indices.contains(index) else {throw CPUControlFailure(message:"mutation array path")}
        array[index]=try cpuReplacing(array[index],path:Array(path.dropFirst()),replacement:replacement)
        return .array(array)
    }
    var fields = try value.members()
    guard let original = fields[path[0]] else { throw CPUControlFailure(message: "decoder mutation path missing") }
    fields[path[0]] = try cpuReplacing(original, path: Array(path.dropFirst()), replacement: replacement)
    return .object(fields)
}

// Decode only copies of this process's actual compiler stdout; originals remain
// on disk. A negative artifact is never returned as CheckedProgram.
private func cpuDecoderControls(source: Data, response: Data, ownedSource: Data, ownedResponse: Data, directory: URL) throws -> JSONValue {
    let original = try ExactJSON.decode(response, limits: .init(bytes: 64*1024*1024, depth: 256, values: 2_000_000, stringBytes: 2*1024*1024))
    let sites=try original.field("program").field("sites").elements()
    let indices=sites.indices.filter{cpuField(sites[$0],"node_kind") == .string("command:fork")}
    guard indices.count == 1,let fork=indices.first else {throw CPUControlFailure(message:"F13 unique FORK")}
    let prefix=try sites[fork].field("site").field("pointer").text()
    guard let scope=sites.first(where:{cpuField(cpuField($0,"site"),"pointer") == .string(prefix+"/scope")}) else {throw CPUControlFailure(message:"fork scope annotation")}
    let scopeUses=try scope.field("usage").field("copy").elements()
    guard scopeUses.count == 1 else {throw CPUControlFailure(message:"scope direct binder")}
    let capture=try sites[fork].field("capture").elements()[0],outer=try capture.field("source"),inner=try capture.field("binder")
    let childIndices=try sites.indices.filter {
        try sites[$0].field("site").field("pointer").text().hasPrefix(prefix+"/job/body/") &&
          cpuField(sites[$0],"node_kind") == .string("value:var") && cpuField(cpuField(sites[$0],"usage"),"copy") == .array([inner])
    }
    guard let child=childIndices.first else {throw CPUControlFailure(message:"captured child variable")}
    let control=try sites[fork].field("generation").field("success_bindings").elements()[1].field("binder")
    let mutations:[(String,[String],JSONValue,String)]=[
        ("D01",["program","sites",String(fork),"generation","nominals","0","sort"],.string("Task"),"generation nominal sorts"),
        ("D02",["program","sites",String(fork),"capture","0","source"],scopeUses[0],"capture complete type correspondence"),
        ("D03",["program","sites",String(child),"usage","copy"],.array([outer]),"source-directed direct usage"),
        ("D04",["program","sites",String(fork),"generation","success_bindings","0","binder"],control,"fork success binding domain")]
    var checks = CPUChecks(), rows: [JSONValue] = []
    for (name,path,replacement,message) in mutations {
        let mutated=try cpuReplacing(original,path:path,replacement:replacement)
        let bytes = try mutated.encoded()
        try bytes.write(to: directory.appendingPathComponent(name + ".response.json"), options: .withoutOverwriting)
        var diagnostic: JSONValue = .null, rejected = false,unexpected:(any Error)?
        do { _ = try decodeCompilerExchange(source: source, response: bytes, actualExitCode: 0) }
        catch let error as ProtocolError { rejected = true; diagnostic = error.diagnostic }
        catch {unexpected=error;diagnostic = .object(["error_type":.string(String(reflecting:type(of:error))),"description":.string(String(describing:error))])}
        // The first differing diagnostic or unexpected exception is evidence,
        // even when the next assertion aborts the entire decoder stage.
        let observation:JSONValue = .object(["case":.string(name),"mutation_path":.array(path.map(JSONValue.string)),"replacement":replacement,
            "source_sha256":.string(CompilerBridge.sha(source)),"original_response_sha256":.string(CompilerBridge.sha(response)),"mutated_response_sha256":.string(CompilerBridge.sha(bytes)),
            "compiler_actual_exit_supplied":.integer(0),"decode_result":.string(unexpected != nil ? "unexpected-exception":rejected ? "protocol-rejected":"accepted"),
            "protocol_rejected":.bool(rejected),"actual_diagnostic":diagnostic,"expected_message":.string(message)])
        try cpuSave(observation,directory.appendingPathComponent(name+".observation.json"))
        if let unexpected {throw unexpected}
        // A different exception is not the intended decoder rejection.
        try checks.equal(name + ".rejected", .bool(rejected), .bool(true))
        try checks.equal(name + ".message", cpuField(diagnostic, "message"), .string(message))
        rows.append(.object(["case": .string(name), "response_sha256": .string(CompilerBridge.sha(bytes)), "diagnostic": diagnostic]))
    }
    let mismatch = try cpuFnResultDecoderControl(source:ownedSource,response:ownedResponse,directory:directory)
    rows.append(try mismatch.field("case_result"))
    checks.rows.append(contentsOf:try mismatch.field("checks").elements())
    return .object(["passed": .bool(true), "actual_source_sha256": .string(CompilerBridge.sha(source)),
        "actual_response_sha256": .string(CompilerBridge.sha(response)),
        "additional_actual_source_sha256":.string(CompilerBridge.sha(ownedSource)),
        "additional_actual_response_sha256":.string(CompilerBridge.sha(ownedResponse)),
        "cases": .array(rows), "checks": .array(checks.rows)])
}

// D05 keeps the declared Fn tag and every other field intact, but changes its
// result Word to existing Unit. Its original checked body keeps result Word.
private func cpuFnResultDecoderControl(source:Data,response:Data,directory:URL)throws->JSONValue {
    let original=try ExactJSON.decode(response,limits:.init(bytes:64*1024*1024,depth:256,values:2_000_000,stringBytes:2*1024*1024))
    let program=try original.field("program"),sites=try program.field("sites").elements(),values=try program.field("static_values").elements()
    func reference(_ term:JSONValue)throws->Int {
        _ = try term.members(exactly:["ref"])
        guard let index=Int(try term.field("ref").text()),values.indices.contains(index) else {throw CPUControlFailure(message:"D05 static ref")}
        return index
    }
    func resolved(_ term:JSONValue)throws->JSONValue {
        var value=term,remaining=values.count+1
        while case .object(let fields)=value,fields["ref"] != nil {
            guard remaining > 0 else {throw CPUControlFailure(message:"D05 static ref cycle")}
            remaining -= 1;value=values[try reference(value)]
        }
        return value
    }
    func object(_ term:JSONValue)throws->[String:JSONValue] {
        var result:[String:JSONValue]=[:]
        for pair in try resolved(term).field("object").elements() {
            let fields=try pair.elements()
            guard fields.count == 2 else {throw CPUControlFailure(message:"D05 static object pair")}
            let key=try fields[0].text()
            guard result[key] == nil else {throw CPUControlFailure(message:"D05 duplicate static key")}
            result[key]=fields[1]
        }
        return result
    }
    func field(_ term:JSONValue,_ key:String)throws->JSONValue {
        guard let value=try object(term)[key] else {throw CPUControlFailure(message:"D05 missing static field "+key)}
        return value
    }
    func tag(_ term:JSONValue)throws->String {try resolved(field(term,"tag")).text()}
    let matches=try sites.filter { row in
        guard cpuField(row,"node_kind") == .string("command:fork") else {return false}
        return try row.field("bindings").elements().contains { binding in
            cpuField(binding,"name") == .string("beforeH") && cpuField(cpuField(binding,"binder"),"role") == .string("generated") &&
              cpuField(cpuField(binding,"binder"),"index") == .string("0")
        }
    }
    guard matches.count == 1 else {throw CPUControlFailure(message:"D05 unique beforeH fork")}
    let fork=matches[0],site=try fork.field("site").field("pointer").text()
    let generated=try fork.field("bindings").elements().filter {cpuField($0,"name") == .string("beforeH")}
    guard generated.count == 1,let body=sites.first(where:{cpuField(cpuField($0,"site"),"pointer") == .string(site+"/job/body")}) else {throw CPUControlFailure(message:"D05 original child")}
    let shared=try generated[0].field("type"),payload=try field(field(shared,"fields"),"payload"),bodyType=try body.field("type")
    let payloadFields=try field(payload,"fields"),bodyFields=try field(bodyType,"fields")
    let fieldsIndex=try reference(payloadFields),bodyFieldsIndex=try reference(bodyFields)
    let unit=try field(payloadFields,"parameter"),originalResult=try field(payloadFields,"result")
    let unitIndex=try reference(unit),wordIndex=try reference(originalResult)
    guard try tag(shared) == "Shared" && tag(payload) == "Fn" && tag(bodyType) == "Fn" && tag(unit) == "Unit" && tag(originalResult) == "Word" &&
        tag(field(bodyFields,"result")) == "Word" && fieldsIndex != bodyFieldsIndex && unitIndex != wordIndex && unitIndex < fieldsIndex else {
        throw CPUControlFailure(message:"D05 independent Fn fields / postorder")
    }
    let fieldPairs=try values[fieldsIndex].field("object").elements()
    let positions=try fieldPairs.indices.filter {try fieldPairs[$0].elements()[0].text() == "result"}
    guard positions.count == 1 else {throw CPUControlFailure(message:"D05 unique result field")}
    let path=["program","static_values",String(fieldsIndex),"object",String(positions[0]),"1","ref"]
    let replacement=JSONValue.string(String(unitIndex)),mutated=try cpuReplacing(original,path:path,replacement:replacement)
    let bytes=try mutated.encoded(),message="fork complete child result correspondence"
    try source.write(to:directory.appendingPathComponent("D05.source.json"),options:.withoutOverwriting)
    try response.write(to:directory.appendingPathComponent("D05.original.response.json"),options:.withoutOverwriting)
    try bytes.write(to:directory.appendingPathComponent("D05.response.json"),options:.withoutOverwriting)
    var diagnostic:JSONValue = .null,rejected=false,unexpected:(any Error)?
    do {_ = try decodeCompilerExchange(source:source,response:bytes,actualExitCode:0)}
    catch let error as ProtocolError {rejected=true;diagnostic=error.diagnostic}
    catch {unexpected=error;diagnostic = .object(["error_type":.string(String(reflecting:type(of:error))),"description":.string(String(describing:error))])}
    let observation:JSONValue = .object(["case":.string("D05"),"mutation_path":.array(path.map(JSONValue.string)),"replacement":replacement,
        "source_sha256":.string(CompilerBridge.sha(source)),"original_response_sha256":.string(CompilerBridge.sha(response)),"mutated_response_sha256":.string(CompilerBridge.sha(bytes)),
        "fork_site":.string(site),"payload_fields_ref":payloadFields,"body_fields_ref":bodyFields,"original_result_ref":originalResult,
        "replacement_ref":unit,"postorder_preserved":.bool(unitIndex < fieldsIndex),"child_body_type_unchanged":bodyType,
        "compiler_actual_exit_supplied":.integer(0),"decode_result":.string(unexpected != nil ? "unexpected-exception":rejected ? "protocol-rejected":"accepted"),
        "protocol_rejected":.bool(rejected),"actual_diagnostic":diagnostic,"expected_message":.string(message)])
    try cpuSave(observation,directory.appendingPathComponent("D05.observation.json"))
    if let unexpected {throw unexpected}
    var checks=CPUChecks()
    try checks.equal("D05.rejected",.bool(rejected),.bool(true))
    try checks.equal("D05.message",cpuField(diagnostic,"message"),.string(message))
    return .object(["case_result":.object(["case":.string("D05"),"response_sha256":.string(CompilerBridge.sha(bytes)),"diagnostic":diagnostic]),"checks":.array(checks.rows)])
}

private func cpuCheckWaiter(_ fixture:CPUFixture,trial:JSONValue,checks:inout CPUChecks)throws {
    let ledger=try trial.field("ledger"),all=try trial.field("events").elements()
    let ordinary=try ledger.field("J").members().filter{cpuField($0.value,"role") == .string("ordinary")}
    try checks.equal("one actual source W",.integer(ordinary.count),.integer(1))
    let (w,job)=ordinary.first!,scope=try job.field("boundary"),caller=try job.field("caller")
    let cells=try ledger.field("publication_waits").field("cells").members().values.filter{cpuField(cpuField($0,"caller"),"job") == .string(w)}
    try checks.equal("one actual W cell",.integer(cells.count),.integer(1))
    let cell=cells.first!,wait=try cell.field("wait")
    try checks.equal("W exact original caller",try cell.field("caller"),caller)
    for key in ["returned","caller_return_observed","resume_issued","resume_begin","resume_returned"] {
        try checks.equal("W "+key,try cell.field(key),.bool(true))
    }
    try checks.equal("native continuation consumed",try cell.field("native_continuation_held"),.bool(false))
    let expected=cpuPair(try cell.field("outcome").field("value"),cpuWord(7001))
    try checks.equal("actual W result and source successor",try job.field("published"),.object(["status":.string("ok"),"value":expected]))
    if !fixture.unknown {try checks.equal("source carries same W result",try trial.field("outcome").field("value").field("left"),expected)}
    let terminalWaits=try ledger.field("waits").members().values.filter{cpuField($0,"target") == .string(w)}
    try checks.equal("parent actually receives terminal W",.bool(terminalWaits.contains{cpuField($0,"returned") == .bool(true)}),.bool(true))
    let observed=cpuEvents(all,"wait.caller-return-observed").filter{cpuField(cpuField($0,"details"),"wait") == wait}
    try checks.count("actual original W receives wake",observed,1)
    let branch=try cpuOne(all,"case.selected","job",.string(w))
    try checks.before("actual W receive before CASE",observed[0],branch)
    try checks.equal("CASE original activation",try branch.field("activation"),try caller.field("invocation"))
    try checks.equal("CASE original wait operand",try branch.field("details").field("operand"),try cell.field("outcome").field("value"))
    let publication=try cpuOne(all,"result.published","job",.string(w)),finalize=try cpuOne(all,"scope.finalize","scope",scope)
    let joins=cpuEvents(all,"scope.join").filter{cpuField(cpuField($0,"details"),"job") == .string(w)}
    try checks.count("W original ownjoin",joins,1)
    try checks.before("source CASE before W publication",branch,publication)
    try checks.before("W publication before ownjoin",publication,joins[0]);try checks.before("W ownjoin before ownscope finalize",joins[0],finalize)
    let cancels=cpuEvents(all,"cancel.accepted").filter{cpuField(cpuField($0,"details"),"target") == .string(w)}
    try checks.count("two source cancels target W",cancels,2)
    try checks.equal("second sees original cancellation bit",try cancels[1].field("details").field("previous"),.bool(true))
    if fixture.policy == .attachedCancellation {
        try checks.equal("D real attached window",try cell.field("registration"),.string("attached"))
        try checks.equal("D right winner",try cell.field("choice"),.string("cancelled"))
        let attached=try cpuOne(all,"native.waiter-attached"),a=try cell.field("target")
        try checks.before("actual attachment before source cancel",attached,cancels[0])
        try checks.before("ownscope finalize before A publication",finalize,try cpuOne(all,"result.published","job",a))
        let held=try attached.field("details").field("state").field("J").field(a.text())
        try checks.equal("A actually prepared",try held.field("prepared").field("status"),.string("ok"))
        try checks.equal("A remains unpublished",try held.field("published"),.null)
        try checks.equal("A remains nonterminal",try held.field("terminalObserved"),.bool(false))
        try checks.equal("A original Task retained",try held.field("actualTaskRetained"),.bool(true))
        try checks.equal("A ownjoin not invented",try held.field("joins"),.array([]))
    } else {
        try checks.count("N no functional producer gate",cpuEvents(all,"native.producer-prepublication-held"),0)
        try checks.equal("N observed actual winner",.bool(["cancelled","delivered"].contains(try cell.field("choice").text())),.bool(true))
    }
}

private func cpuCheckSource(_ fixture: CPUFixture, report: ExecutionReport, before: JSONValue,
                           a32: [UInt64], b7: [UInt64], session: String, trace: JSONValue, checks: inout CPUChecks) throws {
    let trials=try report.json.field("trials").elements();try checks.count("one actual Ledger",trials,1)
    let trial=trials[0],ledger=try trial.field("ledger"),events=try trial.field("events").elements()
    let requests=cpuEvents(events,"mlx.eval_requested"),accepted=cpuEvents(events,"mlx.receipt_accepted")
    let omega=try ledger.field("Omega").members(),jobs=try ledger.field("J").members(),scopes=try ledger.field("S").members()
    let producers=jobs.filter{cpuField($0.value,"role") == .string("producer")},ordinary=jobs.filter{cpuField($0.value,"role") == .string("ordinary")}
    let beta=try ledger.field("backend_returns").members()
    try checks.equal("fixed native watchdog",try report.json.field("config").field("watchdog_seconds"),.integer(3))
    try checks.count("actual EVAL requests",requests,fixture.evals)
    try checks.equal("actual transport claims",try before.field("eval_claims"),.integer(fixture.evals))
    try checks.equal("backend still native owned",try ledger.field("backend_closed"),.bool(false))
    try checks.equal("explicit native backend owner",try ledger.field("backend_close_owner"),.string("native-cpu-fixture"))
    try checks.equal("not production completed",try trial.field("checks").field("completed"),.bool(false))
    try checks.count("no host.return",cpuEvents(events,"host.return"),0)
    try checks.equal("backend root retained",.bool(try ledger.field("roots").elements().contains(.string("BackendSession:"+session))),.bool(true))
    for (id,job) in ordinary {
        try checks.equal(id+" actual original Task observed",try job.field("actual_task_exit_observed"),.bool(true))
        let r=try job.field("caller").field("region").text()
        let exit=try cpuOne(events,"task.runtime-exit","job",.string(id))
        try checks.equal(id+" rJ Live at RuntimeExit",try exit.field("details").field("job_region").field("state"),.string("Live"))
        try checks.equal(id+" rJ Ended only at terminal",try ledger.field("Lambda").field(r).field("state"),.string("Ended"))
        let joins=cpuEvents(events,"scope.join").filter{cpuField(cpuField($0,"details"),"job") == .string(id)}
        try checks.count(id+" ownjoin once",joins,1)
        try checks.equal(id+" real registration boundary",try joins[0].field("scope"),try job.field("boundary"))
        try checks.before(id+" RuntimeExit before ownjoin",exit,joins[0])
    }
    if ["C3N","C3D","C7"].contains(fixture.name) {try cpuCheckWaiter(fixture,trial:trial,checks:&checks)}
    if fixture.unknown {
        try checks.equal("lost ACK stays incomplete",try report.json.field("status"),.string("incomplete"))
        try checks.count("no B admission",cpuEvents(events,"run.acquire"),1)
        try checks.count("no source release",cpuEvents(events,"resource.released"),0)
        try checks.count("no accepted ACK",cpuEvents(events,"backend-return.safe"),0)
        try checks.equal("one beta",.integer(beta.count),.integer(1))
        let (id,row)=beta.first!,request=try row.field("receipt").field("request"),p=try request.field("job").text()
        try checks.equal("sticky beta Unresolved",omega["BackendReturn:"+id] ?? .null,.string("Unresolved"))
        try checks.equal("no fabricated ACK",try row.field("ack"),.null)
        try checks.equal("A Command remains Safe",omega["Command:"+(try request.field("command").text())] ?? .null,.string("Safe"))
        try checks.equal("A Job remains Safe",omega["Job:"+p] ?? .null,.string("Safe"))
        guard let original=jobs[p] else {throw CPUControlFailure(message:"original A missing")}
        try checks.equal("A successful publication retained",try original.field("published").field("status"),.string("ok"))
        try checks.equal("failed ScopeExit snapshot retained",.bool(try ledger.field("unpublished_source_snapshots").members().count>0),.bool(true))
        return
    }
    try checks.equal("native source actually joined",try report.json.field("status"),.string("native-source-joined"))
    try checks.equal("source exit",.integer(Int(report.exitCode)),.integer(0))
    try checks.equal("all obligations Safe",.bool(omega.values.allSatisfy{$0 == .string("Safe")}),.bool(true))
    try checks.equal("main actual join",try ledger.field("host_main_joined"),.bool(true))
    try checks.equal("observer actual join",try ledger.field("host_join_observer").field("actual_joined"),.bool(true))
    try checks.equal("healthy worker",try before.field("fault"),.null)
    try checks.count("all original receipts",accepted,fixture.evals)
    try checks.count("all full ACKs",cpuEvents(events,"backend-return.safe"),fixture.evals)
    let a=a32.reduce(0,+)%151936,b=(b7.reduce(0,+)+a)%151936,value=try trial.field("outcome").field("value")
    switch fixture.name {
    case "C1":
        try checks.equal("actual U-Fn CALL/A values",value,cpuPair(cpuWord(17),cpuWord(a)))
        try checks.equal("three independent ordinary Jobs",.integer(ordinary.count),.integer(3))
        try checks.count("one producer claim",cpuEvents(events,"prefill.claimed"),1)
        try checks.count("actual U-Fn call",cpuEvents(events,"call.enter"),1)
        let phases=try trace.elements().filter{cpuField($0,"event") == .string("host.action_committed") && cpuField($0,"worker_id") == cpuField(before,"worker_id")}
        try checks.equal("actual health-lock commit phases",.array(phases.map{cpuField($0,"phase")}),.array([.string("ready"),.string("eval"),.string("leased")]))
        try checks.before("EVAL_ENTER before actual continue",try cpuOne(events,"native.eval-enter-observed"),try cpuOne(events,"native.eval-continued-after-fork"))
    case "C2":
        try checks.equal("ordinary-only result",value,cpuWord(202))
        try checks.count("explicit no-occupancy",cpuEvents(events,"backend-return.no-occupancy"),1)
        try checks.count("no synthetic RELEASE",cpuEvents(events,"backend-return.release_requested"),0)
    case "C3N","C3D":
        try checks.equal("actual A/B/history",try value.field("right"),cpuPair(cpuWord(a),cpuPair(cpuWord(b),cpuWord(a))))
        try checks.equal("B actual A eighth token",try requests[1].field("details").field("tokens"),.array((b7+[a]).map(JSONValue.unsigned)))
    case "C4":
        let cancelled:JSONValue = .object(["kind":.string("Inr"),"value":.object(["kind":.string("Unit")])])
        try checks.equal("precancel plus terminal AWAIT",value,cpuPair(cancelled,cpuWord(23)))
        let cells=try ledger.field("publication_waits").field("cells").members().values.filter{cpuField($0,"registration") == .string("skipped")}
        try checks.equal("one early-cancel cell",.integer(cells.count),.integer(1))
        let cell=cells.first!,caller=try cell.field("caller").field("job")
        try checks.equal("handler sees original bit",try cell.field("installed_cancel_bit"),.bool(true))
        try checks.equal("cancel wins over cached result",try cell.field("choice"),.string("cancelled"))
        let terminal=try ledger.field("waits").members().values.filter{cpuField(cpuField($0,"caller"),"job") == caller}
        try checks.equal("same cancelled caller has terminal return",.bool(terminal.contains{cpuField($0,"returned") == .bool(true)}),.bool(true))
    case "C5":
        try checks.equal("nested original Fault handled once",value,cpuWord(5005))
        try checks.equal("one ordinary failing Job",.integer(ordinary.count),.integer(1))
        let (p,job)=ordinary.first!,direct=try job.field("synchronousChildren").elements()
        try checks.count("ordinary actual direct child Scope",direct,1)
        let child=try direct[0].text(),registration=try job.field("boundary").text()
        guard let registrationScope=scopes[registration],let childScope=scopes[child] else {throw CPUControlFailure(message:"actual direct/registration scope missing")}
        try checks.equal("registrationScope not synchronous owner",.bool(try registrationScope.field("synchronousChildren").elements().contains(.string(child))),.bool(false))
        try checks.equal("child Scope executes in original ordinary Job",try childScope.field("caller").field("job"),.string(p))
        try checks.equal("actual Job owns ChildScope edge",.bool(try ledger.field("requires").field("Job:"+p).elements().contains(.string("Scope:"+child))),.bool(true))
    case "C6":
        try checks.equal("closed target failure handled",value,cpuWord(6006))
        try checks.count("zero descendant Task creation",cpuEvents(events,"registration.task_prepared"),1)
        try checks.before("close before legal committed START",try cpuOne(events,"native.ordinary-start-opened-by-close"),try cpuOne(events,"native.ordinary-start-released"))
    default:throw CPUControlFailure(message:"unknown CPU fixture")
    }
    let faults=try ledger.field("faults").members()
    try checks.equal("original Fault count",.integer(faults.count),.integer(fixture.fault == nil ? 0:1))
    if let expected=fixture.fault {
        let (id,fault)=faults.first!
        try checks.equal("original Fault code",try fault.field("code"),.string(expected))
        try checks.equal("source handled original Fault",.bool(cpuEvents(events,"fault.handled").contains{(try? $0.field("details").field("ids").elements().contains(.string(id))) == true}),.bool(true))
        if fixture.name == "C5" {
            let channels=try fault.field("reportedVia").elements().map{cpuField($0,"kind")}
            for kind in ["await","local-job"] {try checks.equal("original Fault channel "+kind,.bool(channels.contains(.string(kind))),.bool(true))}
        }
    }
    for (id,scope) in scopes {
        let local=try scope.field("localJobRoots").elements().map{try $0.text()},joined=try scope.field("joined").elements().map{try $0.text()}
        try checks.equal(id+" all local original Tasks joined",.bool(Set(local)==Set(joined)),.bool(true))
    }
    // Bind each accepted receipt to the original producer, complete Task.value,
    // owning scope and independent beta, then to every layer of its ACK.
    for (index, acceptedEvent) in accepted.enumerated() {
        let receipt = try acceptedEvent.field("details"), request = try receipt.field("request"), frame = try receipt.field("worker_frame")
        let job = try request.field("job").text(), scope = try request.field("scope"), betaID = try request.field("backend_return").text()
        guard let betaRow = beta[betaID], let jobRow = jobs[job] else { throw CPUControlFailure(message: "receipt lacks original Job/beta") }
        try checks.equal("receipt retained independently " + betaID, try betaRow.field("receipt"), receipt)
        try checks.equal("actual task.value " + job, try ledger.field("task_exit_observations").field(job).field("kind"), .string("actual-Task.value-returned"))
        try checks.equal("original task identity " + job, try jobRow.field("caller").field("task"), try request.field("task"))
        let joins = cpuEvents(events, "scope.join").filter { cpuField(cpuField($0, "details"), "job") == .string(job) }
        try checks.count("unique ownjoin " + job, joins, 1)
        let release = try cpuOne(events, "backend-return.release_requested", "scope", scope)
        let safe = try cpuOne(events, "backend-return.safe", "scope", scope)
        let released = try cpuOne(events, "resource.released", "scope", scope)
        try checks.before("receipt before owning join " + job, acceptedEvent, joins[0])
        try checks.before("ownjoin before RELEASE " + job, joins[0], release)
        try checks.before("ACK before source resource release " + job, safe, released)
        let ack = try betaRow.field("ack").field("worker_frame")
        try checks.equal("ACK original receipt ID " + betaID, try ack.field("receipt_id"), try frame.field("receipt_id"))
        try checks.equal("ACK original eval seq " + betaID, try ack.field("eval_seq"), try frame.field("seq"))
        try checks.equal("ACK own release seq " + betaID, try ack.field("seq"), .integer(try cpuInt(frame.field("seq")) + 1))
        try checks.equal("wire lease tracks eval count " + betaID, try frame.field("lease_epoch"), .integer(index + 1))
        let expectedOrdinal = index + 1
        try checks.equal("source ordinal independent " + betaID, try request.field("owner_ordinal"), .integer(expectedOrdinal))
        let original = try frame.field("layers").elements(), layers = try ack.field("layers").elements()
        try checks.count("receipt all layers " + betaID, original, 28); try checks.count("ACK all layers " + betaID, layers, 28)
        for layer in 0..<28 {
            let label = "\(betaID)/L\(layer)", beforeLayer = original[layer], returned = layers[layer]
            try checks.equal(label + " original before", try returned.field("before"), beforeLayer)
            try checks.equal(label + " page generation", try beforeLayer.field("owned_pages"), .array([.object(["page_id": .integer(0), "allocation_epoch": .integer(index + 1)])]))
            try checks.equal(label + " returned exact pages", try returned.field("released_pages"), try beforeLayer.field("owned_pages"))
            let after = try returned.field("after")
            for key in ["used_page_ids", "cache_page_ids", "owned_pages", "page_lens"] { try checks.equal(label + " empty " + key, try after.field(key), .array([])) }
            try checks.equal(label + " free partition", try after.field("free_page_ids"), .array([.integer(0)]))
        }
        if index == 1 {
            let previous = try accepted[0].field("details").field("request").field("scope")
            try checks.before("A source RELEASE authorizes B admission", try cpuOne(events, "resource.released", "scope", previous), try cpuOne(events, "run.acquire", "scope", scope))
        }
    }
}

private func cpuCheckClosure(_ fixture: CPUFixture, before: JSONValue, after: JSONValue,
                             trace: JSONValue, checks: inout CPUChecks) throws {
    for root in ["process_root", "stdout_root", "stderr_root", "stdin_root"] { try checks.equal("actual closed " + root, try after.field(root), .bool(false)) }
    for eof in ["stdout_eof", "stderr_eof"] { try checks.equal("actual " + eof, try after.field(eof), .bool(true)) }
    try checks.equal("same actual worker", try after.field("worker_id"), try before.field("worker_id"))
    if !fixture.unknown {
        try checks.equal("normal worker exit", try after.field("actual_exit"), .integer(0))
        try checks.equal("real SHUTDOWN ACK", try after.field("shutdown_ack"), .bool(true))
        try checks.equal("all release counts", try after.field("completed_releases"), .integer(fixture.evals))
        return
    }
    // Forced CPU reap is not KV release: all prior logical unresolved evidence
    // stays exactly as captured before the OS fixture cleanup.
    for key in ["pending_root", "receipt_kv_root", "validated_receipts", "validated_acks", "completed_releases", "fault"] {
        try checks.equal("OS cleanup preserves logical " + key, try after.field(key), try before.field(key))
    }
    try checks.equal("failed worker never SHUTDOWN-completed", try after.field("shutdown_ack"), .bool(false))
    let worker = try after.field("worker_id"), events = try trace.elements()
    let frames = events.filter { cpuField($0, "event") == .string("wire.received") && cpuField($0, "worker_id") == worker }.map { cpuField($0, "message") }
    try checks.equal("actual lost-ACK worker exit", try after.field("actual_exit"), .integer(74))
    try checks.count("no ACK was received", frames.filter { cpuField($0, "kind") == .string("RELEASE_ACK") }, 0)

}

func runCPUControls(sourcesDirectory: URL, evidenceDirectory: URL, context: TransportContext, frontendHost: CPUFrontendHost) async throws -> JSONValue {
    let compilerDirectory = evidenceDirectory.appendingPathComponent("compiler"), casesDirectory = evidenceDirectory.appendingPathComponent("fixtures")
    let decoderDirectory = evidenceDirectory.appendingPathComponent("decoder-negatives")
    try cpuDirectory(compilerDirectory); try cpuDirectory(casesDirectory); try cpuDirectory(decoderDirectory)
    var compiled: [String: CheckedProgram] = [:], sources: [String: Data] = [:], compilerRows: [JSONValue] = []
    // Exactly one real compiler process per independent closed source.
    for name in cpuSources {
        let dir = compilerDirectory.appendingPathComponent(name); try cpuDirectory(dir)
        let source = try Data(contentsOf: sourcesDirectory.appendingPathComponent(name + ".json"))
        let compiler = CompilerBridge(evidenceDirectory: dir, cpuHost: frontendHost)
        let program = try await compiler.checkClosedSource(source)
        compiled[name] = program; sources[name] = source
        compilerRows.append(.object(["source": .string(name), "sha256": .string(CompilerBridge.sha(source)), "process": compiler.provenance]))
    }
    guard let normal = sources["normal"] else { throw CPUControlFailure(message: "normal compiler source missing") }
    guard let normalProgram=compiled["normal"] else {throw CPUControlFailure(message:"normal checked source missing")}
    try validateMLXForkDeterministicProgram(normalProgram)
    let response = try Data(contentsOf: compilerDirectory.appendingPathComponent("normal/frontend.stdout"))
    guard let ownedSource=sources["owned_coexist"] else {throw CPUControlFailure(message:"owned_coexist compiler source missing")}
    let ownedResponse=try Data(contentsOf:compilerDirectory.appendingPathComponent("owned_coexist/frontend.stdout"))
    let decoder = try cpuDecoderControls(source: normal, response: response, ownedSource:ownedSource, ownedResponse:ownedResponse, directory: decoderDirectory)
    try cpuSave(decoder, decoderDirectory.appendingPathComponent("result.json"))
    try context.record("cpu.decoder_controls_complete", ["result": decoder])
    var results: [JSONValue] = [], passed = true
    for fixture in cpuFixtures {
        let dir = casesDirectory.appendingPathComponent(fixture.name); try cpuDirectory(dir)
        var checks = CPUChecks(), failure: JSONValue = .null, reportJSON: JSONValue = .null
        var before: JSONValue = .null, after: JSONValue = .null, close: JSONValue = .null
        var driver: MLXTransportDriver?
        do {
            guard let program = compiled[fixture.source] else { throw CPUControlFailure(message: "missing checked source") }
            let actual = try await context.startCPU(scenario: fixture.scenario); driver = actual
            let report = await executeForMLXTesting(program, backend: actual, policy: fixture.policy, phaseControl: fixture.policy == .ownedCoexist ? actual:nil, returnService: context.returnService)
            reportJSON = report.json; before = actual.snapshot()
            try cpuSave(report.json, dir.appendingPathComponent("core-report.json"))
            try cpuSave(before, dir.appendingPathComponent("worker-before-close.json"))
            // Always perform the bounded CPU cleanup even if a check fails.
            do { try cpuCheckSource(fixture, report: report, before: before, a32: context.a32, b7: context.bPrefix7, session: actual.identity.session, trace: context.events(), checks: &checks) }
            catch { failure = context.fault(error) }
            if fixture.unknown && failure == .null {
                var entered=false,rejected=false
                do {try actual.withHealthyHostAction {entered=true}} catch is MLXBackendFailure {rejected=true}
                do {
                    try checks.equal("observed fault rejects host action",.bool(rejected),.bool(true))
                    try checks.equal("faulted host commit never entered",.bool(entered),.bool(false))
                } catch {failure=context.fault(error)}
            }
            if fixture.unknown || cpuField(report.json, "status") != .string("native-source-joined") {
                await actual.closeFailedCPUFixture()
            } else { close = try await actual.closeWorkerForCPU() }
            after = actual.snapshot()
            try cpuSave(after, dir.appendingPathComponent("worker-after-close.json"))
            if failure == .null { try cpuCheckClosure(fixture, before: before, after: after, trace: context.events(), checks: &checks) }
        } catch {
            if failure == .null { failure = context.fault(error) }
            if let driver { await driver.closeFailedCPUFixture(); after = driver.snapshot() }
            else { await context.closeFailedCPUWorkers() }
        }
        let ok = failure == .null
        let row: JSONValue = .object(["fixture": .string(fixture.name), "source": .string(fixture.source),
            "scenario": .string(fixture.scenario), "policy":.string(fixture.policy.rawValue), "passed": .bool(ok), "error": failure,
            "core_report": reportJSON, "worker_before_close": before, "worker_after_close": after,
            "normal_close": close, "checks": .array(checks.rows), "global_control_closed": .bool(false)])
        try cpuSave(row, dir.appendingPathComponent("result.json")); results.append(row)
        try context.record("cpu.fixture_finished", ["fixture": .string(fixture.name), "passed": .bool(ok)])
        if !ok { passed = false; break } // First unexpected result stops, no retry.
    }
    let result: JSONValue = .object(["schema": .string("cm2-native-cpu-controls-v1"), "passed": .bool(passed && results.count == 8),
        "physical_gpu_evidence": .bool(false), "compiler_processes": .array(compilerRows), "decoder": decoder,
        "fixtures": .array(results), "transport_events": context.events(), "roots_before_global_finish": context.snapshot(),
        "global_finish_owner": .string("root-entrypoint")])
    try cpuSave(result, evidenceDirectory.appendingPathComponent("cpu-controls.json"))
    return result // Root owns the single actual context.finish() and its evidence.
}
