import Foundation
import RuntimeCore
import MLXTransport

// Native CPU source fixtures, authored together with their expectations. This is
// not an independent audit or a GPU/latency result. Root records the first run.
private struct EBDSourceControlFailure: Error { let message: String }
private func ebdField(_ value: JSONValue, _ key: String) -> JSONValue {
    guard case .object(let fields) = value else { return .null }; return fields[key] ?? .null
}
private func ebdDirectory(_ url: URL) throws {
    guard !FileManager.default.fileExists(atPath: url.path) else { throw EBDSourceControlFailure(message: "exclusive directory: " + url.path) }
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
}
private func ebdSave(_ value: JSONValue, _ url: URL) throws {
    try value.encoded().write(to: url, options: .withoutOverwriting)
}
private func ebdEvents(_ events: [JSONValue], _ name: String) -> [JSONValue] {
    events.filter { ebdField($0, "name") == .string(name) }
}
private func ebdOne(_ rows: [JSONValue], _ label: String) throws -> JSONValue {
    guard rows.count == 1 else { throw EBDSourceControlFailure(message: label + " expected one, got " + String(rows.count)) }
    return rows[0]
}
private func ebdWord(_ n: UInt64) -> JSONValue { .object(["kind": .string("Word"), "value": .string(String(n))]) }
private func ebdPair(_ a: JSONValue, _ b: JSONValue) -> JSONValue { .object(["kind": .string("Pair"), "left": a, "right": b]) }
private struct EBDSourceChecks {
    var rows: [JSONValue] = []
    mutating func equal(_ name: String, _ value: JSONValue, _ expected: JSONValue) throws {
        let passed = value == expected
        rows.append(.object(["check": .string(name), "observed": value, "expected": expected, "passed": .bool(passed)]))
        if !passed { throw EBDSourceControlFailure(message: name) }
    }
    mutating func truth(_ name: String, _ value: Bool) throws { try equal(name, .bool(value), .bool(true)) }
    mutating func count(_ name: String, _ rows: [JSONValue], _ count: Int) throws { try equal(name, .integer(rows.count), .integer(count)) }
    mutating func before(_ name: String, _ a: JSONValue, _ b: JSONValue) throws {
        guard let first = UInt64(try a.field("sequence").text()), let second = UInt64(try b.field("sequence").text()) else {
            throw EBDSourceControlFailure(message: name + " sequence")
        }
        try truth(name, first < second)
    }
}
private struct EBDSourceFixture: Sendable {
    let name: String, source: String, scenario: String
    let tokens: [UInt64], policy: ExternalDemandPolicy, prepare: Bool, evals: Int
    var unknown: Bool { name == "E3" || name == "E4" }
    var success: Bool { name == "E1" || name == "E2" }
}
private let ebdPayload1: [UInt64] = [101,203,307,409,503,607,709,811]
private let ebdPayload2: [UInt64] = [1103,1207,1301,1409,1511,1601,1709,1801]
private let ebdSourceFixtures: [EBDSourceFixture] = [
    .init(name:"E1",source:"external_request",scenario:"success",tokens:ebdPayload1,policy:.unchanged,prepare:true,evals:2),
    .init(name:"E2",source:"affine_receive",scenario:"success",tokens:ebdPayload2,policy:.raiseMain,prepare:true,evals:2),
    .init(name:"E3",source:"external_request",scenario:"success",tokens:ebdPayload1,policy:.unchanged,prepare:false,evals:1),
    .init(name:"E4",source:"external_request",scenario:"lost_release_ack",tokens:ebdPayload1,policy:.unchanged,prepare:true,evals:1),
    .init(name:"E5",source:"duplicate_receive",scenario:"success",tokens:ebdPayload1,policy:.unchanged,prepare:true,evals:1),
    .init(name:"E6",source:"a_cleanup_failure",scenario:"success",tokens:ebdPayload1,policy:.unchanged,prepare:true,evals:1)
]

