import Foundation
import Dispatch
import Synchronization
import CryptoKit
import Darwin
import RuntimeCore
import MLXTransport

enum ServiceWorkerSelection: Sendable {
    case mlx
    case nativeCPU(scenario: String)
}

/// A fixed trusted leaf, not a source-selectable process or arbitrary callback.
/// prepare only allocates the root; it creates no file, pipe, task or Context.
struct FixedLeafServiceHostFactory: ServiceHostFactory {
    let leaf: ChildLeafDescriptor, inputsPath: String, worker: ServiceWorkerSelection
    let cpuControl: ParentCompositionCPUControl?
    init(leaf: ChildLeafDescriptor, inputsPath: String, worker: ServiceWorkerSelection,
         cpuControl: ParentCompositionCPUControl? = nil) throws {
        try hostRequire(inputsPath.hasPrefix("/"), "service inputs path must be absolute")
        if case .mlx = worker { try hostRequire(cpuControl == nil, "production service rejects native CPU controls") }
        self.leaf = leaf; self.inputsPath = inputsPath; self.worker = worker; self.cpuControl = cpuControl
    }
    func prepare(_ invocation: ServiceInvocation) throws -> any PreparedServiceHost {
        try hostRequire(invocation.leaf.diagnostic == leaf.diagnostic, "factory only admits its fixed checked leaf")
        return ServiceChildHostRoot(invocation: invocation, inputsPath: inputsPath, worker: worker, cpuControl: cpuControl)
    }
}

private struct ServiceHostIOError: Error, Sendable {
    let failure: HostServiceFailure
}

/// Registered in the host root before open. A close error preserves the
/// original handle and failure; it never retries close on a potentially reused fd.
private final class ServiceArchiveFile: @unchecked Sendable {
    private struct State {
        var openStarted = false, openReturned = false, writeStarted = false, writeCompleted = false
        var closeAttempted = false, closed = false
        var handle: FileHandle?, originalFD: Int32?
        var requestedBytes: Int?, requestedSHA256: String?
        var failures: [HostServiceFailure] = []
    }
    let id = UUID().uuidString, url: URL
    private let state = Mutex(State())
    init(_ url: URL) { self.url = url }
    private func failure(_ code: String, _ detail: String) -> HostServiceFailure {
        HostServiceFailure(domain: "io", code: code, id: UUID().uuidString,
            original: .object(["path": .string(url.path), "file_id": .string(id), "detail": .string(detail)]))
    }
    func writeAndClose(_ bytes: Data) throws {
        let sha256 = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let fresh = state.withLock { s in
            guard !s.openStarted else { return false }; s.openStarted = true
            s.requestedBytes = bytes.count; s.requestedSHA256 = sha256
            return true
        }
        guard fresh else { throw ServiceHostIOError(failure: failure("OutputRepeatedWrite", "original archive file used twice")) }
        let fd = Darwin.open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, mode_t(0o600))
        guard fd >= 0 else {
            let original = failure("OutputOpenFailure", "errno \(errno)")
            state.withLock { $0.openReturned = true; $0.failures.append(original) }
            throw ServiceHostIOError(failure: original)
        }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        state.withLock { s in s.handle = handle; s.originalFD = fd; s.openReturned = true; s.writeStarted = true }
        do { try handle.write(contentsOf: bytes); state.withLock { $0.writeCompleted = true } }
        catch { let original = failure("OutputWriteFailure", String(describing: error)); state.withLock { $0.failures.append(original) } }
        state.withLock { $0.closeAttempted = true }
        do { try handle.close(); state.withLock { $0.closed = true } }
        catch { let original = failure("OutputCloseUnknown", String(describing: error)); state.withLock { $0.failures.append(original) } }
        if let first = state.withLock({ $0.failures.first }) { throw ServiceHostIOError(failure: first) }
    }
    func observation() throws -> HostResourceObservation {
        let facts = state.withLock { ($0.handle != nil, $0.writeStarted, $0.closed) }
        return try HostResourceObservation(kind: .outputFile, id: id, created: facts.0,
            started: facts.1, closed: facts.2, joined: nil, originalEvidence: snapshot())
    }
    func snapshot() -> JSONValue {
        state.withLock { s in .object([
            "file_id": .string(id), "path": .string(url.path), "open_started": .bool(s.openStarted),
            "open_returned": .bool(s.openReturned), "handle_created": .bool(s.handle != nil),
            "original_fd": s.originalFD.map { .integer(Int($0)) } ?? .null,
            "requested_bytes": s.requestedBytes.map(JSONValue.integer) ?? .null,
            "requested_sha256": s.requestedSHA256.map(JSONValue.string) ?? .null,
            "write_started": .bool(s.writeStarted), "write_completed": .bool(s.writeCompleted),
            "close_attempted": .bool(s.closeAttempted), "closed": .bool(s.closed),
            "failures": .array(s.failures.map(\.diagnostic))
        ]) }
    }
}

