import Foundation
import Synchronization
import RuntimeCore

/// Retained by the outer entry before any runnable work. This is a host owner,
/// not a source Job or substitute for the Core's original join observations.
final class ParentEntryRoots: HostRetainedRoot, Sendable {
    private struct State {
        var sealed = false, reason: ServiceAbortReason?
        var composition: ParentCompositionRoot?
        var work: Task<Void, Never>?, timer: Task<Void, Never>?
        var workReturned = false, workJoined = false, timerJoined = false
    }
    private let state = Mutex(State())
    func install(_ root: ParentCompositionRoot) throws {
        try state.withLock { s in
            try hostRequire(!s.sealed && s.composition == nil, "parent entry sealed or composition already installed")
            s.composition = root
        }
    }
    func installWork(_ task: Task<Void, Never>) {
        state.withLock { precondition($0.work == nil); $0.work = task }
    }
    func installTimer(_ task: Task<Void, Never>) {
        state.withLock { precondition($0.timer == nil); $0.timer = task }
    }
    func workReturning() { state.withLock { $0.workReturned = true } }
    func abort(_ reason: ServiceAbortReason) {
        let root = state.withLock { s in
            s.sealed = true; if s.reason == nil { s.reason = reason }; return s.composition
        }
        root?.requestAbort(reason)
    }
    func joinWork() async {
        let task = state.withLock { $0.work }
        precondition(task != nil); await task!.value
        state.withLock { $0.workJoined = true }
    }
    func cancelAndJoinTimer() async {
        let task = state.withLock { $0.timer }
        precondition(task != nil); task!.cancel(); await task!.value
        state.withLock { $0.timerJoined = true }
    }
    func collectAfterFailure() async throws {
        let root = state.withLock { $0.composition }
        guard let root else { return }
        if root.hasPrimaryReport { _ = try await root.joinOriginalHandles() }
        else if !root.parent.isInstalled { _ = await root.registry.sealAndJoin() }
        // An installed but still-running root is not made safe by a host error.
        // Its original work remains retained; the outer timeout saves a prefix.
    }
    func snapshot() -> JSONValue {
        let s = state.withLock { $0 }
        return .object(["schema": .string("src1b-parent-entry-roots-v1"),
            "sealed": .bool(s.sealed), "abort_reason": s.reason.map { .string($0.rawValue) } ?? .null,
            "original_work_retained": .bool(s.work != nil), "work_returning": .bool(s.workReturned),
            "work_actually_joined": .bool(s.workJoined), "original_timer_retained": .bool(s.timer != nil),
            "timer_actually_joined": .bool(s.timerJoined), "composition": s.composition?.snapshot() ?? .null])
    }
}