private func ebdCheckACKs(_ events: [JSONValue], ledger: JSONValue, expected: Int, checks: inout EBDSourceChecks) throws {
    let safe = ebdEvents(events,"backend-return.safe")
    try checks.count("actual accepted complete ACKs",safe,expected)
    let betas = try ledger.field("backend_returns").members()
    for (index,event) in safe.enumerated() {
        let beta = try event.field("details"), id = try beta.field("id").text()
        guard let saved = betas[id] else { throw EBDSourceControlFailure(message:"ACK beta missing") }
        try checks.equal(id+" adopted beta",saved,beta)
        let receipt = try beta.field("receipt"), request = try receipt.field("request")
        let before = try receipt.field("worker_frame"), ack = try beta.field("ack").field("worker_frame")
        try checks.equal(id+" receipt identity",try ack.field("receipt_id"),try before.field("receipt_id"))
        try checks.equal(id+" eval sequence",try ack.field("eval_seq"),try before.field("seq"))
        let layers = try ack.field("layers").elements(), originals = try before.field("layers").elements()
        try checks.count(id+" all 28 ACK layers",layers,28);try checks.count(id+" all 28 receipt layers",originals,28)
        for layer in 0..<28 {
            let label = id+"/"+String(layer), row = layers[layer]
            try checks.equal(label+" original before",try row.field("before"),originals[layer])
            try checks.equal(label+" exact released pages",try row.field("released_pages"),try originals[layer].field("owned_pages"))
            try checks.equal(label+" independent allocation epoch",try originals[layer].field("owned_pages"),
                .array([.object(["page_id":.integer(0),"allocation_epoch":.integer(index+1)])]))
            for field in ["used_page_ids","cache_page_ids","owned_pages","page_lens"] {
                try checks.equal(label+" cleared "+field,try row.field("after").field(field),.array([]))
            }
        }
        let scope = try request.field("scope"), job = try request.field("job").text()
        let joined = try ebdOne(ebdEvents(events,"scope.join").filter { ebdField(ebdField($0,"details"),"job") == .string(job) },id+" ownjoin")
        let release = try ebdOne(ebdEvents(events,"backend-return.release_requested").filter { ebdField($0,"scope") == scope },id+" release")
        let idle = try ebdOne(ebdEvents(events,"resource.released").filter { ebdField($0,"scope") == scope },id+" Owner Idle")
        try checks.before(id+" actual ownjoin precedes RELEASE",joined,release)
        try checks.before(id+" validated ACK precedes Owner Idle",event,idle)
        try checks.equal(id+" actual original Task exit",try ledger.field("task_exit_observations").field(job).field("kind"),.string("actual-Task.value-returned"))
    }
}

