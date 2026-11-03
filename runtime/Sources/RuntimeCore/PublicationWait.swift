import Foundation

enum PublicationChoice:Sendable {
    case undecided,cancelled,delivered(PublishedOutcome)
    var label:String {
        switch self {case .undecided:return "undecided";case .cancelled:return "cancelled";case .delivered:return "delivered"}
    }
    var publication:PublishedOutcome? {if case .delivered(let p)=self{return p};return nil}
    var outcome:Outcome {
        switch self {
        case .cancelled:return .ok(.sum(false,.unit))
        case .delivered(let p):
            switch p.outcome {case .ok(let value):return .ok(.sum(true,value));case .failure(let ids):return .failure(ids);case .infrastructure(let message):return .infrastructure(message)}
        case .undecided:return .infrastructure("undecided publication wait")
        }
    }
}

/// Reference identity is the return capability. A diagnostic identifier alone
/// cannot authorize source restoration or manufacture a delivered payload.
final class PublicationWake:Sendable {
    let wait:String;let choice:PublicationChoice
    init(wait:String,choice:PublicationChoice) {self.wait=wait;self.choice=choice}
}

/// Taken from the serial state machine, then performed after its mutation ends.
/// The tiny lock only takes the native continuation; resume runs after unlock.
final class PublicationResume:@unchecked Sendable {
    let wake:PublicationWake
    private let lock=NSLock()
    private var continuation:CheckedContinuation<PublicationWake,Never>?
    init(wake:PublicationWake,continuation:CheckedContinuation<PublicationWake,Never>) {self.wake=wake;self.continuation=continuation}
    func perform()throws {
        lock.lock()
        guard let pending=continuation else {lock.unlock();throw RuntimeInvariant("native publication continuation resumed twice")}
        continuation=nil;lock.unlock()
        pending.resume(returning:wake)
    }
}

struct PublicationWaitRecord:Sendable {
    let id:String;let caller:Caller;let target:String;let task:String;let site:String
    let operand:JSONValue;let operandType:JSONValue;let resultType:JSONValue
    var kont:WaitContinuation?
    var registration="created",handlerInstalled=false,handlerActive=false
    var installedCancelBit:Bool?;var continuationInstalled=false
    var continuation:CheckedContinuation<PublicationWake,Never>?
    var choice=PublicationChoice.undecided
    var claimSource:String?;var handlerRequests:Set<String>=[];var losers:[JSONValue]=[]
    var resumeIssued=false,resumeBegan=false,resumeReturned=false,returned=false,received=false
    var ticket:PublicationWake?
    var history:JSONValue = .null
    var json:JSONValue {
        .object(["wait":.string(id),"mode":.string("publication"),"caller":caller.json,"target":.string(target),"target_task":.string(task),
            "site":.string(site),"operand":operand,"operand_type":operandType,"result_type":resultType,
            "registration":.string(registration),"handler_installed":.bool(handlerInstalled),"handler_active":.bool(handlerActive),
            "installed_cancel_bit":installedCancelBit.map(JSONValue.bool) ?? .null,"handler_requests":.array(handlerRequests.sorted().map(JSONValue.string)),
            "continuation_installed":.bool(continuationInstalled),"native_continuation_held":.bool(continuation != nil),
            "choice":.string(choice.label),"claim_source":claimSource.map(JSONValue.string) ?? .null,"losers":.array(losers),
            "publication":choice.publication.map{.string($0.id)} ?? .null,"outcome":choice.label == "undecided" ? .null:choice.outcome.json,
            "resume_issued":.bool(resumeIssued),"resume_begin":.bool(resumeBegan),"resume_returned":.bool(resumeReturned),
            "ticket":ticket.map{.string($0.wait+"/native-return")} ?? .null,"returned":.bool(returned),"caller_return_observed":.bool(received),
            "kont":kont?.description ?? history])
    }
}

