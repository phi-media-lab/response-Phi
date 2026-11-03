import Foundation
import Dispatch
import Synchronization
import Darwin
import RuntimeCore

/// Trusted host configuration, fixed for the entire TransportContext.
package enum HRCReturnConversionMode: String, Sendable {
    case original, candidate
}

/// Leaf diagnostic buffer: no protocol decisions, callbacks, native waits,
/// JSON encoding or IO. The retained evidence consists only of immutable JSON
/// values and the original Error; it contains no device/KV access capability.
final class HRCConversionObservation: Sendable {
    private struct Row: Sendable {
        let appendSequence: Int
        let request: MLXReturnRequest
        let input: JSONValue
        let output: WireJSON?
        let error: (any Error)?
        let candidateRoute: CWCRoute?
        let start: UInt64, end: UInt64
    }
    private struct State {
        var attempted = 0, dropped = 0
        var rows: [Row] = []
        init() { rows.reserveCapacity(64) }
    }
    private let state = Mutex(State())
    private let origin = DispatchTime.now().uptimeNanoseconds
    private let pid = Int(getpid())
    let mode: HRCReturnConversionMode
    let session: String
    init(mode: HRCReturnConversionMode, session: String) { self.mode = mode; self.session = session }

    /// Called only after the conversion's end timestamp. No tags, hash,
    /// identity JSON construction, JSON serialization or file IO occurs here.
    func append(request: MLXReturnRequest, input: JSONValue, output: WireJSON?, error: (any Error)?,
                candidateRoute: CWCRoute?, start: UInt64, end: UInt64) {
        state.withLock { s in
            s.attempted += 1
            guard s.rows.count < 64 else { s.dropped += 1; return }
            s.rows.append(Row(appendSequence: s.attempted, request: request, input: input,
                output: output, error: error, candidateRoute: candidateRoute, start: start, end: end))
        }
    }

    /// Called at the original host collection boundary, after successful
    /// work/timer/IO closure, or for an explicitly incomplete failure prefix.
    /// The mutex is released before any full-tree tagging or error description.
    func snapshot(complete: Bool) -> JSONValue {
        let observed = state.withLock { ($0.rows, $0.attempted, $0.dropped) }
        var rows: [JSONValue] = []
        var rowEvidenceValid = true
        for row in observed.0 {
            let original = row.request.receipt.request
            let raw = row.request.receipt.raw
            let errorEvidence = row.error.map(cwcDescribeError)
            let succeeded = row.error == nil
            let route: String
            let routeObservation: String
            if mode == .original {
                route = "original"; routeObservation = "original-entry"
            } else if let actual = row.candidateRoute, succeeded {
                route = actual.rawValue; routeObservation = "returned-value"
            } else if !succeeded {
                route = "fallback"; routeObservation = "source-inferred-failure"
            } else {
                route = "unavailable"; routeObservation = "missing-returned-route"
            }
            let clockValid = row.start >= origin && row.end >= row.start
            let outcomeValid = succeeded ? row.output != nil && (mode == .original || row.candidateRoute != nil) : row.output == nil
            let valid = clockValid && outcomeValid && (errorEvidence?.complete ?? true)
            rowEvidenceValid = rowEvidenceValid && valid
            let requestOrigin: JSONValue = .object([
                "session": .string(original.session.session), "worker_id": .string(original.session.worker),
                "request_id": .string(original.commandID), "receipt_id": raw.hrcField("receipt_id"),
                "backend_return": .string(row.request.returnID), "scope": .string(row.request.scope),
                "owner": .string(original.owner), "source_epoch": .string(original.sourceEpoch),
                "job": .string(original.job), "task": .string(original.task),
                "invocation": .string(original.invocation), "region": .string(original.region),
                "access": .string(original.access), "site": .string(original.site),
                "wire_eval_seq": raw.hrcField("seq"), "wire_lease_epoch": raw.hrcField("lease_epoch")])
            let error: JSONValue
            if let errorEvidence {
                error = .object(["raw": errorEvidence.raw.core, "comparison": errorEvidence.comparison.core,
                    "complete": .bool(errorEvidence.complete)])
            } else { error = .null }
            rows.append(.object([
                "append_seq": .integer(row.appendSequence),
                "callsite": .string("Driver.release:receipt.raw/Core-to-Wire"),
                "session": .string(session), "process_pid": .integer(pid), "mode": .string(mode.rawValue),
                "start_uptime_ns": .string(String(row.start)), "end_uptime_ns": .string(String(row.end)),
                "elapsed_ns": clockValid ? .string(String(row.end - row.start)) : .null,
                "request_origin": requestOrigin, "original_request": original.json,
                "execution_binding": .null,
                "execution_binding_reason": .string("original parent drain call chain by source correspondence; no native/current Caller identity sampled"),
                "status": .string(succeeded ? "success" : "error"),
                "route": .string(route), "route_observation": .string(routeObservation),
                "input_core_tagged": cwcTagCore(row.input).core,
                "output_wire_tagged": row.output.map { cwcTagWire($0).core } ?? .null,
                "error": error, "evidence_valid": .bool(valid)]))
        }
        return .object([
            "schema": .string("hrc1-return-conversion-v1"), "session": .string(session),
            "mode": .string(mode.rawValue), "process_pid": .integer(pid),
            "clock": .object(["domain": .string("Swift DispatchTime.uptimeNanoseconds absolute monotonic ticks"),
                "origin_uptime_ns": .string(String(origin))]),
            "capacity": .integer(64), "attempted": .integer(observed.1),
            "retained": .integer(observed.0.count), "dropped": .integer(observed.2),
            "collection_complete": .bool(complete),
            "measurement_valid": .bool(complete && observed.2 == 0 && rowEvidenceValid),
            "validation_scope": .string("local collection/error/timestamp integrity; independent audit must check protocol, exact call cardinality and full values"),
            "rows": .array(rows)])
    }
}

private extension JSONValue {
    func hrcField(_ name: String) -> JSONValue {
        guard case .object(let fields) = self else { return .null }
        return fields[name] ?? .null
    }
}
