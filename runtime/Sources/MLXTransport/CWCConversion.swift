import Foundation
import CoreFoundation
import RuntimeCore

enum CWCRoute: String, Sendable { case direct, fallback }
struct CWCConverted: Sendable { let value: WireJSON; let route: CWCRoute }

extension WireJSON {
    /// A failed attempt always reruns the unchanged function on the WHOLE root.
    /// Only that original fallback can escape with an error.
    static func cwcFromCore(_ root: JSONValue) throws -> CWCConverted {
        do { return CWCConverted(value: try cwcDirect(root, depth: 0), route: .direct) }
        catch { return CWCConverted(value: try WireJSON.fromCore(root), route: .fallback) }
    }

    private struct CWCDirectGuard: Error {}
    private static func cwcDirect(_ value: JSONValue, depth: Int) throws -> WireJSON {
        guard depth <= 64 else { throw CWCDirectGuard() }
        switch value {
        case .null: return .null
        case .bool(let value): return .bool(value)
        case .number(let token): return try WireJSON.fromCore(.number(token))
        case .string(let value):
            guard value.utf8.allSatisfy({ $0 < 128 }) else { throw CWCDirectGuard() }
            return .string(value)
        case .array(let values):
            return .array(try values.map { try cwcDirect($0, depth: depth + 1) })
        case .object(let fields):
            var result: [String: WireJSON] = [:]
            for (key, child) in fields {
                guard key.utf8.allSatisfy({ $0 < 128 }) else { throw CWCDirectGuard() }
                result[key] = try cwcDirect(child, depth: depth + 1)
            }
            return .object(result)
        }
    }
}

func cwcUTF8(_ value: String) -> WireJSON { .string(Data(value.utf8).base64EncodedString()) }
func cwcKeyOrder(_ a: String, _ b: String) -> Bool { a.utf8.lexicographicallyPrecedes(b.utf8) }

/// These tags never use numeric JSON equality or Swift String equality for a
/// source value. Object entries are sorted by original key UTF-8 bytes.
func cwcTagWire(_ value: WireJSON) -> WireJSON {
    switch value {
    case .null: return .object(["tag": .string("null")])
    case .bool(let x): return .object(["tag": .string("bool"), "value": .bool(x)])
    case .int(let x): return .object(["tag": .string("int"), "decimal": .string(String(x))])
    case .real(let x): return .object(["tag": .string("real"), "bit_pattern_decimal": .string(String(x.bitPattern))])
    case .string(let x): return .object(["tag": .string("string"), "utf8_base64": cwcUTF8(x)])
    case .array(let xs): return .object(["tag": .string("array"), "values": .array(xs.map(cwcTagWire))])
    case .object(let fields):
        return .object(["tag": .string("object"), "fields": .array(fields.keys.sorted(by: cwcKeyOrder).map {
            .array([cwcUTF8($0), cwcTagWire(fields[$0]!)])
        })])
    }
}

func cwcTagCore(_ value: JSONValue) -> WireJSON {
    switch value {
    case .null: return .object(["tag": .string("null")])
    case .bool(let x): return .object(["tag": .string("bool"), "value": .bool(x)])
    case .number(let token): return .object(["tag": .string("number"), "token_utf8_base64": cwcUTF8(token)])
    case .string(let x): return .object(["tag": .string("string"), "utf8_base64": cwcUTF8(x)])
    case .array(let xs): return .object(["tag": .string("array"), "values": .array(xs.map(cwcTagCore))])
    case .object(let fields):
        return .object(["tag": .string("object"), "fields": .array(fields.keys.sorted(by: cwcKeyOrder).map {
            .array([cwcUTF8($0), cwcTagCore(fields[$0]!)])
        })])
    }
}

struct CWCErrorEvidence {
    let raw: WireJSON
    let comparison: WireJSON
    let complete: Bool
}

