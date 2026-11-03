import Foundation
import CryptoKit
import Darwin
import RuntimeCore

// Synchronous observations of the production decoder/configuration APIs only.
// No compiler child, Task, Ledger, transport, FileHandle tool lookup or model.
private struct ProbeFailure: Error { let message: String }
private func require(_ condition: Bool, _ message: String) throws {
    if !condition { throw ProbeFailure(message: message) }
}
private func sha(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
private func pin(_ url: URL, _ data: Data) -> JSONValue {
    .object(["path": .string(url.path), "bytes": .integer(data.count), "sha256": .string(sha(data))])
}
private func diagnostic(_ message: String) -> JSONValue {
    .object(["phase": .string("protocol"), "code": .string("invalid-compiler-exchange"),
             "path": .array([]), "message": .string(message)])
}
private func parsed(_ data: Data) throws -> JSONValue {
    try ExactJSON.decode(data, limits: .init(bytes: 64 * 1024 * 1024, depth: 256, values: 2_000_000, stringBytes: 2 * 1024 * 1024))
}
private func save(_ value: JSONValue, _ url: URL) throws { try value.encoded().write(to: url, options: .withoutOverwriting) }
private func numeric(_ value: JSONValue) throws -> Double {
    guard case .number(let token) = value, let number = Double(token), number.isFinite else {
        throw ProbeFailure(message: "finite number required")
    }
    return number
}
private let python = "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
private let fixedCases: [(String, String)] = [
    ("unknown-profile", "program version"),
    ("wrong-sigma-id", "fixed Sigma implementations"),
    ("source-bytes-hash", "exact source bytes"),
    ("service-result-unit", "parent fieldless Word contract"),
    ("service-tokens-seven", "parent complete Pair contract"),
    ("tool-argument-nat", "parent fieldless Word contract"),
    ("tool-result-word-length", "parent fieldless Nat contract"),
    ("scope-kind", "parent plain Scope kind"),
    ("scope-identity", "parent current scope nominal correspondence"),
    ("service-effect-literal", "parent HostService fixed literal"),
    ("service-effect-scope", "parent current scope nominal correspondence"),
    ("tool-capability", "parent LocalRead fixed literal"),
    ("missing-raise", "parent exact effect arity"),
    ("extra-effect", "parent exact effect arity"),
    ("command-direct-copy", "source-directed direct usage")
]
private let exchangeIDs = ["parent-golden", "parent-empty", "parent-service-only", "child-empty"]
private let sourceIDs = ["golden", "empty", "service-only", "empty"]
private let budgets: JSONValue = .object(["outer": .integer(60), "work": .integer(58),
    "term": .integer(1), "kill_reap": .number("0.5"), "report_reserve": .number("0.5")])

private func wireDifferences(_ left: JSONValue, _ right: JSONValue, path: [String] = []) -> [JSONValue] {
    if left == right { return [] }
    func row(_ old: JSONValue?, _ new: JSONValue?, _ components: [String]) -> JSONValue {
        let pointer = "/" + components.map { $0.replacingOccurrences(of: "~", with: "~0").replacingOccurrences(of: "/", with: "~1") }.joined(separator: "/")
        return .object(["pointer": .string(pointer), "old_present": .bool(old != nil), "new_present": .bool(new != nil),
                        "old": old ?? .null, "new": new ?? .null])
    }
    switch (left, right) {
    case (.object(let a), .object(let b)):
        return Set(a.keys).union(b.keys).sorted().flatMap { key -> [JSONValue] in
            if let old = a[key], let new = b[key] { return wireDifferences(old, new, path: path + [key]) }
            return [row(a[key], b[key], path + [key])]
        }
    case (.array(let a), .array(let b)):
        return (0..<max(a.count, b.count)).flatMap { index -> [JSONValue] in
            if index < a.count && index < b.count { return wireDifferences(a[index], b[index], path: path + [String(index)]) }
            return [row(index < a.count ? a[index] : nil, index < b.count ? b[index] : nil, path + [String(index)])]
        }
    default: return [row(left, right, path)]
    }
}

private final class Probe {
    let output: URL
    var files: [String: JSONValue] = [:]
    var observations: [JSONValue] = []
    var decodeCalls = 0, configurationCalls = 0
    var current = "input-preflight"
    init(output: URL) { self.output = output }

    func observedRead(_ url: URL) throws -> Data {
        let data = try Data(contentsOf: url), identity = pin(url, data)
        if let previous = files[url.path] { try require(previous == identity, "input changed: " + url.path) }
        files[url.path] = identity
        return data
    }
    func pinnedRead(_ identity: JSONValue) throws -> Data {
        _ = try identity.members(exactly: ["path", "bytes", "sha256"])
        let path = try identity.field("path").text()
        try require(path.hasPrefix("/"), "absolute pinned path")
        let url = URL(fileURLWithPath: path), data = try observedRead(url)
        try require(pin(url, data) == identity, "exact input pin: " + path)
        return data
    }
    func recheck() throws {
        for path in files.keys.sorted() {
            let url = URL(fileURLWithPath: path)
            try require(pin(url, try Data(contentsOf: url)) == files[path], "post input changed: " + path)
        }
    }
    func verifyRun(_ row: JSONValue, root: String, parent: Bool) throws {
        let name = try row.field("id").text()
        let run = try parsed(pinnedRead(row.field("run"))), tool = try parsed(pinnedRead(row.field("actual_tool")))
        let command: JSONValue = .array([python, "-I", "-S", "-B", root + "/frontend/" + (parent ? "export_parent.py" : "export.py")].map(JSONValue.string))
        try require(try run.field("schema").text() == "src1b-decoder-frontend-export-run-v1" && run.field("entry").text() == name, "actual frontend run identity")
        try require(try run.field("command") == command && run.field("cwd").text() == root && run.field("source") == row.field("source"), "actual compiler command/stdin identity")
        try require(try run.field("files").field("stdout.log") == row.field("response") && run.field("files").field("stderr.log") == row.field("stderr"), "actual compiler raw stdio identity")
        try require(try run.field("returncode").integerToken() == "0" && run.field("passed").boolean() && run.field("direct_child_reaped").boolean() && run.field("process_group_absent").boolean(), "actual compiler ready exit0")
        try require(try !run.field("timed_out").boolean() && run.field("interrupted").elements().isEmpty && run.field("signals").elements().isEmpty && run.field("failure") == .null, "clean compiler terminal")
        let work = try numeric(run.field("work_elapsed_seconds")), elapsed = try numeric(run.field("elapsed_seconds"))
        try require(try run.field("budgets_seconds") == budgets && work >= 0 && work < 58 && elapsed >= work && elapsed < 60 && run.field("outer_zero_requires_post_save_post_flush_total_budget_gate").boolean(), "compiler work/total final gate contract")
        try require(try tool.field("schema").text() == "src1b-decoder-frontend-export-actual-tool-v1" && tool.field("entry").text() == name && tool.field("run") == row.field("run") && tool.field("child_command") == command, "actual tool/run binding")
        try require(try tool.field("observation").field("exit_code").integerToken() == "0", "actual outer exit0")
    }

    func observe(_ name: String, source: Data, response: Data, configuration: String?,
                 expectedMessage: String?, expectedFailureStage: String?, expectedDomain: String?) throws {
        current = name
        let directory = output.appendingPathComponent(name, isDirectory: true)
        try require(Darwin.mkdir(directory.path, mode_t(0o700)) == 0, "exclusive observation directory")
        let sourceURL = directory.appendingPathComponent("source.json"), responseURL = directory.appendingPathComponent("frontend.stdout")
        try source.write(to: sourceURL, options: .withoutOverwriting)
        try response.write(to: responseURL, options: .withoutOverwriting)
        var result = "accepted", stage = "decode", actual: JSONValue = .null, unexpected: JSONValue = .null
        var decoded = false, metadata: JSONValue = .null
        decodeCalls += 1
        do {
            let program = try decodeCompilerExchange(source: source, response: response, actualExitCode: 0)
            decoded = true
            metadata = .object(["execution_domain": .string(program.executionDomain.rawValue),
                "source_bytes_sha256": .string(program.sourceBytesSHA256), "canonical_source_sha256": .string(program.canonicalSourceSHA256),
                "compiler_response_sha256": .string(program.compilerResponseSHA256)])
            if let configuration {
                stage = "configuration"; configurationCalls += 1
                if configuration == "parent" { try validateParentServiceConfiguration(program) }
                else if configuration == "leaf-configured" { try validateExternalDemandConfiguration(program, configured: true) }
                else { throw ProbeFailure(message: "unrecognized fixed configuration") }
            }
            stage = "none"
        } catch let error as ProtocolError {
            result = "protocol-rejected"; actual = error.diagnostic
        } catch let error as FrontendRejection {
            result = "frontend-rejected"; actual = error.diagnostic
            unexpected = .object(["status": .string(error.status), "exit_code": .integer(Int(error.exitCode))])
        } catch {
            result = "unexpected-error"
            unexpected = .object(["type": .string(String(reflecting: type(of: error))), "description": .string(String(describing: error))])
        }
        let expected = expectedMessage.map(diagnostic) ?? .null
        var matched = expectedMessage == nil ? result == "accepted" : result == "protocol-rejected" && actual == expected && stage == expectedFailureStage
        if let expectedDomain {
            do {
                let canonical = try sha(parsed(source).sourceCanonicalEncoded())
                matched = matched && metadata == .object(["execution_domain": .string(expectedDomain),
                    "source_bytes_sha256": .string(sha(source)), "canonical_source_sha256": .string(canonical),
                    "compiler_response_sha256": .string(sha(response))])
            } catch {
                matched = false
                unexpected = .object(["metadata_validation_error": .string(String(describing: error)),
                                      "original_unexpected_error": unexpected])
            }
        }
        let observation: JSONValue = .object(["case": .string(name), "result": .string(result),
            "production_decode_returned": .bool(decoded), "failure_stage": .string(stage),
            "configuration": configuration.map(JSONValue.string) ?? .null, "program_metadata": metadata,
            "actual_diagnostic": actual, "unexpected_error": unexpected, "expected_diagnostic": expected,
            "expected_failure_stage": expectedFailureStage.map(JSONValue.string) ?? .null,
            "expectation_matched": .bool(matched), "compiler_actual_exit_supplied": .integer(0),
            "source": pin(sourceURL, source), "response": pin(responseURL, response)])
        // Preserve the actual result before asserting it or starting another case.
        try save(observation, directory.appendingPathComponent("observation.json"))
        observations.append(observation)
        try require(matched, "first differing decoder observation: " + name)
    }

    func run(_ manifestURL: URL) throws {
        let manifest = try parsed(observedRead(manifestURL))
        try require(try manifest.field("schema").text() == "src1b-parent-decoder-inputs-v1" && manifest.field("construction_complete").boolean(), "complete decoder input manifest")
        try require(try !manifest.field("decoder_executed").boolean() && !manifest.field("compiler_started").boolean() && !manifest.field("source_runtime_executed").boolean() && !manifest.field("model_loaded").boolean(), "construction-only provenance")
        try require(try manifest.field("mutation_count").integerToken() == "15" && manifest.field("observation_count").integerToken() == "20" && manifest.field("configuration_calls").integerToken() == "5", "fixed finite counts")
        let basis = try manifest.field("program_source_basis")
        _ = try pinnedRead(basis)
        try require(try basis.field("bytes").integerToken() == "63836" && basis.field("sha256").text() == "e7b982727e272bf34bece17c8554427a3ba6adfb51bf57ac6be0772272ff90c9", "reviewed B decoder source basis")
        _ = try pinnedRead(manifest.field("constructor"))
        let oracle = try manifest.field("oracle")
        _ = try pinnedRead(oracle)
        try require(try oracle.field("bytes").integerToken() == "6539" && oracle.field("sha256").text() == "90dd52dae21c9383ea23638a8d0cea777fe3c5a6d2917b09ef04325ecaad01d5", "unchanged reviewed 15-case oracle")
        for input in try manifest.field("inputs").elements() { _ = try pinnedRead(input) }
        let root = try manifest.field("source_root").text()
        try require(root.hasPrefix("/"), "absolute source root")
        let sourceManifest = try parsed(pinnedRead(manifest.field("source_manifest")))
        let index = try parsed(pinnedRead(manifest.field("exchange_index")))
        try require(try sourceManifest.field("schema").text() == "src1b-decoder-sources-v1" && sourceManifest.field("construction_complete").boolean() && sourceManifest.field("source_root").text() == root && sourceManifest.field("program_source_basis") == basis, "source manifest identity")
        for input in try sourceManifest.field("inputs").elements() { _ = try pinnedRead(input) }
        let sourceRows = try sourceManifest.field("sources").elements()
        try require(try sourceRows.map { try $0.field("id").text() } == ["golden", "empty", "service-only"], "fixed source population")
        let sourcePins = try Dictionary(uniqueKeysWithValues: sourceRows.map { (try $0.field("id").text(), try $0.field("source")) })
        try require(try index.field("schema").text() == "src1b-decoder-exchange-index-v1" && index.field("source_root").text() == root && index.field("source_manifest") == manifest.field("source_manifest"), "actual exchange index identity")
        let indexed = try index.field("exchanges").elements(), exchanges = try manifest.field("exchanges").elements()
        try require(indexed.count == 4 && exchanges.count == 4, "four actual exchanges")
        var originals: [(Data, Data)] = []
        for (i, row) in exchanges.enumerated() {
            let name = exchangeIDs[i], parent = i < 3
            current = "input-" + name
            try require(try row.field("id").text() == name && row.field("exporter").text() == (parent ? "parent" : "child") && row.field("source") == sourcePins[sourceIDs[i]], "fixed exchange source/entry")
            for key in ["id", "exporter", "source", "response", "stderr", "run", "actual_tool"] {
                try require(try row.field(key) == indexed[i].field(key), "exchange/index exact correspondence")
            }
            try require(try row.field("actual_compiler_exit_supplied").integerToken() == "0", "actual compiler exit supplied")
            let source = try pinnedRead(row.field("source")), response = try pinnedRead(row.field("response")), stderr = try pinnedRead(row.field("stderr"))
            let copies = try row.field("original_copies")
            try require(try pinnedRead(copies.field("source")) == source && pinnedRead(copies.field("response")) == response && pinnedRead(copies.field("stderr")) == stderr, "original raw exchange preserved")
            try verifyRun(row, root: root, parent: parent)
            originals.append((source, response))
        }
        let mutations = try manifest.field("mutations").elements()
        try require(mutations.count == fixedCases.count && fixedCases.count == 15, "fixed mutation population")
        for (i, row) in mutations.enumerated() {
            try require(try row.field("id").text() == fixedCases[i].0 && row.field("expected_diagnostic") == diagnostic(fixedCases[i].1), "manifest cannot change case order or oracle")
        }
        try observe("GOLDEN-parent-configuration", source: originals[0].0, response: originals[0].1, configuration: "parent", expectedMessage: nil, expectedFailureStage: nil, expectedDomain: "hostParent")
        try observe("GOLDEN-leaf-configuration-rejected", source: originals[0].0, response: originals[0].1, configuration: "leaf-configured", expectedMessage: "external demand requires mlxLeaf domain", expectedFailureStage: "configuration", expectedDomain: "hostParent")
        try observe("EMPTY-parent-configuration-rejected", source: originals[1].0, response: originals[1].1, configuration: "parent", expectedMessage: "parent service configuration requires one call_service occurrence", expectedFailureStage: "configuration", expectedDomain: "hostParent")
        try observe("SERVICE-ONLY-parent-configuration-rejected", source: originals[2].0, response: originals[2].1, configuration: "parent", expectedMessage: "parent service configuration requires one call_tool occurrence", expectedFailureStage: "configuration", expectedDomain: "hostParent")
        try observe("CHILD-parent-configuration-rejected", source: originals[3].0, response: originals[3].1, configuration: "parent", expectedMessage: "parent configuration requires hostParent domain", expectedFailureStage: "configuration", expectedDomain: "mlxLeaf")
        let original = try parsed(originals[0].1), originalTable = try original.field("program").field("static_values").elements()
        for (i, row) in mutations.enumerated() {
            let (name, message) = fixedCases[i]; current = name
            let metadata = try parsed(pinnedRead(row.field("metadata")))
            try require(try metadata.field("schema").text() == "src1b-parent-decoder-mutation-v1" && metadata.field("id").text() == name && metadata.field("expected_diagnostic") == diagnostic(message), "fixed mutation metadata")
            try require(try metadata.field("source") == row.field("source") && metadata.field("response") == row.field("response") && !metadata.field("source_rewritten").boolean() && !metadata.field("decoder_executed").boolean() && metadata.field("actual_compiler_exit_supplied").integerToken() == "0", "mutation exact input/provenance")
            let source = try pinnedRead(row.field("source")), response = try pinnedRead(row.field("response"))
            try require(source == originals[0].0 && response != originals[0].1, "unchanged real source and changed exchange")
            let changed = try parsed(response), table = try changed.field("program").field("static_values").elements()
            let differences = wireDifferences(original, changed)
            try require(!differences.isEmpty && Array(table.prefix(originalTable.count)) == originalTable, "negative without shared original leaf changes")
            try require(try metadata.field("wire_changes") == .array(differences) && metadata.field("wire_changed_count").integerToken() == String(differences.count), "complete actual wire difference")
            try require(try metadata.field("original_static_prefix_unchanged").boolean() && metadata.field("original_static_count").integerToken() == String(originalTable.count) && metadata.field("appended_static_count").integerToken() == String(table.count - originalTable.count), "actual original/appended static population")
            let origin = try metadata.field("input_origin")
            try require(try origin.field("source_sha256").text() == sha(originals[0].0) && origin.field("response_sha256").text() == sha(originals[0].1) && origin.field("actual_frontend_exit").integerToken() == "0", "mutation original actual exchange")
            try observe(name, source: source, response: response, configuration: nil, expectedMessage: message, expectedFailureStage: "decode", expectedDomain: nil)
        }
        try require(observations.count == 20 && decodeCalls == 20 && configurationCalls == 5, "all fixed production calls completed")
        try recheck()
        let result: JSONValue = .object(["schema": .string("src1b-decoder-actual-controls-v1"), "passed": .bool(true),
            "observation_count": .integer(20), "mutation_count": .integer(15), "decode_calls": .integer(decodeCalls),
            "configuration_calls": .integer(configurationCalls), "observations": .array(observations),
            "files": .array(files.keys.sorted().map { files[$0]! }), "input_pins_unchanged": .bool(true),
            "source_runtime_executed": .bool(false), "compiler_started": .bool(false), "worker_started": .bool(false),
            "scope": .string("20 finite synchronous decoder/configuration observations; no parent/child runtime, lookup, response receipt, HostClose, GPU or general PL proof")])
        try save(result, output.appendingPathComponent("result.json"))
        FileHandle.standardOutput.write(Data("{\"passed\":true,\"observations\":20,\"decoder_mutations\":15,\"configuration_calls\":5}\n".utf8))
    }
    func fail(_ error: any Error) {
        let result: JSONValue = .object(["schema": .string("src1b-decoder-actual-controls-v1"), "passed": .bool(false),
            "first_failure_case": .string(current), "error_type": .string(String(reflecting: type(of: error))),
            "error": .string(String(describing: error)), "observations": .array(observations),
            "decode_calls": .integer(decodeCalls), "configuration_calls": .integer(configurationCalls),
            "files": .array(files.keys.sorted().map { files[$0]! }), "source_runtime_executed": .bool(false)])
        do { try save(result, output.appendingPathComponent("first-failure.json")) }
        catch { FileHandle.standardError.write(Data(("first-failure save error: " + String(describing: error) + "\n").utf8)) }
    }
}

@main
private struct Main {
    static func main() {
        var probe: Probe?
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            try require(args.count == 4 && args[0] == "--manifest" && args[2] == "--output-dir", "usage: src1b-decoder-cpu --manifest ABS --output-dir NEW_ABS")
            try require(args[1].hasPrefix("/") && args[3].hasPrefix("/"), "absolute paths required")
            let output = URL(fileURLWithPath: args[3], isDirectory: true)
            try FileManager.default.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)
            try require(Darwin.mkdir(output.path, mode_t(0o700)) == 0, "exclusive new output directory")
            let current = Probe(output: output); probe = current
            try current.run(URL(fileURLWithPath: args[1]))
        } catch {
            probe?.fail(error)
            FileHandle.standardError.write(Data(("SRC1 B decoder first failure: " + String(describing: error) + "\n").utf8))
            Darwin.exit(1)
        }
    }
}
