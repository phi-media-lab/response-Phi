import Foundation
import Dispatch
import Synchronization
import CryptoKit
import Darwin

package enum ExternalDemandPolicy: String, Sendable { case unchanged; case raiseMain = "raise-main" }

/// One immutable external B8 request. This is not a queue or a cancellation API.
package struct ExternalDemandRequest: Sendable {
    package let session: String, requestID: String, port: String, operation: String
    package let tokens: [UInt64], raw: JSONValue, path: String, bytes: Int, sha256: String
    package static let byteLimit = 65_536
    package init(data: Data, path: String, expectedSession: String) throws {
        try runtimeRequire(data.count <= Self.byteLimit, "external request exceeds 64 KiB")
        let value = try ExactJSON.decode(data)
        _ = try value.members(exactly: ["schema", "session", "request_id", "port", "operation", "tokens"])
        try runtimeRequire(try value.field("schema").text() == "ebd1-request-v1", "external request schema")
        session = try value.field("session").text()
        requestID = try value.field("request_id").text(); port = try value.field("port").text()
        operation = try value.field("operation").text()
        try runtimeRequire(session == expectedSession && UUID(uuidString: session) != nil,
                           "external request session")
        try runtimeRequire(requestID == "B" && port == "B" && operation == "Prefill8", "fixed B request identity")
        tokens = try value.field("tokens").elements().map { try unsignedToken($0.integerToken()) }
        try runtimeRequire(tokens.count == 8 && tokens.allSatisfy { $0 < 151_936 }, "external B8 tokens")
        raw = value; self.path = path; bytes = data.count
        sha256 = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
    package var json: JSONValue { .object(["request": raw, "file": .object([
        "path": .string(path), "bytes": .integer(bytes), "sha256": .string(sha256)])]) }
}

package struct ExternalDemandFailure: Error, Sendable, CustomStringConvertible {
    package let reason: String
    package var description: String { "external demand sealed: " + reason }
}

/// Native requester and one payload/result exchange. No operation discharges a
/// source frame, Scope or BackendReturn. Ledger owns the sole VM continuation.
package final class ExternalDemandControl: Sendable {
    private struct Stamp: Sendable {
        let ns: UInt64, current: Int?, base: Int?
        static func capture() -> Self {
            let ns = DispatchTime.now().uptimeNanoseconds
            return withUnsafeCurrentTask { task in
                Self(ns: ns, current: task.map { Int($0.priority.rawValue) },
                     base: task.map { Int($0.basePriority.rawValue) })
            }
        }
    }
    private struct Edge: Sendable {
        let kernel: Caller, cleanup: Caller, exit: Caller, site: String, publication: String
        let successful: Bool
        var json: JSONValue { .object(["kernel": kernel.json, "cleanup": cleanup.json,
            "exit": exit.json, "site": .string(site), "publication": .string(publication),
            "successful": .bool(successful)]) }
    }
    private struct Target: Sendable {
        let worker: String, scope: ScopeHandle, main: Caller, producerSite: String, backendReturn: String
        var edge: Edge?, ownJoin: Caller?, frontier: Caller?, registeredMain: Caller?
        var returnRequest: MLXReturnRequest?
        var frontierSeen = false, released = false
        var eligibility: String?
        var json: JSONValue { .object(["worker_id": .string(worker),
            "scope": RuntimeValue.scope(scope).diagnostic, "main_at_bind": main.json,
            "producer_site": .string(producerSite), "backend_return": .string(backendReturn),
            "producer_edge": edge?.json ?? .null, "ownjoin_caller": ownJoin?.json ?? .null,
            "frontier_caller": frontier?.json ?? .null, "registered_main_caller": registeredMain?.json ?? .null,
            "return_request": returnRequest?.json ?? .null, "frontier_seen": .bool(frontierSeen),
            "owner_released": .bool(released), "eligibility": eligibility.map(JSONValue.string) ?? .null]) }
    }
    private struct Row: Sendable {
        let event: String, stamp: Stamp, queued: Bool, consumed: Bool, frontier: Bool, released: Bool
        let reason: String?, caller: Caller?, apiCalled: Bool?, token: UInt64?
        var json: JSONValue { .object(["event": .string(event), "capture_ns": .string(String(stamp.ns)),
            "current_priority": stamp.current.map(JSONValue.integer) ?? .null,
            "base_priority": stamp.base.map(JSONValue.integer) ?? .null,
            "priority_null_reason": stamp.current == nil ? .string("no-current-swift-task") : .null,
            "queued": .bool(queued), "consumed": .bool(consumed), "frontier_seen": .bool(frontier),
            "owner_released": .bool(released), "reason": reason.map(JSONValue.string) ?? .null,
            "caller": caller?.json ?? .null, "api_called": apiCalled.map(JSONValue.bool) ?? .null,
            "token": token.map(JSONValue.unsigned) ?? .null]) }
    }
    private typealias PayloadResult = Result<[UInt64], any Error>
    private typealias ReplyResult = Result<UInt64, ExternalDemandFailure>
    private struct State {
        var prepared = false, started = false, opportunity = false
        var queued = false, consumed = false, receiveClaimed = false
        var bodyDone = false, joined = false, finishStarted = false, closed = false
        var task: Task<Void, Never>?
        var gateWaiter: CheckedContinuation<Bool, Never>?
        var payloadWaiter: CheckedContinuation<PayloadResult, Never>?
        var resultWaiter: CheckedContinuation<ReplyResult, Never>?
        var doneWaiters: [CheckedContinuation<Void, Never>] = []
        var closeWaiters: [CheckedContinuation<Void, Never>] = []
        var seal: ExternalDemandFailure?, target: Target?
        var receiveCaller: Caller?, receiveSite: String?, receiveOwner: String?
        var bRequest: MLXEvalRequest?, bReceipt: MLXHostReceipt?
        var reply: UInt64?, replyCaller: Caller?, replyPublication: String?
        var delivered: ReplyResult?
        var rows: [Row] = [], attempted = 0, dropped = 0, raises = 0
        var issues: [String] = []
        var serviceRuntime:RuntimeExecutionSeal?
    }
    private let state = Mutex(State())
    package let request: ExternalDemandRequest, policy: ExternalDemandPolicy
    let serviceResponsePort:ServiceResponsePort?
    private let serviceControlID:String?
    private let origin = DispatchTime.now().uptimeNanoseconds
    private let capacity = 128
    package init(request: ExternalDemandRequest, policy: ExternalDemandPolicy) {
        self.request = request; self.policy = policy;serviceResponsePort=nil;serviceControlID=nil
    }
    init(request:ExternalDemandRequest,response:ServiceResponsePort,controlID:String){self.request=request;policy = .unchanged;serviceResponsePort=response;serviceControlID=controlID}
    package var requesterWasCreated:Bool {state.withLock{$0.prepared}}
    func bindServiceRuntime(_ root:RuntimeExecutionRoot,ledger:String)throws {
        guard let port=serviceResponsePort else{return}
        try state.withLock{s in
            try require(&s,s.serviceRuntime == nil && s.seal == nil && port.pending,"service runtime binding after startup abort/duplicate")
            guard let serviceControlID else {throw RuntimeInvariant("service control seal absent")}
            try port.bindRuntime(controlID:serviceControlID,root:root,ledger:ledger)
            s.serviceRuntime=root.seal;record(&s,"service.runtime-bound",reason:root.id)
        }
    }
    func admitServiceRuntime(_ root:RuntimeExecutionRoot)throws {
        guard let port=serviceResponsePort else {try root.admitStartup();return}
        try state.withLock{s in
            guard let serviceControlID else {throw RuntimeInvariant("service control identity missing")}
            try require(&s,s.seal == nil && s.serviceRuntime === root.seal,"service abort before runtime admission")
            try port.admitRuntime(controlID:serviceControlID,root:root);record(&s,"service.runtime-admitted",reason:root.id)
        }
    }
    func sealStartupRequester(invocation:ServiceInvocation)throws {
        guard let port=serviceResponsePort else {throw RuntimeInvariant("startup requester is not service-bound")}
        let actions=try state.withLock{s->(CheckedContinuation<Bool,Never>?,CheckedContinuation<PayloadResult,Never>?,ExternalDemandFailure) in
            try require(&s,port.invocation === invocation && s.serviceRuntime == nil && s.target == nil && !s.opportunity &&
                !s.queued && !s.consumed && s.reply == nil && s.prepared && port.noSuccessfulReservation,"startup requester abort after runtime/response creation")
            let failure=s.seal ?? ExternalDemandFailure(reason:"service-startup-before-runtime")
            s.seal=failure;record(&s,"service.startup-requester-sealed",reason:failure.reason)
            let values=(s.gateWaiter,s.payloadWaiter,failure);s.gateWaiter=nil;s.payloadWaiter=nil;return values
        }
        // No child runtime exists and no request can have been admitted. This
        // terminates the original requester, without deciding the parent cell.
        actions.0?.resume(returning:false);actions.1?.resume(returning:.failure(actions.2))
    }
    private func record(_ s: inout State, _ event: String, reason: String? = nil,
                        caller: Caller? = nil, apiCalled: Bool? = nil, token: UInt64? = nil) {
        let row = Row(event: event, stamp: .capture(), queued: s.queued, consumed: s.consumed,
            frontier: s.target?.frontierSeen ?? false, released: s.target?.released ?? false,
            reason: reason, caller: caller, apiCalled: apiCalled, token: token)
        s.attempted += 1
        if s.rows.count < capacity { s.rows.append(row) } else { s.dropped += 1 }
    }
    private func require(_ s: inout State, _ condition: Bool, _ reason: String) throws {
        guard condition else {
            if s.issues.count < 32 { s.issues.append(reason) }
            record(&s, "control.invalid", reason: reason)
            throw RuntimeInvariant("external demand: " + reason)
        }
    }
    /// Context/HostEvidence already retain this object before this creates work.
    /// The mutex prevents the requester touching any cell before its saved handle.
    package func prepare() {
        state.withLock { s in
            guard !s.prepared else {
                if s.issues.count < 32 { s.issues.append("duplicate prepare") }
                record(&s, "control.invalid", reason: "duplicate prepare"); return
            }
            guard s.seal == nil else { record(&s, "requester.prepare.ignored", reason: "sealed"); return }
            s.prepared = true; s.rows.reserveCapacity(capacity)
            s.task = Task.detached(priority: .high) { await self.runRequester() }
            record(&s, "requester.created", reason: "original-high-task-handle-installed")
        }
    }
    private func awaitOpportunity() async -> Bool {
        await withCheckedContinuation { continuation in
            let ready = state.withLock { s -> Bool? in
                s.started = true; record(&s, "requester.started")
                if s.seal != nil { record(&s, "requester.trigger.cached", reason: "abort"); return false }
                if s.opportunity { record(&s, "requester.trigger.cached", reason: "producer-edge"); return true }
                precondition(s.gateWaiter == nil)
                s.gateWaiter = continuation; record(&s, "requester.trigger.registered"); return nil
            }
            if let ready { continuation.resume(returning: ready) }
        }
    }
    private func enqueue() throws {
        let waiter = try state.withLock { s -> CheckedContinuation<PayloadResult, Never>? in
            if let failure = s.seal { throw failure }
            try require(&s, s.prepared && s.started && s.task != nil && s.opportunity && !s.queued,
                        "one actual original requester enqueue")
            try require(&s, s.target?.edge != nil, "enqueue needs actual A producer edge")
            s.queued = true
            record(&s, "request.enqueued", reason: s.target!.frontierSeen ? "late-after-policy-check" : "before-policy-check")
            if let waiter = s.payloadWaiter {
                s.payloadWaiter = nil; s.consumed = true
                record(&s, "request.consumed", reason: "registered-receive", caller: s.receiveCaller)
                return waiter
            }
            return nil
        }
        waiter?.resume(returning: .success(request.tokens))
    }
    private func awaitReply() async -> ReplyResult {
        await withCheckedContinuation { continuation in
            let value = state.withLock { s -> ReplyResult? in
                if let token = s.reply { record(&s, "requester.result.cached", token: token); return .success(token) }
                if let failure = s.seal { record(&s, "requester.result.cached", reason: "abort"); return .failure(failure) }
                precondition(s.resultWaiter == nil)
                s.resultWaiter = continuation; record(&s, "requester.result.registered"); return nil
            }
            if let value { continuation.resume(returning: value) }
        }
    }
    private func runRequester() async {
        let result: ReplyResult
        var serviceEvidence:ChildResponseEvidence?
        if await awaitOpportunity() {
            do {
                try enqueue()
                if let port=serviceResponsePort {
                    switch await port.awaitRequester() {
                    case .success(let evidence):serviceEvidence=evidence;result = .success(evidence.token)
                    case .failure(let failure):result = .failure(ExternalDemandFailure(reason:failure.code))
                    }
                } else {result = await awaitReply()}
            }
            catch {
                let failure = (error as? ExternalDemandFailure) ?? ExternalDemandFailure(reason: String(describing: error))
                abort(reason: failure.reason); result = .failure(failure)
            }
        } else {
            result = .failure(state.withLock { $0.seal ?? ExternalDemandFailure(reason: "trigger closed") })
        }
        state.withLock { s in
            s.delivered = result
            switch result {
            case .success(let token): record(&s, "requester.result.received", token: token)
            case .failure(let failure): record(&s, "requester.failure.received", reason: failure.reason)
            }
        }
        // Delivered is produced only by this original requester after its
        // actual receive. It is not inferred from the child's publication.
        if let evidence=serviceEvidence,let port=serviceResponsePort {
            do {try port.delivered(evidence)}
            catch {state.withLock{s in s.issues.append("service delivery invariant: "+String(describing:error));record(&s,"control.invalid",reason:String(describing:error))}}
        }
        let waiters = state.withLock { s -> [CheckedContinuation<Void, Never>] in
            s.bodyDone = true; record(&s, "requester.body_done")
            let found = s.doneWaiters; s.doneWaiters.removeAll(); return found
        }
        for waiter in waiters { waiter.resume() }
    }
    func bindA(worker: String, scope: ScopeHandle, main: Caller, producerSite: String, backendReturn: String) throws {
        try state.withLock { s in
            try require(&s, s.target == nil && s.seal == nil, "unique unsealed A binding")
            try require(&s, scope.owner != nil && scope.epoch != nil && main.scopes.last == scope.id &&
                !worker.isEmpty && !producerSite.isEmpty && !backendReturn.isEmpty, "actual acquired A identity")
            s.target = Target(worker: worker, scope: scope, main: main, producerSite: producerSite, backendReturn: backendReturn)
            record(&s, "target.bound", caller: main)
        }
    }
    @discardableResult func producerEdge(worker: String, scope: ScopeHandle, kernel: Caller,
        cleanup: Caller, exit: Caller, site: String, publication: String, successful: Bool = true) throws -> Bool {
        let result = try state.withLock { s -> (Bool, CheckedContinuation<Bool, Never>?) in
            guard let t = s.target, t.worker == worker, t.scope == scope else { return (false, nil) }
            if s.seal != nil { record(&s, "producer.edge.ignored", reason: "sealed"); return (false, nil) }
            try require(&s, t.edge == nil && site == t.producerSite && kernel == exit && kernel.scopes.isEmpty &&
                cleanup.scopes == [scope.id] && kernel.job == cleanup.job && kernel.task == cleanup.task &&
                !publication.isEmpty, "one original A producer edge")
            s.target!.edge = Edge(kernel: kernel, cleanup: cleanup, exit: exit, site: site,
                                 publication: publication, successful: successful)
            // A normal source-failure publication still has an original Task
            // and may own an adopted receipt. Preserve its join/return identity;
            // only a successful source edge offers ingress to the requester.
            guard successful else {
                record(&s, "producer.edge.failed", caller: exit)
                return (false, nil)
            }
            s.opportunity = true; record(&s, "producer.edge", caller: exit)
            let waiter = s.gateWaiter; s.gateWaiter = nil
            return (true, waiter)
        }
        result.1?.resume(returning: true)
        return result.0
    }
    func ownJoinReturned(worker: String, scope: ScopeHandle, main: Caller, producer: Caller, publication: String) throws {
        try state.withLock { s in
            guard let t = s.target, t.worker == worker, t.scope == scope else { return }
            guard let edge = t.edge else { try require(&s, false, "ownjoin before A edge"); return }
            try require(&s, t.ownJoin == nil && producer == edge.exit && publication == edge.publication &&
                main == t.main, "actual A publication/Task ownjoin")
            s.target!.ownJoin = main; record(&s, "ownjoin.return", caller: main)
        }
    }
    /// Called synchronously by the original source main at its one return
    /// frontier. A later enqueue cannot invoke this API or retain the main handle.
    func applyFrontier(worker: String, scope: ScopeHandle, main: Caller, registeredMain: Caller?,
        mainRole: String?, task: Task<RuntimeTaskExit, Error>?, request returnRequest: MLXReturnRequest) throws {
        let eligible = try state.withLock { s -> Bool in
            guard let t = s.target, t.worker == worker, t.scope == scope else { return false }
            guard let edge = t.edge else { try require(&s, false, "frontier before original producer edge"); return false }
            let q = returnRequest.receipt.request
            try require(&s, !t.frontierSeen && !t.released && t.ownJoin == main && main == t.main && task != nil &&
                mainRole == "main" && registeredMain?.job == main.job && registeredMain?.task == main.task &&
                registeredMain?.scopes.isEmpty == true && main.scopes.last == scope.id &&
                q.session.session == request.session && q.session.worker == worker && q.operation == "Prefill32" &&
                q.job == edge.kernel.job && q.task == edge.kernel.task && q.invocation == edge.kernel.invocation &&
                q.region == edge.kernel.region && q.site == edge.site && q.scope == scope.id &&
                q.owner == scope.owner && q.sourceEpoch == scope.epoch && q.returnID == t.backendReturn &&
                returnRequest.returnID == t.backendReturn && returnRequest.scope == scope.id,
                "frontier original main/receipt/Owner/epoch/backend-return")
            s.target!.frontierSeen = true; s.target!.frontier = main
            s.target!.registeredMain = registeredMain; s.target!.returnRequest = returnRequest
            let eligible = s.queued && !s.consumed && s.seal == nil
            s.target!.eligibility = eligible ? "queued-at-frontier" : s.seal == nil ? "no-blocker-at-policy-check" : "sealed"
            record(&s, "policy.frontier", reason: s.target!.eligibility, caller: main)
            if eligible { record(&s, "policy.requested", reason: policy.rawValue, caller: main, apiCalled: false) }
            return eligible
        }
        guard eligible else { return }
        var called = false
        if policy == .raiseMain {
            if #available(macOS 26.0, *) { task!.escalatePriority(to: .high); called = true }
            else { throw RuntimeInvariant("external demand priority API unavailable") }
        }
        state.withLock { s in
            if called { s.raises += 1 }
            record(&s, "policy.returned", reason: policy.rawValue, caller: main, apiCalled: called)
        }
    }
    func ownerReleased(worker: String, scope: ScopeHandle) throws {
        try state.withLock { s in
            guard let t = s.target, t.worker == worker, t.scope == scope else { return }
            try require(&s, t.frontierSeen && !t.released, "unique A release after frontier")
            s.target!.released = true; record(&s, "owner.release", caller: t.main)
        }
    }
    /// Only the payload is parked here. Ledger retains/claims/restores the sole
    /// full Machine holder and validates the registered idle Owner separately.
    func receive(port: String, owner: OwnerHandle, caller: Caller, site: String) async throws -> [UInt64] {
        let result: PayloadResult = await withCheckedContinuation { continuation in
            let immediate = state.withLock { s -> PayloadResult? in
                do {
                    if let failure = s.seal { return .failure(failure) }
                    guard let t = s.target else { try require(&s, false, "receive before A binding"); return nil }
                    try require(&s, port == request.port && owner.id == t.scope.owner &&
                        caller.job == t.main.job && caller.task == t.main.task && t.released &&
                        !s.receiveClaimed && !s.consumed && s.payloadWaiter == nil && !site.isEmpty,
                        "one receive of B on original main and released Owner")
                    s.receiveClaimed = true; s.receiveCaller = caller; s.receiveSite = site; s.receiveOwner = owner.id
                    record(&s, "receive.called", caller: caller)
                    if s.queued {
                        s.consumed = true; record(&s, "request.consumed", reason: "cached-enqueue", caller: caller)
                        return .success(request.tokens)
                    }
                    s.payloadWaiter = continuation; record(&s, "receive.registered", caller: caller); return nil
                } catch {
                    return .failure(error)
                }
            }
            if let immediate { continuation.resume(returning: immediate) }
        }
        let tokens = try result.get()
        state.withLock { s in record(&s, "receive.return", caller: caller) }
        return tokens
    }
    func observeBRequest(_ q: MLXEvalRequest) throws {
        try state.withLock { s in
            guard let t = s.target else { try require(&s, false, "B request without A"); return }
            try require(&s, s.seal == nil && s.queued && s.consumed && s.bRequest == nil && t.released &&
                q.operation == request.operation && q.tokens == request.tokens && q.session.session == request.session &&
                q.session.worker == t.worker && q.owner == t.scope.owner && q.sourceEpoch != t.scope.epoch &&
                q.scope != t.scope.id && q.ownerOrdinal == (t.returnRequest?.receipt.request.ownerOrdinal ?? -1) + 1,
                "B original request consumes exact external payload on fresh epoch")
            s.bRequest = q; record(&s, "b.request.bound")
        }
    }
    func observeBReceipt(_ receipt: MLXHostReceipt) throws {
        let token = try receipt.token
        try state.withLock { s in
            try require(&s, s.seal == nil && s.bRequest == receipt.request && s.bReceipt == nil,
                        "one adopted B receipt bound to actual request")
            s.bReceipt = receipt; record(&s, "b.receipt.bound", token: token)
        }
    }
    /// Caller supplies the token from the source's bound response intent,
    /// after the original main join and all source safety guards at the trusted
    /// host delivery boundary. No diagnostic JSON is interpreted; this does
    /// not discharge the independent backend/IO close obligations.
    func reply(token: UInt64, caller: Caller, publication: String) throws {
        try runtimeRequire(serviceResponsePort == nil,"service response requires opaque actual commit evidence")
        let waiter = try state.withLock { s -> CheckedContinuation<ReplyResult, Never>? in
            guard let t = s.target, let receipt = s.bReceipt else { try require(&s, false, "reply without adopted B"); return nil }
            try require(&s, s.seal == nil && s.consumed && s.reply == nil && !s.bodyDone &&
                caller.job == t.main.job && caller.task == t.main.task && !publication.isEmpty &&
                token == (try receipt.token), "one final source B reply equals adopted B token")
            s.reply = token; s.replyCaller = caller; s.replyPublication = publication
            record(&s, "reply.published", caller: caller, token: token)
            let found = s.resultWaiter; s.resultWaiter = nil; return found
        }
        waiter?.resume(returning: .success(token))
    }
    func commitServiceResponse(_ evidence:ChildResponseEvidence)throws {
        guard let port=serviceResponsePort else {throw RuntimeInvariant("service commit on legacy demand")}
        try state.withLock{s in
            guard let target=s.target,let receipt=s.bReceipt else {throw RuntimeInvariant("service commit lacks original adopted B")}
            try require(&s,s.seal == nil && s.consumed && s.reply == nil && !s.bodyDone &&
                evidence.invocation === port.invocation && evidence.invocation.session == request.session &&
                evidence.invocation.requestSHA256 == request.sha256 && evidence.receipt == receipt &&
                evidence.publication.job == target.main.job && evidence.publication.task == target.main.task && evidence.token == (try receipt.token),"service commit original identities")
            // Shared port is the sole winner. These legacy fields are diagnostic
            // mirrors written only after that exact same commit succeeds.
            try port.reserve(evidence)
            s.reply=evidence.token;s.replyCaller=target.main;s.replyPublication=evidence.publication.id
            record(&s,"reply.published",caller:target.main,token:evidence.token)
        }
    }
    /// Synchronous failure seal. A committed request is not withdrawn; all
    /// evidence remains, and unresolved source/backend authority stays Unknown.
    package func abort(reason: String) {
        let action = state.withLock { s -> (ExternalDemandFailure, CheckedContinuation<Bool, Never>?, CheckedContinuation<PayloadResult, Never>?, CheckedContinuation<ReplyResult, Never>?)? in
            guard s.seal == nil else { return nil }
            if let port=serviceResponsePort {
                let code:String
                switch reason {case "original-source-failure":code="ServiceChildSourceFailure"
                case "source-response-contract:MissingResponseIntent":code="ServiceResponseContractFailure"
                default:code="ServiceAborted"}
                _=port.abort(.init(code:code,original:.object(["reason":.string(reason),"request":request.json])))
            }
            let failure = ExternalDemandFailure(reason: reason); s.seal = failure
            record(&s, "control.sealed", reason: reason)
            let found = (failure, s.gateWaiter, s.payloadWaiter, s.resultWaiter)
            s.gateWaiter = nil; s.payloadWaiter = nil; s.resultWaiter = nil
            return found
        }
        if let action {
            action.1?.resume(returning: false)
            action.2?.resume(returning: .failure(action.0))
            action.3?.resume(returning: .failure(action.0))
        }
    }
    private func awaitBodyDone() async {
        await withCheckedContinuation { continuation in
            let done = state.withLock { s -> Bool in
                if s.bodyDone || !s.prepared { return true }
                s.doneWaiters.append(continuation); return false
            }
            if done { continuation.resume() }
        }
    }
    package func validateCompletedReply() throws {
        try state.withLock { s in
            try require(&s, s.seal == nil && s.queued && s.consumed && s.reply != nil && s.bReceipt != nil,
                        "completed host requires final source reply")
        }
    }
    /// First wait on our own notification. Only after body_done do we await the
    /// original requester handle; neither main nor A handles are ever awaited.
    package func finish() async {
        let first = state.withLock { s -> Bool in
            if s.finishStarted { return false }
            s.finishStarted = true; record(&s, "control.join.begin"); return true
        }
        if !first {
            await withCheckedContinuation { continuation in
                let closed = state.withLock { s -> Bool in
                    if s.closed { return true }; s.closeWaiters.append(continuation); return false
                }
                if closed { continuation.resume() }
            }
            return
        }
        await awaitBodyDone()
        let task = state.withLock { $0.task }
        if let task {
            await task.value
            state.withLock { s in s.joined = true; s.task = nil; record(&s, "requester.joined") }
        }
        let waiters = state.withLock { s -> [CheckedContinuation<Void, Never>] in
            s.closed = true; record(&s, "control.closed")
            let found = s.closeWaiters; s.closeWaiters.removeAll(); return found
        }
        for waiter in waiters { waiter.resume() }
    }
    /// unresolved_successful_reply records the absence of a successful source reply.
    /// It can remain true after abort delivery and actual requester join; live
    /// native continuations and Task handles are reported separately.
    package func snapshot(complete: Bool) -> JSONValue {
        state.withLock { s in
            let delivered: JSONValue
            switch s.delivered {
            case .success(let token)?: delivered = .object(["status": .string("success"), "token": .unsigned(token)])
            case .failure(let failure)?: delivered = .object(["status": .string("failure"), "reason": .string(failure.reason)])
            case nil: delivered = .null
            }
            return .object(["schema": .string("ebd1-external-demand-v1"), "session": .string(request.session),
                "service_binding": serviceResponsePort.map { .object(["service_id":.string($0.serviceID),"control_id":serviceControlID.map(JSONValue.string) ?? .null,"runtime_root_id":s.serviceRuntime.map{.string($0.id)} ?? .null]) } ?? .null,
                "process_pid": .integer(Int(getpid())), "policy": .string(policy.rawValue), "request": request.json,
                "clock": .object(["domain": .string("swift-dispatch-uptime-ns"), "origin_ns": .string(String(origin))]),
                "capacity": .integer(capacity), "attempted": .integer(s.attempted), "retained": .integer(s.rows.count),
                "dropped": .integer(s.dropped), "collection_complete": .bool(complete),
                "measurement_valid": .bool(complete && s.dropped == 0 && s.issues.isEmpty),
                "prepared": .bool(s.prepared), "started": .bool(s.started), "opportunity": .bool(s.opportunity),
                "queued": .bool(s.queued), "consumed": .bool(s.consumed), "receive_claimed": .bool(s.receiveClaimed),
                "body_done": .bool(s.bodyDone), "actual_joined": .bool(s.joined), "closed": .bool(s.closed),
                "retained_task": .bool(s.task != nil), "retained_payload": .bool(s.queued && !s.consumed),
                "unresolved_successful_reply": .bool(s.queued && s.reply == nil), "seal": s.seal.map { .string($0.reason) } ?? .null,
                "retained_waiters": .object(["trigger": .bool(s.gateWaiter != nil), "payload": .bool(s.payloadWaiter != nil),
                    "result": .bool(s.resultWaiter != nil), "done": .integer(s.doneWaiters.count), "close": .integer(s.closeWaiters.count)]),
                "target": s.target?.json ?? .null, "receive_caller": s.receiveCaller?.json ?? .null,
                "receive_site": s.receiveSite.map(JSONValue.string) ?? .null, "receive_owner": s.receiveOwner.map(JSONValue.string) ?? .null,
                "b_request": s.bRequest?.json ?? .null, "b_receipt": s.bReceipt?.json ?? .null,
                "reply": s.reply.map(JSONValue.unsigned) ?? .null, "reply_caller": s.replyCaller?.json ?? .null,
                "reply_publication": s.replyPublication.map(JSONValue.string) ?? .null, "delivered": delivered,
                "raise_calls": .integer(s.raises), "issues": .array(s.issues.map(JSONValue.string)),
                "events": .array(s.rows.enumerated().map { index, row in
                    var fields = try! row.json.members(); fields["append_seq"] = .integer(index + 1)
                    return .object(fields)
                })])
        }
    }
    func requesterJoinObservation()throws->RequesterJoinObservation {
        guard let port=serviceResponsePort else {throw RuntimeInvariant("legacy requester has no service seal")}
        let joined=state.withLock{$0.prepared && $0.bodyDone && $0.joined && $0.closed && $0.task == nil}
        try runtimeRequire(joined,"service requester observation before original Task.value")
        return RequesterJoinObservation(control:self,serviceID:port.serviceID,actualJoined:joined,evidence:snapshot(complete:true))
    }
}

package func makeServiceDemandControl(invocation:ServiceInvocation,response:ServiceResponsePort,archivedRequestPath:String)throws->ExternalDemandControl {
    try runtimeRequire(response.invocation === invocation && archivedRequestPath.hasPrefix("/"),"service demand invocation/path")
    let request=try ExternalDemandRequest(data:invocation.requestBytes,path:archivedRequestPath,expectedSession:invocation.session)
    let controlID=UUID().uuidString;try response.claimDemandControl(controlID)
    return ExternalDemandControl(request:request,response:response,controlID:controlID)
}
package func finishServiceRequester(_ control:ExternalDemandControl)async throws->RequesterJoinObservation {
    await control.finish();return try control.requesterJoinObservation()
}
package func finishAbortedServiceRequesterBeforeRuntime(_ control:ExternalDemandControl,invocation:ServiceInvocation)async throws->RequesterJoinObservation {
    try control.sealStartupRequester(invocation:invocation)
    await control.finish();return try control.requesterJoinObservation()
}
