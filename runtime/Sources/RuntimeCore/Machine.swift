import Foundation

/// Each instance belongs to one real Swift Task. Continuations retain affine slots in explicit frames.
final class Machine: @unchecked Sendable {
    let program:CheckedProgram;let ledger:Ledger
    var environment:Environment;var caller:Caller
    private var frames:[ActiveFrame]=[]
    // Ledger retains this whole machine while unresolved. It never reads or
    // executes it concurrently; these environments belong to this Task only.
    private var suspended:[String:Environment]=[:]
    private var pending:[Int:Environment]=[:]
    private var nextPending=0
    private var evaluations:[MachineEvaluation]=[]
    private var nextEvaluation=0
    private let waitLock=NSLock()
    private var storedWaitContinuation:WaitContinuation?
    private var waitContinuation:WaitContinuation? {
        get {waitLock.lock();defer{waitLock.unlock()};return storedWaitContinuation}
        set {waitLock.lock();storedWaitContinuation=newValue;waitLock.unlock()}
    }
    var heldWaitDescription:JSONValue {waitContinuation?.description ?? .null}
    /// Called by Ledger only from this Machine's own awaited drain entry; it
    /// must not be used as a concurrent poll of another running Machine.
    func drainStateDescription()->JSONValue {
        WaitMachineState(environment:environment,caller:caller,frames:frames,suspended:suspended,pending:pending,
            evaluations:evaluations,nextPending:nextPending,nextEvaluation:nextEvaluation).json
    }
    init(program:CheckedProgram,ledger:Ledger,environment:Environment,caller:Caller) {
        self.program=program;self.ledger=ledger;self.environment=environment;self.caller=caller
    }
    private func observeService(_ event:String,site:String,scope:ScopeHandle? = nil,
                                owner:String? = nil,details:JSONValue = .null) {
        let requestOrigin:JSONValue = event.hasPrefix("producer.") ? .object([
            "job":.string(caller.job),"task":.string(caller.task),"command":.null,
            "scope":scope.map{.string($0.id)} ?? .null,"backend_return":.null,
            "source_epoch":(scope?.epoch).map(JSONValue.string) ?? .null,
            "owner":(scope?.owner).map(JSONValue.string) ?? .null,"site":.string(site)]) : .null
        ledger.serviceObservation.record(event,binding:caller.json,origin:requestOrigin,
            key:brsOperation(session:ledger.serviceSession,worker:ledger.serviceWorker,
                scope:scope?.id,owner:owner ?? scope?.owner,epoch:scope?.epoch,site:site),details:details)
    }
    func installInitialFrames(_ initial:[ActiveFrame]) { precondition(frames.isEmpty);frames=initial }
    func retirementReady()->Bool {waitContinuation == nil && environment.slots.isEmpty && environment.names.values.isEmpty && frames.isEmpty && suspended.isEmpty && pending.isEmpty && evaluations.isEmpty}
    private func beginEvaluation(_ site:String,kind:String)->Int {
        let id=nextEvaluation;nextEvaluation += 1;evaluations.append(MachineEvaluation(id:id,site:site,kind:kind));return id
    }
    private func finishEvaluation(_ id:Int,value:RuntimeValue?)throws {
        try runtimeRequire(evaluations.last?.id == id,"non-LIFO evaluation transfer")
        evaluations.removeLast()
        if let value,!evaluations.isEmpty {evaluations[evaluations.count-1].values.append(value)}
    }
    private func keepPending(_ saved:Environment)->Int {
        let id=nextPending;nextPending += 1;pending[id]=saved;return id
    }
    /// Move affine slots into a Machine-owned pending root before any suspension.
    private func retainForAwait(_ plan:FramePlan)throws->Int {
        keepPending(try environment.retainFrame(plan))
    }
    private func savedFrame(_ frame:ActiveFrame)throws->Environment {
        try runtimeRequire(waitContinuation == nil,"frame borrowed while wait owns control")
        guard let saved=suspended[frame.id] else {throw RuntimeInvariant("missing frame environment")};return saved
    }
    /// Environment.restore checks all affine conflicts before writing. This
    /// normal move clears the old authority before the next actor await. The
    /// exceptional Copy-only path keeps the un-restored affine abandonment
    /// root until pop succeeds, so a failed pop cannot lose that root.
    private func restoreFrame(_ frame:ActiveFrame,copyOnly:Bool=false)throws {
        let saved=try savedFrame(frame)
        if copyOnly {try environment.restore(Environment(slots:saved.slots.filter{$0.value.copyable},names:saved.names))}
        else {try environment.restore(saved);suspended.removeValue(forKey:frame.id)}
    }
    private func moveFrameToPending(_ frame:ActiveFrame)throws->Int {
        let id=keepPending(try savedFrame(frame))
        suspended.removeValue(forKey:frame.id)
        return id
    }
    private func suspendWait()throws->WaitContinuation {
        try runtimeRequire(waitContinuation == nil,"nested active machine wait")
        let state=WaitMachineState(environment:environment,caller:caller,frames:frames,suspended:suspended,pending:pending,
                                   evaluations:evaluations,nextPending:nextPending,nextEvaluation:nextEvaluation)
        let holder=WaitContinuation(state);waitContinuation=holder
        environment=Environment();frames=[];suspended=[:];pending=[:];evaluations=[]
        return holder
    }
    /// Called only by Ledger while this Task is suspended in awaitTerminal.
    /// All guards precede restoration; no await exposes an empty returned frame.
    func restoreWait(_ holder:WaitContinuation,state:WaitMachineState)throws {
        try runtimeRequire(waitContinuation === holder && environment.slots.isEmpty && environment.names.values.isEmpty &&
                           frames.isEmpty && suspended.isEmpty && pending.isEmpty && evaluations.isEmpty,"wait restoration target not empty")
        environment=state.environment;caller=state.caller;frames=state.frames;suspended=state.suspended;pending=state.pending
        evaluations=state.evaluations;nextPending=state.nextPending;nextEvaluation=state.nextEvaluation;waitContinuation=nil
    }
    private func adopt(_ frame:ActiveFrame,pending id:Int?)throws {
        if let id {
            guard let saved=pending[id] else {throw RuntimeInvariant("missing pending frame environment")}
            suspended[frame.id]=saved;pending.removeValue(forKey:id)
        }
        frames.append(frame)
    }
    private func install(_ b:Binding,_ value:RuntimeValue)throws {try environment.install(b,value,copyable:program.table.copyable(b.type))}
    private func lexical(_ ids:[SlotID],names:NameEnv?=nil,body:String)async throws->RuntimeValue {
        func close() {for id in ids {environment.remove(id)};if let names {environment.names=names}}
        do {let result=try await command(body);close();return result}
        catch let e as SourceFailure {close();throw e}
        catch {throw error} // Unknown/infrastructure keeps the actual source root and lexical state.
    }
    private func node(_ path:String)throws->JSONValue {
        try runtimeRequire(program.nodes[path] != nil,"missing executable source node")
        let a=try program.annotation(path);try runtimeRequire(a.execution == "reachable","attempt to execute checked ghost/dead source")
        return program.nodes[path]!
    }
    private func child(_ path:String,_ field:String)->String { pointerChild(path,field) }
    private func push(_ kind:String,path:String,retained:[SlotID]=[],pending:Int?=nil) async throws -> ActiveFrame {
        let frame=ActiveFrame(id:await ledger.fresh("frame"),kind:kind,site:path,retained:retained,caller:caller)
        try await ledger.push(frame);try adopt(frame,pending:pending);return frame
    }
    private func pop(_ frame:ActiveFrame) async throws {
        try runtimeRequire(frames.last?.id == frame.id,"machine non-LIFO pop")
        try await ledger.pop(frame);frames.removeLast();suspended.removeValue(forKey:frame.id)
    }
    private func plan(_ path:String,_ kind:String)throws->FramePlan {
        guard let p=try program.annotation(path).frame,p.kind == kind else { throw RuntimeInvariant("missing checked \(kind) frame at \(path)") };return p
    }
    private func bindNames(_ ids:[NominalID],_ actual:[Witness])throws {
        try runtimeRequire(ids.count == actual.count,"generation witness count")
        for (id,witness) in zip(ids,actual) { try environment.names.bind(id,witness) }
    }
    private func capture(_ annotation:Annotation) throws -> Environment {
        var captured=Environment();captured.names=environment.names
        for c in annotation.captures {
            let value=try environment.take(c.source,move:c.move)
            try runtimeRequire(captured.slots[c.binder] == nil,"duplicate capture destination")
            captured.slots[c.binder]=Slot(type:c.type,value:value,copyable:try program.table.copyable(c.type))
        }
        return captured
    }
    func value(_ path:String) async throws -> RuntimeValue {
        let evaluation=beginEvaluation(path,kind:"value")
        do {
            let result=try await valueBody(path)
            try finishEvaluation(evaluation,value:result);return result
        } catch let failure as SourceFailure {
            try finishEvaluation(evaluation,value:nil);throw failure
        } catch {throw error} // Actual operand values remain rooted on Unknown/infrastructure.
    }
    private func valueBody(_ path:String) async throws -> RuntimeValue {
        try await ledger.live();let source=try node(path);let a=try program.annotation(path)
        switch try source.field("value").text() {
        case "unit":return .unit
        case "bool":return .bool(try source.field("literal").boolean())
        case "word":return .word(try wordToken(source.field("literal").integerToken()))
        case "nat":return .nat(try unsignedToken(source.field("literal").integerToken()))
        case "var":
            try runtimeRequire(a.copy.count+a.move.count == 1,"variable lacks unique checked slot use")
            let move = !a.move.isEmpty;let id=(move ? a.move:a.copy)[0]
            return try environment.take(id,move:move)
        case "pair":return try await .pair(value(child(path,"left")),value(child(path,"right")))
        case "inl","inr":return try await .sum(source.field("value").text() == "inl",value(child(path,"payload")))
        case "pack":
            var witnesses:[Witness]=[]
            for raw in try source.field("witnesses").elements() {
                let fields=try raw.members()
                if let size=fields["size"],case .number(let token)=size {witnesses.append(.size(try unsignedToken(token)))}
                else {
                    let name:String
                    if let n=fields["name"] {name=try n.text()}
                    else {name=try raw.field("size").field("witness").text()}
                    guard let id=program.nominalEnvironments[path]?[name] else { throw RuntimeInvariant("PACK lacks lexical nominal") }
                    let witness=try environment.names.witness(id)
                    if fields["size"] != nil {guard case .size=witness else {throw RuntimeInvariant("PACK Size witness sort")}}
                    witnesses.append(witness)
                }
            }
            let payload=try await value(child(path,"payload"));let id=await ledger.fresh("package")
            let package=PackageValue(id:id,witnesses:witnesses,payload:payload)
            await ledger.emit("pack",site:path,caller:caller,details:RuntimeValue.package(package).diagnostic)
            return .package(package)
        case "lambda":
            let captured=try capture(a);let id=await ledger.fresh("closure");let mode=try source.field("mode").text()
            try runtimeRequire(mode == "A" || (mode == "U" && captured.affine.isEmpty),"unrestricted closure has affine captures")
            let closure=ClosureValue(id:id,site:path,mode:mode,environment:captured)
            await ledger.emit("closure.capture",site:path,caller:caller,details:.object(["closure":.string(id),"captures":captured.description,"name_env":captured.names.json]))
            return .closure(closure)
        default:throw RuntimeInvariant("unsupported executable value")
        }
    }
    func expression(_ path:String) async throws -> RuntimeValue {
        let source=try node(path)
        switch try source.field("expression").text() {
        case "value":return try await value(child(path,"value"))
        case "fst","snd":
            let v=try await value(child(path,"value"));guard case .pair(let l,let r)=v else { throw RuntimeInvariant("projection of non-pair") }
            return try source.field("expression").text() == "fst" ? l:r
        case "op":
            try runtimeRequire(try source.field("name").text() == "body_error","unknown fixed op")
            let args=try source.field("arguments").elements();try runtimeRequire(args.count == 1,"body_error arity")
            let argument=try await value(path+"/arguments/0");guard case .unit=argument else { throw RuntimeInvariant("body_error argument") }
            return .errorPayload("BodyRequestedFailure")
        default:throw RuntimeInvariant("unsupported executable expression")
        }
    }
    func command(_ path:String) async throws -> RuntimeValue {
        let evaluation=beginEvaluation(path,kind:"command")
        do {
            try await ledger.live();let a=try program.annotation(path)
            let result=try await commandBody(path)
            evaluations[evaluations.count-1].values.append(result)
            guard let expected=a.remaining else { throw RuntimeInvariant("normal exit from statically all-exception command") }
            try runtimeRequire(environment.affine == Set(expected),"normal affine residual mismatch at \(path)")
            try finishEvaluation(evaluation,value:result);return result
        } catch let failure as SourceFailure {
            try finishEvaluation(evaluation,value:nil);throw failure
        } catch {throw error}
    }
    private func commandBody(_ path:String) async throws -> RuntimeValue {
        let source=try node(path),a=try program.annotation(path)
        switch try source.field("command").text() {
        case "return":return try await value(child(path,"value"))
        case "let_e":
            let v=try await expression(child(path,"expression")),b=try a.binder("let")
            try install(b,v)
            return try await lexical([b.id],body:child(path,"next"))
        case "let_c":
            var frame:ActiveFrame?=nil
            if let p=a.frame {
                try runtimeRequire(p.kind == "Bind","LET-C unexpected frame")
                let retained=try retainForAwait(p)
                frame=try await push("Bind",path:path,retained:p.retained,pending:retained)
            }
            let v:RuntimeValue
            do { v=try await command(child(path,"first")) }
            catch {
                guard error is SourceFailure else {throw error}
                if let frame { try await pop(frame) } // exceptional Bind abandons its unique retained slots
                throw error
            }
            if let frame {try restoreFrame(frame);try await pop(frame)}
            let b=try a.binder("let");try install(b,v)
            return try await lexical([b.id],body:child(path,"next"))
        case "call":return try await call(path)
        case "if":
            guard case .bool(let b)=try await value(child(path,"condition")) else { throw RuntimeInvariant("IF value") }
            return try await command(child(path,b ? "then":"else"))
        case "case":
            let operand=try await value(child(path,"value"))
            guard case .sum(let left,let v)=operand else { throw RuntimeInvariant("CASE value") }
            await ledger.emit("case.selected",site:path,caller:caller,details:.object(["arm":.string(left ? "left":"right"),
                "operand":operand.diagnostic,"payload":v.diagnostic,"operand_type":try program.annotation(child(path,"value")).type,"result_type":a.type]))
            let binding=try a.binder("case",left ? 0:1);try install(binding,v)
            return try await lexical([binding.id],body:child(path,left ? "left":"right"))
        case "unpack":
            guard case .package(let p)=try await value(child(path,"package")),let generation=a.generation,generation.kind == "unpack" else { throw RuntimeInvariant("UNPACK value/origin") }
            let oldNames=environment.names;try bindNames(generation.names,p.witnesses)
            let b=try a.binder("generated");try install(b,p.payload)
            await ledger.emit("unpack",site:path,caller:caller,details:.object(["package":.string(p.id),"name_env":environment.names.json,"fresh_runtime_brands":.bool(false)]))
            return try await lexical([b.id],names:oldNames,body:child(path,"body"))
        case "drop":
            let v=try await value(child(path,"value"))
            await ledger.emit("value.dropped",site:path,caller:caller,details:v.diagnostic)
            return try await command(child(path,"next"))
        case "throw":
            guard case .errorPayload(let code)=try await value(child(path,"payload")) else { throw RuntimeInvariant("THROW value") }
            throw await ledger.fault(code,caller:caller,site:path)
        case "rethrow":
            guard case .failure(let ids)=try await value(child(path,"failure")) else { throw RuntimeInvariant("RETHROW value") }
            await ledger.rethrown(ids,caller:caller,site:path);throw SourceFailure(ids:ids)
        case "try":return try await tryCommand(path)
        case "load":
            guard case .read(let r)=try await value(child(path,"read")),case .nat(let index)=try await value(child(path,"index")) else { throw RuntimeInvariant("LOAD operands") }
            return .word(try await ledger.load(r,index:index,caller:caller,site:path))
        case "receive_request":
            guard case .owner(let owner)=try await value(child(path,"owner")) else {throw RuntimeInvariant("RECEIVE non-Owner operand")}
            let port=try source.field("name").text(),holder=try suspendWait()
            let (wait,input)=try await ledger.receiveExternal(port:port,owner:owner,caller:caller,site:path,kont:holder,machine:self)
            // Only this original source Task can observe the restored return.
            guard let tokens=input.words else {throw RuntimeInvariant("RECEIVE returned non-Word payload")}
            try await ledger.externalReceiveReturned(wait,caller:caller,tokens:tokens)
            return input
        case "call_service":
            guard case .scope(let scope)=try await value(child(path,"scope")) else {throw RuntimeInvariant("CALL_SERVICE requires Scope")}
            let tokens=try await value(child(path,"tokens")),holder=try suspendWait()
            let result=try await ledger.callService(name:source.field("name").text(),scope:scope,tokens:tokens,caller:caller,site:path,kont:holder,machine:self)
            guard case .word(let token)=result else {throw RuntimeInvariant("CALL_SERVICE returned non-Word")}
            try await ledger.serviceReceiveReturned(caller:caller,token:token)
            return result
        case "call_tool":
            guard case .word(let argument)=try await value(child(path,"argument")) else {throw RuntimeInvariant("CALL_TOOL requires Word")}
            let (invocation,root,tool)=try await ledger.prepareTool(name:source.field("name").text(),argument:argument,caller:caller,site:path)
            // Real open/read/parse/close on this original source Task, outside
            // the Ledger actor. The watchdog can still snapshot retained roots.
            let outcome=tool.perform(invocation,root:root)
            return try await ledger.completeTool(outcome,invocation:invocation,root:root,caller:caller,site:path)
        case "offer_response":
            guard case .owner(let owner)=try await value(child(path,"owner")),
                  case .word(let token)=try await value(child(path,"token")) else {
                throw RuntimeInvariant("OFFER_RESPONSE requires Owner and Word operands")
            }
            // One actor entry; its body has no suspension, publication, reply
            // or source-holder transfer. A registered intent is not delivery.
            try await ledger.offerExternalResponse(port:source.field("name").text(),owner:owner,
                                                   token:token,caller:caller,site:path)
            return .unit
        case "await","await_cancel":
            let operandPath=child(path,"shared"),operand=try await value(operandPath)
            guard case .shared(let target)=operand else {throw RuntimeInvariant("AWAIT non-Shared value")}
            let operandType=try program.annotation(operandPath).type
            try runtimeRequire(program.table.tag(operandType) == "Shared","AWAIT operand complete type")
            let fields=try program.table.fields(operandType)
            guard let nominal=fields["job"] else {throw RuntimeInvariant("AWAIT missing Shared job")}
            var budget=100
            let rawNominal=try program.table.smallValue(nominal,remaining:&budget)
            let named=try rawNominal.field("nominal")
            let identity=NominalID(site:try named.field("site").field("pointer").text(),role:try named.field("role").text(),
                                   index:try decimalIndex(named.field("index").text()),sort:try named.field("sort").text())
            try runtimeRequire(environment.names.witness(identity) == .brand(sort:"Job",id:target),"AWAIT Shared NameEnv identity")
            let holder=try suspendWait()
            if try source.field("command").text() == "await_cancel" {
                let w=try await ledger.createPublicationWait(target:target,caller:caller,site:path,operand:operand.diagnostic,
                    operandType:operandType,resultType:a.type,kont:holder,machine:self)
                let wake=try await ledger.waitPublication(w)
                let outcome=try await ledger.returnPublicationWait(wake,caller:caller,machine:self)
                // This executes on the original caller only after the waiting
                // function and the full source-state restoration have returned.
                let observedOutcome:String
                switch outcome {
                case .ok(.sum(let left,_)):observedOutcome=left ? "delivered":"cancelled"
                case .failure:observedOutcome="failure"
                case .infrastructure:observedOutcome="infrastructure"
                default:observedOutcome="other"
                }
                observeService("waiter.return",site:path,details:.object(["wait":.string(w),"target":.string(target),"outcome":.string(observedOutcome)]))
                try await ledger.publicationReturnObserved(w,caller:caller,outcome:outcome)
                return try unwrap(outcome)
            }
            let outcome=try await ledger.awaitTerminal(target:target,caller:caller,site:path,operand:operand.diagnostic,
                                                       operandType:operandType,resultType:a.type,kont:holder,machine:self)
            return try unwrap(outcome)
        case "new_owner":
            guard case .nat(let size)=try await value(child(path,"value")),let g=a.generation,g.kind == "new-owner" else { throw RuntimeInvariant("NEW operands/origin") }
            let p=try plan(path,"GenBind"),retained=try retainForAwait(p)
            let frame=try await push("GenBind",path:path,retained:p.retained,pending:retained)
            do {
                let (owner,names)=try await ledger.allocate(size:size,caller:caller,site:path)
                try restoreFrame(frame);try await pop(frame)
                let old=environment.names;try bindNames(g.names,names);let b=try a.binder("generated");try install(b,.owner(owner))
                return try await lexical([b.id],names:old,body:child(path,"body"))
            } catch {
                guard error is SourceFailure else {throw error}
                if frames.last?.id == frame.id { try await pop(frame) };throw error
            }
        case "scope","run":return try await scopeCommand(path)
        case "produce":return try await produce(path)
        case "fork":return try await fork(path)
        case "request_cancel":
            guard case .control(let p)=try await value(child(path,"control")) else { throw RuntimeInvariant("REQUEST-CANCEL value") }
            try await ledger.requestCancel(p,caller:caller,site:path)
            return try await command(child(path,"next"))
        default:throw RuntimeInvariant("unsupported executable command")
        }
    }
    private func call(_ path:String) async throws -> RuntimeValue {
        guard case .closure(let f)=try await value(child(path,"function")) else { throw RuntimeInvariant("CALL non-function") }
        let argument=try await value(child(path,"argument")),p=try plan(path,"CallReturn")
        try runtimeRequire(environment.affine == Set(p.retained),"CALL retained environment mismatch")
        let retained=try retainForAwait(p)
        let frame=try await push("CallReturn",path:path,retained:p.retained,pending:retained),parent=caller
        let calleeAnnotation=try program.annotation(f.site)
        environment=try f.enter();let parameter=try calleeAnnotation.binder("parameter");try install(parameter,argument)
        await ledger.emit("call.enter",site:path,caller:caller,details:.object(["closure":.string(f.id),"definition_site":.string(f.site),"name_env":environment.names.json]))
        do {
            let result=try await command(child(f.site,"body"))
            try runtimeRequire(environment.affine.isEmpty,"callee retains affine source slots")
            environment=Environment();try restoreFrame(frame);caller=parent;try await pop(frame)
            await ledger.emit("call.exit",site:path,caller:caller,details:result.diagnostic);return result
        } catch {
            guard error is SourceFailure else {throw error}
            // The CallReturn owns retained caller slots exactly once; exceptions abandon them.
            environment=Environment();try restoreFrame(frame,copyOnly:true);caller=parent
            if !(error is IncompleteStop) { try await pop(frame) };throw error
        }
    }
    private func tryCommand(_ path:String) async throws -> RuntimeValue {
        let a=try program.annotation(path),p=try plan(path,"Try")
        let retained=try retainForAwait(p)
        let frame=try await push("Try",path:path,retained:p.retained,pending:retained)
        do {
            let result=try await command(child(path,"body"));try restoreFrame(frame);try await pop(frame);return result
        } catch let failure as SourceFailure {
            environment.slots=environment.slots.filter{$0.value.copyable}
            let restorePending=try moveFrameToPending(frame)
            try await pop(frame)
            let restore=try await push("Restore",path:path,retained:p.retained,pending:restorePending)
            let b=try a.binder("handler");try install(b,.failure(failure.ids))
            do {
                let result=try await command(child(path,"handler"));environment.remove(b.id)
                try restoreFrame(restore);try await pop(restore)
                await ledger.markHandled(failure.ids,caller:caller,site:path);return result
            } catch { if error is SourceFailure {environment.remove(b.id);try await pop(restore)};throw error }
        } catch { throw error }
    }
    private func scopeCommand(_ path:String) async throws -> RuntimeValue {
        let source=try node(path),a=try program.annotation(path),tag=try source.field("command").text()
        var owner:OwnerHandle?=nil
        if tag == "run" { guard case .owner(let o)=try await value(child(path,"owner")) else { throw RuntimeInvariant("RUN owner") };owner=o }
        guard let g=a.generation else { throw RuntimeInvariant("scope origin") }
        let oldNames=environment.names,parent=caller,p=try plan(path,"ScopeExit")
        // ScopeExit keeps only its explicitly checked residual slots. Body retains its own inputs.
        let retained=try retainForAwait(p)
        let context:ScopeContext
        if let owner {observeService("scope.enter_call",site:path,owner:owner.id,details:.object(["operation_kind":.string("RUN-enter")]))}
        do {context=try await ledger.enterScope(owner:owner,parent:caller,site:path,retained:p.retained)}
        catch let e as SourceFailure {pending.removeValue(forKey:retained);throw e}
        catch {throw error}
        let frame=context.frame;try adopt(frame,pending:retained)
        caller=context.caller;try bindNames(g.names,context.names)
        let b=try a.binder("generated");try install(b,.scope(context.handle))
        let outcome:Outcome
        do { outcome = .ok(try await command(child(path,"body"))) }
        catch let e as SourceFailure { outcome = .failure(e.ids) }
        catch { await ledger.markUnresolved(caller,message:String(describing:error));throw error }
        environment.remove(b.id)
        if owner != nil {observeService("drain.call",site:path,scope:context.handle)}
        let drained=try await ledger.drain(context.handle,body:outcome,site:path)
        if owner != nil {observeService("drain.return",site:path,scope:context.handle)}
        environment.names=oldNames;caller=parent
        if case .ok=drained {try restoreFrame(frame)}
        else {try restoreFrame(frame,copyOnly:true)}
        try await pop(frame)
        return try unwrap(drained)
    }
    private func produce(_ path:String) async throws -> RuntimeValue {
        let a=try program.annotation(path)
        guard case .scope(let h)=try await value(child(path,"scope")),let g=a.generation else { throw RuntimeInvariant("PRODUCE operands") }
        let argument=try await value(child(path,"argument")),operation=try node(path).field("kernel").text()
        let arguments=try await ledger.prefillArguments(operation:operation,value:argument,caller:caller,site:path)
        let captured=try capture(a);try runtimeRequire(captured.affine.isEmpty,"producer captured affine source")
        let p=try plan(path,"GenBind"),retained=try retainForAwait(p)
        let frame=try await push("GenBind",path:path,retained:p.retained,pending:retained)
        let context:ProducerContext
        do { context=try await ledger.launchProducer(scope:h,arguments:arguments,environment:captured,site:path,parent:caller,genBind:frame) }
        catch { if error is SourceFailure { try await pop(frame) };throw error }
        try await ledger.producerParentReady(context,site:path,parent:caller)
        try restoreFrame(frame);try await pop(frame)
        let old=environment.names;try bindNames(g.names,context.names)
        let hb=try a.binder("generated",0),kb=try a.binder("generated",1)
        try install(hb,.shared(context.caller.job));try install(kb,.control(context.caller.job))
        return try await lexical([hb.id,kb.id],names:old,body:child(path,"next"))
    }
    private func fork(_ path:String)async throws->RuntimeValue {
        let a=try program.annotation(path)
        guard case .scope(let target)=try await value(child(path,"scope")),let generation=a.generation,generation.kind == "fork" else {
            throw RuntimeInvariant("FORK operands/generation")
        }
        let captures=try capture(a);try runtimeRequire(captures.affine.isEmpty,"FORK captured affine source")
        let plan=try plan(path,"GenBind"),retained=try retainForAwait(plan)
        let frame=try await push("GenBind",path:path,retained:plan.retained,pending:retained)
        let context:OrdinaryContext
        do {context=try await ledger.launchOrdinary(scope:target,environment:captures,site:path,parent:caller,genBind:frame)}
        catch {if error is SourceFailure {try await pop(frame)};throw error}
        try await ledger.ordinaryParentReady(context,parent:caller)
        try restoreFrame(frame);try await pop(frame)
        let oldNames=environment.names;try bindNames(generation.names,context.names)
        let h=try a.binder("generated",0),k=try a.binder("generated",1)
        try install(h,.shared(context.caller.job));try install(k,.control(context.caller.job))
        return try await lexical([h.id,k.id],names:oldNames,body:child(path,"next"))
    }
    private func unwrap(_ outcome:Outcome)throws->RuntimeValue {
        switch outcome { case .ok(let v):return v;case .failure(let ids):throw SourceFailure(ids:ids);case .infrastructure(let e):throw RuntimeInvariant(e) }
    }
    private func clearAtJobFinish(_ outcome:Outcome,site:String)async throws {
        if case .ok=outcome {try runtimeRequire(environment.affine.isEmpty,"normal affine residual at JobFinish")}
        else {await ledger.emit("source.abandoned",site:site,caller:caller,details:.object(["affine_slots":.array(environment.affine.sorted{$0.key<$1.key}.map{.string($0.key)})]))}
        environment=Environment()
    }
    func runMain(scope:ScopeHandle,hostCaller:Caller) async -> RuntimeTaskExit {
        do {
            try await ledger.started(caller)
            try runtimeRequire(frames.count == 2 && frames[0].kind == "JobFinish" && frames[1].kind == "ScopeExit","INIT frames missing")
            let finish=frames[0],exit=frames[1]
            let body:Outcome
            do { body = .ok(try await command("/main/body")) } catch let e as SourceFailure { body = .failure(e.ids) }
            let outcome=try await ledger.drain(scope,body:body,site:"/main/body")
            try await pop(exit);caller=hostCaller
            try await clearAtJobFinish(outcome,site:"/main/body");try await pop(finish)
            try await ledger.retire(caller,outcome:outcome,site:"/main/body")
            return .published(try await finishPublication(site:"/main/body"))
        } catch { await ledger.markUnresolved(caller,message:String(describing:error));await ledger.bodyReturningUnpublished(caller,error:error,sourceSnapshot:drainStateDescription());return .source(.infrastructure(String(describing:error))) }
    }
    func runProducer(context:ProducerContext,arguments:PrefillArguments,site:String) async -> RuntimeTaskExit {
        observeService("producer.enter",site:site,scope:context.scope)
        do {
            try await ledger.started(caller)
            try runtimeRequire(frames.count == 1 && frames[0].kind == "KernelFinish","producer kernel continuation missing")
            let a=try program.annotation(site);guard let g=a.generation else { throw RuntimeInvariant("producer origin missing") }
            try bindNames(g.names,context.names)
            try await ledger.evaluatePrefill(context,arguments:arguments,site:site)
            try await ledger.observeCancel(caller,site:site)
            let finish=try await ledger.finishWrite(context,site:site)
            try runtimeRequire(frames[0].id == finish.id,"kernel continuation identity changed")
            frames[0]=finish;caller=context.caller
            let readBinding=try a.binder("cleanup-parameter");try install(readBinding,.read(context.read))
            let outcome:Outcome
            do { outcome = .ok(try await command(site+"/cleanup/body")) } catch let e as SourceFailure { outcome = .failure(e.ids) }
            try await clearAtJobFinish(outcome,site:site)
            try await ledger.finishAccesses(caller,site:site)
            try await pop(finish)
            // JobFinish exits cleanup scope/region into producer root before publishing.
            let rp=context.names[3];guard case .brand(_,let region)=rp else { throw RuntimeInvariant("producer root region") }
            caller=Caller(job:caller.job,task:caller.task,invocation:caller.invocation,region:region,scopes:[])
            try await ledger.retire(caller,outcome:outcome,site:site)
            let publication=try await finishPublication(site:site)
            observeService("producer.body_return_edge",site:site,scope:context.scope,details:.object(["published":.bool(true),"publication":.string(publication.id)]))
            if case .ok = publication.outcome {
                ledger.returnService?.producerEdge(worker:ledger.serviceWorker,scope:context.scope,
                    kernel:context.kernelCaller,cleanup:context.caller,exit:caller,site:site,publication:publication.id)
            }
            if arguments.operation == "Prefill32" {
                let successful:Bool
                switch publication.outcome {
                case .ok:successful=true
                case .failure:successful=false
                case .infrastructure:throw RuntimeInvariant("published infrastructure is not a source failure edge")
                }
                // A normal SourceFailure still returns its original Task and
                // owes the same backend ACK. Record that edge without opening
                // the request gate; observation must not block safe cleanup.
                _ = try ledger.externalDemand?.producerEdge(worker:ledger.serviceWorker,scope:context.scope,
                    kernel:context.kernelCaller,cleanup:context.caller,exit:caller,site:site,
                    publication:publication.id,successful:successful)
            }
            return .published(publication)
        } catch {
            // This Task is about to return an unpublished infrastructure exit.
            // The retained Machine is not a claim that its Swift stack still runs.
            await ledger.markUnresolved(caller,message:String(describing:error))
            await ledger.bodyReturningUnpublished(caller,error:error,sourceSnapshot:drainStateDescription())
            observeService("producer.body_return_edge",site:site,scope:context.scope,details:.object(["published":.bool(false)]))
            return .source(.infrastructure(String(describing:error)))
        }
    }
    func runOrdinary(context:OrdinaryContext)async->RuntimeTaskExit {
        do {
            try await ledger.started(caller)
            try runtimeRequire(caller == context.caller && frames.count == 1 && frames[0].kind == "JobFinish","ordinary entry caller/sentinel")
            let finish=frames[0],annotation=try program.annotation(context.site)
            guard let generation=annotation.generation,generation.kind == "fork" else {throw RuntimeInvariant("ordinary generation missing")}
            try bindNames(generation.names,context.names)
            let outcome:Outcome
            do {outcome = .ok(try await command(context.site+"/job/body"))}
            catch let failure as SourceFailure {outcome = .failure(failure.ids)}
            try runtimeRequire(frames.count == 1 && frames[0].id == finish.id,"ordinary reached exit with unfinished source frames")
            try await clearAtJobFinish(outcome,site:context.site);try await pop(finish)
            caller=context.exitCaller
            try await ledger.retire(caller,outcome:outcome,site:context.site)
            return .published(try await finishPublication(site:context.site))
        } catch {
            await ledger.markUnresolved(caller,message:String(describing:error))
            await ledger.bodyReturningUnpublished(caller,error:error,sourceSnapshot:drainStateDescription())
            return .source(.infrastructure(String(describing:error)))
        }
    }
    /// Each actor call is made by this original producer/main Task. Cache
    /// publication and per-cell claims are separate scheduling opportunities;
    /// there is no observer Task and no Task.value on publication wait paths.
    private func finishPublication(site:String)async throws->PublishedOutcome {
        try await ledger.beforePublication(caller.job,site:site)
        let (publication,candidates)=try await ledger.publish(caller.job,site:site)
        for w in candidates {try await ledger.claimPublication(w,publication:publication,site:site)}
        try await ledger.retirePublicationRegistry(publication,site:site)
        return try await ledger.producerEpilogue(caller.job,site:site)
    }
}

