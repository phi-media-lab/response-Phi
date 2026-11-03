import Foundation
import Dispatch
import Synchronization
import CryptoKit
import RuntimeCore

/// Separate native IO boundaries. These are not the B-C01...B-C10 cases.
enum NativeToolBoundaryCase: String, CaseIterable, Sendable {
    case t01 = "B-T01", t02 = "B-T02", t03 = "B-T03", t04 = "B-T04"
    case t05 = "B-T05", t06 = "B-T06", t07 = "B-T07", t08 = "B-T08"
    case t09 = "B-T09", t10 = "B-T10", t11 = "B-T11", t12 = "B-T12"
    case t13 = "B-T13", t14 = "B-T14"

    var token: UInt64 {
        switch self {
        case .t02: return 1
        case .t03: return 151643
        case .t04: return 151669
        case .t05: return 151936
        default: return 0
        }
    }
}

/// A forwarding observer on the original parent task. Ledger still mints the
/// invocation/root from a genuinely consumed child response. This class never
/// constructs response, tool outcome, receipt, or resource-close evidence.
final class ParentNativeToolControl: VocabSymbolTool, HostRetainedRoot, Sendable {
    private struct State: Sendable {
        var invocation: ToolInvocation?, root: ToolReadRoot?, outcome: ToolCallOutcome?
        var performCalls = 0, returned = false
        var firstReceipt: JSONValue = .null, receiptAfterReentry: JSONValue = .null
        var reentry: JSONValue = .object(["enabled": .bool(false)])
        var cpuBudgetWork: JSONValue = .null
        var issues: [String] = [], events: [JSONValue] = []
    }
    let boundaryCase: NativeToolBoundaryCase
    var caseID: String { boundaryCase.rawValue }
    private let state = Mutex(State())

    init(case boundaryCase: NativeToolBoundaryCase) { self.boundaryCase = boundaryCase }

    private func row(_ event: String, detail: JSONValue, into s: inout State) {
        s.events.append(.object([
            "event": .string(event), "capture_ns": .string(String(DispatchTime.now().uptimeNanoseconds)),
            "append_seq": .integer(s.events.count + 1), "details": detail
        ]))
    }

    /// Actual native CPU work to exercise the unchanged tool's elapsed budget.
    /// It creates no Task, sleeps nowhere, and makes no IO latency claim.
    private func exerciseCPUBudget() -> JSONValue {
        let bytes = Data((0..<65_536).map { UInt8($0 & 255) })
        let seed = Data("SRC1B-T14-original-tool-budget-cpu-work-v1".utf8)
        let inputHash = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let minimum: UInt64 = 2_000_000_000
        let started = DispatchTime.now().uptimeNanoseconds
        var finished = started, iterations: UInt64 = 0, previous = seed
        repeat {
            var hasher = SHA256()
            hasher.update(data: bytes)
            hasher.update(data: previous)
            previous = Data(hasher.finalize())
            iterations += 1
            finished = DispatchTime.now().uptimeNanoseconds
        } while finished - started < minimum
        return .object([
            "algorithm": .string("previous=SHA256(fixed_input_bytes||previous); initial_previous=seed"),
            "input_base64": .string(bytes.base64EncodedString()), "input_bytes": .integer(bytes.count),
            "input_sha256": .string(inputHash), "seed_base64": .string(seed.base64EncodedString()),
            "final_digest_hex": .string(previous.map { String(format: "%02x", $0) }.joined()),
            "iterations": .string(String(iterations)), "clock": .string("swift-dispatch-uptime-ns"),
            "started_ns": .string(String(started)), "finished_ns": .string(String(finished)),
            "elapsed_ns": .string(String(finished - started)), "minimum_ns": .string(String(minimum)),
            "elapsed_at_least_minimum": .bool(finished - started >= minimum),
            "additional_native_cpu_work": .bool(true), "io_latency_claimed": .bool(false),
            "sleep_used": .bool(false), "extra_task_created": .bool(false)
        ])
    }