/// Core owns and joins the *original* work task that calls run. This adapter
/// creates no surrogate work task, timer or child-result task, and never exits
/// the process. Every retained member exists before the corresponding start.
final class ServiceChildHostRoot: PreparedServiceHost, Sendable {
    private struct State {
        var runClaimed = false, bodyDone = false, directoryCreated = false
        var originalWorkIdentity: String?, abort: ServiceAbortReason?
        var phase: ServiceCreationPhase = .createdGated
        var context: TransportContext?, demand: ExternalDemandControl?, runtime: RuntimeExecutionRoot?
        var driver: MLXTransportDriver?, requesterPrepareRequested = false
        var primary: ExecutionReport?, closure: RuntimeClosureObservation?, requester: RequesterJoinObservation?
        var files: [ServiceArchiveFile] = [], events: [JSONValue] = [], failures: [HostServiceFailure] = []
    }
    let invocation: ServiceInvocation
    private let inputsPath: String, worker: ServiceWorkerSelection
    private let cpuControl: ParentCompositionCPUControl?
    private let state = Mutex(State())
    init(invocation: ServiceInvocation, inputsPath: String, worker: ServiceWorkerSelection,
         cpuControl: ParentCompositionCPUControl? = nil) {
        self.invocation = invocation; self.inputsPath = inputsPath; self.worker = worker; self.cpuControl = cpuControl
    }
    private func event(_ name: String, _ detail: JSONValue = .null, onOriginalWork: Bool = true) {
        let stamp = DispatchTime.now().uptimeNanoseconds
        state.withLock { s in s.events.append(.object([
            "event": .string(name), "append_seq": .integer(s.events.count + 1),
            "process_pid": .integer(Int(getpid())), "session": .string(invocation.session),
            "service_id": .string(invocation.serviceID), "capture_ns": .string(String(stamp)),
            "actual_task_identity": onOriginalWork ? (s.originalWorkIdentity.map(JSONValue.string) ?? .null) : .null,
            "task_identity_reason": .string(onOriginalWork ? "original-registry-work-permit" : "abort-caller-not-bound-to-work"),
            "details": detail
        ])) }
    }
    private func capture(_ error: any Error, code: String) -> HostServiceFailure {
        let original: HostServiceFailure
        if let io = error as? ServiceHostIOError { original = io.failure }
        else if let backend = error as? MLXBackendFailure {
            original = HostServiceFailure(domain: "transport", code: backend.code, id: backend.faultID, original: backend.raw)
        } else {
            original = HostServiceFailure(domain: "host", code: code, id: UUID().uuidString,
                original: .object(["cause": .string(String(describing: error))]))
        }
        state.withLock { $0.failures.append(original) }
        event("host.failure", original.diagnostic)
        return original
    }
    private func requireNotAborted() throws {
        if let reason = state.withLock({ $0.abort }) {
            throw ServiceHostIOError(failure: HostServiceFailure(domain: "host", code: "ServiceCreationAborted",
                id: UUID().uuidString, original: .object(["reason": .string(reason.rawValue)])))
        }
    }
    private func archive(_ name: String, bytes: Data) throws {
        let file = ServiceArchiveFile(invocation.leaf.outputDirectory.appendingPathComponent(name))
        state.withLock { $0.files.append(file) }
        try file.writeAndClose(bytes)
        event("host.output-file-closed", file.snapshot())
    }
    private func archive(_ name: String, value: JSONValue) throws { try archive(name, bytes: value.encoded()) }

