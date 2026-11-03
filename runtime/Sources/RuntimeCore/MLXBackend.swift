import Foundation

/// This package contract is implemented by the trusted transport target. It is
/// never supplied by source JSON. There is deliberately no default driver.
package protocol MLXBackendDriver: Sendable {
    var identity: MLXSessionIdentity { get }
    var serviceObservation: BRSServiceTrace { get }
    /// Hold the same health/phase mutex through this nonescaping synchronous
    /// commit. No suspension, callback into Ledger, or reverse lock acquisition.
    /// This covers observed health, not an omniscient guarantee of OS liveness.
    func withHealthyAdmission<T>(_ body: () throws -> T) throws -> T
    /// Host-only registration: same health mutex, but eval/leased/release are
    /// admissible. No callback may suspend or acquire Ledger in reverse.
    func withHealthyHostAction<T>(_ body: () throws -> T) throws -> T
    func checkHealthy() throws
    func evaluate(_ request: MLXEvalRequest) async throws -> MLXHostReceipt
    func release(_ request: MLXReturnRequest) async throws -> MLXReturnACK
    /// Immutable diagnostic snapshot of independently retained process/IO roots.
    func snapshot() -> JSONValue
    /// Actual SHUTDOWN ACK + reap + EOF/reader/permit fences. Called after main
    /// Task.value; producer Tasks never wait on this host finalizer.
    func shutdown() async throws -> MLXClosedEvidence
}

package struct MLXBackendFailure: Error, Sendable {
    package let faultID:String, code:String, raw:JSONValue
    package init(faultID:String,code:String,raw:JSONValue) {self.faultID=faultID;self.code=code;self.raw=raw}
}
func backendDiagnostic(_ error:any Error)->JSONValue {
    if let failure=error as? MLXBackendFailure {
        return .object(["fault_id":.string(failure.faultID),"code":.string(failure.code),"raw":failure.raw])
    }
    return .object(["kind":.string("local-infrastructure"),"message":.string(String(describing:error))])
}

package let cm1BackendContract: JSONValue = .object([
    "id":.string("p8-cm1-mlx-backend-return-v1"),"allocation_kind":.string("MLXPrefillOwner1"),
    "word_slots":.integer(1),"private_kv_layers":.integer(28),
    "return_obligation":.string("BackendReturn-v1"),"unknown_policy":.string("host-incomplete-sticky-v1")])

package struct MLXPoolIdentity: Sendable, Equatable {
    package let layer: Int, poolID: String, objectID: String
    package init(layer:Int,poolID:String,objectID:String) {self.layer=layer;self.poolID=poolID;self.objectID=objectID}
    package var json:JSONValue {.object(["layer":.integer(layer),"pool_id":.string(poolID),"pool_object_id":.string(objectID)])}
}
package struct MLXSessionIdentity: Sendable, Equatable {
    package let session:String, worker:String, pid:Int, pgid:Int, poolGroup:String
    package let pools:[MLXPoolIdentity], provenance:JSONValue
    package init(session:String,worker:String,pid:Int,pgid:Int,poolGroup:String,pools:[MLXPoolIdentity],provenance:JSONValue) throws {
        try runtimeRequire(!session.isEmpty && !worker.isEmpty && pid > 0 && pgid > 0 && !poolGroup.isEmpty,"invalid initialized MLX session")
        try runtimeRequire(pools.map(\.layer) == Array(0..<28) && Set(pools.map(\.poolID)).count == 28 && Set(pools.map(\.objectID)).count == 28,"MLX requires 28 distinct actual READY pools")
        self.session=session;self.worker=worker;self.pid=pid;self.pgid=pgid;self.poolGroup=poolGroup;self.pools=pools;self.provenance=provenance
    }
    package var json:JSONValue {.object(["session":.string(session),"worker_id":.string(worker),"pid":.integer(pid),"pgid":.integer(pgid),"pool_group":.string(poolGroup),"pools":.array(pools.map(\.json)),"provenance":provenance])}
}
package struct MLXEvalRequest: Sendable, Equatable {
    package let commandID:String, operation:String, tokens:[UInt64], inputSHA256:String
    package let owner:String, sourceEpoch:String, ownerOrdinal:Int, scope:String, returnID:String
    package let job:String, task:String, invocation:String, region:String, access:String, site:String
    package let session:MLXSessionIdentity
    init(commandID:String,operation:String,tokens:[UInt64],owner:String,sourceEpoch:String,ownerOrdinal:Int,scope:String,returnID:String,caller:Caller,access:String,site:String,session:MLXSessionIdentity) throws {
        self.commandID=commandID;self.operation=operation;self.tokens=tokens
        inputSHA256=try digest(JSONValue.array(tokens.map(JSONValue.unsigned)).encoded())
        self.owner=owner;self.sourceEpoch=sourceEpoch;self.ownerOrdinal=ownerOrdinal;self.scope=scope;self.returnID=returnID
        job=caller.job;task=caller.task;invocation=caller.invocation;region=caller.region;self.access=access;self.site=site;self.session=session
    }
    var serviceOrigin:JSONValue {.object([
        "job":.string(job),"task":.string(task),"command":.string(commandID),"scope":.string(scope),
        "backend_return":.string(returnID),"source_epoch":.string(sourceEpoch),"owner":.string(owner),"site":.string(site)])}
    package var json:JSONValue {.object(["command":.string(commandID),"operation":.string(operation),"tokens":.array(tokens.map(JSONValue.unsigned)),"input_sha256":.string(inputSHA256),"owner":.string(owner),"source_epoch":.string(sourceEpoch),"owner_ordinal":.integer(ownerOrdinal),"scope":.string(scope),"backend_return":.string(returnID),"job":.string(job),"task":.string(task),"invocation":.string(invocation),"region":.string(region),"access":.string(access),"site":.string(site),"session_identity":session.json])}
}

