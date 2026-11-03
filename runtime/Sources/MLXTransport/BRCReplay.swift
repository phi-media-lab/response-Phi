import Foundation
import Dispatch
import Darwin
import RuntimeCore

private let brcLabels = ["receipt-core-encode", "receipt-from-core", "ack-parse",
    "transport-payload-validate", "core-return-validate", "trace-wire-received", "trace-ack-validated"]

private struct BRCInputCase: Sendable {
    let name: String
    let ready: WireJSON
    let receipt: WireReceipt
    let ack: WireJSON
    let ackLine: Data
    let core: BRCPreparedCoreCase
    let receiptLineNumber: Int
    let ackLineNumber: Int
}

private func brcError(_ error: any Error) -> WireJSON {
    if let original = error as? TransportFault {
        return .object(["type": .string("TransportFault"), "id": .string(original.id),
            "code": .string(original.code), "detail": .string(original.detail),
            "description": .string(original.description)])
    }
    return .object(["type": .string(String(describing: type(of: error))),
        "description": .string(String(describing: error))])
}

private func brcPin(_ data: Data, path: String) -> WireJSON {
    .object(["path": .string(path), "bytes": .n(data.count), "sha256": .string(transportDigest(data))])
}

/// One CPU-only task runs synchronous original leaf functions. No Session,
/// driver, worker, source Machine, observer Task or resource-return Task exists.
@MainActor package enum BRCReplay {
    package static func run(arguments: [String]) async -> Int32 {
        do {
            try require(arguments.count == 6 && arguments[0] == "--worker-input" &&
                arguments[2] == "--core-input" && arguments[4] == "--output-dir",
                "usage: brc-replay --worker-input ABS --core-input ABS --output-dir ABS")
            for index in [1, 3, 5] { try require(arguments[index].hasPrefix("/"), "absolute BRC path") }
            let output = URL(fileURLWithPath: arguments[5], isDirectory: true)
            try require(!FileManager.default.fileExists(atPath: output.path), "BRC output must not exist")
            try FileManager.default.createDirectory(at: output, withIntermediateDirectories: false)
            let run = BRCReplayRun(output: output)
            return await run.execute(workerPath: arguments[1], corePath: arguments[3])
        } catch {
            // CLI/output initialization failures precede trials; external recorder
            // retains this original error and the nonzero process exit.
            FileHandle.standardError.write(Data(("BRC initialization: " + String(describing: error) + "\n").utf8))
            return 2
        }
    }
}

@MainActor private final class BRCReplayRun {
    private let output: URL
    private let origin = DispatchTime.now().uptimeNanoseconds
    private let trace: TransportTrace
    private var trials: [WireJSON] = []
    private var controls: [WireJSON] = []
    private var provenance: [String: WireJSON] = [:]
    private var inputCases: [BRCInputCase] = []
    private var reconstructionPin: WireJSON = .null
    private var prefix: FileHandle?
    private var phase = "initialization"
    private var activeTrial: WireJSON = .null
    private var measuredFailure: WireJSON = .null
    private var outputData: [String: Data] = [:]

    init(output: URL) {
        self.output = output
        self.trace = TransportTrace("brc1-diagnostic-" + UUID().uuidString)
    }