    func run(_ permit: ServiceStartPermit, response: ServiceResponsePort) async -> ServiceHostCompletion {
        // Reject a foreign or repeated call without aborting/closing an already
        // running original invocation. Only the first valid permit may mutate it.
        do {
            try invocation.validateStart(permit, response: response)
        } catch {
            let failure = HostServiceFailure(domain: "host", code: "ServiceForeignStart", id: UUID().uuidString,
                original: .object(["cause": .string(String(describing: error))]))
            return .incomplete(resources: [], failures: [failure], originalEvidence: snapshot())
        }
        let first = state.withLock { s in
            guard !s.runClaimed else { return false }
            s.runClaimed = true; s.originalWorkIdentity = permit.originalWorkIdentity; s.phase = .committed
            return true
        }
        guard first else {
            let failure = HostServiceFailure(domain: "host", code: "ServiceRepeatedStart", id: UUID().uuidString,
                original: .object(["detail": .string("original service host run exactly once")]))
            return .incomplete(resources: [], failures: [failure], originalEvidence: snapshot())
        }
        do {
            event("host.work.enter")
            if case .mlx = worker { try hostRequire(cpuControl == nil, "production child rejects native CPU controls") }
            try requireNotAborted()
            let path = invocation.leaf.outputDirectory.path
            guard Darwin.mkdir(path, mode_t(0o700)) == 0 else {
                let originalErrno = errno
                throw ServiceHostIOError(failure: HostServiceFailure(domain: "host", code: "ServiceOutputDirectory",
                    id: UUID().uuidString, original: .object(["path": .string(path), "errno": .integer(Int(originalErrno))])))
            }
            state.withLock { $0.directoryCreated = true }
            let requestPath = invocation.leaf.outputDirectory.appendingPathComponent("request.json").path
            try archive("request.json", bytes: invocation.requestBytes)
            let demand = try makeServiceDemandControl(invocation: invocation, response: response, archivedRequestPath: requestPath)
            state.withLock { $0.demand = demand }
            try requireNotAborted()
            let context = try TransportContext(inputsPath: inputsPath, session: invocation.session,
                outputDirectory: invocation.leaf.outputDirectory, returnConversion: .original,
                returnServicePolicy: .unchanged, competitionTasks: 0, deferActivation: true, serviceDemand: demand)
            state.withLock { $0.context = context }
            try requireNotAborted()
            try context.activate()
            state.withLock { $0.phase = .contextActivated }
            event("host.context-activated", context.snapshot())
            try requireNotAborted()
            let driver: MLXTransportDriver
            switch worker {
            case .mlx: driver = try await context.startChecked()
            case .nativeCPU(let scenario): driver = try await context.startCPU(scenario: scenario)
            }
            state.withLock { $0.driver = driver; $0.phase = .workerCreated }
            try requireNotAborted()
            state.withLock { $0.requesterPrepareRequested = true }
            demand.prepare()
            try hostRequire(demand.requesterWasCreated, "requester preparation did not create an original task")
            state.withLock { $0.phase = .requesterCreated }
            try requireNotAborted()
            let root = RuntimeExecutionRoot()
            state.withLock { $0.runtime = root }
            // The native wrapper only controls the original CPU driver's call
            // boundaries. Production always passes the original MLX driver.
            let backend: any MLXBackendDriver
            if let cpuControl { backend = cpuControl.wrapBackend(driver) } else { backend = driver }
            try await startChildExecution(invocation.leaf.program, backend: backend, root: root,
                returnService: context.returnService, externalDemand: demand,
                hostBudgetSeconds: invocation.leaf.watchdogSeconds)
            state.withLock { $0.phase = .coreCreated }
            let report = await root.awaitReport()
            state.withLock { $0.primary = report }
            event("host.primary-report", report.json)
            try archive("core-primary.json", value: report.json)
            try hostRequire(root.startupAdmitted, "partial child runtime did not admit original main execution")
            // A watchdog report does not authorize a second response or erase
            // closing work. Observe the original handles even after timeout.
            let closure = await root.joinOriginalHandles()
            state.withLock { $0.closure = closure }
            event("host.late-runtime-observation", closure.diagnostic)
            if report.exitCode == 0 { try demand.validateCompletedReply() }
            else { demand.abort(reason: "child-primary-exit-\(report.exitCode)") }
            let requester = try await finishServiceRequester(demand)
            state.withLock { $0.requester = requester }
            event("host.requester-joined", requester.diagnostic)
            await context.returnService.finish(reason: "service-child-work-completed")
            try archive("core-closure.json", value: closure.diagnostic)
            try archive("external-demand.json", value: demand.snapshot(complete: true))
            try archive("return-service.json", value: context.returnService.snapshot(complete: true))
            await context.flush()
            return finish(runtime: closure, requester: requester, startup: nil)
        } catch {
            let original = capture(error, code: "ServiceHostInfrastructure")
            return await closeAfterFailure(original)
        }
    }