func backendInt(_ value:JSONValue)throws->UInt64 {try unsignedToken(value.integerToken())}
func backendInts(_ value:JSONValue)throws->[UInt64] {try value.elements().map(backendInt)}

/// raw is the unmodified, strictly decoded worker frame. The driver binds the
/// original request object after validating transport sequence and phase. Core
/// independently validates all identity/pool/value/epoch fields before adoption.
package struct MLXHostReceipt: Sendable, Equatable {
    package let request:MLXEvalRequest, raw:JSONValue
    package init(request:MLXEvalRequest,raw:JSONValue) {self.request=request;self.raw=raw}
    var evalSequence:UInt64 {get throws {try backendInt(raw.field("seq"))}}
    var wireLease:UInt64 {get throws {try backendInt(raw.field("lease_epoch"))}}
    var receiptID:String {get throws {try raw.field("receipt_id").text()}}
    var token:UInt64 {get throws {try backendInt(raw.field("token_id"))}}
    var layers:[JSONValue] {get throws {try raw.field("layers").elements()}}
    package var json:JSONValue {.object(["request":request.json,"worker_frame":raw])}
}
package struct MLXReturnRequest: Sendable, Equatable {
    package let receipt:MLXHostReceipt, returnID:String, scope:String
    package init(receipt:MLXHostReceipt,returnID:String,scope:String) {self.receipt=receipt;self.returnID=returnID;self.scope=scope}
    package var json:JSONValue {.object(["backend_return":.string(returnID),"scope":.string(scope),"eval_receipt":receipt.json])}
}
package struct MLXReturnACK: Sendable, Equatable {
    package let request:MLXReturnRequest, raw:JSONValue
    package init(request:MLXReturnRequest,raw:JSONValue) {self.request=request;self.raw=raw}
    package var json:JSONValue {.object(["request":request.json,"worker_frame":raw])}
}
package struct MLXClosedEvidence: Sendable {
    package let session:MLXSessionIdentity, shutdownACK:JSONValue, finalRoots:JSONValue
    package let exitCode:Int, reaped:Bool, stdoutJoined:Bool, stderrJoined:Bool, stdinClosed:Bool, permitJoined:Bool
    package init(session:MLXSessionIdentity,shutdownACK:JSONValue,finalRoots:JSONValue,exitCode:Int,reaped:Bool,stdoutJoined:Bool,stderrJoined:Bool,stdinClosed:Bool,permitJoined:Bool) {
        self.session=session;self.shutdownACK=shutdownACK;self.finalRoots=finalRoots;self.exitCode=exitCode;self.reaped=reaped;self.stdoutJoined=stdoutJoined;self.stderrJoined=stderrJoined;self.stdinClosed=stdinClosed;self.permitJoined=permitJoined
    }
    var complete:Bool {exitCode == 0 && reaped && stdoutJoined && stderrJoined && stdinClosed && permitJoined}
    package var json:JSONValue {.object(["identity":session.json,"shutdown_ack":shutdownACK,"final_roots":finalRoots,"actual_exit":.integer(exitCode),"reaped":.bool(reaped),"stdout_joined":.bool(stdoutJoined),"stderr_joined":.bool(stderrJoined),"stdin_closed":.bool(stdinClosed),"permit_joined":.bool(permitJoined)])}
}

struct ProducerContext:Sendable {
    let caller:Caller;let kernelCaller:Caller;let scope:ScopeHandle;let write:String;let read:ReadHandle;let names:[Witness]
}
struct OrdinaryContext:Sendable {
    let caller:Caller;let exitCaller:Caller;let scope:ScopeHandle;let names:[Witness];let site:String
}
struct ScopeContext:Sendable {let handle:ScopeHandle;let caller:Caller;let names:[Witness];let frame:ActiveFrame}
struct PrefillArguments:Sendable {let operation:String;let tokens:[UInt64]}

struct MLXCommandRecord:Sendable {
    let request:MLXEvalRequest
    var receipt:MLXHostReceipt?,phase="issued",failure:JSONValue = .null
    var json:JSONValue {.object(["request":request.json,"phase":.string(phase),"receipt":receipt?.json ?? .null,"failure":failure])}
}
struct BackendReturnRecord:Sendable {
    let id:String,owner:String,epoch:String,scope:String,poolGroup:String
    var phase="unoccupied",command:String?,receipt:MLXHostReceipt?,ack:MLXReturnACK?,failure:JSONValue = .null
    var json:JSONValue {.object(["id":.string(id),"owner":.string(owner),"source_epoch":.string(epoch),"scope":.string(scope),"pool_group":.string(poolGroup),"phase":.string(phase),"command":command.map(JSONValue.string) ?? .null,"receipt":receipt?.json ?? .null,"ack":ack?.json ?? .null,"failure":failure])}
}