    func execute(workerPath: String, corePath: String) async -> Int32 {
        var originalFailure: (any Error)?
        do {
            let prefixURL = output.appendingPathComponent("trials.jsonl")
            try require(FileManager.default.createFile(atPath: prefixURL.path, contents: nil), "create trial prefix")
            prefix = try FileHandle(forWritingTo: prefixURL)
            try FileManager.default.createDirectory(at: output.appendingPathComponent("outputs"), withIntermediateDirectories: false)
            try prepare(workerPath: workerPath, corePath: corePath)
            phase = "negative-controls"
            try negativeControls(inputCases[0])
            phase = "trials"
            for round in 0...8 {
                let shift = round == 0 ? 0 : round % brcLabels.count
                let labels = Array(brcLabels[shift...] + brcLabels[..<shift])
                let cases = round == 0 || round % 2 == 0 ? inputCases : Array(inputCases.reversed())
                var position = 0
                for label in labels {
                    for input in cases {
                        position += 1
                        activeTrial = .object(["label": .string(label), "case": .string(input.name),
                            "round": .n(round), "position_in_round": .n(position)])
                        let trial = try await measure(label: label, input: input, round: round, position: position)
                        trials.append(trial)
                        try prefix?.write(contentsOf: trial.data() + Data([10]))
                        activeTrial = .null
                    }
                }
            }
            try require(trials.count == 126 && trace.snapshot().array?.count == 36, "BRC fixed call/trace counts")
            for label in brcLabels {
                for name in ["A", "B"] {
                    let matching = trials.filter { $0["config"]["label"].string == label && $0["config"]["case"].string == name }
                    try require(matching.count == 9 && matching.filter { $0["config"]["warmup"].bool == true }.count == 1,
                        "BRC every label/case has one warmup and eight measurements")
                }
            }
            phase = "complete"
        } catch { originalFailure = error }

        // Flush the original trace even on a caught failure. It is outside every
        // measured call; no new Task, worker or live resource is being joined.
        await trace.flush()
        do {
            try prefix?.synchronize()
            try prefix?.close()
            prefix = nil
        } catch { if originalFailure == nil { originalFailure = error } }
        do {
            let tracePin = try save(.object(["schema": .string("brc1-original-trace-v1"),
                "diagnostic_session": .string(trace.session), "events": trace.snapshot(),
                "flush_completed": .bool(true)]), named: "trace.json")
            let controlPin = try save(.object(["schema": .string("brc1-negative-controls-v1"),
                "controls": .array(controls)]), named: "negative-controls.json")
            let result: WireJSON = .object([
                "schema_version": .n(1), "experiment": .string("backend-return-cost-brc1"),
                "status": .string(originalFailure == nil ? "completed" : "failed"),
                "provenance": .object(provenance), "config": config,
                "trials": .array(trials), "negative_controls": controlPin,
                "reconstructed": reconstructionPin, "trace": tracePin,
                "phase": .string(phase), "active_trial": activeTrial, "failed_call": measuredFailure,
                "error": originalFailure.map(brcError) ?? .null,
                "summary": summary,
                "clock": .object(["domain": .string("this BRC process DispatchTime.uptimeNanoseconds"),
                    "origin_uptime_ns": .int(Int64(origin)), "process_pid": .n(Int(getpid()))]),
                "checks": .object(["complete": .bool(originalFailure == nil),
                    "trace_flush_completed": .bool(true), "workers_started": .n(0),
                    "model_evaluations": .n(0), "production_sessions_created": .n(0),
                    "stdout_trace_requires_independent_audit": .bool(true)])])
            _ = try save(result, named: "result.json")
        } catch {
            FileHandle.standardError.write(Data(("BRC evidence write: " + String(describing: error) + "\n").utf8))
            if originalFailure == nil { originalFailure = error }
        }
        if let originalFailure {
            FileHandle.standardError.write(Data(("BRC failure: " + String(describing: originalFailure) + "\n").utf8))
            return 1
        }
        return 0
    }

    private var config: WireJSON {
        .object(["labels": .strings(brcLabels), "cases": .strings(["A", "B"]),
            "warmup_round": .n(0), "measurement_rounds": .n(8), "warmup_calls": .n(14),
            "measurement_calls": .n(112), "total_calls": .n(126), "trace_calls": .n(36),
            "measurement_order": .string("left shift base labels by round modulo 7; odd rounds B,A; even rounds A,B"),
            "warmup_order": .string("base labels; each A then B"),
            "execution_context": .string("one CPU CLI main-actor call chain; not historical Session context"),
            "historical_pids_only": .bool(true), "diagnostic_trace_session": .string(trace.session),
            "inputs_loaded_and_reconstruction_outside_timing": .bool(true),
            "validation_digest_output_and_flush_outside_timing": .bool(true)])
    }

