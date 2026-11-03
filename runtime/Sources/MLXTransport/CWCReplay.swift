import Foundation
import Dispatch
import Darwin
import RuntimeCore

private struct CWCInput: Sendable {
    let id: String
    let category: String
    let source: String
    let description: WireJSON
    let value: JSONValue
    let expectedRoute: String
    let expectedStatus: String
    let oracle: WireJSON
}

private struct CWCOutcome {
    let json: WireJSON
    let comparison: WireJSON
    let status: String
    let route: String
    let complete: Bool
}

private func cwcOutcome(value: WireJSON?, route: String, error: (any Error)?, arm: String) -> CWCOutcome {
    if let error {
        let evidence = cwcDescribeError(error)
        let json = WireJSON.object(["status": .string("error"), "route": .string(route),
            "route_observation": .string(arm == "candidate" ? "source-inferred-failure" : "original-entry"),
            "value": .null, "error": .object(["raw": evidence.raw, "comparison": evidence.comparison,
                "complete": .bool(evidence.complete)])])
        return CWCOutcome(json: json, comparison: .object(["status": .string("error"),
            "error": evidence.comparison]), status: "error", route: route, complete: evidence.complete)
    }
    let tagged = cwcTagWire(value!)
    return CWCOutcome(json: .object(["status": .string("success"), "route": .string(route),
        "route_observation": .string(arm == "candidate" ? "returned-value" : "original-entry"),
        "value": tagged, "error": .null]), comparison: .object(["status": .string("success"), "value": tagged]),
        status: "success", route: route, complete: true)
}

private func cwcInvoke(_ input: JSONValue, arm: String) -> CWCOutcome {
    do {
        if arm == "original" { return cwcOutcome(value: try WireJSON.fromCore(input), route: "original", error: nil, arm: arm) }
        let result = try WireJSON.cwcFromCore(input)
        return cwcOutcome(value: result.value, route: result.route.rawValue, error: nil, arm: arm)
    } catch {
        return cwcOutcome(value: nil, route: arm == "candidate" ? "fallback" : "original", error: error, arm: arm)
    }
}

private func cwcPin(_ data: Data, path: String) -> WireJSON {
    .object(["path": .string(path), "bytes": .n(data.count), "sha256": .string(transportDigest(data))])
}

/// Builder for the independent corpus description, not a JSONValue parser.
/// Invalid number tokens remain constructed .number values for both arms.
private func cwcBuild(_ node: WireJSON, level: Int = 0) throws -> JSONValue {
    try require(level <= 256, "corpus descriptor nesting bound")
    guard let fields = node.object, let tag = node["tag"].string else { throw ExactJSONError("corpus node requires tag/object") }
    func keys(_ names: Set<String>) throws { try require(Set(fields.keys) == names, "corpus node fields for " + tag) }
    switch tag {
    case "null": try keys(["tag"]); return .null
    case "bool":
        try keys(["tag", "value"])
        guard let value = node["value"].bool else { throw ExactJSONError("corpus bool value") }; return .bool(value)
    case "number":
        try keys(["tag", "token"])
        guard let token = node["token"].string else { throw ExactJSONError("corpus number token string") }; return .number(token)
    case "string":
        try keys(["tag", "value"])
        guard let value = node["value"].string else { throw ExactJSONError("corpus string value") }; return .string(value)
    case "array":
        try keys(["tag", "values"])
        guard let values = node["values"].array else { throw ExactJSONError("corpus array values") }
        return .array(try values.map { try cwcBuild($0, level: level + 1) })
    case "object":
        try keys(["tag", "fields"])
        guard let entries = node["fields"].array else { throw ExactJSONError("corpus object fields") }
        var result: [String: JSONValue] = [:]
        for entry in entries {
            guard let pair = entry.array, pair.count == 2, let key = pair[0].string else { throw ExactJSONError("corpus object pair") }
            try require(result[key] == nil, "corpus duplicate object key is not JSONValue input")
            result[key] = try cwcBuild(pair[1], level: level + 1)
        }
        return .object(result)
    case "nest":
        try keys(["tag", "depth", "leaf", "kind"])
        guard let depth = node["depth"].integer, (0...256).contains(depth),
              let kind = node["kind"].string, ["array", "object"].contains(kind) else { throw ExactJSONError("corpus nest descriptor") }
        var value = try cwcBuild(node["leaf"], level: level + 1)
        for _ in 0..<depth { value = kind == "array" ? .array([value]) : .object(["x": value]) }
        return value
    default: throw ExactJSONError("unknown corpus tag: " + tag)
    }
}