private func ebdCheckSource(_ fixture: EBDSourceFixture, report: ExecutionReport, worker: JSONValue,
                            demand: JSONValue, trace: JSONValue, a32: [UInt64], checks: inout EBDSourceChecks) throws {
    let trials = try report.json.field("trials").elements();try checks.count("one actual source Ledger",trials,1)
    let trial = trials[0], ledger = try trial.field("ledger"), events = try trial.field("events").elements()
    let requests = ebdEvents(events,"mlx.eval_requested"), receipts = ebdEvents(events,"mlx.receipt_accepted")
    try checks.count("actual source EVAL requests",requests,fixture.evals)
    try checks.count("actual adopted receipts",receipts,fixture.evals)
    try checks.equal("actual worker EVAL claims",try worker.field("eval_claims"),.integer(fixture.evals))
    try checks.equal("native owns backend",try ledger.field("backend_close_owner"),.string("native-cpu-fixture"))
    try checks.equal("core does not claim backend close",try ledger.field("backend_closed"),.bool(false))
    try checks.count("no production HOST-RETURN",ebdEvents(events,"host.return"),0)
    let jobs = try ledger.field("J").members(), mains = jobs.filter { ebdField($0.value,"role") == .string("main") }
    guard mains.count == 1, let main = mains.first else { throw EBDSourceControlFailure(message:"one original INIT main") }
    let mainID = main.key, mainCaller = try main.value.field("caller")
    let expectedTokens = JSONValue.array(fixture.tokens.map(JSONValue.unsigned))
    try checks.equal("immutable complete request tokens",try demand.field("request").field("request").field("tokens"),expectedTokens)
    let inputs = try ledger.field("host_input_waits").members()
    for (id,input) in inputs {
        try checks.equal(id+" original main Job",try input.field("caller").field("job"),.string(mainID))
        try checks.equal(id+" original main Task",try input.field("caller").field("task"),try mainCaller.field("task"))
        let saved = try input.field("kont").field("saved")
        for field in ["environment","name_env","caller","frames","suspended","pending","evaluations","next_pending","next_evaluation"] {
            try checks.truth(id+" complete holder field "+field,try saved.members().keys.contains(field))
        }
        try checks.truth(id+" real source frames retained",try !saved.field("frames").elements().isEmpty)
        try checks.truth(id+" real evaluator suffix retained",try !saved.field("evaluations").elements().isEmpty)
        if ebdField(input,"received") == .bool(true) {
            try checks.equal(id+" received immutable full B8",try input.field("tokens"),expectedTokens)
        }
    }
    let workerID = try worker.field("worker_id")
    let wire = try trace.elements().filter { ebdField($0,"worker_id") == workerID }
    let sent = wire.filter { ebdField($0,"event") == .string("wire.sent") }.map { ebdField($0,"message") }
    let received = wire.filter { ebdField($0,"event") == .string("wire.received") }.map { ebdField($0,"message") }
    let evals = sent.filter { ebdField($0,"kind") == .string("EVAL") }
    try checks.count("actual pipe EVAL input frames",evals,fixture.evals)
    for index in 0..<fixture.evals {
        let expected = index == 0 ? a32:fixture.tokens
        let request = try requests[index].field("details"), receipt = try receipts[index].field("details")
        try checks.equal("source complete input "+String(index),try request.field("tokens"),.array(expected.map(JSONValue.unsigned)))
        try checks.equal("pipe complete input "+String(index),try evals[index].field("token_ids"),.array(expected.map(JSONValue.unsigned)))
        try checks.equal("pipe exact source context "+String(index),try evals[index].field("source_context"),request)
        try checks.equal("receipt exact original request "+String(index),try receipt.field("request"),request)
        try checks.equal("receipt actual input hash "+String(index),try receipt.field("worker_frame").field("input_sha256"),try request.field("input_sha256"))
        try checks.equal("CPU independent numerical oracle "+String(index),try receipt.field("worker_frame").field("token_id"),.unsigned(expected.reduce(0,+)%151936))
        let matching = received.filter { ebdField($0,"kind") == .string("EVAL_RECEIPT") && ebdField($0,"request_id") == ebdField(request,"command") }
        try checks.count("original worker receipt "+String(index),matching,1)
        try checks.equal("adopted original raw receipt "+String(index),try receipt.field("worker_frame"),matching[0])
    }
    let ackCount = fixture.name == "E4" ? 0:fixture.evals
    try ebdCheckACKs(events,ledger:ledger,expected:ackCount,checks:&checks)
    let bRequests = requests.filter { ebdField(ebdField($0,"details"),"operation") == .string("Prefill8") }
    if fixture.success {
        try checks.equal("source completed with native backend retained",try report.json.field("status"),.string("native-source-joined"))
        try checks.equal("original source success exit",.integer(Int(report.exitCode)),.integer(0))
        try checks.count("one actual B request",bRequests,1)
        try checks.equal("one full original input consumed",.integer(inputs.count),.integer(1))
        let input = inputs.first!.value
        try checks.equal("received all eight values",try input.field("tokens"),expectedTokens)
        try checks.equal("original caller observed receive",try input.field("received"),.bool(true))
        try checks.equal("source input returned",try input.field("returned"),.bool(true))
        try checks.equal("full holder no longer live",try input.field("kont").field("held"),.bool(false))
        let sourceArguments = try ebdOne(ebdEvents(events,"external.b-source-arguments"),"source B argument binding")
        try checks.equal("B source arguments use full receive",try sourceArguments.field("details").field("tokens"),expectedTokens)
        try checks.equal("control adopts same actual B request",try demand.field("b_request"),try bRequests[0].field("details"))
        try checks.equal("control adopts same actual B receipt",try demand.field("b_receipt"),try receipts[1].field("details"))
        let a = a32.reduce(0,+)%151936, b = fixture.tokens.reduce(0,+)%151936
        let value = try trial.field("outcome").field("value")
        try checks.equal("original A/B/historyA values",try value.field("right"),ebdPair(ebdWord(a),ebdPair(ebdWord(b),ebdWord(a))))
        let ordinary = jobs.values.filter { ebdField($0,"role") == .string("ordinary") }
        let waiter = try ebdOne(ordinary,"original W Job")
        try checks.equal("W value retained in original final",try value.field("left"),try waiter.field("published").field("value"))
        try checks.equal("W stable suffix",try value.field("left").field("right"),ebdWord(7001))
        try checks.equal("reply derives actual B value",try demand.field("reply"),.unsigned(b))
        let reply = try ebdOne(ebdEvents(events,"external.source-reply"),"source reply")
        try checks.equal("reply actual original main publication",try demand.field("reply_publication"),try main.value.field("publication_identity"))
        try checks.before("actual source receive precedes B register",try ebdOne(ebdEvents(events,"external.receive-returned"),"receive returned"),sourceArguments)
        try checks.before("actual main join precedes reply",try ebdOne(ebdEvents(events,"host.join"),"host join"),reply)
        try checks.equal("reply endpoint deliberately native",try reply.field("details").field("endpoint"),.string("native-source-joined"))
        if fixture.name == "E2" {
            let saved = try input.field("kont").field("saved")
            var affineSlots: [(String,String,JSONValue)] = []
            for storage in ["suspended","pending"] {
                for (holder,environment) in try saved.field(storage).members() {
                    for slot in try environment.field("slots").elements() {
                        let value = try slot.field("value")
                        if ebdField(value,"kind") == .string("Closure") && ebdField(value,"mode") == .string("A") {
                            affineSlots.append((storage,holder,slot))
                        }
                    }
                }
            }
            try checks.equal("one actual affine closure in saved frame storage",.integer(affineSlots.count),.integer(1))
            let (storage,holder,slot) = affineSlots[0], binder = try slot.field("binder"), closure = try slot.field("value")
            let frames = try saved.field("frames").elements().filter {
                ebdField($0,"kind") == .string("Bind") && ((try? $0.field("retained").elements().contains(binder)) == true)
            }
            let bind = try ebdOne(frames,"actual Bind retains original affine slot")
            if storage == "suspended" {try checks.equal("actual suspended frame identity",try bind.field("id"),.string(holder))}
            let entered = try ebdOne(ebdEvents(events,"call.enter"),"one original affine CALL")
            let exited = try ebdOne(ebdEvents(events,"call.exit"),"one original affine CALL return")
            try checks.equal("CALL consumes the saved exact Closure identity",try entered.field("details").field("closure"),try closure.field("identity"))
            try checks.equal("CALL uses the saved source definition",try entered.field("details").field("definition_site"),try closure.field("site"))
            try checks.equal("affine CALL executes and returns 17",try exited.field("details"),ebdWord(17))
            try checks.before("receive observed before affine CALL",try ebdOne(ebdEvents(events,"external.receive-returned"),"receive return"),entered)
            try checks.before("same affine CALL return follows enter",entered,exited)
            try checks.before("affine CALL completes before original B argument registration",exited,sourceArguments)
        }
    } else {
        try checks.count("no B execution",bRequests,0)
        try checks.count("no B source registration",ebdEvents(events,"external.b-source-arguments"),0)
        try checks.count("no successful source reply",ebdEvents(events,"external.source-reply"),0)
        try checks.equal("no control reply",try demand.field("reply"),.null)
        try checks.equal("no retained B request",try demand.field("b_request"),.null)
    }
    if fixture.name == "E3" {
        try checks.equal("unsubmitted request has true source suspension",try report.json.field("status"),.string("incomplete"))
        try checks.equal("fixed three-second Core watchdog",.integer(Int(report.exitCode)),.integer(124))
        try checks.equal("no requester Task was prepared",try demand.field("prepared"),.bool(false))
        try checks.equal("no actual request was enqueued",try demand.field("queued"),.bool(false))
        try checks.equal("A actually reusable before empty mailbox",try demand.field("target").field("owner_released"),.bool(true))
        try checks.equal("one original suspended receive",.integer(inputs.count),.integer(1))
        let (id,input) = inputs.first!
        try checks.equal("suspended holder not restored",try input.field("returned"),.bool(false))
        try checks.equal("suspended holder retained",try input.field("kont").field("held"),.bool(true))
        try checks.truth("suspended HostInput root retained",try ledger.field("roots").elements().contains(.string("HostInput:"+id)))
        try checks.truth("original source root retained",try ledger.field("roots").elements().contains(.string("Source:"+mainID)))
        try checks.equal("original main not joined",try ledger.field("host_main_joined"),.bool(false))
    }
    if fixture.name == "E4" {
        try checks.equal("missing ACK covers actually enqueued complete B",try demand.field("queued"),.bool(true))
        try checks.equal("lost ACK keeps Core incomplete",try report.json.field("status"),.string("incomplete"))
        try checks.equal("original missing-ACK failure exit",.integer(Int(report.exitCode)),.integer(2))
        try checks.equal("receive never entered before A ACK",.integer(inputs.count),.integer(0))
        try checks.count("no A logical release",ebdEvents(events,"resource.released"),0)
        let betas = try ledger.field("backend_returns").members()
        guard betas.count == 1,let (id,beta) = betas.first else {throw EBDSourceControlFailure(message:"one Unknown beta")}
        try checks.equal("original beta Unknown",try beta.field("phase"),.string("unknown"))
        try checks.equal("original beta unresolved",try ledger.field("Omega").field("BackendReturn:"+id),.string("Unresolved"))
        try checks.equal("no invented ACK",try beta.field("ack"),.null)
        let owners = try ledger.field("O").members()
        try checks.truth("Owner epoch remains occupied",owners.count == 1 && owners.values.allSatisfy { ebdField($0,"source_epoch") != .null })
        try checks.count("no RELEASE_ACK on original pipe",received.filter { ebdField($0,"kind") == .string("RELEASE_ACK") },0)
    }
    if fixture.name == "E5" {
        try checks.equal("duplicate rejection is ordinary source failure",try report.json.field("status"),.string("native-source-joined"))
        try checks.equal("source failure exit",.integer(Int(report.exitCode)),.integer(1))
        try checks.equal("both original receive records",.integer(inputs.count),.integer(2))
        try checks.equal("first receive succeeded once",.integer(inputs.values.filter { ebdField($0,"received") == .bool(true) }.count),.integer(1))
        try checks.equal("second receive rejected once",.integer(inputs.values.filter { ebdField($0,"rejected") == .bool(true) }.count),.integer(1))
        for (id,input) in inputs {
            try checks.equal(id+" full frame restored",try input.field("returned"),.bool(true))
            try checks.equal(id+" no live frame after failure propagation",try input.field("kont").field("held"),.bool(false))
        }
        let faults = try ledger.field("faults").members()
        guard faults.count == 1,let (id,fault) = faults.first else {throw EBDSourceControlFailure(message:"one original duplicate SourceFailure")}
        try checks.equal("original duplicate fault identity",try fault.field("code"),.string("HostInputAlreadyClaimed"))
        try checks.equal("same fault propagated once",try trial.field("outcome").field("faults"),.array([.string(id)]))
        try checks.count("one fault creation",ebdEvents(events,"fault.created"),1)
    }
    if fixture.name == "E6" {
        try checks.equal("A cleanup fault remains normal source failure",try report.json.field("status"),.string("native-source-joined"))
        try checks.equal("A cleanup original SourceFailure exit",.integer(Int(report.exitCode)),.integer(1))
        try checks.equal("failed A never reaches receive",.integer(inputs.count),.integer(0))
        let faults = try ledger.field("faults").members()
        guard faults.count == 1,let (id,fault) = faults.first else {throw EBDSourceControlFailure(message:"one original A cleanup ReadBounds")}
        try checks.equal("A cleanup original fault kind",try fault.field("code"),.string("ReadBounds"))
        try checks.equal("original fault reaches final source publication",try trial.field("outcome").field("faults"),.array([.string(id)]))
        try checks.count("one actual A fault creation",ebdEvents(events,"fault.created"),1)
        let channels = try fault.field("reportedVia").elements()
        try checks.truth("original failure keeps native source return channel",channels.contains { ebdField($0,"kind") == .string("native-source-return") })
        let request = try receipts[0].field("details").field("request"), producerID = try request.field("job").text()
        guard let producer = jobs[producerID] else {throw EBDSourceControlFailure(message:"original failed A producer Job")}
        try checks.equal("A actual normal failure publication",try producer.field("published").field("faults"),.array([.string(id)]))
        try checks.equal("A producer Task actually observed",try producer.field("actual_task_exit_observed"),.bool(true))
        let target = try demand.field("target"), edge = try target.field("producer_edge")
        try checks.equal("failed edge retains original A publication",try edge.field("publication"),try producer.field("publication_identity"))
        try checks.equal("A edge explicitly records failure",try edge.field("successful"),.bool(false))
        try checks.equal("failed publication still permits original ownjoin",try target.field("ownjoin_caller"),try target.field("main_at_bind"))
        try checks.equal("failed A complete ACK releases Owner",try target.field("owner_released"),.bool(true))
        try checks.equal("failed edge does not open request gate",try demand.field("opportunity"),.bool(false))
        try checks.equal("failed A causes no request enqueue",try demand.field("queued"),.bool(false))
        let controlEvents = try demand.field("events").elements()
        try checks.count("one failed producer edge",controlEvents.filter { ebdField($0,"event") == .string("producer.edge.failed") },1)
        try checks.count("no successful producer edge",controlEvents.filter { ebdField($0,"event") == .string("producer.edge") },0)
        try checks.count("no control invariant replacing ReadBounds",controlEvents.filter { ebdField($0,"event") == .string("control.invalid") },0)
        let betas = try ledger.field("backend_returns").members()
        try checks.truth("A beta acknowledged rather than Unknown",betas.count == 1 && betas.values.allSatisfy { ebdField($0,"phase") == .string("acknowledged") })
        try checks.truth("A Owner Idle after failure",try ledger.field("O").members().values.allSatisfy { ebdField($0,"source_epoch") == .null })
        // W may observe this same failure or its already chosen cancellation.
        // Neither natural branch is forced or retried by this fixture.
    }
    if !fixture.unknown {
        try checks.equal("original main actually joined",try ledger.field("host_main_joined"),.bool(true))
        try checks.equal("original host observer joined",try ledger.field("host_join_observer").field("actual_joined"),.bool(true))
        try checks.truth("all original source obligations Safe",try ledger.field("Omega").members().values.allSatisfy { $0 == .string("Safe") })
        try checks.truth("no source input root remains",try !ledger.field("roots").elements().contains { (try? $0.text().hasPrefix("HostInput:")) == true })
    }
}