    private func prepare(workerPath: String, corePath: String) throws {
        let workerData = try Data(contentsOf: URL(fileURLWithPath: workerPath))
        let coreData = try Data(contentsOf: URL(fileURLWithPath: corePath))
        provenance["worker_input"] = brcPin(workerData, path: workerPath)
        provenance["core_input"] = brcPin(coreData, path: corePath)
        provenance["adapter_source"] = .string(#filePath)
        provenance["executable"] = .string(CommandLine.arguments[0])
        provenance["arguments"] = .strings(CommandLine.arguments)
        provenance["cwd"] = .string(FileManager.default.currentDirectoryPath)
        provenance["os"] = .string(ProcessInfo.processInfo.operatingSystemVersionString)
        provenance["historical_stage"] = .string("BRS1 unique N; no new MLX execution")
        try require(workerData.count == 260147 && transportDigest(workerData) ==
            "71b846bc0bf84f0dce01b86b2f09f1364d9fefc6c38045e063c3f90d518fd69d", "fixed complete historical worker bytes")
        try require(coreData.count == 2619282 && transportDigest(coreData) ==
            "c5a16f3470b2e7ff6a5627f45594d1a5b760424f65ffc83505e204fce4a0c9a3", "fixed complete historical Core bytes")
        try require(workerData.last == 10, "original worker trailing LF")
        let lines = workerData.dropLast().split(separator: 10, omittingEmptySubsequences: false).map { Data($0) }
        try require(lines.count == 9 && lines.allSatisfy { !$0.isEmpty && $0.count < 1_048_576 }, "nine complete original wire lines")
        let frames = try lines.map(WireJSON.parse)
        let kinds = ["HELLO", "READY", "EVAL_ENTER", "EVAL_RECEIPT", "RELEASE_ACK",
            "EVAL_ENTER", "EVAL_RECEIPT", "RELEASE_ACK", "SHUTDOWN_ACK"]
        let ready = frames[1]
        guard let session = ready["session"].string, let worker = ready["worker_id"].string else {
            throw TransportFault("brc_input", "missing historical session/worker")
        }
        for (index, frame) in frames.enumerated() {
            try require(frame["kind"].string == kinds[index] && frame["event_seq"].integer == index + 1 &&
                frame["session"].string == session && frame["worker_id"].string == worker,
                "original ordered nine frames")
        }
        let coreReport = try ExactJSON.decode(coreData)
        let originalTrials = try coreReport.field("trials").elements()
        try require(originalTrials.count == 1, "one original Core trial")
        let ledger = try originalTrials[0].field("ledger")
        let rows: [(String, Int, Int, Int)] = [("A", 3, 4, 1), ("B", 6, 7, 3)]
        var reconstructed: [WireJSON] = []
        for (name, receiptIndex, ackIndex, seq) in rows {
            let receipt = frames[receiptIndex], ack = frames[ackIndex]
            try require(receipt["seq"].integer == seq && ack["seq"].integer == seq + 1 &&
                frames[receiptIndex - 1]["seq"].integer == seq, "original A/B sequence binding")
            guard let request = receipt["request_id"].string,
                  let task = receipt["source_context"]["task"].string,
                  let epoch = receipt["lease_epoch"].integer else {
                throw TransportFault("brc_input", "original lease fields")
            }
            let lease = WireLease(session: session, worker: worker, request: request, task: task, epoch: epoch)
            let wireReceipt = WireReceipt(lease: lease, raw: receipt)
            let prepared = try BRCPreparedCoreCase(ready: ready.core, receipt: receipt.core,
                acknowledgement: ack.core, ledger: ledger)
            let input = BRCInputCase(name: name, ready: ready, receipt: wireReceipt, ack: ack,
                ackLine: lines[ackIndex], core: prepared,
                receiptLineNumber: receiptIndex + 1, ackLineNumber: ackIndex + 1)
            inputCases.append(input)
            reconstructed.append(.object(["case": .string(name), "wire_lease": lease.json,
                "receipt_line": .n(input.receiptLineNumber), "ack_line": .n(input.ackLineNumber),
                "ack_parser_input": brcPin(input.ackLine, path: "worker.stdout.jsonl line \(input.ackLineNumber), excluding LF"),
                "wire_receipt": receipt, "wire_ack": ack, "core": try WireJSON.fromCore(prepared.evidence)]))
        }
        try require(frames[8]["seq"].integer == 5 && frames[8]["all_released"].bool == true,
            "original terminal shutdown evidence")
        reconstructionPin = try save(.object(["schema": .string("brc1-reconstructed-historical-evidence-v1"),
            "ready": ready, "cases": .array(reconstructed),
            "frames": .array(frames.enumerated().map { index, value in
                .object(["line": .n(index + 1), "kind": value["kind"], "event_seq": value["event_seq"],
                    "seq": value["seq"], "parser_bytes": .n(lines[index].count),
                    "parser_sha256": .string(transportDigest(lines[index]))])
            }), "source_pins": .object(provenance)]), named: "reconstructed.json")
    }

    private func negativeControls(_ input: BRCInputCase) throws {
        // All mutations start from the original A ACK; neither original input
        // nor original receipt/expected return request is modified.
        guard var ackFields = input.ack.object, let originalLayers = input.ack["layers"].array else {
            throw TransportFault("brc_input", "ACK object/layers")
        }
        ackFields["layers"] = .array(Array(originalLayers.dropLast()))
        try reject(name: "missing-last-layer", input: input, mutated: .object(ackFields),
            path: "/layers/27", expected: .object(["type": .string("TransportFault"),
                "code": .string("release"), "detail": .string("full layer set missing")]),
            isCore: false)
        ackFields = input.ack.object!
        var layers = originalLayers
        var layer = layers[0].object!
        var pages = layer["released_pages"]!.array!
        var page = pages[0].object!
        guard let epoch = page["allocation_epoch"]?.integer, epoch < Int.max else {
            throw TransportFault("brc_input", "bounded original allocation epoch")
        }
        page["allocation_epoch"] = .n(epoch + 1)
        pages[0] = .object(page); layer["released_pages"] = .array(pages); layers[0] = .object(layer)
        ackFields["layers"] = .array(layers)
        try reject(name: "wrong-released-allocation-epoch", input: input, mutated: .object(ackFields),
            path: "/layers/0/released_pages/0/allocation_epoch", expected: .object([
                "type": .string("TransportFault"), "code": .string("invariant"),
                "detail": .string("release exact original pages/epochs/before")]), isCore: false)
        ackFields = input.ack.object!
        var source = input.ack["source_context"].object!
        let badScope = "brc1-invalid-scope-for-negative-control"
        try require(source["scope"] != .string(badScope), "negative scope must actually differ")
        source["scope"] = .string(badScope); ackFields["source_context"] = .object(source)
        try reject(name: "wrong-core-source-scope", input: input, mutated: .object(ackFields),
            path: "/source_context/scope", expected: .object(["type": .string("RuntimeInvariant"),
                "description": .string("release acknowledgement identity")]), isCore: true)
    }

    private func reject(name: String, input: BRCInputCase, mutated: WireJSON, path: String,
                        expected: WireJSON, isCore: Bool) throws {
        var observed: (any Error)?
        do {
            if isCore { try input.core.validateReturn(raw: mutated.core) }
            else { try TransportSession.brcValidateRelease(mutated, receipt: input.receipt, ready: input.ready) }
        } catch { observed = error }
        let matched: Bool
        if let observed {
            if isCore { matched = BRCPreparedCoreCase.isOriginalIdentityError(observed) }
            else if let original = observed as? TransportFault {
                matched = original.code == expected["code"].string && original.detail == expected["detail"].string
            } else { matched = false }
        } else { matched = false }
        controls.append(.object(["name": .string(name), "case": .string(input.name),
            "mutation_path": .string(path), "original_ack": input.ack, "mutated_ack": mutated,
            "original_expected": try WireJSON.fromCore(input.core.evidence.field("expected")),
            "expected_error": expected, "actual_error": observed.map(brcError) ?? .null,
            "returned_without_error": .bool(observed == nil), "matched_original_error": .bool(matched)]))
        // Record actual result before asserting the expected diagnosis.
        _ = try save(.object(["schema": .string("brc1-negative-controls-v1"), "controls": .array(controls)]),
            named: "negative-controls.json")
        try require(matched, "BRC negative control did not produce exact original error: \(name)")
    }

    private func measure(label: String, input: BRCInputCase, round: Int, position: Int) async throws -> WireJSON {
        var start: UInt64 = 0
        var end: UInt64 = 0
        var returnedData: Data?
        var returnedValue: WireJSON?
        var flush: WireJSON = .null
        var traceSequence: Int?
        // Each case places timestamps immediately around the unchanged call.
        // Result checks, encoded summaries and output writes are all below.
        do {
            switch label {
            case "receipt-core-encode":
                start = DispatchTime.now().uptimeNanoseconds
                returnedData = try input.core.receiptCore.encoded()
                end = DispatchTime.now().uptimeNanoseconds
            case "receipt-from-core":
                start = DispatchTime.now().uptimeNanoseconds
                returnedValue = try WireJSON.fromCore(input.core.receiptCore)
                end = DispatchTime.now().uptimeNanoseconds
            case "ack-parse":
                start = DispatchTime.now().uptimeNanoseconds
                returnedValue = try WireJSON.parse(input.ackLine)
                end = DispatchTime.now().uptimeNanoseconds
            case "transport-payload-validate":
                start = DispatchTime.now().uptimeNanoseconds
                try TransportSession.brcValidateRelease(input.ack, receipt: input.receipt, ready: input.ready)
                end = DispatchTime.now().uptimeNanoseconds
            case "core-return-validate":
                start = DispatchTime.now().uptimeNanoseconds
                try input.core.validateReturn()
                end = DispatchTime.now().uptimeNanoseconds
            case "trace-wire-received":
                start = DispatchTime.now().uptimeNanoseconds
                trace.record("wire.received", ["worker_id": input.ack["worker_id"], "message": input.ack])
                end = DispatchTime.now().uptimeNanoseconds
            case "trace-ack-validated":
                start = DispatchTime.now().uptimeNanoseconds
                trace.record("transport.ack_validated", ["worker_id": input.ack["worker_id"], "ack": input.ack])
                end = DispatchTime.now().uptimeNanoseconds
            default: throw TransportFault("brc_label", label)
            }
        } catch {
            end = DispatchTime.now().uptimeNanoseconds
            measuredFailure = .object(["stage": .string("original-call"), "config": activeTrial, "start_uptime_ns": .int(Int64(start)),
                "end_uptime_ns": .int(Int64(end)), "error": brcError(error)])
            throw error
        }
        // Keep the actual return and endpoint pair alive before any postcheck.
        // Encoding this failure evidence is lazy and never inside the sample.
        let originalReturn = (data: returnedData, value: returnedValue,
            trace: label.hasPrefix("trace-") ? trace.snapshot().array?.last : nil)
        do {
            try require(start >= origin && end >= start, "BRC monotonic sample")
            var result: WireJSON
            if let data = returnedData {
                let decoded = try WireJSON.parse(data)
                try require(decoded == input.receipt.raw, "full Core-encoded receipt round trip")
                result = try preserveOutput(data, input: input, label: label, kind: "original-core-encoded-data")
            } else if let value = returnedValue {
                let expected = label == "ack-parse" ? input.ack : input.receipt.raw
                try require(value == expected, "full original returned WireJSON equality")
                result = try preserveOutput(value.data(), input: input, label: label, kind: "returned-WireJSON-canonical-data")
            } else if label.hasPrefix("trace-") {
                let flushStart = DispatchTime.now().uptimeNanoseconds
                await trace.flush()
                let flushEnd = DispatchTime.now().uptimeNanoseconds
                flush = .object(["start_uptime_ns": .int(Int64(flushStart)), "end_uptime_ns": .int(Int64(flushEnd)),
                    "elapsed_ns": .int(Int64(flushEnd - flushStart)), "included_in_call_duration": .bool(false)])
                guard let events = trace.snapshot().array, let event = events.last,
                      let sequence = event["swift_seq"].integer else { throw TransportFault("brc_trace", "missing original trace event") }
                let key = label == "trace-wire-received" ? "message" : "ack"
                let name = label == "trace-wire-received" ? "wire.received" : "transport.ack_validated"
                try require(event[key] == input.ack && event["event"].string == name &&
                    event["worker_id"] == input.ack["worker_id"] && event["session"].string == trace.session &&
                    events.count == sequence, "original trace full payload and sequence")
                traceSequence = sequence
                let data = try event.data()
                result = .object(["kind": .string("original-trace-event"), "trace_seq": .n(sequence),
                    "bytes": .n(data.count), "sha256": .string(transportDigest(data)),
                    "full_payload_equal": .bool(true), "stdout_verified_here": .bool(false)])
            } else {
                result = .object(["kind": .string("Void"), "returned": .bool(true),
                    "original_validator": .string(label == "core-return-validate" ? "validateMLXReturn" : "TransportSession.validateRelease")])
            }
            return .object(["trial_id": .string("r\(round)/\(label)/\(input.name)"),
                "config": .object(["label": .string(label), "case": .string(input.name), "round": .n(round),
                    "warmup": .bool(round == 0), "position_in_round": .n(position)]),
                "events": .array([event("call.start", tick: start, request: input.receipt.lease.request),
                    event("call.end", tick: end, request: input.receipt.lease.request)]),
                "metrics": .object(["start_uptime_ns": .int(Int64(start)), "end_uptime_ns": .int(Int64(end)),
                    "elapsed_ns": .int(Int64(end - start))]),
                "output": result, "flush": flush, "trace_seq": traceSequence.map(WireJSON.n) ?? .null,
                "checks": .object(["original_call_returned": .bool(true), "post_call_validation": .bool(true),
                    "post_call_validation_in_duration": .bool(false)])])
        } catch {
            measuredFailure = .object(["stage": .string("post-call-validation-or-save"), "config": activeTrial,
                "start_uptime_ns": .int(Int64(start)), "end_uptime_ns": .int(Int64(end)),
                "original_call_returned": .bool(true),
                "original_return": .object([
                    "data_base64": originalReturn.data.map { .string($0.base64EncodedString()) } ?? .null,
                    "data_bytes": originalReturn.data.map { .n($0.count) } ?? .null,
                    "wire_value": originalReturn.value ?? .null,
                    "trace_event": originalReturn.trace ?? .null,
                    "void_return": .bool(originalReturn.data == nil && originalReturn.value == nil && originalReturn.trace == nil)]),
                "error": brcError(error)])
            throw error
        }
    }

    private func preserveOutput(_ data: Data, input: BRCInputCase, label: String, kind: String) throws -> WireJSON {
        let name = "outputs/\(input.name)-\(label).json"
        if let previous = outputData[name] { try require(previous == data, "all nine original outputs byte-identical") }
        else { try data.write(to: output.appendingPathComponent(name), options: .atomic); outputData[name] = data }
        return .object(["kind": .string(kind), "artifact": .string(name), "bytes": .n(data.count),
            "sha256": .string(transportDigest(data)), "full_value_equal": .bool(true)])
    }

    private func event(_ name: String, tick: UInt64, request: String) -> WireJSON {
        .object(["name": .string(name), "t_ms": .real(Double(tick - origin) / 1_000_000),
            "uptime_ns": .int(Int64(tick)), "request_id": .string(request)])
    }

    private var summary: WireJSON {
        var rows: [WireJSON] = []
        for label in brcLabels {
            for name in ["A", "B"] {
                let samples = trials.filter { $0["config"]["label"].string == label &&
                    $0["config"]["case"].string == name && $0["config"]["warmup"].bool == false }
                    .compactMap { $0["metrics"]["elapsed_ns"].integer }.sorted()
                let median: WireJSON = samples.count == 8 ? .real((Double(samples[3]) + Double(samples[4])) / 2) : .null
                rows.append(.object(["label": .string(label), "case": .string(name), "count": .n(samples.count),
                    "median_ns": median, "min_ns": samples.first.map(WireJSON.n) ?? .null,
                    "max_ns": samples.last.map(WireJSON.n) ?? .null]))
            }
        }
        return .object(["measurement_only": .bool(true), "rows": .array(rows),
            "completed_calls": .n(trials.count), "original_trace_events": .n(trace.snapshot().array?.count ?? 0)])
    }

    @discardableResult private func save(_ value: WireJSON, named name: String) throws -> WireJSON {
        let data = try value.data()
        let url = output.appendingPathComponent(name)
        try data.write(to: url, options: .atomic)
        return brcPin(data, path: url.path)
    }
}