    private func closeAfterFailure(_ cause: HostServiceFailure) async -> ServiceHostCompletion {
        let roots = state.withLock { ($0.runtime, $0.demand, $0.context, $0.requesterPrepareRequested, $0.phase) }
        var closure = state.withLock { $0.closure }, requester = state.withLock { $0.requester }
        var startup: (ServiceCreationPhase, HostServiceFailure)?
        if let root = roots.0, root.isInstalled {
            roots.1?.abort(reason: "service-host-failure:" + cause.code)
            if state.withLock({ $0.primary == nil }) {
                let report = await root.awaitReport()
                state.withLock { $0.primary = report }
                event("host.primary-report", report.json)
            }
            if closure == nil {
                closure = await root.joinOriginalHandles()
                state.withLock { $0.closure = closure }
                if let closure { event("host.late-runtime-observation", closure.diagnostic) }
            }
        }
        if roots.0?.startupAdmitted != true {
            // Installed storage/partial gated handles are not authority for a
            // normal shutdown observer. Join those actual handles above, then
            // close the original, verified zero-EVAL transport prefix.
            if let context = roots.2 { _ = await context.closeStartupPrefix() }
            startup = (roots.4, cause)
        }
        if requester == nil, let demand = roots.1, demand.requesterWasCreated {
            do {
                if roots.0?.isInstalled == true {
                    requester = try await finishServiceRequester(demand)
                } else {
                    requester = try await finishAbortedServiceRequesterBeforeRuntime(demand, invocation: invocation)
                }
                state.withLock { $0.requester = requester }
                if let requester { event("host.requester-joined", requester.diagnostic) }
            } catch { _ = capture(error, code: "ServiceRequesterJoinFailure") }
        } else if requester == nil, let demand = roots.1 {
            await demand.finish()
            event("host.requester-not-created", demand.snapshot(complete: false))
        }
        if let context = roots.2 {
            await context.returnService.finish(reason: "service-child-work-failed")
            await context.flush()
        }
        return finish(runtime: closure, requester: requester, startup: startup)
    }
    private func resourceObservations() throws -> [HostResourceObservation] {
        let roots = state.withLock { ($0.context, $0.files) }
        var observations = try roots.0?.hostResourceObservations() ?? []
        observations += try roots.1.map { try $0.observation() }
        return observations
    }
    private func finish(runtime: RuntimeClosureObservation?, requester: RequesterJoinObservation?,
                        startup: (ServiceCreationPhase, HostServiceFailure)?) -> ServiceHostCompletion {
        state.withLock { $0.bodyDone = true }
        event("host.adapter-body-done") // Core's original work suffix/timer has not joined yet.
        do {
            // Take the population only after original requester/control cleanup.
            // The startup and final completion objects refer to the very same
            // observations, not two snapshots separated by more close events.
            let resources = try resourceObservations()
            let startupObservation: ServiceStartupAbortObservation?
            if let startup {
                startupObservation = try ServiceStartupAbortObservation(stage: startup.0,
                    resources: resources, cause: startup.1, originalEvidence: snapshot())
            } else { startupObservation = nil }
            return try ServiceHostCompletion(runtime: runtime, requester: requester,
                backend: runtime?.backendEvidence, startupAbort: startupObservation,
                resources: resources, failures: state.withLock { $0.failures })
        } catch {
            _ = capture(error, code: "ServiceCompletionObservationFailure")
            return .incomplete(runtime: runtime, requester: requester, backend: runtime?.backendEvidence,
                resources: [], failures: state.withLock { $0.failures }, originalEvidence: snapshot())
        }
    }
    func requestAbort(_ reason: ServiceAbortReason) {
        let roots = state.withLock { s in
            if s.abort == nil { s.abort = reason }
            return (s.demand, s.context)
        }
        event("host.abort-requested", .object(["reason": .string(reason.rawValue)]), onOriginalWork: false)
        roots.0?.abort(reason: "service-host-abort:" + reason.rawValue)
        roots.1?.abortReturnServiceControls(reason: "service-host-abort:" + reason.rawValue)
        // In particular, do not quarantine an already-safe child merely for a
        // parent timeout: its original observer must still complete shutdown.
    }
    func snapshot() -> JSONValue {
        let copy = state.withLock { s in
            (s.runClaimed, s.bodyDone, s.directoryCreated, s.originalWorkIdentity, s.abort, s.phase,
             s.context, s.demand, s.runtime, s.requesterPrepareRequested,
             s.primary, s.closure, s.requester, s.files, s.events, s.failures)
        }
        return .object([
            "schema": .string("src1b-host-composition-v1"), "invocation": invocation.diagnostic,
            "run_claimed": .bool(copy.0), "adapter_body_done": .bool(copy.1), "directory_created": .bool(copy.2),
            "original_work_identity": copy.3.map(JSONValue.string) ?? .null,
            "abort_requested": copy.4.map { .string($0.rawValue) } ?? .null, "phase": .string(copy.5.rawValue),
            "context": copy.6?.snapshot() ?? .null, "demand": copy.7?.snapshot(complete: false) ?? .null,
            "runtime": copy.8?.snapshot() ?? .null, "requester_prepare_requested": .bool(copy.9),
            "requester_actually_created": .bool(copy.7?.requesterWasCreated == true),
            "primary_report": copy.10?.json ?? .null, "late_runtime_observation": copy.11?.diagnostic ?? .null,
            "requester_observation": copy.12?.diagnostic ?? .null,
            "files": .array(copy.13.map { $0.snapshot() }), "events": .array(copy.14),
            "failures": .array(copy.15.map(\.diagnostic))
        ])
    }
}