private func ebdCheckClosed(_ fixture: EBDSourceFixture, before: JSONValue, after: JSONValue,
                            demand: JSONValue, checks: inout EBDSourceChecks) throws {
    try checks.equal("same actual worker after CPU closure",try after.field("worker_id"),try before.field("worker_id"))
    for name in ["process_root","stdout_root","stderr_root","stdin_root"] {
        try checks.equal("bounded closure "+name,try after.field(name),.bool(false))
    }
    for name in ["stdout_eof","stderr_eof"] {try checks.equal("actual "+name,try after.field(name),.bool(true))}
    if !fixture.unknown {
        try checks.equal("normal worker exit",try after.field("actual_exit"),.integer(0))
        try checks.equal("actual worker SHUTDOWN ACK",try after.field("shutdown_ack"),.bool(true))
    } else {
        for name in ["pending_root","receipt_kv_root","validated_receipts","validated_acks","completed_releases"] {
            try checks.equal("OS cleanup preserves protocol "+name,try after.field(name),try before.field(name))
        }
        try checks.equal("OS cleanup is not SHUTDOWN success",try after.field("shutdown_ack"),.bool(false))
        if fixture.name == "E4" {
            try checks.equal("actual lost-ACK worker exit",try after.field("actual_exit"),.integer(74))
            try checks.equal("original worker fault retained",try after.field("fault"),try before.field("fault"))
        }
    }
    try checks.equal("control closed after independent requester notification",try demand.field("closed"),.bool(true))
    try checks.equal("no requester Task handle left",try demand.field("retained_task"),.bool(false))
    try checks.equal("original requester actually joined",try demand.field("actual_joined"),.bool(fixture.prepare))
    try checks.equal("requester body_done observed",try demand.field("body_done"),.bool(fixture.prepare))
    let waiting = try demand.field("retained_waiters")
    for name in ["trigger","payload","result"] {try checks.equal("no parked "+name+" waiter",try waiting.field(name),.bool(false))}
    for name in ["done","close"] {try checks.equal("no parked "+name+" waiter",try waiting.field(name),.integer(0))}
    try checks.equal("no control invariant violation",try demand.field("issues"),.array([]))
    try checks.equal("all observations retained",try demand.field("dropped"),.integer(0))
    if fixture.success {
        let b = fixture.tokens.reduce(0,+)%151936
        try checks.equal("actual original requester receives B token",try demand.field("delivered"),.object(["status":.string("success"),"token":.unsigned(b)]))
        try checks.equal("successful request unsealed",try demand.field("seal"),.null)
        try checks.equal("exactly full request consumed",try demand.field("consumed"),.bool(true))
        let events = try demand.field("events").elements()
        for name in ["request.enqueued","request.consumed","requester.result.received","requester.body_done","requester.joined","control.closed"] {
            try checks.count("original requester event "+name,events.filter { ebdField($0,"event") == .string(name) },1)
        }
        let done = try ebdOne(events.filter { ebdField($0,"event") == .string("requester.body_done") },"requester done")
        let joined = try ebdOne(events.filter { ebdField($0,"event") == .string("requester.joined") },"requester actual join")
        guard let doneIndex = Int(try done.field("append_seq").integerToken()), let joinIndex = Int(try joined.field("append_seq").integerToken()) else {
            throw EBDSourceControlFailure(message:"requester event sequence")
        }
        try checks.truth("body_done notification before original Task.value join",doneIndex < joinIndex)
    }
    if fixture.name == "E6" {
        try checks.equal("requester receives original source failure seal",try demand.field("seal"),.string("original-source-failure"))
        try checks.equal("actual requester receives failure rather than token",try demand.field("delivered"),
            .object(["status":.string("failure"),"reason":.string("original-source-failure")]))
        try checks.equal("A failure leaves no successful reply",try demand.field("reply"),.null)
    }
    let eligible = ebdField(ebdField(demand,"target"),"eligibility") == .string("queued-at-frontier")
    try checks.equal("conditional API calls (late miss retained)",try demand.field("raise_calls"),.integer(fixture.policy == .raiseMain && eligible ? 1:0))
}

