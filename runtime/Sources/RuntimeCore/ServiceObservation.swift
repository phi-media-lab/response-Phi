import Foundation
import Dispatch
import Synchronization

/// BRS1 diagnostics only. Neither capture nor append calls protocol code, IO,
/// an actor, or an asynchronous operation. Logical labels are not Task IDs.
package struct BRSServiceStamp: Sendable {
    package let captureNS: UInt64
    package let priority: Int?
    package let priorityNullReason: String?
    package static func capture(domain: String) -> Self {
        let time = DispatchTime.now().uptimeNanoseconds
        guard domain == "swift_task" else {
            return Self(captureNS: time, priority: nil, priorityNullReason: "non-task-domain")
        }
        let priority: Int? = withUnsafeCurrentTask { task in
            guard task != nil else { return nil }
            return Int(Task.currentPriority.rawValue)
        }
        return Self(captureNS: time, priority: priority,
                    priorityNullReason: priority == nil ? "no-current-task" : nil)
    }
}

package final class BRSServiceTrace: Sendable {
    private struct State {
        var rows: [JSONValue] = []
        var dropped = 0
        var nextID = 0
    }
    private let state = Mutex(State())
    package let session: String
    private let role: String
    private let capacity: Int
    private let origin = DispatchTime.now().uptimeNanoseconds
    package init(session: String, role: String = "production", capacity: Int = 2048) {
        precondition(capacity > 0 && capacity <= 2048)
        self.session = session; self.role = role; self.capacity = capacity
    }
    /// Independent observation identity, never Ledger.fresh or wire event_seq.
    package func nextWaitID() -> String {
        state.withLock { s in s.nextID += 1; return "brs-wait-" + String(s.nextID) }
    }
    package func record(_ event: String, domain: String = "swift_task",
                        stamp: BRSServiceStamp? = nil, binding: JSONValue = .null,
                        origin requestOrigin: JSONValue = .null, key: JSONValue = .null,
                        details: JSONValue = .null) {
        let captured = stamp ?? .capture(domain: domain)
        state.withLock { s in
            guard s.rows.count < capacity else { s.dropped += 1; return }
            let recorded = DispatchTime.now().uptimeNanoseconds
            let sequence = s.rows.count + 1
            s.rows.append(.object([
                "schema": .string("brs1-service-v1"), "role": .string(role),
                "event": .string(event), "clock_domain": .string("swift-dispatch-uptime-ns"),
                "capture_ns": .string(String(captured.captureNS)),
                "recorded_ns": .string(String(recorded)), "append_seq": .integer(sequence),
                "execution_domain": .string(domain), "execution_binding": binding,
                "request_origin": requestOrigin, "operation_key": key,
                "priority": captured.priority.map(JSONValue.integer) ?? .null,
                "priority_null_reason": captured.priorityNullReason.map(JSONValue.string) ?? .null,
                "details": details]))
        }
    }
    package func snapshot(complete: Bool) -> JSONValue {
        state.withLock { s in .object([
            "schema": .string("brs1-service-snapshot-v1"), "role": .string(role),
            "session": .string(session), "pid": .integer(Int(ProcessInfo.processInfo.processIdentifier)),
            "clock": .object(["domain": .string("swift-dispatch-uptime-ns"),
                "origin_ns": .string(String(origin)), "ticks": .string("absolute DispatchTime.now().uptimeNanoseconds"),
                "cross_python_subtraction_allowed": .bool(false)]),
            "capacity": .integer(capacity), "dropped": .integer(s.dropped),
            "overflow": .bool(s.dropped != 0), "observation_complete": .bool(complete),
            "collection_boundary": .string(complete ? "original-host-and-io-close" : "nonquiescent-prefix"),
            "late_callback_rows_possible": .bool(true),
            "measurement_valid": .bool(complete && s.dropped == 0),
            "validity_scope": .string("collection intent only; preexisting deadline callbacks can append later; all required cardinalities and worker observations require independent raw audit"),
            "events": .array(s.rows)]) }
    }
}

/// Non-throwing projection for diagnostics. No validation or protocol decision
/// consumes these fields; original full request/receipt/ACK remain authoritative.
package extension JSONValue {
    func brsField(_ key: String) -> JSONValue {
        guard case .object(let fields) = self else { return .null }
        return fields[key] ?? .null
    }
}

package func brsOperation(session: String, worker: String, scope: String? = nil,
                          owner: String? = nil, epoch: String? = nil, beta: String? = nil,
                          command: String? = nil, site: String? = nil) -> JSONValue {
    .object(["session": .string(session), "worker_id": .string(worker),
        "scope": scope.map(JSONValue.string) ?? .null, "owner": owner.map(JSONValue.string) ?? .null,
        "source_epoch": epoch.map(JSONValue.string) ?? .null,
        "backend_return": beta.map(JSONValue.string) ?? .null,
        "command": command.map(JSONValue.string) ?? .null, "source_site": site.map(JSONValue.string) ?? .null,
        "wire_seq": .null, "wire_lease_epoch": .null, "event_seq": .null, "receipt_id": .null])
}