/// The caller must validate the compiler exchange before initializing this
/// real driver. There is no implicit model, Metal device, or fallback backend.
/// Only the trusted host can supply its remaining fixed budget. There is no
/// source or ordinary CLI deadline field; values outside 1...180 are rejected
/// before INIT and no driver command is sent.
package func execute(_ program:CheckedProgram,backend:any MLXBackendDriver,hostBudgetSeconds:Int = 180,returnService:ReturnServiceControl? = nil,externalDemand:ExternalDemandControl? = nil,
                     externalResultDelivery:ExternalResultDeliveryPolicy = .afterBackendClose)async->ExecutionReport {
    await executeRuntime(program,backend:backend,watchdogSeconds:hostBudgetSeconds,nativeOwnsBackend:false,returnService:returnService,externalDemand:externalDemand,externalResultDelivery:externalResultDelivery)
}
package func executeMLXForkDeterministic(_ program:CheckedProgram,backend:any MLXBackendDriver,hostBudgetSeconds:Int = 180,returnService:ReturnServiceControl? = nil,externalDemand:ExternalDemandControl? = nil,
                                         externalResultDelivery:ExternalResultDeliveryPolicy = .afterBackendClose)async->ExecutionReport {
    await executeRuntime(program,backend:backend,watchdogSeconds:hostBudgetSeconds,nativeOwnsBackend:false,forkPolicy:.attachedCancellation,returnService:returnService,externalDemand:externalDemand,externalResultDelivery:externalResultDelivery)
}
/// Fixed native-only deadline; source and the public CLI cannot set a budget.
package enum MLXNativeWatchdog:Sendable {case threeSeconds}
package func executeForMLXTesting(_ program:CheckedProgram,backend:any MLXBackendDriver,watchdog:MLXNativeWatchdog = .threeSeconds,
                                  policy:MLXForkTestPolicy = .none,phaseControl:(any MLXForkCPUPhaseControl)? = nil,returnService:ReturnServiceControl? = nil,externalDemand:ExternalDemandControl? = nil)async->ExecutionReport {
    switch watchdog {case .threeSeconds:return await executeRuntime(program,backend:backend,watchdogSeconds:3,nativeOwnsBackend:true,forkPolicy:policy,phaseControl:phaseControl,returnService:returnService,externalDemand:externalDemand,externalResultDelivery:.afterBackendClose)}
}
private func executeRuntime(_ program:CheckedProgram,backend:any MLXBackendDriver,watchdogSeconds:Int,nativeOwnsBackend:Bool,
                            forkPolicy:MLXForkTestPolicy = .none,phaseControl:(any MLXForkCPUPhaseControl)? = nil,returnService:ReturnServiceControl? = nil,externalDemand:ExternalDemandControl? = nil,
                            externalResultDelivery:ExternalResultDeliveryPolicy = .afterBackendClose)async->ExecutionReport {
    let root=RuntimeExecutionRoot()
    do {
        try await startRuntime(program,backend:backend,root:root,watchdogSeconds:watchdogSeconds,nativeOwnsBackend:nativeOwnsBackend,
            forkPolicy:forkPolicy,phaseControl:phaseControl,returnService:returnService,externalDemand:externalDemand,externalResultDelivery:externalResultDelivery,parentHost:nil)
        return await root.awaitReport()
    } catch {return ExecutionReport(json:.object(["schema_version":.integer(1),"experiment":.string("core-language-runtime-mlx-fork-cm2"),"status":.string("policy-unbound-or-mismatch"),"diagnostic":backendDiagnostic(error),"backend_roots":backend.snapshot()]),exitCode:2)}
}
package func startChildExecution(_ program:CheckedProgram,backend:any MLXBackendDriver,root:RuntimeExecutionRoot,
    returnService:ReturnServiceControl,externalDemand:ExternalDemandControl,hostBudgetSeconds:Int)async throws {
    try runtimeRequire(program.executionDomain == .mlxLeaf && externalDemand.serviceResponsePort != nil &&
        returnService.policy == .unchanged && returnService.competitionTasks == 0 && externalDemand.policy == .unchanged,"fixed leaf service runtime configuration")
    try validateExternalDemandConfiguration(program,configured:true)
    try await startRuntime(program,backend:backend,root:root,watchdogSeconds:hostBudgetSeconds,nativeOwnsBackend:false,
        returnService:returnService,externalDemand:externalDemand,externalResultDelivery:.afterSourceJoin,parentHost:nil)
}
package func executeParent(_ program:CheckedProgram,root:RuntimeExecutionRoot,environment:ParentHostEnvironment,
    hostBudgetSeconds:Int=50)async->ExecutionReport {
    var ownedClaim:RuntimeStartClaim?
    do {
        let claim=try root.claimStart();ownedClaim=claim
        try validateParentServiceConfiguration(program)
        try runtimeRequire(!environment.file.isNativeFixture,"production parent rejects native file capability")
        try await startRuntime(program,backend:nil,root:root,watchdogSeconds:hostBudgetSeconds,nativeOwnsBackend:false,
            externalResultDelivery:.afterBackendClose,parentHost:environment,startClaim:claim)
        return await root.awaitReport()
    } catch {
        let report=ExecutionReport(json:.object(["schema_version":.integer(1),"experiment":.string("src1b-parent-host-close"),"status":.string("configuration-rejected"),"diagnostic":backendDiagnostic(error),"execution_root":root.snapshot()]),exitCode:2)
        // No original Task was created on a preflight rejection.
        if let ownedClaim {await root.rejectPreflight(report,claim:ownedClaim,domain:.hostParent)}
        return report
    }
}
package func executeParentForTesting(_ program:CheckedProgram,root:RuntimeExecutionRoot,environment:ParentHostEnvironment,
    hostBudgetSeconds:Int=50,control:(any ParentServiceCPUControl)? = nil)async->ExecutionReport {
    var ownedClaim:RuntimeStartClaim?
    do {
        let claim=try root.claimStart();ownedClaim=claim
        try validateParentServiceConfiguration(program)
        try await startRuntime(program,backend:nil,root:root,watchdogSeconds:hostBudgetSeconds,nativeOwnsBackend:false,
            externalResultDelivery:.afterBackendClose,parentHost:environment,parentCPU:control,startClaim:claim)
        return await root.awaitReport()
    } catch {
        let report=ExecutionReport(json:.object(["status":.string("configuration-rejected"),"native_parent_control":.bool(true),"diagnostic":backendDiagnostic(error),"execution_root":root.snapshot()]),exitCode:2)
        if let ownedClaim {await root.rejectPreflight(report,claim:ownedClaim,domain:.hostParent)}
        return report
    }
}
private func startRuntime(_ program:CheckedProgram,backend:(any MLXBackendDriver)?,root:RuntimeExecutionRoot,
    watchdogSeconds:Int,nativeOwnsBackend:Bool,forkPolicy:MLXForkTestPolicy = .none,phaseControl:(any MLXForkCPUPhaseControl)? = nil,
    returnService:ReturnServiceControl? = nil,externalDemand:ExternalDemandControl? = nil,
    externalResultDelivery:ExternalResultDeliveryPolicy,parentHost:ParentHostEnvironment?,parentCPU:(any ParentServiceCPUControl)? = nil,startClaim:RuntimeStartClaim? = nil)async throws {
    let claim=try startClaim ?? root.claimStart()
    try runtimeRequire(!root.isInstalled,"RuntimeExecutionRoot already installed")
    try runtimeRequire((1...180).contains(watchdogSeconds) && (!nativeOwnsBackend || watchdogSeconds == 3),"invalid trusted host budget")
    try runtimeRequire((program.executionDomain == .hostParent) == (parentHost != nil) && (backend == nil) == (parentHost != nil),"runtime execution domain/backend mismatch")
    try runtimeRequire(externalDemand == nil || returnService?.policy != .raiseMain,"external demand cannot combine with unconditional return-service escalation")
    try runtimeRequire(externalResultDelivery == .afterBackendClose || (!nativeOwnsBackend && externalDemand != nil),"after-source-join requires production external demand and production backend ownership")
    try runtimeRequire(forkPolicy != .ownedCoexist || (nativeOwnsBackend && phaseControl != nil),"C1 requires native CPU transport")
    let binding = forkPolicy == .attachedCancellation ? try deterministicBinding(program):nil
    try backend?.checkHealthy()
    let ledger=Ledger(program:program,backend:backend,watchdogSeconds:watchdogSeconds,nativeOwnsBackend:nativeOwnsBackend,
        forkPolicy:forkPolicy,deterministic:binding,phaseControl:phaseControl,returnService:returnService,externalDemand:externalDemand,
        externalResultDelivery:externalResultDelivery,parentHost:parentHost,parentCPU:parentCPU)
    try root.install(ledger:ledger,domain:program.executionDomain,serviceID:externalDemand?.serviceResponsePort?.serviceID,claim:claim)
    defer {root.creationEnded()}
    let observerGate=ServiceGate(),watchdogGate=ServiceGate()
    // From this point a failure is an installed-root report, not a thrown
    // preflight error. Any created main gate is opened only after stop is set.
    do {
        try externalDemand?.bindServiceRuntime(root,ledger:ledger.ledgerID)
        try parentHost?.registry.install(parentLedger:ledger.ledgerID)
        let (job,main)=try await ledger.startMain(root:root)
        let observer=Task<Void,Never> {
            guard await observerGate.wait() else{return}
            do {
                let exit=try await main.value;root.observedMain()
                if case .published(let publication)=exit {
                    if let report=await ledger.finishHost(job:job,publication:publication) {await root.offerReport(report)}
                } else if let report=await ledger.hostTaskAwaitFailed(job:job,message:"host joined unpublished or registration-only Task exit",actualExit:exit) {await root.offerReport(report)}
            } catch {
                root.observedMain()
                if let report=await ledger.hostTaskAwaitFailed(job:job,message:String(describing:error)) {await root.offerReport(report)}
            }
            await ledger.hostObserverReturning()
        }
        try root.installObserver(observer);try await ledger.installHostObserver(observer)
        let watchdog=Task<Void,Never> {
            guard await watchdogGate.wait() else{return}
            do {try await Task.sleep(for:.seconds(watchdogSeconds))} catch{return}
            if let report=await ledger.timeout(){await root.offerReport(report)}
        }
        try root.installWatchdog(watchdog)
        if let externalDemand {try externalDemand.admitServiceRuntime(root)} else {try root.admitStartup()}
        observerGate.resolve(true);watchdogGate.resolve(true);await ledger.openMainGate()
    } catch {
        parentHost?.registry.requestAbortIfOwned(parentLedger:ledger.ledgerID,reason:.creationAborted)
        let report=await ledger.infrastructure(String(describing:error)) ?? ExecutionReport(json:.object(["status":.string("incomplete"),"diagnostic":backendDiagnostic(error),"root":root.snapshot()]),exitCode:2)
        observerGate.resolve(true);watchdogGate.resolve(true)
        await ledger.openMainGate();await root.offerReport(report)
    }
}