@MainActor package enum CWCReplay {
    package static func run(arguments: [String]) async -> Int32 {
        do {
            try require(arguments.count == 8 && arguments[0] == "--worker-input" &&
                arguments[2] == "--core-input" && arguments[4] == "--corpus" && arguments[6] == "--output-dir",
                "usage: cwc-replay --worker-input ABS --core-input ABS --corpus ABS --output-dir ABS")
            for i in [1, 3, 5, 7] { try require(arguments[i].hasPrefix("/"), "absolute CWC path") }
            let output = URL(fileURLWithPath: arguments[7], isDirectory: true)
            try require(!FileManager.default.fileExists(atPath: output.path), "exclusive CWC output")
            try FileManager.default.createDirectory(at: output, withIntermediateDirectories: false)
            return CWCReplayRun(output: output).execute(workerPath: arguments[1], corePath: arguments[3], corpusPath: arguments[5])
        } catch {
            FileHandle.standardError.write(Data(("CWC initialization: " + String(describing: error) + "\n").utf8))
            return 2
        }
    }
}

@MainActor private final class CWCReplayRun {
    private let output: URL
    private let origin = DispatchTime.now().uptimeNanoseconds
    private var provenance: [String: WireJSON] = [:]
    private var semanticRows: [WireJSON] = []
    private var semanticChecks: [WireJSON] = []
    private var trials: [WireJSON] = []
    private var historical: [CWCInput] = []
    private var expectedHistorical: [String: Data] = [:]
    private var outputValues: [String: Data] = [:]
    private var semanticsFile: FileHandle?
    private var trialsFile: FileHandle?
    private var phase = "prepare"
    private var active: WireJSON = .null
    private var pendingEvidence: WireJSON = .null
    private var reconstruction: WireJSON = .null
    private var corpusCount = 0

    init(output: URL) { self.output = output }

    func execute(workerPath: String, corePath: String, corpusPath: String) -> Int32 {
        var originalError: (any Error)?
        do {
            semanticsFile = try prefixFile("semantics.jsonl")
            trialsFile = try prefixFile("trials.jsonl")
            try FileManager.default.createDirectory(at: output.appendingPathComponent("outputs"), withIntermediateDirectories: false)
            let corpus = try prepare(workerPath: workerPath, corePath: corePath, corpusPath: corpusPath)
            phase = "semantics"
            for input in corpus + historical { try semantic(input) }
            phase = "measurements"
            let receipts = [historical[0], historical[1]] // A-receipt, B-receipt.
            for round in 0...8 {
                let indices = round % 2 == 0 ? [0, 1] : [1, 0]
                var position = 0
                for index in indices {
                    let arms = (round + index) % 2 == 0 ? ["original", "candidate"] : ["candidate", "original"]
                    for arm in arms {
                        position += 1
                        try measure(receipts[index], caseName: index == 0 ? "A" : "B", caseIndex: index,
                            arm: arm, round: round, position: position)
                    }
                }
            }
            try require(trials.count == 36 && outputValues.count == 4, "CWC exact measurement/artifact counts")
            for name in ["A", "B"] {
                for arm in ["original", "candidate"] {
                    let group = trials.filter { $0["config"]["case"].string == name && $0["config"]["arm"].string == arm }
                    try require(group.count == 9 && group.filter { $0["config"]["warmup"].bool == true }.count == 1,
                        "CWC each case/arm one warmup plus eight measured calls")
                }
            }
            phase = "complete"
        } catch { originalError = error }
        for file in [semanticsFile, trialsFile] {
            do { try file?.synchronize(); try file?.close() }
            catch { if originalError == nil { originalError = error } }
        }
        do {
            let semanticPin = try save(.object(["schema": .string("cwc1-semantic-results-v1"),
                "rows": .array(semanticRows), "checks": .array(semanticChecks)]), name: "semantic-results.json")
            let result = WireJSON.object(["schema_version": .n(1), "experiment": .string("core-wire-conversion-cwc1"),
                "status": .string(originalError == nil ? "completed" : "failed"),
                "provenance": .object(provenance), "config": config, "clock": .object([
                    "domain": .string("this CWC process DispatchTime.uptimeNanoseconds elapsed wall"),
                    "origin_uptime_ns": .int(Int64(origin)), "pid": .n(Int(getpid()))]),
                "semantic_results": semanticPin, "reconstructed": reconstruction, "trials": .array(trials),
                "summary": summary, "phase": .string(phase), "active": active,
                "pending_evidence": pendingEvidence, "error": originalError.map { cwcDescribeError($0).raw } ?? .null,
                "checks": .object(["completed": .bool(originalError == nil), "workers_started": .n(0),
                    "sessions_created": .n(0), "model_evaluations": .n(0)])])
            _ = try save(result, name: "result.json")
        } catch {
            FileHandle.standardError.write(Data(("CWC evidence write: " + String(describing: error) + "\n").utf8))
            if originalError == nil { originalError = error }
        }
        if let originalError {
            FileHandle.standardError.write(Data(("CWC failure: " + String(describing: originalError) + "\n").utf8))
            return 1
        }
        return 0
    }