    private func observeReentry(root: ToolReadRoot, original: ToolCallOutcome,
                                firstDiagnostic: JSONValue, firstReceipt: JSONValue) -> JSONValue {
        let before = root.snapshot()
        var readThrew = false, readExactGuard = false, readError: JSONValue = .null
        do { _ = try root.readAndClose() }
        catch {
            readThrew = true
            let type = String(reflecting: Swift.type(of: error)), description = String(describing: error)
            readExactGuard = type == "RuntimeCore.RuntimeInvariant" && description == "tool read called twice"
            readError = .object(["type": .string(type), "description": .string(description)])
        }
        let afterRead = root.snapshot()
        // This is deliberately the same root's second finish, not a second
        // Ledger.completeTool call. Only the first original outcome is returned.
        let repeated = root.finish(.readFailure)
        let repeatedDiagnostic = repeated.diagnostic
        let afterFinish = root.snapshot()
        let repeatedReceipt = (try? repeatedDiagnostic.field("receipt")) ?? .null
        let repeatedResult = (try? repeatedDiagnostic.field("result")) ?? .null
        let finishRejected = (try? repeatedResult.field("kind").text()) == "infrastructure" &&
            (try? repeatedResult.field("code").text()) == "ToolInfrastructure" &&
            (try? repeatedResult.field("detail").text()) == "tool finish repeated"
        let originalUnchanged = original.diagnostic == firstDiagnostic
        let rootReceipt = (try? afterFinish.field("receipt")) ?? .null
        let receiptUnchanged = repeatedReceipt == firstReceipt && rootReceipt == firstReceipt
        let passed = readThrew && readExactGuard && before == afterRead && afterRead == afterFinish &&
            finishRejected && receiptUnchanged && originalUnchanged
        return .object([
            "enabled": .bool(true), "read_threw": .bool(readThrew), "read_exact_guard": .bool(readExactGuard),
            "read_error": readError, "read_root_unchanged": .bool(before == afterRead),
            "finish_outcome": repeatedDiagnostic, "finish_rejected": .bool(finishRejected),
            "finish_root_unchanged": .bool(afterRead == afterFinish),
            "finish_receipt_unchanged": .bool(receiptUnchanged),
            "original_outcome_unchanged": .bool(originalUnchanged),
            "first_root_snapshot": before, "after_read_snapshot": afterRead, "after_finish_snapshot": afterFinish,
            "second_outcome_submitted_to_core": .bool(false), "passed": .bool(passed)
        ])
    }

    func perform(_ invocation: ToolInvocation, root: ToolReadRoot) -> ToolCallOutcome {
        state.withLock { s in
            precondition(s.invocation == nil && s.root == nil, "native boundary forwards one original tool call")
            s.performCalls += 1; s.invocation = invocation; s.root = root
            if invocation.token != boundaryCase.token { s.issues.append("checked tool literal differs from fixed native case") }
            row("native.tool-original-enter", detail: invocation.diagnostic, into: &s)
        }
        if boundaryCase == .t14 {
            let observation = exerciseCPUBudget()
            state.withLock { s in
                s.cpuBudgetWork = observation
                row("native.tool-cpu-budget-work-returned", detail: observation, into: &s)
            }
        }
        let original = TokenizerVocabSymbolTool().perform(invocation, root: root)
        let diagnostic = original.diagnostic
        let receipt = (try? diagnostic.field("receipt")) ?? .null
        state.withLock { s in
            s.outcome = original; s.firstReceipt = receipt
            if receipt == .null { s.issues.append("original tool outcome omitted receipt") }
            row("native.tool-original-return", detail: diagnostic, into: &s)
        }
        if boundaryCase == .t01 {
            let reentry = observeReentry(root: root, original: original, firstDiagnostic: diagnostic, firstReceipt: receipt)
            state.withLock { s in
                s.reentry = reentry
                if (try? reentry.field("passed").boolean()) != true { s.issues.append("original tool read/finish reentry invariant failed") }
                row("native.tool-reentry-observed", detail: reentry, into: &s)
            }
        }
        let finalReceipt = (try? root.snapshot().field("receipt")) ?? .null
        state.withLock { s in
            s.receiptAfterReentry = finalReceipt
            if finalReceipt != receipt { s.issues.append("first original receipt changed after observation") }
            row("native.tool-forward-return", detail: .object(["original_outcome_returned": .bool(true)]), into: &s)
            s.returned = true
        }
        return original
    }

    func snapshot() -> JSONValue {
        let s = state.withLock { $0 }
        return .object([
            "schema": .string("src1b-native-tool-control-v1"), "case_id": .string(caseID),
            "tool_argument_decimal": .string(String(boundaryCase.token)), "perform_calls": .integer(s.performCalls),
            "tool_invocation": s.invocation?.diagnostic ?? .null, "tool_root": s.root?.snapshot() ?? .null,
            "tool_outcome": s.outcome?.diagnostic ?? .null, "first_receipt": s.firstReceipt,
            "first_receipt_after_reentry": s.receiptAfterReentry, "reentry": s.reentry,
            "cpu_budget_work": s.cpuBudgetWork, "returned": .bool(s.returned),
            "issues": .array(s.issues.map(JSONValue.string)), "events": .array(s.events),
            "clock": .string("swift-dispatch-uptime-ns"), "model_execution": .bool(false),
            "literal_tool_argument_not_response_dataflow_claim": .bool(true)
        ])
    }
}
