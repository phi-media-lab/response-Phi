import Foundation
import Dispatch
import Synchronization
import RuntimeCore

/// The original handle is retained even if close throws. A close attempt is
/// never retried: its fd number might already have been reused by the OS.
/// File IO happens outside the state lock, so watchdog snapshots stay usable.
final class HostFileRoot: @unchecked Sendable {
    private struct State {
        var attempted = false, closed = false
        var started: UInt64?, finished: UInt64?, failure: TransportFault?
    }
    let handle: FileHandle
    let id: String, role: String, descriptor: Int32
    private let state = Mutex(State())
    init(_ handle: FileHandle, role: String) {
        self.handle = handle; self.role = role; self.descriptor = handle.fileDescriptor
        self.id = UUID().uuidString
    }
    func close() throws {
        let claim: Bool = try state.withLock { s in
            if let failure = s.failure { throw failure }
            if s.closed { return false }
            try require(!s.attempted, "concurrent close of original handle \(id)")
            s.attempted = true; s.started = DispatchTime.now().uptimeNanoseconds
            return true
        }
        guard claim else { return }
        do {
            try handle.close()
            state.withLock { s in s.closed = true; s.finished = DispatchTime.now().uptimeNanoseconds }
        } catch {
            let fault = TransportFault("file_close_unknown", "\(role) original fd \(descriptor): \(error)")
            state.withLock { s in s.failure = fault; s.finished = DispatchTime.now().uptimeNanoseconds }
            throw fault
        }
    }
    var closed: Bool { state.withLock { $0.closed } }
    var failure: TransportFault? { state.withLock { $0.failure } }
    func observation(kind: HostResourceKind) throws -> HostResourceObservation {
        try HostResourceObservation(kind: kind, id: id, created: true, started: false,
            closed: closed, joined: nil, originalEvidence: snapshot().core)
    }
    func snapshot() -> WireJSON {
        state.withLock { s in .object([
            "handle_id": .string(id), "role": .string(role), "original_fd": .n(Int(descriptor)),
            "created": .bool(true), "close_attempted": .bool(s.attempted), "closed": .bool(s.closed),
            "close_started_ns": s.started.map { .string(String($0)) } ?? .null,
            "close_finished_ns": s.finished.map { .string(String($0)) } ?? .null,
            "fault": s.failure?.json ?? .null
        ]) }
    }
}
