import Foundation
import Darwin
import RuntimeCore

enum ParentEntryMode: String, Sendable { case checked, cpu, boundary }

/// Separate options prevent a production source invocation from selecting
/// native workers, injected errors, ordering gates, or untrusted checked JSON.
struct ParentHostOptions: Sendable {
    let mode: ParentEntryMode
    let inputs: String, session: String
    let directory: URL, vocabulary: URL
    let parentSource: URL?, childSource: URL?, sourcesDirectory: URL?
    let fixture: String?
    let boundaryCase: NativeToolBoundaryCase?

    init(_ arguments: [String], mode: ParentEntryMode) throws {
        let common: Set<String> = ["--inputs", "--session", "--pgid", "--output-dir", "--vocab"]
        let extra: Set<String>
        switch mode {
        case .checked: extra = ["--parent-source", "--child-source"]
        case .cpu: extra = ["--sources-dir", "--fixture"]
        case .boundary: extra = ["--sources-dir", "--case"]
        }
        let allowed = common.union(extra)
        try hostRequire(arguments.count.isMultiple(of: 2), "parent options require key/value pairs")
        var values: [String: String] = [:]
        for i in stride(from: 0, to: arguments.count, by: 2) {
            try hostRequire(allowed.contains(arguments[i]) && values[arguments[i]] == nil && !arguments[i + 1].isEmpty,
                            "unknown, duplicate or empty parent option")
            values[arguments[i]] = arguments[i + 1]
        }
        try hostRequire(Set(values.keys) == allowed, "all fixed parent entry options are required")
        guard let session = values["--session"], UUID(uuidString: session) != nil,
              let pgid = Int32(values["--pgid"]!), pgid >= 0 else {
            throw hostFault("parent-usage", "invalid session or PGID")
        }
        try hostRequire(getpid() == getpgrp() && getpgrp() == (pgid == 0 ? getpid() : pgid),
                        "parent entry must be the wrapper's actual group leader")
        for key in ["--inputs", "--output-dir", "--vocab", "--parent-source", "--child-source", "--sources-dir"] {
            if let value = values[key] { try hostRequire(value.hasPrefix("/"), "parent entry paths must be absolute") }
        }
        if mode == .cpu {
            try hostRequire((1...10).map { String(format: "B-C%02d", $0) }.contains(values["--fixture"]!),
                            "unknown fixed parent CPU fixture")
        }
        let boundaryCase = values["--case"].flatMap { NativeToolBoundaryCase(rawValue: $0) }
        if mode == .boundary { try hostRequire(boundaryCase != nil, "unknown fixed native tool boundary case") }
        self.mode = mode; self.session = session; self.inputs = values["--inputs"]!
        directory = URL(fileURLWithPath: values["--output-dir"]!).standardizedFileURL
        vocabulary = URL(fileURLWithPath: values["--vocab"]!).standardizedFileURL
        parentSource = values["--parent-source"].map { URL(fileURLWithPath: $0).standardizedFileURL }
        childSource = values["--child-source"].map { URL(fileURLWithPath: $0).standardizedFileURL }
        sourcesDirectory = values["--sources-dir"].map { URL(fileURLWithPath: $0).standardizedFileURL }
        fixture = values["--fixture"]
        self.boundaryCase = boundaryCase
        // The wrapper prepares only the parent directory. Never overwrite or
        // merge a previous candidate's output, including a failed prefix.
        guard Darwin.mkdir(directory.path, mode_t(0o700)) == 0 else {
            throw hostFault("parent-output-create", "exclusive output directory failed, errno \(errno)")
        }
    }
    var diagnostic: JSONValue {
        var fields: [String: JSONValue] = ["mode": .string(mode.rawValue), "inputs": .string(inputs), "session": .string(session),
            "output_directory": .string(directory.path), "vocabulary": .string(vocabulary.path),
            "parent_source": parentSource.map { .string($0.path) } ?? .null,
            "child_source": childSource.map { .string($0.path) } ?? .null,
            "sources_directory": sourcesDirectory.map { .string($0.path) } ?? .null,
            "fixture": fixture.map(JSONValue.string) ?? .null]
        if let boundaryCase { fields["boundary_case"] = .string(boundaryCase.rawValue) }
        return .object(fields)
    }
}
