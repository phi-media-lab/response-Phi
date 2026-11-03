import Foundation
import Synchronization
import RuntimeCore

/// The outer host retains this root before calling run. Construction performs
/// validation/storage only. Core, rather than this coordinator, creates the
/// original parent main/observer/watchdog and service work/timer/observer tasks.
final class ParentCompositionRoot: HostRetainedRoot, Sendable {
    private struct State {
        var started = false
        var report: ExecutionReport?
        var parentClosure: RuntimeClosureObservation?
        var registryClosure: RegistryClosureObservation?
    }
    private let state = Mutex(State())
    private let program: CheckedProgram
    private let environment: ParentHostEnvironment
    private let worker: ServiceWorkerSelection
    private let cpuControl: ParentCompositionCPUControl?
    private let nativeTool: ParentNativeToolControl?
    private let budget: Int
    let parent = RuntimeExecutionRoot()
    let registry: ServiceRegistryRoot

    init(program: CheckedProgram, leaf: ChildLeafDescriptor, inputsPath: String,
         worker: ServiceWorkerSelection, vocabulary: VocabFileCapability,
         parentWatchdogSeconds: Int = 50, hostBudgetSeconds: Int = 50,
         cpuControl: ParentCompositionCPUControl? = nil,
         nativeTool: ParentNativeToolControl? = nil) throws {
        try validateParentServiceConfiguration(program)
        try hostRequire((1...180).contains(parentWatchdogSeconds) && (1...180).contains(hostBudgetSeconds),
            "fixed parent and host budgets")
        if case .mlx = worker { try hostRequire(cpuControl == nil && nativeTool == nil, "production parent rejects native CPU controls") }
        try hostRequire(cpuControl == nil || nativeTool == nil, "one fixed native tool control per parent")
        let factory = try FixedLeafServiceHostFactory(leaf: leaf, inputsPath: inputsPath, worker: worker, cpuControl: cpuControl)
        let registry = try ServiceRegistryRoot(leaf: leaf, factory: factory, hostBudgetSeconds: hostBudgetSeconds)
        self.registry = registry
        let tool: any VocabSymbolTool
        if let nativeTool { tool = nativeTool } else if let cpuControl { tool = cpuControl } else { tool = TokenizerVocabSymbolTool() }
        self.environment = try ParentHostEnvironment(registry: registry, tool: tool, file: vocabulary)
        self.program = program; self.worker = worker; self.budget = parentWatchdogSeconds; self.cpuControl = cpuControl
        self.nativeTool = nativeTool
    }

    private func claim() throws {
        let first = state.withLock { s in
            guard !s.started else { return false }; s.started = true; return true
        }
        try hostRequire(first, "parent composition starts once")
    }
    var hasPrimaryReport: Bool { state.withLock { $0.report != nil } }
    func run() async throws -> ExecutionReport {
        guard case .mlx = worker else { throw hostFault("production-parent-worker", "production parent requires the MLX worker") }
        try hostRequire(nativeTool == nil, "production run rejects native tool controls")
        try claim()
        let report = await executeParent(program, root: parent, environment: environment, hostBudgetSeconds: budget)
        state.withLock { $0.report = report }
        return report
    }
    /// This method is used only by the separately compiled fixed CPU suite.
    /// Production run has no parameter that can install a gate or native file.
    func runForTesting(control: (any ParentServiceCPUControl)? = nil) async throws -> ExecutionReport {
        guard case .nativeCPU = worker else { throw hostFault("native-parent-control", "native controls require a CPU worker") }
        try claim()
        let report = await executeParentForTesting(program, root: parent, environment: environment,
            hostBudgetSeconds: budget, control: control)
        state.withLock { $0.report = report }
        return report
    }

    /// Called by the outer host after its primary report has been saved. A
    /// timeout does not bypass these original handles or update that report.
    /// Parent ScopeExit waits on per-service closure, so it does not depend on
    /// the final registry seal/join below. No additional shutdown is issued.
    func joinOriginalHandles() async throws -> JSONValue {
        try hostRequire(state.withLock { $0.report != nil }, "save/observe parent report before final collection")
        if parent.isInstalled {
            let closure = await parent.joinOriginalHandles()
            state.withLock { $0.parentClosure = closure }
        }
        let closure = await registry.sealAndJoin()
        state.withLock { $0.registryClosure = closure }
        return snapshot()
    }

    func requestAbort(_ reason: ServiceAbortReason) {
        registry.requestAbort(reason)
        // Only the outer host/fixture failure path opens native test gates.
        // Core's three-second watchdog still talks directly to the registry;
        // it cannot release C09's gate before both reports have been saved.
        cpuControl?.releaseAll()
    }
    func snapshot() -> JSONValue {
        let current = state.withLock { ($0.started, $0.report, $0.parentClosure, $0.registryClosure) }
        return .object([
            "schema": .string("src1b-parent-composition-root-v1"), "started": .bool(current.0),
            "primary_report": current.1?.json ?? .null,
            "parent_original_handles": parent.snapshot(),
            "parent_late_closure": current.2?.diagnostic ?? .null,
            "registry": registry.snapshot(), "registry_final_closure": current.3?.diagnostic ?? .null,
            "primary_report_preserved": .bool(true)
        ])
    }
}
