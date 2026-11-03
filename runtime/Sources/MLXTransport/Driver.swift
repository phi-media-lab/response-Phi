import Foundation
import Dispatch
import Synchronization
import Darwin
import RuntimeCore

/// Trusted host context. Legacy construction activates immediately. B prepares
/// an inert context, retains it in its host root, then activates after commit.
/// Only start* spawns a worker; fixed metadata reads do not start any tasks.
package final class TransportContext: Sendable {
    private struct State {
        var sessions: [TransportSession] = []
        var realStarted = false, finishing = false, finished = false
        var activationClaimed = false, activationSettled = false, activated = false, returnServicePrepared = false
        var admissionClosed = false, startupCloseClaimed = false
        var startupObservations: [JSONValue] = []
        var quarantine: MLXBackendFailure?
        var returnServiceAbort: ReturnServiceControl?, returnServiceSealed = false
    }
    private let state = Mutex(State())
    private let finished = TransportPromise<Bool>()
    private let activationReturned = TransportPromise<Bool>()
    private let inputs: TransportInputs
    private let trace: TransportTrace
    private let router: TransportPermitRouter
    private let directory: URL
    private static let root = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    package let serviceObservation: BRSServiceTrace
    package let returnConversion: HRCReturnConversionMode
    package let returnService: ReturnServiceControl
    package let externalDemand: ExternalDemandControl?
    fileprivate let returnConversionObservation: HRCConversionObservation
    package let session: String
    package init(inputsPath: String, session: String, outputDirectory: URL,
                 returnConversion: HRCReturnConversionMode, returnServicePolicy: ReturnServicePolicy,
                 competitionTasks: Int, externalRequest: ExternalDemandRequest? = nil,
                 demandPolicy: ExternalDemandPolicy? = nil,
                 deferActivation: Bool = false, serviceDemand: ExternalDemandControl? = nil) throws {
        guard #available(macOS 26.0, *) else { throw TransportFault("unsupported_os", "RSP1 requires macOS 26 Task escalation API") }
        try require(returnConversion == .original && [0, 8].contains(competitionTasks), "RSP1 fixed conversion/load configuration")
        try require(!deferActivation || (competitionTasks == 0 && returnServicePolicy == .unchanged),
                    "staged B context uses unchanged K0 control")
        try require(!session.isEmpty && outputDirectory.isFileURL && outputDirectory.path.hasPrefix("/"), "transport host configuration")
        try require(getpgrp() == getpid(), "Swift must be the wrapper-created group leader")
        try require((externalRequest == nil) == (demandPolicy == nil), "external demand request/policy pair")
        try require(externalRequest == nil || (externalRequest!.session == session && returnServicePolicy == .unchanged),
                    "EBD1 session and unchanged legacy service policy")
        if let serviceDemand {
            try require(deferActivation && externalRequest == nil && demandPolicy == nil &&
                serviceDemand.request.session == session && serviceDemand.policy == .unchanged &&
                returnServicePolicy == .unchanged && competitionTasks == 0,
                "B prepared original demand, K0 and unchanged policies")
        }
        let inputs = try TransportInputs(inputsPath)
        let trace = TransportTrace(session)
        self.inputs = inputs; self.trace = trace; self.router = TransportPermitRouter(trace)
        self.directory = outputDirectory; self.session = session
        self.serviceObservation = BRSServiceTrace(session: session)
        self.returnConversion = returnConversion
        self.returnConversionObservation = HRCConversionObservation(mode: returnConversion, session: session)
        self.returnService = ReturnServiceControl(session: session, policy: returnServicePolicy, competitionTasks: competitionTasks)
        self.externalDemand = serviceDemand ?? externalRequest.map { ExternalDemandControl(request: $0, policy: demandPolicy!) }
        trace.record("transport.context_created", ["inputs_sha256": .string(inputs.sha256),
            "python": .string(TransportInputs.python), "roots": router.snapshot(), "deferred_activation": .bool(deferActivation)])
        if !deferActivation { try activate() }
    }
    /// The caller retains this object before activation, even if activation
    /// fails after creating a reader. Activation and worker launch are distinct.
    package func activate() throws {
        try state.withLock { s in
            if let failure = s.quarantine { throw failure }
            try require(!s.activationClaimed && !s.admissionClosed && !s.finishing && !s.finished, "context activation once before close")
            s.activationClaimed = true
        }
        do {
            try router.start()
            returnService.prepare()
            state.withLock { $0.returnServicePrepared = true; $0.activated = true; $0.activationSettled = true }
            trace.record("transport.context_activated", ["roots": router.snapshot()])
            activationReturned.resolve(.success(true))
        } catch {
            state.withLock { $0.activationSettled = true }
            activationReturned.resolve(.failure((error as? TransportFault) ?? TransportFault("activation", String(describing: error))))
            throw error
        }
    }
    package var a32: [UInt64] { TransportInputs.a32.map { UInt64($0) } }
    package var bPrefix7: [UInt64] { TransportInputs.b7.map { UInt64($0) } }
    package var inputProvenance: JSONValue { inputs.raw.core }
    package func record(_ name: String, _ fields: [String: JSONValue] = [:]) throws {
        let wire = try WireJSON.fromCore(.object(fields))
        trace.record(name, wire.object!)
    }
    package func events() -> JSONValue { trace.snapshot().core }
    /// Final/failed-prefix archival only. Observation IO cannot replace the
    /// original protocol result or authorize cleanup.
    package func saveServiceObservation(complete: Bool) -> JSONValue {
        let snapshot = serviceObservation.snapshot(complete: complete)
        let url = directory.appendingPathComponent("service-swift.json")
        do {
            try snapshot.encoded().write(to: url, options: .atomic)
            return .object(["file": .string(url.lastPathComponent), "written": .bool(true),
                "observation_complete": .bool(complete), "measurement_valid": snapshot.brsField("measurement_valid"),
                "worker_observation": .string("audit BRS1_SERVICE_TRACE in each original worker stderr; missing is unavailable")])
        } catch {
            return .object(["file": .string(url.lastPathComponent), "written": .bool(false),
                "measurement_valid": .bool(false), "error": .string(String(describing: error))])
        }
    }
    /// Full conversion tagging/encoding is delayed until this original host
    /// closure/prefix boundary. Observation failure never replaces protocol exit.
    package func saveReturnConversionObservation(complete: Bool) -> JSONValue {
        let snapshot = returnConversionObservation.snapshot(complete: complete)
        let url = directory.appendingPathComponent("return-conversion.json")
        do {
            try snapshot.encoded().write(to: url, options: .withoutOverwriting)
            return .object(["file": .string(url.lastPathComponent), "written": .bool(true),
                "collection_complete": .bool(complete), "measurement_valid": snapshot.brsField("measurement_valid"),
                "mode": .string(returnConversion.rawValue)])
        } catch {
            return .object(["file": .string(url.lastPathComponent), "written": .bool(false),
                "measurement_valid": .bool(false), "mode": .string(returnConversion.rawValue),
                "error": .string(String(describing: error))])
        }
    }
    /// Retain the actual independent probe before prepare creates its Tasks.
    package func installReturnServiceAbortControl() throws -> ReturnServiceControl {
        try state.withLock { s in
            if let failure = s.quarantine { throw failure }
            try require(!s.returnServiceSealed && s.returnServiceAbort == nil, "one unsealed native abort control")
            let control = ReturnServiceControl(session: session, policy: .unchanged, competitionTasks: 8, role: "abort-control")
            s.returnServiceAbort = control
            return control
        }
    }
    /// Synchronous deadline/entry-failure action only; never joins either group.
    package func abortReturnServiceControls(reason: String) {
        let probe = state.withLock { s in s.returnServiceSealed = true; return s.returnServiceAbort }
        returnService.abortIfParked(reason: reason)
        probe?.abortIfParked(reason: reason)
        externalDemand?.abort(reason: reason)
    }
    package func saveExternalDemandObservation(complete: Bool) -> JSONValue {
        guard let control = externalDemand else { return .null }
        let snapshot = control.snapshot(complete: complete)
        let url = directory.appendingPathComponent("external-demand.json")
        do {
            try snapshot.encoded().write(to: url, options: .withoutOverwriting)
            return .object(["file": .string(url.lastPathComponent), "written": .bool(true),
                "collection_complete": .bool(complete), "measurement_valid": snapshot.brsField("measurement_valid"),
                "policy": .string(control.policy.rawValue)])
        } catch {
            return .object(["file": .string(url.lastPathComponent), "written": .bool(false),
                "measurement_valid": .bool(false), "error": .string(String(describing: error))])
        }
    }
    package func returnServiceAbortSnapshot(complete: Bool) -> JSONValue {
        let probe = state.withLock { $0.returnServiceAbort }
        return probe?.snapshot(complete: complete) ?? .null
    }
    package func saveReturnServiceObservation(complete: Bool) -> JSONValue {
        let snapshot = returnService.snapshot(complete: complete)
        let url = directory.appendingPathComponent("return-service.json")
        do {
            try snapshot.encoded().write(to: url, options: .withoutOverwriting)
            return .object(["file": .string(url.lastPathComponent), "written": .bool(true),
                "collection_complete": .bool(complete), "measurement_valid": snapshot.brsField("measurement_valid"),
                "policy": .string(returnService.policy.rawValue), "competition_tasks": .integer(returnService.competitionTasks)])
        } catch {
            return .object(["file": .string(url.lastPathComponent), "written": .bool(false),
                "measurement_valid": .bool(false), "error": .string(String(describing: error))])
        }
    }
    package func flush() async { await trace.flush() }
    package func snapshot() -> JSONValue {
        let observed = state.withLock { ($0.sessions, $0.realStarted, $0.finishing, $0.finished, $0.quarantine,
            $0.activationClaimed, $0.activated, $0.returnServicePrepared, $0.startupObservations,
            $0.admissionClosed, $0.startupCloseClaimed) }
        var result = WireJSON.object(["session": .string(session), "real_worker_created": .bool(observed.1),
            "finishing": .bool(observed.2), "finished": .bool(observed.3), "control": router.snapshot(),
            "workers": .array(observed.0.map { $0.snapshot() })]).core
        if case .object(var fields) = result {
            fields["activation_claimed"] = .bool(observed.5); fields["activated"] = .bool(observed.6)
            fields["return_service_prepared"] = .bool(observed.7)
            fields["host_resource_population"] = .array(observed.0.map { $0.startupSnapshot().core })
            fields["startup_observations"] = .array(observed.8)
            fields["admission_closed"] = .bool(observed.9); fields["startup_close_claimed"] = .bool(observed.10)
            result = .object(fields)
        }
        if let original = observed.4, case .object(var fields) = result {
            fields["quarantine"] = .object(["fault_id": .string(original.faultID), "code": .string(original.code), "raw": original.raw])
            result = .object(fields)
        }
        return result
    }
    /// Typed trusted-host facts are built from these original retained roots.
    /// Their raw evidence distinguishes a process reap, queue fence and fd close.
    package func hostResourceObservations() throws -> [HostResourceObservation] {
        let observed = state.withLock { ($0.sessions, $0.activationClaimed, $0.returnServicePrepared,
            $0.admissionClosed, $0.activationSettled) }
        var resources = try observed.0.flatMap { try $0.hostResourceObservations() }
        let control = router.snapshot(), returned = returnService.snapshot(complete: false)
        let controlStarted = control["reader_started"].bool == true
        resources.append(try HostResourceObservation(kind: .permitRouter, id: session + "/permit-router",
            created: true, started: controlStarted,
            closed: !controlStarted || control["reader_joined"].bool == true,
            joined: controlStarted ? control["reader_joined"].bool : nil, originalEvidence: control.core))
        let returnClosed = returned.brsField("closed") == .bool(true)
        resources.append(try HostResourceObservation(kind: .returnService, id: session + "/return-service",
            created: true, started: observed.2, closed: returnClosed, joined: nil, originalEvidence: returned))
        let closed = observed.3 && (!observed.1 || observed.4) &&
            resources.allSatisfy { !$0.created || ($0.closed && $0.joined != false) }
        resources.append(try HostResourceObservation(kind: .context, id: session + "/context",
            created: true, started: observed.1, closed: closed, joined: nil, originalEvidence: snapshot()))
        return resources
    }
    /// Synchronous timeout winner callback. Existing sessions linearize at
    /// their same health mutex; after return all reject new admission/commands.
    /// Original receipt/ACK evidence, producer Tasks and KV/IO roots remain.
    package func quarantine(_ failure: MLXBackendFailure) {
        let claim = state.withLock { s -> (MLXBackendFailure, [TransportSession]) in
            if case .none = s.quarantine { s.quarantine = failure }
            return (s.quarantine!, s.sessions)
        }
        let detail = (try? claim.0.raw.encoded()).map { String(decoding: $0, as: UTF8.self) } ?? String(describing: claim.0.raw)
        externalDemand?.abort(reason: "transport-quarantine:" + claim.0.code)
        let fault = TransportFault(claim.0.code, detail, id: claim.0.faultID)
        for worker in claim.1 { worker.fail(fault) }
        trace.record("transport.quarantined", ["fault": fault.json])
    }
    package func fault(_ error: any Error) -> JSONValue {
        if let original = error as? MLXBackendFailure {
            return .object(["fault_id": .string(original.faultID), "code": .string(original.code), "raw": original.raw])
        }
        if let original = error as? TransportFault { return original.json.core }
        return .object(["kind": .string("host-infrastructure"), "message": .string(String(describing: error))])
    }
    fileprivate func backendFailure(_ error: any Error, worker: TransportSession? = nil) -> MLXBackendFailure {
        if let original = error as? MLXBackendFailure { return original }
        let fault = (error as? TransportFault) ?? TransportFault("transport_infrastructure", String(describing: error))
        return MLXBackendFailure(faultID: fault.id, code: fault.code,
            raw: .object(["fault": fault.json.core, "worker_roots": worker?.snapshot().core ?? .null,
                "transport_roots": snapshot()]))
    }
    private func startWorker(cpu: Bool, scenario: String) async throws -> TransportSession {
        let worker: TransportSession = try state.withLock { s in
            if let failure = s.quarantine { throw failure }
            try require(s.activated && !s.admissionClosed && !s.finishing && !s.finished, "worker requires activated context before finalization")
            if !cpu { try require(!s.realStarted && s.sessions.isEmpty, "one real worker per process session"); s.realStarted = true }
            else { try require(!s.realStarted, "CPU stubs cannot share a real worker session") }
            let script = Self.root.appendingPathComponent("backend/" + (cpu ? "stub_worker.py" : "worker.py")).path
            let worker = TransportSession(inputs: inputs, trace: trace, router: router, directory: directory, service: serviceObservation,
                python: TransportInputs.python, script: script, cpu: cpu, scenario: scenario)
            s.sessions.append(worker) // IO/process responsibilities precede spawn.
            return worker
        }
        do { try await worker.start(); try worker.checkHealthy(); return worker }
        catch { throw backendFailure(error, worker: worker) }
    }
    package func startChecked() async throws -> MLXTransportDriver {
        let worker = try await startWorker(cpu: false, scenario: "success")
        return try MLXTransportDriver(context: self, worker: worker, cpu: false)
    }
    package func startReference() async throws -> NativeReferenceDriver {
        let worker = try await startWorker(cpu: false, scenario: "success")
        return try NativeReferenceDriver(context: self, worker: worker)
    }
    /// Only the separately compiled native CPU entry selects a stub scenario.
    /// There is no corresponding source constructor or public source CLI flag.
    package func startCPU(scenario: String) async throws -> MLXTransportDriver {
        let allowed: Set<String> = ["success", "normal", "wrong_identity", "wrong_session", "wrong_seq",
            "duplicate_receipt", "truncated_receipt", "eof_eval", "eof_after_enter", "timeout_eval", "timeout_after_enter",
            "partial_release", "lost_release_ack", "nonzero_after_receipt", "no_permit", "wrong_receipt_request",
            "wrong_release_epoch", "duplicate_release_ack", "nonzero_after_release_ack", "cm2_eval_hold",
            "src1b_added_token", "src1b_missing_token"]
        try require(allowed.contains(scenario), "fixed CPU fixture scenario")
        let worker = try await startWorker(cpu: true, scenario: scenario)
        return try MLXTransportDriver(context: self, worker: worker, cpu: true)
    }
    /// Used after an expected CPU bootstrap failure before a driver existed.
    /// Logical Unknown remains recorded; process disappearance is not an ACK.
    package func closeFailedCPUWorkers() async {
        let workers = state.withLock { $0.sessions }
        for worker in workers { await worker.closeFailedCPUFixture() }
    }
    /// A B startup abort has no EVAL/release authority. Preserve each partial
    /// resource population and actual close/fence, including failed spawn.
    /// These append-only observations do not rewrite the normal finish promise.
    package func closeStartupPrefix() async -> JSONValue {
        // Admission and the population snapshot share startWorker's mutex.
        // An activation that won before this seal must actually return before
        // we classify its reader/control as created or never started.
        let claim = state.withLock { s -> (Bool, Bool) in
            guard !s.startupCloseClaimed && !s.finishing else { return (false, s.activationClaimed) }
            s.admissionClosed = true; s.startupCloseClaimed = true
            return (true, s.activationClaimed)
        }
        guard claim.0 else { return snapshot() }
        if claim.1 {
            do { _ = try await activationReturned.wait() }
            catch {
                let original = fault(error)
                state.withLock { $0.startupObservations.append(.object(["activation_failure": original])) }
            }
        }
        let workers = state.withLock { $0.sessions }
        for worker in workers {
            let observation: JSONValue
            do { observation = try await worker.closeStartupPrefix().core }
            catch { observation = .object(["failure": fault(error), "actual_population": worker.startupSnapshot().core]) }
            state.withLock { $0.startupObservations.append(observation) }
        }
        // Staged contexts use K0, so an unactivated control has zero tasks;
        // finish records that actual empty population rather than faking joins.
        await returnService.finish(reason: "service-startup-abort")
        do { try await router.finish() }
        catch { state.withLock { $0.startupObservations.append(.object(["permit_close_failure": fault(error)])) } }
        let result = snapshot()
        trace.record("transport.startup_abort_observed", ["observation": (try? WireJSON.fromCore(result)) ?? .null])
        return result
    }
    @discardableResult package func finish() async throws -> JSONValue {
        let first = try state.withLock { s -> Bool in
            try require(!s.startupCloseClaimed, "normal finish cannot replace startup-abort observation")
            if s.finishing { return false }; s.finishing = true; s.admissionClosed = true; return true
        }
        if first {
            do {
                let workers = state.withLock { $0.sessions }
                for worker in workers {
                    let roots = worker.snapshot()
                    try require(roots["process_root"].bool == false && roots["stdout_root"].bool == false &&
                        roots["stderr_root"].bool == false && roots["stdin_root"].bool == false,
                        "control close requires actual worker reap/IO fences")
                }
                try await router.finish()
                state.withLock { $0.finished = true }
                finished.resolve(.success(true))
            } catch {
                let original = (error as? TransportFault) ?? TransportFault("control_finish", String(describing: error))
                finished.resolve(.failure(original))
            }
        }
        do { _ = try await finished.wait(); return snapshot() }
        catch { throw backendFailure(error) }
    }
    fileprivate func closedEvidence(worker: TransportSession, identity: MLXSessionIdentity, ack: WireJSON) -> MLXClosedEvidence {
        let roots = worker.snapshot(), control = router.snapshot()
        return MLXClosedEvidence(session: identity, shutdownACK: ack.core, finalRoots: snapshot(),
            exitCode: roots["actual_exit"].integer ?? -1, reaped: roots["process_root"].bool == false,
            stdoutJoined: roots["stdout_root"].bool == false, stderrJoined: roots["stderr_root"].bool == false,
            stdinClosed: roots["stdin_root"].bool == false, permitJoined: control["reader_joined"].bool == true)
    }
}