/// This is the production protocol, also exercised directly by CPU tests.
/// Its owner must serialize calls (Ledger actor in production, test actor in CPU
/// controls). No Ledger, Metal device, source Machine, Task or observer is made.
struct PublicationWaitProtocol:Sendable {
    private(set) var cells:[String:PublicationWaitRecord]=[:]
    private(set) var publications:[String:PublishedOutcome]=[:]
    private(set) var registry:[String:[String]]=[:]
    private(set) var publicationCandidates:[String:[String]]=[:]
    private(set) var attempted:[String:Set<String>]=[:]
    private(set) var retiredRegistries:Set<String>=[]
    private(set) var cancelBits:[String:Bool]=[:]
    private var requests:Set<String>=[]
    func isCancelled(_ caller:String)->Bool {cancelBits[caller] ?? false}
    func record(_ w:String)throws->PublicationWaitRecord {
        guard let record=cells[w] else {throw RuntimeInvariant("unknown publication wait")};return record
    }
    mutating func create(id:String,caller:Caller,target:String,task:String,site:String,operand:JSONValue,
                         operandType:JSONValue,resultType:JSONValue,kont:WaitContinuation)throws {
        try runtimeRequire(cells[id] == nil && kont.caller == caller,"publication wait identity/holder mismatch")
        try kont.claim(id)
        cells[id]=PublicationWaitRecord(id:id,caller:caller,target:target,task:task,site:site,
            operand:operand,operandType:operandType,resultType:resultType,kont:kont)
    }
    mutating func installHandler(_ w:String)throws {
        var row=try record(w)
        try runtimeRequire(!row.handlerInstalled && !row.continuationInstalled && !row.returned,"handler installed twice/late")
        let cancelled=isCancelled(row.caller.job)
        row.handlerInstalled=true;row.handlerActive=true;row.installedCancelBit=cancelled
        // One atomic installation: cached delivery cannot overtake this claim.
        if cancelled {row.choice = .cancelled;row.claimSource="handler-install-already-cancelled"}
        cells[w]=row
    }
    mutating func installContinuation(_ w:String,_ continuation:CheckedContinuation<PublicationWake,Never>)throws->PublicationResume? {
        var row=try record(w)
        try runtimeRequire(row.handlerInstalled && !row.continuationInstalled && !row.returned,"continuation installed before handler/twice/late")
        row.continuationInstalled=true;row.continuation=continuation
        if case .cancelled=row.choice {row.registration="skipped"}
        else if let publication=publications[row.target] {
            try runtimeRequire(publication.task == row.task,"cached publication Task identity")
            row.registration="cached";row.choice = .delivered(publication);row.claimSource="cached"
        } else {row.registration="attached";registry[row.target,default:[]].append(w)}
        cells[w]=row
        return try takeResume(w)
    }
    /// One source request may invoke each currently active handler at most once.
    /// Uninstalled cells are covered by the monotone bit during later install.
    mutating func requestCancel(caller:String,request:String)throws->(Bool,[String],[PublicationResume]) {
        try runtimeRequire(requests.insert(request).inserted,"source cancel request replayed")
        let previous=isCancelled(caller);cancelBits[caller]=true
        var invoked:[String]=[],actions:[PublicationResume]=[]
        for w in cells.keys.sorted() {
            guard var row=cells[w],row.caller.job == caller,row.handlerActive,!row.returned else {continue}
            try runtimeRequire(row.handlerRequests.insert(request).inserted,"handler invoked twice for one request")
            invoked.append(w)
            if case .undecided=row.choice {row.choice = .cancelled;row.claimSource=request}
            else {row.losers.append(.object(["kind":.string("cancel-lost"),"request":.string(request),"winner":.string(row.choice.label)]))}
            cells[w]=row
            if let action=try takeResume(w) {actions.append(action)}
        }
        return (previous,invoked,actions)
    }
    /// Only the original producer calls publish after legal RETIRE. Installing
    /// the cache intentionally does not claim any attached cell in this step.
    mutating func publish(_ publication:PublishedOutcome)throws->[String] {
        try runtimeRequire(publications[publication.job] == nil,"publication repeated")
        if case .infrastructure=publication.outcome {throw RuntimeInvariant("Shared infrastructure outcome")}
        publications[publication.job]=publication
        let candidates=registry[publication.job] ?? []
        publicationCandidates[publication.job]=candidates;attempted[publication.job]=[]
        return candidates
    }
    mutating func claimDelivery(_ w:String,publication:PublishedOutcome)throws->PublicationResume? {
        var row=try record(w)
        try runtimeRequire(publications[publication.job] === publication && row.target == publication.job && row.task == publication.task &&
            publicationCandidates[publication.job]?.contains(w) == true && row.registration == "attached" &&
            !(attempted[publication.job] ?? []).contains(w),"delivery foreign/replayed/nonattached")
        attempted[publication.job,default:[]].insert(w)
        if case .undecided=row.choice {row.choice = .delivered(publication);row.claimSource="publication:"+publication.id}
        else {row.losers.append(.object(["kind":.string("delivery-lost"),"publication":.string(publication.id),"winner":.string(row.choice.label)]))}
        cells[w]=row
        return try takeResume(w)
    }
    mutating func retireRegistry(_ publication:PublishedOutcome)throws {
        try runtimeRequire(publications[publication.job] === publication && !retiredRegistries.contains(publication.job) &&
            Set(publicationCandidates[publication.job] ?? []) == attempted[publication.job],"registry retired early/twice/foreign")
        registry[publication.job]=[];retiredRegistries.insert(publication.job)
    }
    private mutating func takeResume(_ w:String)throws->PublicationResume? {
        var row=try record(w)
        guard row.choice.label != "undecided",row.continuationInstalled,!row.resumeIssued else {return nil}
        guard let continuation=row.continuation else {throw RuntimeInvariant("resume lost native continuation")}
        let wake=PublicationWake(wait:w,choice:row.choice)
        row.continuation=nil;row.resumeIssued=true;row.ticket=wake;cells[w]=row
        return PublicationResume(wake:wake,continuation:continuation)
    }
    mutating func resumeBegin(_ wake:PublicationWake)throws {
        let row=try record(wake.wait)
        try runtimeRequire(row.ticket === wake && row.resumeIssued && !row.resumeBegan,"resume begin foreign/twice")
        cells[wake.wait]!.resumeBegan=true
    }
    mutating func resumeReturned(_ wake:PublicationWake)throws {
        let row=try record(wake.wait)
        try runtimeRequire(row.ticket === wake && row.resumeBegan && !row.resumeReturned,"resume return foreign/twice")
        cells[wake.wait]!.resumeReturned=true
    }
    mutating func restore(_ wake:PublicationWake,caller:Caller,install:(WaitContinuation,WaitMachineState)throws->Void)throws->Outcome {
        var row=try record(wake.wait)
        guard row.caller == caller,row.ticket === wake,row.resumeIssued,row.resumeBegan,!row.returned,let holder=row.kont else {
            throw RuntimeInvariant("publication return wrong caller/ticket/owner or repeated")
        }
        try holder.restore(row.id) {try install(holder,$0)}
        row.history=holder.description;row.kont=nil;row.returned=true;row.handlerActive=false;cells[row.id]=row
        return row.choice.outcome
    }
    mutating func observedReturn(_ w:String,caller:Caller)throws {
        let row=try record(w)
        try runtimeRequire(row.caller == caller && row.returned && row.kont == nil && !row.handlerActive && !row.received,"caller return without restoration/twice")
        cells[w]!.received=true
    }
    var json:JSONValue {
        .object(["cells":.object(cells.mapValues(\.json)),"cancel_bits":.object(cancelBits.mapValues(JSONValue.bool)),
            "publications":.object(publications.mapValues{.object(["id":.string($0.id),"job":.string($0.job),"task":.string($0.task),"outcome":$0.outcome.json])}),
            "registry":.object(registry.mapValues{.array($0.map(JSONValue.string))}),
            "publication_candidates":.object(publicationCandidates.mapValues{.array($0.map(JSONValue.string))}),
            "claim_attempts":.object(attempted.mapValues{.array($0.sorted().map(JSONValue.string))}),
            "retired_registries":.array(retiredRegistries.sorted().map(JSONValue.string)),
            "registry_entries":.integer(registry.values.reduce(0){$0+$1.count}),
            "undecided_attached":.integer(cells.values.filter{$0.registration == "attached" && $0.choice.label == "undecided"}.count),
            "unreturned_cells":.integer(cells.values.filter{!$0.returned}.count)])
    }
}