func cwcDescribeError(_ error: any Error) -> CWCErrorEvidence {
    if let fault = error as? TransportFault {
        return CWCErrorEvidence(raw: .object(["type": .string("TransportFault"),
            "id": .string(fault.id), "code": .string(fault.code), "detail": .string(fault.detail),
            "description": .string(fault.description)]),
            comparison: .object(["type": .string("TransportFault"), "code": cwcUTF8(fault.code),
                "detail": cwcUTF8(fault.detail)]), complete: true)
    }
    if let exact = error as? ExactJSONError {
        return CWCErrorEvidence(raw: .object(["type": .string("ExactJSONError"),
            "description": .string(exact.description)]), comparison: .object([
                "type": .string("ExactJSONError"), "description_utf8_base64": cwcUTF8(exact.description)]), complete: true)
    }
    do {
        if let decoding = error as? DecodingError {
            let kind: String
            let context: DecodingError.Context
            var extra: [String: WireJSON] = [:]
            switch decoding {
            case .typeMismatch(let type, let c): kind = "typeMismatch"; context = c; extra["expected_type"] = .string(String(reflecting: type))
            case .valueNotFound(let type, let c): kind = "valueNotFound"; context = c; extra["expected_type"] = .string(String(reflecting: type))
            case .keyNotFound(let key, let c): kind = "keyNotFound"; context = c; extra["key"] = cwcCodingKey(key)
            case .dataCorrupted(let c): kind = "dataCorrupted"; context = c
            @unknown default: throw ExactJSONError("unrecognized DecodingError case")
            }
            var seen: [ObjectIdentifier: String] = [:]
            let underlying = try context.underlyingError.map {
                try cwcNSError($0 as NSError, path: "underlying", seen: &seen)
            } ?? .null
            let structured = WireJSON.object(["type": .string("DecodingError"), "case": .string(kind),
                "coding_path": .array(context.codingPath.map(cwcCodingKey)),
                "debug_description": .string(context.debugDescription),
                "debug_description_utf8_base64": cwcUTF8(context.debugDescription),
                "underlying_error": underlying, "extra": .object(extra)])
            return CWCErrorEvidence(raw: .object(["structured": structured,
                "description": .string(String(describing: decoding))]), comparison: structured, complete: true)
        }
        // Foundation errors are kept structurally as NSError, never just text.
        var seen: [ObjectIdentifier: String] = [:]
        let structured = try cwcNSError(error as NSError, path: "error", seen: &seen)
        return CWCErrorEvidence(raw: .object(["swift_type": .string(String(reflecting: type(of: error))),
            "description": .string(String(describing: error)), "ns_error": structured]),
            comparison: .object(["swift_type": .string(String(reflecting: type(of: error))), "ns_error": structured]), complete: true)
    } catch let serializationError {
        // Unknown userInfo types are not silently declared equal. The semantic
        // row is retained, then the harness fails the completeness check.
        let raw = WireJSON.object(["type": .string(String(reflecting: type(of: error))),
            "description": .string(String(describing: error)),
            "serialization_error": .string(String(describing: serializationError))])
        return CWCErrorEvidence(raw: raw, comparison: .null, complete: false)
    }
}

private func cwcCodingKey(_ key: any CodingKey) -> WireJSON {
    .object(["string_value_utf8_base64": cwcUTF8(key.stringValue), "int_value": key.intValue.map(WireJSON.n) ?? .null])
}

private func cwcNSError(_ error: NSError, path: String,
                        seen: inout [ObjectIdentifier: String]) throws -> WireJSON {
    let id = ObjectIdentifier(error)
    if let prior = seen[id] { return .object(["type": .string("NSError-reference"), "path": .string(prior)]) }
    seen[id] = path
    var entries: [WireJSON] = []
    for key in error.userInfo.keys.sorted(by: cwcKeyOrder) {
        entries.append(.array([cwcUTF8(key), try cwcFoundationValue(error.userInfo[key]!,
            path: path + "/" + Data(key.utf8).base64EncodedString(), seen: &seen)]))
    }
    return .object(["type": .string("NSError"), "domain_utf8_base64": cwcUTF8(error.domain),
        "code_decimal": .string(String(error.code)), "user_info": .array(entries)])
}

private func cwcFoundationValue(_ value: Any, path: String,
                               seen: inout [ObjectIdentifier: String]) throws -> WireJSON {
    if value is NSNull { return .object(["type": .string("NSNull")]) }
    if let error = value as? NSError { return try cwcNSError(error, path: path, seen: &seen) }
    if let text = value as? String { return .object(["type": .string("String"), "utf8_base64": cwcUTF8(text)]) }
    if let number = value as? NSNumber {
        if CFGetTypeID(number) == CFBooleanGetTypeID() {
            return .object(["type": .string("Bool"), "value": .bool(number.boolValue)])
        }
        let type = String(cString: number.objCType)
        return .object(["type": .string("NSNumber"), "objc_type": .string(type),
            "decimal": .string(number.stringValue),
            "double_bit_pattern_decimal": ["f", "d"].contains(type) ? .string(String(number.doubleValue.bitPattern)) : .null])
    }
    if let data = value as? Data { return .object(["type": .string("Data"), "base64": .string(data.base64EncodedString())]) }
    if let date = value as? Date { return .object(["type": .string("Date"), "reference_interval_bits": .string(String(date.timeIntervalSinceReferenceDate.bitPattern))]) }
    if let url = value as? URL { return .object(["type": .string("URL"), "absolute_utf8_base64": cwcUTF8(url.absoluteString)]) }
    if let values = value as? [Any] {
        return .object(["type": .string("Array"), "values": .array(try values.enumerated().map {
            try cwcFoundationValue($0.element, path: path + "/" + String($0.offset), seen: &seen)
        })])
    }
    if let fields = value as? [String: Any] {
        return .object(["type": .string("Dictionary"), "fields": .array(try fields.keys.sorted(by: cwcKeyOrder).map {
            .array([cwcUTF8($0), try cwcFoundationValue(fields[$0]!,
                path: path + "/" + Data($0.utf8).base64EncodedString(), seen: &seen)])
        })])
    }
    throw ExactJSONError("unrepresented NSError userInfo type: " + String(reflecting: type(of: value)))
}