/// The checked producer calls evaluate on its original Task. This driver
/// creates no replacement producer/observer Task and owns no Machine holder.
package final class MLXTransportDriver: MLXBackendDriver, MLXForkCPUPhaseControl, Sendable {
    package var serviceObservation: BRSServiceTrace { context.serviceObservation }
    package let identity: MLXSessionIdentity
    private let context: TransportContext, worker: TransportSession
    private let cpu: Bool
    fileprivate init(context: TransportContext, worker: TransportSession, cpu: Bool) throws {
        self.context = context; self.worker = worker; self.cpu = cpu; self.identity = try worker.initializedIdentity()
    }
    package func withHealthyAdmission<T>(_ body: () throws -> T) throws -> T {
        do { return try worker.withAdmission(body) }
        catch let error as TransportFault { throw context.backendFailure(error, worker: worker) }
        // A Core closure's original error remains that same Core error.
    }
    package func withHealthyHostAction<T>(_ body: () throws -> T) throws -> T {
        do { return try worker.withHostAction(body) }
        catch let error as TransportFault { throw context.backendFailure(error, worker: worker) }
        // Preserve SourceFailure/RuntimeInvariant from the original Core action.
    }
    package func waitForEvaluationEntered(job: String) async throws -> JSONValue {
        do {
            try require(cpu, "CPU phase control is unavailable to real MLX")
            return try await worker.waitForEvaluationEntered(job: job).core
        } catch { throw context.backendFailure(error, worker: worker) }
    }
    package func continueEvaluation(job: String) async throws -> JSONValue {
        do {
            try require(cpu, "CPU phase control is unavailable to real MLX")
            return try await worker.continueEvaluation(job: job).core
        } catch { throw context.backendFailure(error, worker: worker) }
    }
    package func checkHealthy() throws {
        do { try worker.checkHealthy() } catch { throw context.backendFailure(error, worker: worker) }
    }
    package func evaluate(_ request: MLXEvalRequest) async throws -> MLXHostReceipt {
        do {
            try require(request.session == identity, "checked request belongs to this initialized worker")
            // Range/shape precede JSON conversion as well as command/cache/KV.
            let count = request.operation == "Prefill32" ? 32 : request.operation == "Prefill8" ? 8 : 0
            try require(count > 0 && request.tokens.count == count && request.tokens.allSatisfy { $0 < 151936 }, "checked request range/shape")
            let spec = try WireRequest(name: request.commandID, operation: request.operation, task: request.task,
                purpose: "checked-source", tokens: request.tokens, sourceContext: try .fromCore(request.json), nativeContext: .null)
            try require(spec.digest == request.inputSHA256, "checked original input digest")
            let receipt = try await worker.eval(spec)
            return MLXHostReceipt(request: request, raw: receipt.raw.core)
        } catch { throw context.backendFailure(error, worker: worker) }
    }
    package func release(_ request: MLXReturnRequest) async throws -> MLXReturnACK {
        do {
            let original = request.receipt.request
            try require(original.session == identity && request.returnID == original.returnID && request.scope == original.scope,
                "return matches original source obligation/scope")
            let raw: WireJSON
            var candidateRoute: CWCRoute?
            let conversionStart = DispatchTime.now().uptimeNanoseconds
            do {
                switch context.returnConversion {
                case .original:
                    raw = try WireJSON.fromCore(request.receipt.raw)
                case .candidate:
                    let converted = try WireJSON.cwcFromCore(request.receipt.raw)
                    raw = converted.value; candidateRoute = converted.route
                }
            } catch {
                let conversionEnd = DispatchTime.now().uptimeNanoseconds
                context.returnConversionObservation.append(request: request, input: request.receipt.raw,
                    output: nil, error: error, candidateRoute: nil, start: conversionStart, end: conversionEnd)
                throw error // Original error reaches the unchanged outer backendFailure wrapper.
            }
            let conversionEnd = DispatchTime.now().uptimeNanoseconds
            context.returnConversionObservation.append(request: request, input: request.receipt.raw,
                output: raw, error: nil, candidateRoute: candidateRoute, start: conversionStart, end: conversionEnd)
            guard let epoch = raw["lease_epoch"].integer else { throw TransportFault("lease", "validated receipt lacks integer wire lease") }
            let lease = WireLease(session: identity.session, worker: identity.worker, request: original.commandID, task: original.task, epoch: epoch)
            let ack = try await worker.release(WireReceipt(lease: lease, raw: raw))
            return MLXReturnACK(request: request, raw: ack.core)
        } catch { throw context.backendFailure(error, worker: worker) }
    }
    package func snapshot() -> JSONValue { worker.snapshot().core }
    package func shutdown() async throws -> MLXClosedEvidence {
        do {
            let ack = try await worker.shutdown()
            _ = try await context.finish()
            return context.closedEvidence(worker: worker, identity: identity, ack: ack)
        } catch { throw context.backendFailure(error, worker: worker) }
    }
    /// CPU multi-fixture entry: close just this worker, then start the next.
    /// The returned snapshot explicitly leaves the shared permit reader open.
    package func closeWorkerForCPU() async throws -> JSONValue {
        try require(cpu, "CPU-only worker closure")
        do {
            let ack = try await worker.shutdown()
            return .object(["shutdown_ack": ack.core, "worker_roots": worker.snapshot().core,
                "global_control_finished": .bool(false)])
        } catch {
            await worker.closeFailedCPUFixture()
            throw context.backendFailure(error, worker: worker)
        }
    }
    package func closeFailedCPUFixture() async {
        precondition(cpu)
        await worker.closeFailedCPUFixture()
    }
}