func runExternalDemandSourceControls(sourcesDirectory: URL, evidenceDirectory: URL, context: TransportContext,
                                    frontendHost: CPUFrontendHost, evidence: HostEvidence) async throws -> JSONValue {
    guard ebdPayload1.count == 8 && ebdPayload2.count == 8 && zip(ebdPayload1,ebdPayload2).allSatisfy({ $0.0 != $0.1 }) else {
        throw EBDSourceControlFailure(message:"all eight independent payload positions differ")
    }
    try ebdDirectory(evidenceDirectory)
    let compilerDirectory = evidenceDirectory.appendingPathComponent("compiler"), casesDirectory = evidenceDirectory.appendingPathComponent("fixtures")
    try ebdDirectory(compilerDirectory);try ebdDirectory(casesDirectory)
    var programs: [String:CheckedProgram] = [:], compilerRows: [JSONValue] = []
    for name in ["external_request","affine_receive","duplicate_receive","a_cleanup_failure"] {
        let dir = compilerDirectory.appendingPathComponent(name);try ebdDirectory(dir)
        let bytes = try Data(contentsOf:sourcesDirectory.appendingPathComponent(name+".json"))
        let bridge = CompilerBridge(evidenceDirectory:dir,cpuHost:frontendHost)
        programs[name] = try await bridge.checkClosedSource(bytes)
        compilerRows.append(.object(["source":.string(name),"source_sha256":.string(CompilerBridge.sha(bytes)),"actual_compiler":bridge.provenance]))
    }
    var results: [JSONValue] = []
    for fixture in ebdSourceFixtures {
        let dir = casesDirectory.appendingPathComponent(fixture.name);try ebdDirectory(dir)
        var checks = EBDSourceChecks(), failure:JSONValue = .null
        var core:JSONValue = .null, before:JSONValue = .null, after:JSONValue = .null, close:JSONValue = .null
        var demandBefore:JSONValue = .null, demandAfter:JSONValue = .null
        var driver:MLXTransportDriver?, control:ExternalDemandControl?
        do {
            guard let program = programs[fixture.source] else {throw EBDSourceControlFailure(message:"missing actual checked source")}
            let raw:JSONValue = .object(["schema":.string("ebd1-request-v1"),"session":.string(context.session),
                "request_id":.string("B"),"port":.string("B"),"operation":.string("Prefill8"),"tokens":.array(fixture.tokens.map(JSONValue.unsigned))])
            let requestURL = dir.appendingPathComponent("request.json"), bytes = try raw.encoded()
            try bytes.write(to:requestURL,options:.withoutOverwriting)
            let request = try ExternalDemandRequest(data:bytes,path:requestURL.path,expectedSession:context.session)
            let demand = ExternalDemandControl(request:request,policy:fixture.policy);control=demand
            // Retain actual authority before prepare can create the requester.
            evidence.retain("ebd_source_"+fixture.name,ExternalDemandHostRoot(demand))
            if fixture.prepare {demand.prepare()}
            let actual = try await context.startCPU(scenario:fixture.scenario);driver=actual
            // Legacy RSP policy/competition control is deliberately not supplied:
            // the per-case demand is the only new requester/priority authority.
            let report = await executeForMLXTesting(program,backend:actual,externalDemand:demand)
            core=report.json;before=actual.snapshot();demandBefore=demand.snapshot(complete:false)
            try ebdSave(core,dir.appendingPathComponent("core-report.json"))
            try ebdSave(before,dir.appendingPathComponent("worker-before-close.json"))
            try ebdSave(demandBefore,dir.appendingPathComponent("demand-before-close.json"))
            do {try ebdCheckSource(fixture,report:report,worker:before,demand:demandBefore,trace:context.events(),a32:context.a32,checks:&checks)}
            catch {failure=context.fault(error)}
            if fixture.unknown || ebdField(core,"status") != .string("native-source-joined") {
                demand.abort(reason:"native-source-fixture-incomplete")
                await actual.closeFailedCPUFixture()
                await demand.finish()
            } else {
                if fixture.success {try demand.validateCompletedReply()}
                await demand.finish()
                close = try await actual.closeWorkerForCPU()
            }
            after=actual.snapshot();demandAfter=demand.snapshot(complete:true)
            if failure == .null {try ebdCheckClosed(fixture,before:before,after:after,demand:demandAfter,checks:&checks)}
        } catch {
            if failure == .null {failure=context.fault(error)}
            control?.abort(reason:"native-source-fixture-failed")
            if let driver {await driver.closeFailedCPUFixture();after=driver.snapshot()}
            else {await context.closeFailedCPUWorkers()}
            if let control {await control.finish();demandAfter=control.snapshot(complete:true)}
        }
        // Immutable original Core is never rewritten after forced OS cleanup.
        try ebdSave(after,dir.appendingPathComponent("worker-after-close.json"))
        try ebdSave(demandAfter,dir.appendingPathComponent("demand-after-close.json"))
        try ebdSave(context.events(),dir.appendingPathComponent("transport-events.json"))
        let ok = failure == .null
        let row:JSONValue = .object(["fixture":.string(fixture.name),"source":.string(fixture.source),"scenario":.string(fixture.scenario),
            "policy":.string(fixture.policy.rawValue),"requester_prepared":.bool(fixture.prepare),"passed":.bool(ok),"error":failure,
            "core_report":core,"worker_before_close":before,"worker_after_close":after,"worker_normal_close":close,
            "demand_before_close":demandBefore,"demand_after_close":demandAfter,"checks":.array(checks.rows),
            "queued_at_frontier":.bool(ebdField(ebdField(demandAfter,"target"),"eligibility") == .string("queued-at-frontier")),
            "physical_gpu_evidence":.bool(false),"global_control_closed":.bool(false)])
        try ebdSave(row,dir.appendingPathComponent("result.json"));results.append(row)
        try context.record("ebd.cpu_source_fixture_finished",["fixture":.string(fixture.name),"passed":.bool(ok)])
        if !ok {break} // Preserve first unexpected result; never retry to obtain coverage.
    }
    let passed = results.count == ebdSourceFixtures.count && results.allSatisfy { ebdField($0,"passed") == .bool(true) }
    let result:JSONValue = .object(["schema":.string("ebd1-native-cpu-source-controls-v1"),"passed":.bool(passed),
        "compiler_processes":.array(compilerRows),"fixtures":.array(results),"physical_gpu_evidence":.bool(false),
        "independent_actual_audit":.bool(false),"transport_events":context.events(),"roots_before_global_finish":context.snapshot(),
        "retained_request_roots":evidence.snapshot(),"global_finish_owner":.string("root-entrypoint")])
    try ebdSave(result,evidenceDirectory.appendingPathComponent("external-demand-source-controls.json"))
    return result
}