    private var config: WireJSON {
        .object(["corpus_cases": .n(corpusCount), "historical_semantic_cases": .n(4),
            "warmup_calls": .n(4), "measured_calls": .n(32), "total_calls": .n(36),
            "rounds": .n(9), "case_indices": .object(["A": .n(0), "B": .n(1)]),
            "case_order": .string("even round A,B; odd round B,A"),
            "arm_order": .string("(round+fixed_case_index)%2==0 original,candidate; otherwise candidate,original"),
            "timing": .string("one MainActor CPU call chain; conversion only, tag/digest/check/write after end"),
            "original_functions_unchanged": .bool(true), "no_performance_pass_threshold": .bool(true)])
    }

    private func prepare(workerPath: String, corePath: String, corpusPath: String) throws -> [CWCInput] {
        let workerData = try Data(contentsOf: URL(fileURLWithPath: workerPath))
        let coreData = try Data(contentsOf: URL(fileURLWithPath: corePath))
        let corpusData = try Data(contentsOf: URL(fileURLWithPath: corpusPath))
        provenance = ["worker_input": cwcPin(workerData, path: workerPath), "core_input": cwcPin(coreData, path: corePath),
            "corpus": cwcPin(corpusData, path: corpusPath), "arguments": .strings(CommandLine.arguments),
            "executable": .string(CommandLine.arguments[0]), "source": .string(#filePath),
            "cwd": .string(FileManager.default.currentDirectoryPath),
            "os": .string(ProcessInfo.processInfo.operatingSystemVersionString)]
        try require(workerData.count == 260147 && transportDigest(workerData) ==
            "71b846bc0bf84f0dce01b86b2f09f1364d9fefc6c38045e063c3f90d518fd69d", "fixed original worker bytes")
        try require(coreData.count == 2619282 && transportDigest(coreData) ==
            "c5a16f3470b2e7ff6a5627f45594d1a5b760424f65ffc83505e204fce4a0c9a3", "fixed original Core bytes")
        try require(corpusData.count == 35983 && transportDigest(corpusData) ==
            "3a1ab37059959193083233b48d510229f2eda7d49440f8578b0e660236eae598", "fixed independent 74-case corpus")
        try require(workerData.last == 10, "original trailing LF")
        let lines = workerData.dropLast().split(separator: 10, omittingEmptySubsequences: false).map { Data($0) }
        try require(lines.count == 9 && lines.allSatisfy { !$0.isEmpty }, "original nine frames")
        let frames = try lines.map(WireJSON.parse)
        let kinds = ["HELLO", "READY", "EVAL_ENTER", "EVAL_RECEIPT", "RELEASE_ACK", "EVAL_ENTER", "EVAL_RECEIPT", "RELEASE_ACK", "SHUTDOWN_ACK"]
        let ready = frames[1]
        for (i, frame) in frames.enumerated() {
            try require(frame["kind"].string == kinds[i] && frame["event_seq"].integer == i + 1 &&
                frame["session"] == ready["session"] && frame["worker_id"] == ready["worker_id"], "historical ordered wire identity")
        }
        let report = try ExactJSON.decode(coreData)
        let originalTrials = try report.field("trials").elements()
        try require(originalTrials.count == 1, "one historical Core trial")
        let ledger = try originalTrials[0].field("ledger")
        var preparedRows: [WireJSON] = []
        var acknowledgements: [CWCInput] = []
        for (name, ri, ai, seq) in [("A", 3, 4, 1), ("B", 6, 7, 3)] {
            try require(frames[ri]["seq"].integer == seq && frames[ai]["seq"].integer == seq + 1, "historical A/B wire sequences")
            let prepared = try BRCPreparedCoreCase(ready: ready.core, receipt: frames[ri].core,
                acknowledgement: frames[ai].core, ledger: ledger)
            preparedRows.append(.object(["case": .string(name), "receipt_line": .n(ri + 1), "ack_line": .n(ai + 1),
                "core": try WireJSON.fromCore(prepared.evidence)]))
            historical.append(CWCInput(id: name + "-receipt", category: "historical-receipt", source: "historical",
                description: .object(["line": .n(ri + 1), "parser_bytes": .n(lines[ri].count),
                    "parser_sha256": .string(transportDigest(lines[ri]))]), value: prepared.receiptCore,
                expectedRoute: "direct", expectedStatus: "success", oracle: .null))
            acknowledgements.append(CWCInput(id: name + "-ack", category: "historical-ack", source: "historical",
                description: .object(["line": .n(ai + 1), "parser_bytes": .n(lines[ai].count),
                    "parser_sha256": .string(transportDigest(lines[ai]))]), value: frames[ai].core,
                expectedRoute: "direct", expectedStatus: "success", oracle: .null))
        }
        historical.append(contentsOf: acknowledgements)
        reconstruction = try save(.object(["schema": .string("cwc1-historical-reconstruction-v1"),
            "ready": ready, "cases": .array(preparedRows), "inputs": .object(provenance)]), name: "reconstructed.json")

        let corpus = try WireJSON.parse(corpusData)
        guard corpus["schema"].string == "cwc1-corpus-v1", let cases = corpus["cases"].array, cases.count == 74 else {
            throw ExactJSONError("independent cwc1-corpus-v1 cases")
        }
        var ids = Set(historical.map(\.id))
        var inputs: [CWCInput] = []
        for item in cases {
            guard let id = item["id"].string, !id.isEmpty, let category = item["category"].string,
                  let route = item["expected_route"].string, ["direct", "fallback"].contains(route),
                  let status = item["expected_status"].string, ["success", "error"].contains(status) else {
                throw ExactJSONError("corpus case metadata")
            }
            try require(ids.insert(id).inserted, "unique corpus/historical IDs")
            inputs.append(CWCInput(id: id, category: category, source: "corpus", description: item["input"],
                value: try cwcBuild(item["input"]), expectedRoute: route, expectedStatus: status, oracle: item["oracle"]))
        }
        corpusCount = inputs.count
        return inputs
    }

    private func semantic(_ input: CWCInput) throws {
        active = .object(["phase": .string("semantics"), "id": .string(input.id)])
        let original = cwcInvoke(input.value, arm: "original")
        let candidate = cwcInvoke(input.value, arm: "candidate")
        let row = WireJSON.object(["id": .string(input.id), "category": .string(input.category), "source": .string(input.source),
            "input_description": input.description, "input_core_tagged": cwcTagCore(input.value), "oracle": input.oracle,
            "expected_route": .string(input.expectedRoute), "expected_status": .string(input.expectedStatus),
            "original": original.json, "candidate": candidate.json])
        pendingEvidence = row
        semanticRows.append(row)
        try semanticsFile?.write(contentsOf: row.data() + Data([10]))
        // Only after both complete actual outcomes have been appended.
        let originalComparison = try original.comparison.data()
        let candidateComparison = try candidate.comparison.data()
        let checks: [String: WireJSON] = ["error_evidence_complete": .bool(original.complete && candidate.complete),
            "full_tagged_or_structured_error_equal": .bool(originalComparison == candidateComparison),
            "original_expected_status": .bool(original.status == input.expectedStatus),
            "candidate_expected_status": .bool(candidate.status == input.expectedStatus),
            "candidate_expected_route": .bool(candidate.route == input.expectedRoute)]
        semanticChecks.append(.object(["id": .string(input.id), "checks": .object(checks)]))
        try require(checks.values.allSatisfy { $0.bool == true }, "CWC semantic mismatch: " + input.id)
        if input.source == "historical" { expectedHistorical[input.id] = originalComparison }
        pendingEvidence = .null; active = .null
    }

    private func measure(_ input: CWCInput, caseName: String, caseIndex: Int, arm: String,
                         round: Int, position: Int) throws {
        let config = WireJSON.object(["case": .string(caseName), "case_index": .n(caseIndex), "arm": .string(arm),
            "round": .n(round), "warmup": .bool(round == 0), "position_in_round": .n(position)])
        active = .object(["phase": .string("measurement"), "config": config])
        var value: WireJSON?
        var error: (any Error)?
        var converted: CWCConverted?
        var route = arm == "original" ? "original" : "fallback"
        let start: UInt64
        let end: UInt64
        if arm == "original" {
            start = DispatchTime.now().uptimeNanoseconds
            do { value = try WireJSON.fromCore(input.value) } catch let caught { error = caught }
            end = DispatchTime.now().uptimeNanoseconds
        } else {
            start = DispatchTime.now().uptimeNanoseconds
            do { converted = try WireJSON.cwcFromCore(input.value) }
            catch let caught { error = caught }
            end = DispatchTime.now().uptimeNanoseconds
            if let converted { value = converted.value; route = converted.route.rawValue }
        }
        // Actual result retained through end. Tagging, raw evidence, hashing,
        // equality and output writes start here, outside the call interval.
        let outcome = cwcOutcome(value: value, route: route, error: error, arm: arm)
        let rawTrial = WireJSON.object(["trial_id": .string("r\(round)/\(caseName)/\(arm)"), "config": config,
            "events": .array([event("call.start", tick: start, request: input.id), event("call.end", tick: end, request: input.id)]),
            "metrics": .object(["start_uptime_ns": .int(Int64(start)), "end_uptime_ns": .int(Int64(end)),
                "elapsed_ns": .int(Int64(end - start))]), "outcome": outcome.json])
        pendingEvidence = rawTrial
        trials.append(rawTrial)
        try trialsFile?.write(contentsOf: rawTrial.data() + Data([10]))
        // Keep original endpoints/actual output even if this comparison fails.
        try require(end >= start && start >= origin && outcome.complete && outcome.status == "success",
            "CWC measured call must return complete success")
        let comparison = try outcome.comparison.data()
        try require(comparison == expectedHistorical[input.id], "CWC measured full output equality")
        try require(route == (arm == "original" ? "original" : "direct"), "CWC historical measured route")
        let filename = "outputs/\(caseName)-\(arm).json"
        let artifact = try outcome.json.data()
        if let previous = outputValues[filename] { try require(previous == artifact, "CWC all nine actual outputs byte-identical") }
        else { try artifact.write(to: output.appendingPathComponent(filename), options: .atomic); outputValues[filename] = artifact }
        pendingEvidence = .null; active = .null
    }

    private var summary: WireJSON {
        var rows: [WireJSON] = []
        var paired: [WireJSON] = []
        for name in ["A", "B"] {
            for arm in ["original", "candidate"] {
                let xs = trials.filter { $0["config"]["case"].string == name && $0["config"]["arm"].string == arm &&
                    $0["config"]["warmup"].bool == false }.compactMap { $0["metrics"]["elapsed_ns"].integer }.sorted()
                let median: WireJSON = xs.count == 8 ? .real((Double(xs[3]) + Double(xs[4])) / 2) : .null
                rows.append(.object(["case": .string(name), "arm": .string(arm), "count": .n(xs.count),
                    "median_ns": median, "min_ns": xs.first.map(WireJSON.n) ?? .null, "max_ns": xs.last.map(WireJSON.n) ?? .null]))
            }
            for round in 1...8 {
                let xs = trials.filter { $0["config"]["case"].string == name && $0["config"]["round"].integer == round }
                if let a = xs.first(where: { $0["config"]["arm"].string == "original" })?["metrics"]["elapsed_ns"].integer,
                   let b = xs.first(where: { $0["config"]["arm"].string == "candidate" })?["metrics"]["elapsed_ns"].integer {
                    paired.append(.object(["case": .string(name), "round": .n(round), "original_ns": .n(a),
                        "candidate_ns": .n(b), "original_minus_candidate_ns": .n(a - b)]))
                }
            }
        }
        let artifacts = outputValues.keys.sorted().map { cwcPin(outputValues[$0]!, path: $0) }
        return .object(["rows": .array(rows), "paired": .array(paired), "outputs": .array(artifacts),
            "semantic_rows": .n(semanticRows.count), "performance_calls": .n(trials.count)])
    }

    private func event(_ name: String, tick: UInt64, request: String) -> WireJSON {
        .object(["name": .string(name), "t_ms": .real(Double(tick - origin) / 1_000_000),
            "uptime_ns": .int(Int64(tick)), "request_id": .string(request)])
    }

    private func prefixFile(_ name: String) throws -> FileHandle {
        let url = output.appendingPathComponent(name)
        try require(FileManager.default.createFile(atPath: url.path, contents: nil), "create CWC prefix")
        return try FileHandle(forWritingTo: url)
    }

    @discardableResult private func save(_ value: WireJSON, name: String) throws -> WireJSON {
        let data = try value.data(); let url = output.appendingPathComponent(name)
        try data.write(to: url, options: .atomic)
        return cwcPin(data, path: url.path)
    }
}