/// A native reference has a real host Task label but no source Caller, epoch,
/// Read/Write access, source site or BackendReturn capability.
package struct NativeReferenceRequest: Sendable, Equatable {
    package let requestID: String, taskID: String, operation: String, tokens: [UInt64]
    package init(requestID: String, taskID: String, operation: String, tokens: [UInt64]) {
        self.requestID = requestID; self.taskID = taskID; self.operation = operation; self.tokens = tokens
    }
    package var json: JSONValue { .object(["purpose": .string("native-reference"), "request_id": .string(requestID),
        "task_id": .string(taskID), "operation": .string(operation), "tokens": .array(tokens.map(JSONValue.unsigned))]) }
}
package struct NativeReferenceReceipt: Sendable, Equatable {
    package let request: NativeReferenceRequest, raw: JSONValue, token: UInt64
    fileprivate let wire: WireReceipt
    fileprivate init(request: NativeReferenceRequest, wire: WireReceipt) {
        self.request = request; self.wire = wire; self.raw = wire.raw.core; self.token = UInt64(wire.token)
    }
    package var json: JSONValue { .object(["request": request.json, "worker_frame": raw, "token": .unsigned(token)]) }
}
package final class NativeReferenceDriver: Sendable {
    package let identity: MLXSessionIdentity
    private let context: TransportContext, worker: TransportSession
    fileprivate init(context: TransportContext, worker: TransportSession) throws {
        self.context = context; self.worker = worker; self.identity = try worker.initializedIdentity()
    }
    package func checkHealthy() throws {
        do { try worker.checkHealthy() } catch { throw context.backendFailure(error, worker: worker) }
    }
    package func evaluateReference(_ request: NativeReferenceRequest) async throws -> NativeReferenceReceipt {
        do {
            let native = WireJSON.object(["purpose": .string("native-reference"), "request_id": .string(request.requestID), "task_id": .string(request.taskID)])
            let spec = try WireRequest(name: request.requestID, operation: request.operation, task: request.taskID,
                purpose: "native-reference", tokens: request.tokens, sourceContext: .null, nativeContext: native)
            return NativeReferenceReceipt(request: request, wire: try await worker.eval(spec))
        } catch { throw context.backendFailure(error, worker: worker) }
    }
    /// Host owns the join obligation. The native entry must await the saved
    /// original Task.value before calling this; no synthetic join is claimed.
    package func releaseReference(_ receipt: NativeReferenceReceipt) async throws -> JSONValue {
        do { return try await worker.release(receipt.wire).core }
        catch { throw context.backendFailure(error, worker: worker) }
    }
    package func snapshot() -> JSONValue { worker.snapshot().core }
    package func shutdown() async throws -> MLXClosedEvidence {
        do {
            let ack = try await worker.shutdown(); _ = try await context.finish()
            return context.closedEvidence(worker: worker, identity: identity, ack: ack)
        } catch { throw context.backendFailure(error, worker: worker) }
    }
}
